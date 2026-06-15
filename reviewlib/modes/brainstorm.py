"""brainstorm: multi-round persona ideation with a moderator.

`review brainstorm "<topic>"` — iterative persona ideation in a loop. Composable with
`--diff`/`--staged` (or a piped diff) so the personas can brainstorm ABOUT a specific
change as optional grounding. Originally the `--brainstorm` flag (Stage 0
decomposition); now a first-class SUBCOMMAND backed by the `MODE` descriptor at the
bottom of this file (see `modes/contract.py`). PERSONAS lives here because it is only
used by this mode.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..panel import PanelJob, format_result, run_moderator, run_panel
from ..process import log_dir
from .contract import ModeContext, ModeSpec

# Distinct expert personas for brainstorm rotation (pool >= 5). Each round assigns
# >= 3 of these, rotating so backends see a fresh role each round.
PERSONAS = (
    (
        "Pragmatic staff engineer",
        "20 years shipping production systems; values simplicity, "
        "incremental delivery, and proven tech over novelty.",
    ),
    (
        "Security-paranoid reviewer",
        "thinks adversarially about every input, trust boundary, secret, "
        "and failure mode; assumes the worst actor and the worst case.",
    ),
    (
        "DX / ergonomics designer",
        "obsessed with developer and user experience: clear APIs, good "
        "defaults, discoverability, error messages, and minimal friction.",
    ),
    (
        "Skeptical SRE",
        "cares about operability, observability, blast radius, rollback, "
        "and what breaks at 3am; distrusts anything without a failure plan.",
    ),
    (
        "Product-minded architect",
        "connects technical choices to user value and roadmap; weighs "
        "long-term flexibility against time-to-market.",
    ),
    (
        "Cost-conscious performance engineer",
        "watches latency, throughput, token/compute spend, and resource "
        "footprint; allergic to waste and premature scale.",
    ),
)


def mode_brainstorm(
    topic: str,
    models: list[str],
    cwd: Path,
    timeout: int,
    moderators: list[str],
    rounds: int,
    max_rounds: int,
    diff: str = "",
) -> int:
    min_rounds = max(rounds, 5)
    max_rounds = max(max_rounds, min_rounds)
    panel = models  # run-as-is; personas always fill >= 3 slots even if panel < 3
    # OPTIONAL grounding diff: when a working-tree / --staged / piped diff is present,
    # the brainstorm is ABOUT that specific change — the diff is fed to every persona
    # (and the moderator) as constant context so the ideation is grounded, not abstract.
    # An empty diff keeps the classic pure-ideation behaviour unchanged.
    grounded = bool(diff.strip())
    # A one-line note appended to each persona prompt when grounded, so the model knows
    # the fenced ```diff``` block (added by the backend's _payload from PanelJob.diff) is
    # the concrete change to brainstorm about, not stray context.
    diff_note = (
        "\n\nA specific code change is provided below as a ```diff``` block — brainstorm "
        "concretely ABOUT this change (its design, risks, alternatives, follow-ups), "
        "grounding every idea in it rather than reasoning in the abstract."
        if grounded else ""
    )
    transcript_blocks: list[str] = []
    moderator_label = ">".join(moderators)
    out: list[str] = [f"# Brainstorm: {topic}", f"panel={','.join(panel)} moderator={moderator_label} "
                      f"rounds>={min_rounds} max={max_rounds}{' grounded=diff' if grounded else ''}"]

    # DISCUSSION LOG: write the conversation to one file as each round/decision
    # lands (line-buffered, 0600, O_EXCL). The whole brainstorm used to be
    # accumulated in memory and printed only at the very end, so a timeout / kill /
    # crash lost everything; now the discussion-so-far is always on disk + tail-f-able.
    disc_path = log_dir() / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')}-brainstorm.md"
    disc = None
    try:
        disc = os.fdopen(os.open(str(disc_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
                         "w", encoding="utf-8", buffering=1)
        print(f"[review-cli] brainstorm discussion log: {disc_path} (tail -f to follow)",
              file=sys.stderr, flush=True)
    except OSError as exc:
        # A read-only log dir must NOT take down a brainstorm that worked without one.
        print(f"[review-cli] discussion log unavailable ({exc}); continuing without it",
              file=sys.stderr, flush=True)

    def _disc(text: str) -> None:
        if disc is None:
            return
        disc.write(text if text.endswith("\n") else text + "\n")
        disc.flush()

    _disc(f"# Brainstorm: {topic}\n\npanel={','.join(panel)} moderator={moderator_label} "
          f"rounds>={min_rounds} max={max_rounds}\n")

    persona_index = 0
    completed = 0
    synth = None
    try:
        for round_no in range(1, max_rounds + 1):
            shared = "\n\n".join(transcript_blocks) if transcript_blocks else "(this is the first round)"
            jobs: list[PanelJob] = []
            # Assign >= 3 distinct personas this round, rotating across the pool.
            slot_count = max(3, len(panel))
            for slot in range(slot_count):
                persona_name, persona_bg = PERSONAS[persona_index % len(PERSONAS)]
                persona_index += 1
                model = panel[slot % len(panel)]
                # The shared transcript is fed to backends over STDIN (see the
                # claude/codex backends), NOT a `-p` argv argument — it grows each
                # round and would blow past ARG_MAX as a command-line arg.
                prompt = (
                    f"You are a '{persona_name}' ({persona_bg}). You are in round {round_no} of a "
                    "multi-round brainstorm. Build on the shared transcript of prior rounds — "
                    "react, extend, challenge, or propose new angles from YOUR perspective. "
                    "Be concrete; offer ideas, not pleasantries. Do not edit files."
                    f"{diff_note}\n\n"
                    f"TOPIC:\n{topic}\n\n=== SHARED TRANSCRIPT (prior rounds) ===\n{shared}"
                )
                # The constant grounding diff (if any) rides PanelJob.diff so the backend's
                # _payload appends it as a fenced ```diff``` block over STDIN — ARG_MAX-safe,
                # the same transport the shared transcript uses.
                jobs.append(PanelJob(model=model, prompt=prompt, diff=diff, label=f"{persona_name} ({model})", round_no=round_no))

            round_results = run_panel(jobs, cwd, timeout)
            round_text = "\n\n".join(
                f"#### {r.model}\n{(r.stdout.strip() or r.stderr.strip() or '(no output)')}"
                for r in round_results
            )
            transcript_blocks.append(f"## Round {round_no}\n{round_text}")
            out.append(f"\n# Round {round_no}\n" + "\n\n---\n\n".join(format_result(r) for r in round_results))
            _disc(f"\n# Round {round_no}\n{round_text}\n")
            completed = round_no

            # Moderator summary + continue/stop decision (cannot stop before min_rounds).
            mod_prompt = (
                "You are the MODERATOR of a brainstorm. Summarize the round below in a few bullets, "
                "then decide whether the discussion has CONVERGED / saturated (diminishing new ideas). "
                "End your reply with a final line that is EXACTLY one of: 'DECISION: STOP' or "
                "'DECISION: CONTINUE'."
                f"{diff_note}\n\n"
                f"TOPIC:\n{topic}\n\n=== ROUND {round_no} ===\n{round_text}"
            )
            mod_result = run_moderator(moderators, mod_prompt, cwd, timeout, diff=diff, round_no=round_no)
            out.append(f"\n## Moderator (round {round_no})\n" + format_result(mod_result))
            _disc(f"\n## Moderator (round {round_no})\n"
                  f"{(mod_result.stdout.strip() or mod_result.stderr.strip() or '(no output)')}\n")
            # Promote the candidate that actually worked to the front so a dead
            # top moderator (e.g. a timing-out model) is paid once, not re-tried
            # at the head of every subsequent round + the final synthesis.
            if mod_result.returncode == 0:
                moderators = [mod_result.model] + [m for m in moderators if m != mod_result.model]

            if round_no < min_rounds:
                continue
            if "DECISION: STOP" in mod_result.stdout.upper():
                break

        # Final synthesis.
        full_transcript = "\n\n".join(transcript_blocks)
        synth_prompt = (
            "You are the MODERATOR. The brainstorm is complete. Read the full transcript and produce a "
            "final synthesis with: BEST IDEAS (ranked), TRADEOFFS, and a single concrete RECOMMENDATION."
            f"{diff_note}\n\n"
            f"TOPIC:\n{topic}\n\n=== FULL TRANSCRIPT ({completed} rounds) ===\n{full_transcript}"
        )
        # Final synthesis is part of the brainstorm: stamp it with the completed round
        # count so it logs as `-r{N}` (>=1), keeping the whole invocation off `-r0` and
        # the parser's brainstorm inference correct (HYP-742 finding 3).
        synth = run_moderator(moderators, synth_prompt, cwd, timeout, diff=diff, round_no=max(completed, 1))
        out.append("\n# Final synthesis\n" + format_result(synth))
        _disc(f"\n# Final synthesis\n{(synth.stdout.strip() or synth.stderr.strip() or '(no output)')}\n")
    finally:
        if disc is not None:
            disc.close()
    print("\n\n".join(out))
    if disc is not None:
        print(f"[review-cli] full discussion log: {disc_path}", file=sys.stderr, flush=True)
    return 0 if synth is not None and synth.returncode == 0 else 1


def brainstorm_pool(models: list[str]) -> list[str]:
    """The per-round persona-slot pool for a given panel — the GROUND-TRUTH dispatch
    size that keys the run-stats ETA (NOT len(models)).

    `mode_brainstorm` fills `max(3, len(panel))` persona slots per round, repeating
    models (`panel[slot % len(panel)]`) when the panel has < 3 backends. So a 1-2 model
    brainstorm still dispatches 3 slots; recording the raw models list would undercount
    a small panel and mis-key its history (codex P2). This mirrors that exact slot
    assignment so the recorded `pool_size`/`models` match what really runs. An empty
    panel returns [] (there is nothing to dispatch)."""
    if not models:
        return models
    slot_count = max(3, len(models))
    return [models[slot % len(models)] for slot in range(slot_count)]


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("topic", help="the open design topic to brainstorm")
    # Brainstorm-only: how many rounds the loop runs (min and hard cap). Added here (not
    # in the CLI's shared options) so they appear ONLY on `review brainstorm --help` and
    # `review just-ask --rounds 5` errors instead of silently parsing.
    parser.add_argument("--rounds", type=int, default=5, help="minimum rounds before STOP is allowed (min & default 5)")
    parser.add_argument("--max-rounds", type=int, default=8, help="hard cap on rounds (default 8)")


def _handler(ctx: ModeContext) -> int:
    return mode_brainstorm(
        ctx.with_visual(ctx.args.topic), ctx.models, ctx.cwd, ctx.timeout,
        ctx.moderators, ctx.args.rounds, ctx.args.max_rounds,
        # When there IS a diff (working-tree, --staged/--diff, or piped) the personas
        # see it as grounding context so they brainstorm ABOUT a specific change. No
        # diff -> pure ideation, exactly as before.
        diff=ctx.diff,
    )


# Brainstorm keys its run-stats ETA on the per-round persona-SLOT count (brainstorm_pool),
# not on len(models) — so the descriptor advertises that override and the CLI uses it.
MODE = ModeSpec(
    name="brainstorm",
    subcommand="brainstorm",
    diff_policy="optional",
    stats_mode="brainstorm",
    summary="multi-round persona ideation (composable with --diff/--staged grounding)",
    handler=_handler,
    add_arguments=_add_arguments,
    announce_logs=True,
)

