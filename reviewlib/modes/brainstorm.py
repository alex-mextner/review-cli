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
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..panel import (
    PanelJob,
    format_result,
    recount_round_by_usability,
    result_is_usable,
    run_moderator,
    run_panel,
)
from ..process import log_dir
from .contract import ModeContext, ModeSpec

# Stable, distinct exit code for "the panel backends produced nothing" (dead / credential-less
# backends). A brainstorm that runs its rounds with every seat returning empty used to print a
# hollow synthesis and exit 0 as if it worked (the moderator stamps DECISION: STOP over a
# transcript of "(no output)") — wasting ~20 min of "it's still thinking". This code lets a
# caller branch on "the run was empty because the backends are dead" vs a real failure (1) or
# success (0). 0=success, 1=synthesis failed, 2=argparse/usage. 5 is the dead-panel class.
EXIT_DEAD_PANEL = 5

# UNFORGEABLE structural sentinels emitted by the discussion-log WRITER (this module) and
# read by the session parser (reviewlib.sessions). The key to making them unforgeable is a
# per-run random NONCE: each brainstorm picks an unguessable token, writes it ONCE in the
# log header (`<!-- review:session <nonce> -->`), and stamps every structural sentinel with
# it (`<!-- review:round N nonce=<nonce> -->`, `<!-- review:final nonce=<nonce> -->`). The
# personas NEVER see the nonce (it is not in any prompt), so a model echoing `# Round 2` —
# even quoting the fixed marker text or this very diff — cannot reproduce the matching
# nonce'd sentinel. The parser keys structure on a nonce-VALID sentinel, not on the
# human-readable headings. Formats are kept in lockstep with the regexes in
# `reviewlib.sessions` (SYNC: change both files together).
_SESSION_SENTINEL = "<!-- review:session {nonce} -->"
_ROUND_SENTINEL = "<!-- review:round {n} nonce={nonce} -->"
_FINAL_SENTINEL = "<!-- review:final nonce={nonce} -->"

# Fixed lead-ins of the two NON-persona prompts (moderator round / final synthesis). Defined
# here, at the prompt SOURCE, and reused both in the prompt builders below AND by the
# env-gated test fake (`backends.review_fake`, which imports these) so its role detection
# can never silently drift from the real prompt text — a reword updates one place.
MODERATOR_PROMPT_LEADIN = "You are the MODERATOR of a brainstorm."
SYNTHESIS_PROMPT_MARKER = "The brainstorm is complete."


def _new_session_nonce() -> str:
    """An unguessable per-run token stamped into every structural sentinel so model output
    (which never sees it) cannot forge a round/final delimiter. `secrets` (CSPRNG) — this is
    a spoof-resistance boundary, not cosmetic, so it must not be predictable."""
    import secrets

    return secrets.token_hex(12)


def _read_trusted_header_nonce(log: Path) -> str:
    """The existing log's session nonce, read ONLY from its TRUSTED header position — line
    index 1, immediately after the `# Brainstorm:` topic on line 0. Returns "" for a LEGACY
    (pre-sentinel) log or any file whose line 1 is not a session sentinel. Anchoring to the
    fixed header line (never an unanchored search) is what keeps a model-authored `<!--
    review:session ... -->` in some body line from being reused as a trusted nonce — mirrors
    the parser's trust rule in reviewlib.sessions (SYNC: line-1 header position)."""
    try:
        head = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    if len(head) >= 2 and head[0].startswith("# Brainstorm:"):
        m = re.match(r"^<!-- review:session ([0-9a-fA-F]+) -->\s*$", head[1])
        if m:
            return m.group(1)
    return ""


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


