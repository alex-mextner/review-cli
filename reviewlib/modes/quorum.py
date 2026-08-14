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
from . import _diff_context_block, _run_effort, _visual_images
from .contract import ModeContext, ModeSpec

if TYPE_CHECKING:
    from ..config import EffortOverride


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
    expert_prompt = (
        "You are one expert on a panel. Give a clear RECOMMENDATION on the question below. "
        "Cite concrete evidence for every claim (file path, line number, command output, "
        "or a verifiable fact). If you do not have an evidence base to answer, say exactly "
        "'INSUFFICIENT EVIDENCE' and explain what you would need — do NOT guess. "
        "Do not edit files or run commands.\n\n"
        f"QUESTION:\n{question}" + _diff_context_block(dispatch_diff)
    )
    jobs = [
        PanelJob(
            model=model,
            prompt=expert_prompt,
            diff="",
            images=visual_images,
            effort=_run_effort(effort_override, model),
        )
        for model in models
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
    mod_prompt = (
        "You are the MODERATOR of an expert panel. Below are independent expert answers to "
        "one question. Produce a structured summary with exactly these sections:\n"
        "1. QUORUM — points where a majority of experts agree AND cite evidence "
        "(state the point, who agrees, and the evidence).\n"
        "2. DISAGREEMENT / NO QUORUM — points where experts conflict or no majority exists.\n"
        "3. ABSTAINED — experts who said INSUFFICIENT EVIDENCE, and on what.\n"
        "Do not invent agreement. Do not edit files.\n\n"
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
