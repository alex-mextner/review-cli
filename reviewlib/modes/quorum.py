"""quorum: experts answer + a moderator finds consensus/disagreement.

`review quorum "<question>"` — a two-phase structured panel (experts cite evidence,
a moderator finds quorum/disagreement). Originally the `--quorum` flag (Stage 0
decomposition); now a first-class SUBCOMMAND backed by the `MODE` descriptor at the
bottom of this file (see `modes/contract.py`).
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

from ..backends import cap_diff_for_dispatch
from ..panel import (
    PanelJob,
    format_result,
    recount_round_by_usability,
    result_is_usable,
    run_moderator,
    run_panel,
)
from . import PERSONAS, _diff_context_block, _run_effort, _visual_images
from .contract import ModeContext, ModeSpec

if TYPE_CHECKING:
    from ..config import EffortOverride


def _expert_prompt(
    persona_name: str, persona_bg: str, question: str, diff_block: str
) -> str:
    """One seat's prompt: brainstorm's persona framing grafted onto quorum's existing
    evidence-citing / INSUFFICIENT-EVIDENCE contract (Alex, 2026-08-18 — quorum reuses
    brainstorm's role set so a padded/reused seat gets a genuinely distinct lens, not
    just a disclosure label)."""
    return (
        f"You are a '{persona_name}' ({persona_bg}) serving as one expert on a panel. "
        "Give a clear RECOMMENDATION on the question below, reasoned from YOUR "
        "perspective as this role. Cite concrete evidence for every claim (file path, "
        "line number, command output, or a verifiable fact). If you do not have an "
        "evidence base to answer, say exactly 'INSUFFICIENT EVIDENCE' and explain what "
        "you would need — do NOT guess. Do not edit files or run commands.\n\n"
        f"QUESTION:\n{question}" + diff_block
    )


def _seat_assignments(
    models: list[str],
) -> tuple[list[str], list[tuple[str, str]]]:
    """One (label, persona) pair per seat, computed ONCE and shared by both the
    transcript label and the seat's own prompt (Fable/k3 review finding: computing
    `PERSONAS[i % len(PERSONAS)]` independently in two places can silently desync a
    seat's disclosed lens from the lens it actually reasoned from).

    Persona is chosen by PER-MODEL occurrence, offset by that model's OWN first seat
    index — not by the raw global seat index — so two seats sharing one model always
    land on DISTINCT personas up to `len(PERSONAS)` repeats of that one model. Raw
    `PERSONAS[i % len(PERSONAS)]` collides once a model's repeats are spaced exactly
    `len(PERSONAS)` seats apart (e.g. seats 0 and 6 of a 7-seat `[A,B,A,B,A,B,A]`
    panel, which `expand_flat_models_with_reuse`'s cycling padding reaches for real
    once a pool is down to 2 reachable models) — Fable AND k3 independently found
    this exact collision, reintroducing the zero-diversity duplicate-seat failure
    this feature exists to prevent. Distinct models MAY still land on the same
    persona by position (harmless — the invariant that matters is same-model seats
    never repeating a lens, not global uniqueness). The one case this can't fully
    avoid is a SINGLE model occupying more than `len(PERSONAS)` seats by itself (a
    >6x reuse of one model in one panel) — k3 review finding: this IS reachable
    today, not just theoretical, via a config.yaml `models:` list of 7+ entries
    (no length cap, cli.py's `expand_flat_models_with_reuse(reachable, ...)`) where
    usage-limit exclusion (or its all-near-limit least-depleted fallback) leaves
    only one survivor — the panel becomes `[A]*7+` and seat #1/#7 of A repeat a
    lens. Deliberately NOT guarded against here (a large `models:` list collapsing
    to one survivor is itself an unusual config), but a caller building much
    larger or more usage-constrained panels should re-check this — tracked as a
    follow-up (review-cli#206) rather than adding warn/fail-loud machinery now.

    Labels are `<model> [<persona>]`, or `<model>#N [<persona>]` when a model
    occupies more than one seat (reuse-aware panel padding, `expand_flat_models_
    with_reuse` in cli.py) — the `#N` keeps repeated seats grep-able by model
    identity even though their persona differs."""
    first_index: dict[str, int] = {}
    seat_counts: dict[str, int] = {}
    for i, model in enumerate(models):
        first_index.setdefault(model, i)
        seat_counts[model] = seat_counts.get(model, 0) + 1

    occurrence: dict[str, int] = {}
    labels: list[str] = []
    personas: list[tuple[str, str]] = []
    for model in models:
        # 0-indexed occurrence of THIS model so far -> occ+1 is its 1-indexed seat
        # number, reused directly for both the persona offset and the `#N` label
        # (Fable review finding, round 3: a separate `seen` counter that always
        # equalled `occ + 1` was redundant bookkeeping in a function whose whole
        # point is eliminating exactly this class of desync risk).
        occ = occurrence.get(model, 0)
        occurrence[model] = occ + 1
        persona = PERSONAS[(first_index[model] + occ) % len(PERSONAS)]
        personas.append(persona)
        if seat_counts[model] > 1:
            labels.append(f"{model}#{occ + 1} [{persona[0]}]")
        else:
            labels.append(f"{model} [{persona[0]}]")
    return labels, personas


def mode_quorum(
    question: str,
    models: list[str],
    diff: str,
    cwd: Path,
    timeout: int,
    moderators: list[str],
    visual_images: tuple[Path, ...] = (),
    effort_override: "EffortOverride | None" = None,
    diff_from_stdin: bool = False,
    diff_already_capped: bool = False,
) -> int:
    # codex review finding: the dispatch-time diff cap used to be applied ONLY by
    # cli.py's `_dispatch`, so a caller reaching this function directly (a library
    # consumer, an MCP seam, a test) could still send an uncapped diff to every expert
    # — capped here too, at the mode's own dispatch boundary, mirroring mode_review /
    # mode_brainstorm / mode_just_ask. Same stdin exemption: a piped diff was already an
    # explicit, deliberate scope choice by the caller.
    #
    # codex review finding, round 3 — corrects an earlier, INACCURATE version of this
    # comment that claimed capping applies "to every expert AND THE MODERATOR": the
    # moderator call below (`run_moderator(moderators, mod_prompt, cwd, timeout)`) has
    # NEVER received `diff` at all, in any version of this function — it synthesizes
    # QUORUM/DISAGREEMENT from the experts' own transcript, not from the raw diff again.
    # That is PRE-EXISTING behavior this PR did not introduce or change, so this cap
    # genuinely has nothing to do for the moderator today. Whether the moderator SHOULD
    # see the diff too is a separate, real question — tracked as review-cli#189, not
    # fixed here (out of scope for the diff-cap feature this comment is actually about).
    #
    # codex review finding (round 2, applied here to match just_ask.py's identical fix):
    # capping AGAIN when the CLI layer already capped it (`diff_already_capped`) is
    # silently harmless at the default cap (the first call's output is already <= cap,
    # so a second call is a true no-op) but NOT idempotent when
    # `$REVIEW_DIFF_MAX_BYTES` is set below the truncation marker's own length — a
    # second application would re-truncate the FIRST call's marker text and report ITS
    # byte count as "the full diff". `diff_already_capped` (default False, so a direct
    # library caller bypassing the CLI is still protected) skips the redundant second
    # call entirely rather than relying on it happening to be a no-op.
    dispatch_diff = (
        diff if diff_from_stdin or diff_already_capped else cap_diff_for_dispatch(diff)
    )
    diff_block = _diff_context_block(dispatch_diff)
    # Every seat gets a persona/role from the SAME rotation brainstorm uses (Alex,
    # 2026-08-18: quorum panels should run "под разными полями" — distinct fields/roles
    # — not just distinct models). `models` can also contain the SAME model more than
    # once (reviewlib.config's reuse-aware panel padding — see cli.py's
    # `expand_flat_models_with_reuse` wiring): when a distinct-model pool is scarce or
    # some models are near their usage limit, one model fills several seats instead of
    # shrinking the panel — a model covering multiple roles in parallel is a perfectly
    # valid panel shape, not a lesser one (Alex, 2026-08-21). `_seat_assignments` picks
    # ONE (label, persona) pair per seat and both the label and the prompt below consume
    # that SAME value — see its docstring for why persona is keyed on per-model
    # occurrence rather than raw seat index. Repeated occurrences stay grep-able via a
    # "<model>#N" prefix (via PanelJob.label, which `run_panel` uses to relabel the
    # result), so the transcript and stats can always tell which seat produced which
    # answer even when one model fills several of them.
    labels, personas = _seat_assignments(models)

    jobs = [
        PanelJob(
            model=model,
            prompt=_expert_prompt(*personas[i], question, diff_block),
            diff="",
            images=visual_images,
            effort=_run_effort(effort_override, model),
            label=labels[i],
        )
        for i, model in enumerate(models)
    ]
    expert_results = run_panel(jobs, cwd, timeout)
    # glm-5.2 review finding (2026-08 seat-cooldown feature): `run_panel`'s own
    # auto-tally counts a cached-cooldown sentinel (rc=0, non-empty "unavailable"
    # body) as `ok` in run-stats. `run_moderator` (called below) already fixes this
    # for the moderator's own tally; this recount does the same for the expert panel
    # so the two halves of one quorum run agree.
    recount_round_by_usability(expert_results)

    transcript = "\n\n".join(
        f"### Expert: {r.model} [{'ok' if r.returncode == 0 else f'exit {r.returncode}'}]\n"
        f"{(r.stdout.strip() or r.stderr.strip() or '(no output)')}"
        for r in expert_results
    )
    # Unconditional (Fable/k3 review finding, round 1): every seat now carries a
    # `[<lens>]` suffix, not just duplicates, so the moderator needs the notation
    # explained regardless of whether any model repeats. NOT worded as "distinct"
    # (Fable review finding, round 2): `_seat_assignments` only guarantees a
    # repeated MODEL never repeats its own lens — two DIFFERENT models can land on
    # the same lens by position, which is fine (two independent security reviews
    # from two different vendors), but claiming blanket distinctness to the
    # moderator would be false and could bias its de-dup judgement. Bounded to
    # "up to its first len(PERSONAS) seats" (Fable/k3 review finding, round 3):
    # beyond that, a single model CAN repeat a lens — see `_seat_assignments`'
    # own docstring for the exact (reachable, not just theoretical) condition.
    # The count is INTERPOLATED, not hardcoded (Fable/k3 review finding, round 4
    # — both independently caught that a hardcoded "SIX" would silently go stale
    # the moment someone resizes the PERSONAS tuple, since the only guard on the
    # tuple's size was `>= 5`, not `== 6`; interpolating means the note can never
    # be wrong regardless of pool size, closing the gap for good instead of just
    # tightening one assertion). The worked example is ALSO interpolated, not a
    # hardcoded persona name (Fable review finding, round 5): this very diff
    # renamed one persona, and a future rename of whichever one happened to be
    # hardcoded here would go stale the same way — pulling from `PERSONAS`
    # directly means any future rename updates the example automatically.
    lens_note = (
        "\n\nNOTE: each expert below is assigned a role/lens (shown in brackets "
        f"after the model, e.g. `glm [{PERSONAS[1][0]}]`; not necessarily unique "
        "across different models, and a single model is only guaranteed a "
        f"different lens across its first {len(PERSONAS)} seats) — each reasons "
        "from that role, but the lens does not change how their answer should be "
        "weighted."
    )
    mod_prompt = (
        "You are the MODERATOR of an expert panel. Below are independent expert answers to "
        "one question. Produce a structured summary with exactly these sections:\n"
        "1. QUORUM — points where a majority of experts agree AND cite evidence "
        "(state the point, who agrees, and the evidence).\n"
        "2. DISAGREEMENT / NO QUORUM — points where experts conflict or no majority exists.\n"
        "3. ABSTAINED — experts who said INSUFFICIENT EVIDENCE, and on what.\n"
        "Do not invent agreement. Do not edit files."
        f"{lens_note}\n\n"
        f"QUESTION:\n{question}\n\n=== EXPERT ANSWERS ===\n{transcript}"
    )
    # No `diff=` here — pre-existing (not something this diff-cap feature changed): the
    # moderator synthesizes from the experts' own transcript above, never the raw diff
    # again. Whether it SHOULD also see the diff directly is tracked separately as
    # review-cli#189 (codex review finding, round 3), out of scope for this cap.
    mod_result = run_moderator(moderators, mod_prompt, cwd, timeout)

    out = [
        "# Expert answers",
        "\n\n---\n\n".join(format_result(r) for r in expert_results),
    ]
    out += ["\n# Moderator summary", format_result(mod_result)]
    print("\n\n".join(out))
    # codex review finding (2026-08 seat-cooldown feature): a cached-cooldown-skip
    # result deliberately mirrors a live "is currently unavailable" sentinel — rc=0,
    # non-empty body — so a plain `returncode == 0` check would count a cooling-down
    # expert (or moderator) as a real answer instead of a cache hit. `result_is_usable`
    # is the same predicate `mode_review`'s flat/board paths already use for this.
    ok = all(result_is_usable(r) for r in expert_results) and result_is_usable(
        mod_result
    )
    return 0 if ok else 1


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("question", help="the question to put to the expert quorum")


def _handler(ctx: ModeContext) -> int:
    images = _visual_images(ctx)
    diff_from_stdin = bool(ctx.extra.get("diff_from_stdin", False))
    diff_already_capped = bool(ctx.extra.get("diff_already_capped", False))
    if images:
        return mode_quorum(
            ctx.with_visual(ctx.args.question),
            ctx.models,
            ctx.diff,
            ctx.cwd,
            ctx.timeout,
            ctx.moderators,
            images,
            effort_override=ctx.effort_override,
            diff_from_stdin=diff_from_stdin,
            diff_already_capped=diff_already_capped,
        )
    return mode_quorum(
        ctx.with_visual(ctx.args.question),
        ctx.models,
        ctx.diff,
        ctx.cwd,
        ctx.timeout,
        ctx.moderators,
        effort_override=ctx.effort_override,
        diff_from_stdin=diff_from_stdin,
        diff_already_capped=diff_already_capped,
    )


MODE = ModeSpec(
    name="quorum",
    subcommand="quorum",
    diff_policy="none",
    stats_mode="quorum",
    summary="experts cite evidence + a moderator finds quorum/disagreement",
    handler=_handler,
    add_arguments=_add_arguments,
    announce_logs=True,
)