def _round_is_dead(round_results: list) -> bool:
    """Did this brainstorm round come back with NO real persona output?

    A round is "dead" when MOST of its seats produced no usable verdict — empty stdout, a
    non-zero exit, or an "unavailable" sentinel body (the same `result_is_usable` shape the
    failover board uses, so the two judgements can't drift). "Most" = a strict majority are
    unusable, which is exactly the all-/most-empty case the dead-panel guard exists to catch
    (every backend credential-less / suspended). A round with at least half its seats usable
    is NOT dead — a single flaky backend must not abort an otherwise productive brainstorm.

    An empty result list counts as dead (nothing ran). The threshold is on the round's OWN
    seats, so it scales with the panel size (a 3-slot or a 6-slot round)."""
    if not round_results:
        return True
    usable = sum(1 for r in round_results if result_is_usable(r))
    return usable * 2 < len(round_results)


def mode_brainstorm(
    topic: str,
    models: list[str],
    cwd: Path,
    timeout: int,
    moderators: list[str],
    rounds: int,
    max_rounds: int,
    diff: str = "",
    *,
    seed_transcript: list[str] | None = None,
    seed_persona_index: int = 0,
    start_round: int = 1,
    resume_log: Path | None = None,
    synthesize_only: bool = False,
) -> int:
    """Run (or RESUME) a multi-round brainstorm.

    Fresh start (the default): `seed_transcript=None`, `start_round=1` — identical to the
    original behaviour, first round sees `(this is the first round)`.

    RESUME (`reviewlib.sessions`): the caller hands back a prior session's parsed
    `seed_transcript` (its `## Round N` blocks), the `seed_persona_index` to continue the
    persona rotation, `start_round = completed_round + 1`, and `resume_log` = the original
    discussion-log path to APPEND to (so the resumed run continues the same log rather than
    opening a new one). The loop continues from `start_round` to `max_rounds` (respecting
    min/STOP) and produces the final synthesis. Topic / panel / moderator are passed in by
    the caller from the saved session, so a resume reuses them unchanged.

    `synthesize_only=True` (a forced re-synthesis of an already-COMPLETED session): skip the
    round loop entirely and produce a fresh synthesis over the seeded transcript — no new
    rounds, honouring the moderator's prior decision that the brainstorm was done.
    """
    min_rounds = max(rounds, 5)
    max_rounds = max(max_rounds, min_rounds)
    # `synthesize_only` (a forced re-synthesis of a finished session): skip the round loop
    # entirely and go straight to the synthesis over the seeded transcript. Without this an
    # explicit "no new rounds" intent is impossible — the min_rounds>=5 re-floor above would
    # otherwise drag a low cap back up to 5 and run rounds the caller did not want.
    loop_end = (start_round - 1) if synthesize_only else max_rounds
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
    resuming = resume_log is not None
    # On resume, seed the transcript with the prior session's rounds so the personas see
    # the same shared history a continuous run would have built; the persona rotation and
    # the completed-round counter continue where the saved session stopped.
    transcript_blocks: list[str] = list(seed_transcript) if seed_transcript else []
    moderator_label = ">".join(moderators)
    out: list[str] = [f"# Brainstorm: {topic}", f"panel={','.join(panel)} moderator={moderator_label} "
                      f"rounds>={min_rounds} max={max_rounds}{' grounded=diff' if grounded else ''}"]

    # DISCUSSION LOG: write the conversation to one file as each round/decision
    # lands (line-buffered, 0600, O_EXCL). The whole brainstorm used to be
    # accumulated in memory and printed only at the very end, so a timeout / kill /
    # crash lost everything; now the discussion-so-far is always on disk + tail-f-able.
    # On RESUME the original log is APPENDED to (O_APPEND, no O_EXCL/O_CREAT-new), so the
    # continued rounds + the final synthesis land in the SAME session log — a resumed
    # session is not a fresh file.
    if resuming:
        disc_path = resume_log  # type: ignore[assignment]
    else:
        disc_path = log_dir() / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')}-brainstorm.md"
    disc = None
    try:
        flags = (os.O_WRONLY | os.O_APPEND) if resuming else (os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        disc = os.fdopen(os.open(str(disc_path), flags, 0o600),
                         "w", encoding="utf-8", buffering=1)
        verb = "resuming discussion log" if resuming else "brainstorm discussion log"
        print(f"[review-cli] {verb}: {disc_path} (tail -f to follow)",
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

    # Per-run nonce for the structural sentinels (see _SESSION_SENTINEL). The nonce is the
    # whole file's single authority, so it is only ever taken from a TRUSTED position a model
    # cannot author:
    #   * FRESH run: mint a fresh nonce and declare it on line index 1 (right after the
    #     `# Brainstorm:` topic on line 0) — the writer owns the file header absolutely.
    #   * RESUME of a sentinel-era log: reuse that log's HEADER nonce, read ONLY from line
    #     index 1 (`_read_trusted_header_nonce`) — never an unanchored search that could pick
    #     up a model-authored `<!-- review:session ... -->` from a body line. The appended
    #     rounds carry that same nonce; no second session declaration is written.
    #   * RESUME of a LEGACY log (no trusted header nonce): nonce stays "" — the resumed
    #     rounds are appended WITHOUT sentinels, keeping the whole file legacy end-to-end, so
    #     the parser stays on its sequential-heuristic fallback for it. This deliberately
    #     introduces NO mid-file nonce, sidestepping any mixed-trust forging surface.
    if resuming and resume_log is not None:
        nonce = _read_trusted_header_nonce(resume_log)
    else:
        nonce = _new_session_nonce()

    if resuming:
        _disc(f"\n# Resumed (continuing from round {start_round})\n")
    else:
        # The session sentinel MUST land on line index 1 — that fixed position is the parser's
        # sole trust anchor. So the `# Brainstorm:` heading must be exactly ONE line: a topic
        # carrying newlines (e.g. `--visual` appends a multi-line context note) would otherwise
        # push the sentinel off line 1, and the whole log would silently fall back to legacy
        # parsing with the nonce'd round sentinels leaking into the transcript (codex P2).
        # Collapse the heading to a single line here; the full multi-line `topic` is still fed
        # to the personas unchanged — only this human-readable header line is flattened.
        topic_heading = " ".join(topic.splitlines()).strip()
        _disc(f"# Brainstorm: {topic_heading}\n{_SESSION_SENTINEL.format(nonce=nonce)}\n\n"
              f"panel={','.join(panel)} moderator={moderator_label} "
              f"rounds>={min_rounds} max={max_rounds}\n")

    persona_index = seed_persona_index
    completed = start_round - 1
    synth = None
    try:
        for round_no in range(start_round, loop_end + 1):
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
            out.append(f"\n# Round {round_no}\n" + "\n\n---\n\n".join(format_result(r) for r in round_results))

            # FAIL LOUD on a dead panel (see _round_is_dead): abort before recording the round
            # as COMPLETED, so a dead round is never appended to the transcript / counted in
            # `completed` / given a structural sentinel — a resume re-runs it rather than seeding
            # its "(no output)" (codex P2). The round is still logged below for diagnosis.
            #
            # ONLY when NO usable round has accumulated yet (`not transcript_blocks`): that is
            # the bug this guards — every seat dead from round 1 (keyless/suspended backends),
            # which used to print a hollow synthesis and exit 0. If earlier rounds WERE
            # productive and a later round flakes (a transient rate-limit on a couple of
            # seats), we must NOT throw away the good rounds — fall through and synthesize what
            # we have, the pre-existing graceful behavior (claude-opus review). Mid-run
            # transient-failure resilience (retry/reserve-swap) is a separate ROADMAP item.
            if not transcript_blocks and _round_is_dead(round_results):
                usable = sum(1 for r in round_results if result_is_usable(r))
                msg = (
                    f"[review-cli] brainstorm aborted: round {round_no} produced no usable output "
                    f"({usable}/{len(round_results)} panel seats answered).\n"
                    "  most/all backends are dead or credential-less (empty output, an error, or an "
                    "'unavailable' notice) — continuing would only print a hollow synthesis.\n"
                    f"  panel: {', '.join(panel)}\n"
                    "  fix: check the backends are reachable (keys present, CLIs installed, provider "
                    "not suspended), then re-run — or pass a working panel with `-m`."
                )
                print(msg, file=sys.stderr, flush=True)
                # Correct the run-stats tally: run_panel auto-counted each rc=0 seat as ok, but a
                # dead seat (rc=0, empty/"unavailable") is not a real verdict — reclassify it to
                # fail so the recorded run is honest and doesn't poison the ETA average (codex P2).
                recount_round_by_usability(round_results)
                # Diagnosis-only: log the dead round WITHOUT a structural round sentinel, so the
                # session parser does NOT count it as a completed round (a resume re-runs it).
                _disc(f"\n# Round {round_no} (ABORTED: dead panel — "
                      f"{usable}/{len(round_results)} seats usable)\n{round_text}\n")
                # Surface the partial output we DID gather so a human sees the dead seats.
                if out:
                    print("\n\n".join(out))
                if disc is not None:
                    print(f"[review-cli] partial discussion log: {disc_path}", file=sys.stderr, flush=True)
                return EXIT_DEAD_PANEL

            transcript_blocks.append(f"## Round {round_no}\n{round_text}")
            # The nonce'd `<!-- review:round N nonce=... -->` sentinel on its own line right
            # after the heading is the UNFORGEABLE structural marker the session parser keys
            # on: the personas never see the per-run nonce, so a model echoing `# Round 9`
            # (even reproducing the fixed marker text) cannot stamp the matching nonce. The
            # human-readable `# Round N` heading stays for readability. A legacy resume
            # (nonce == "") writes NO sentinel — the appended rounds stay legacy, parsed by
            # the sequential-heuristic fallback like the rest of that file.
            round_sentinel = f"{_ROUND_SENTINEL.format(n=round_no, nonce=nonce)}\n" if nonce else ""
            _disc(f"\n# Round {round_no}\n{round_sentinel}{round_text}\n")
            completed = round_no

            # Moderator summary + continue/stop decision (cannot stop before min_rounds).
            mod_prompt = (
                f"{MODERATOR_PROMPT_LEADIN} Summarize the round below in a few bullets, "
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
            f"You are the MODERATOR. {SYNTHESIS_PROMPT_MARKER} Read the full transcript and produce a "
            "final synthesis with: BEST IDEAS (ranked), TRADEOFFS, and a single concrete RECOMMENDATION."
            f"{diff_note}\n\n"
            f"TOPIC:\n{topic}\n\n=== FULL TRANSCRIPT ({completed} rounds) ===\n{full_transcript}"
        )
        # Final synthesis is part of the brainstorm: stamp it with the completed round
        # count so it logs as `-r{N}` (>=1), keeping the whole invocation off `-r0` and
        # the parser's brainstorm inference correct (HYP-742 finding 3).
        synth = run_moderator(moderators, synth_prompt, cwd, timeout, diff=diff, round_no=max(completed, 1))
        out.append("\n# Final synthesis\n" + format_result(synth))
        # The nonce'd `<!-- review:final nonce=... -->` sentinel marks the REAL synthesis —
        # the parser keys completion on it, so a persona echoing `# Final synthesis` in its
        # body (it cannot stamp the per-run nonce) never falsely completes-and-truncates the
        # session. A legacy resume (nonce == "") writes no sentinel; the parser falls back to
        # completing on the synthesis heading inside the moderator region for that file.
        final_sentinel = f"{_FINAL_SENTINEL.format(nonce=nonce)}\n" if nonce else ""
        _disc(f"\n# Final synthesis\n{final_sentinel}"
              f"{(synth.stdout.strip() or synth.stderr.strip() or '(no output)')}\n")
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

