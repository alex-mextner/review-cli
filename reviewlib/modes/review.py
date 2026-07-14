"""diff review: run the diff across every selected backend in parallel.

`review diff …` is the diff review. A bare `review` prints top-level help, and the
old `review review …` spelling is a usage error that points here. **--diff/diff is
the default** (the mode REQUIRES a diff, as the pre-commit path always has).
Originally the flag-less default of `bin/review:main()` (Stage 0 decomposition);
now a first-class SUBCOMMAND backed by the `MODE` descriptor at the bottom of this
file (see `modes/contract.py`).

The reviewer-board path (HYP-741) is layered on top: when a board is passed, each
reviewer gets its own role-lens prompt + label, but the parallel run, result
formatting, and staged-stamp behaviour are otherwise identical.

The failover pool (priority + availability) is layered on the board path: the board
is a PRIORITY-ordered list, the active pool is the top-N AVAILABLE seats (startup
failover), and a seat that fails DURING the run is replaced by the next-priority
reserve (mid-run failover) so the run still yields N working verdicts when possible.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from ..backends import (
    ReviewResult,
    backend_available,
    call_backend,
    resolve_backend,
    review_with_images,
)
from ..config import DEFAULT_POOL_SIZE, BoardReviewer, split_pool_reserve
from ..install import _touch_review_marker, _write_review_stamp
from ..panel import (
    FailoverOutcome,
    _tally_result,
    format_result,
    run_board_with_failover,
)
from ..retry import retry_default, run_seat_with_retry
from . import _visual_images
from .contract import ModeContext, ModeSpec

if TYPE_CHECKING:
    from ..config import EffortOverride


def mode_review(
    models: list[str], prompt: str, diff: str, cwd: Path, timeout: int, staged: bool,
    board: list[BoardReviewer] | None = None, pool_size: int = DEFAULT_POOL_SIZE,
    outcome_sink: list[FailoverOutcome] | None = None,
    diff_from_stdin: bool = False,
    visual_images: tuple[Path, ...] = (),
    exact_board: bool = False,
    effort_override: "EffortOverride | None" = None,
) -> int:
    if not diff.strip():
        print("No diff to review.", file=sys.stderr)
        return 1

    if board:
        # The board already carries the run-scoped effort — the CLI applied it onto the
        # seats before handing the board in (apply_effort_override), so the failover path
        # needs nothing extra here.
        return _mode_review_board(
            board, prompt, diff, cwd, timeout, staged, pool_size, outcome_sink,
            diff_from_stdin, visual_images, exact_board,
        )

    # The flat `-m` / config-`models:` path: each seat runs in parallel AND now gets in-seat
    # retry on a transient failure (`--retry` / $REVIEW_RETRY_COUNT) — not just the board path.
    # Each seat's per-attempt dispatch goes through `resolve_backend` (kept as THIS module's
    # name so existing tests that stub `review.resolve_backend` still drive the fakes), wrapped
    # in `run_seat_with_retry` which retries the same seat on a transient class and returns the
    # final outcome. The per-call run-stats tally records that final outcome once per seat.
    results: list[ReviewResult] = []

    def _seat_effort(model: str) -> str | None:
        # The flat `-m` path has no BoardReviewer, so the run-scoped `--effort` is the only
        # effort source. None (no flag) keeps the dispatch byte-identical to before.
        return effort_override.effort_for(model) if effort_override is not None else None

    def _dispatch(model: str) -> ReviewResult:
        backend = resolve_backend(model)
        effort = _seat_effort(model)
        if visual_images:
            if effort is None:
                return review_with_images(model, prompt, diff, cwd, timeout, 0, visual_images)
            return review_with_images(model, prompt, diff, cwd, timeout, 0, visual_images, effort=effort)
        if effort is None:
            return backend(model, prompt, diff, cwd, timeout)
        return call_backend(backend, model, prompt, diff, cwd, timeout, effort=effort)

    def _run_seat_with_retry(model: str) -> ReviewResult:
        return run_seat_with_retry(model, lambda: _dispatch(model))

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(models)) as pool:
        futures = {pool.submit(_run_seat_with_retry, model): model for model in models}
        for future in concurrent.futures.as_completed(futures):
            model = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001 - report, never crash the panel
                results.append(ReviewResult(model=model, command="internal", returncode=127, stdout="", stderr=str(exc)))
            # One tally per logical seat (its FINAL outcome after any retry). No-op outside a
            # CLI-driven run; this flat path runs its own executor, so it tallies here.
            _tally_result(results[-1].returncode)

    by_model = {result.model: result for result in results}
    print("\n\n---\n\n".join(format_result(by_model[model]) for model in models))
    ok = all(result.returncode == 0 for result in results)
    _stamp_if_staged_commit_review(ok, staged, diff_from_stdin, cwd, diff)
    return 0 if ok else 1


def _stamp_if_staged_commit_review(
    ok: bool, staged: bool, diff_from_stdin: bool, cwd: Path, diff: str,
) -> None:
    """Write the diff-scoped review-stamp and touch the session marker iff this review
    genuinely satisfies the staged commit gate. Shared by the flat and board paths so the
    gate condition stays in ONE place.

    Conditions (all required):
      * `ok`             — the review actually passed (every seat usable / not degraded).
      * `staged`         — `--staged`; an unstaged/working-tree review is not the gate.
      * NOT `diff_from_stdin` — the diff came from `git diff --cached`, not piped stdin.
        `printf ... | review --staged` reviews ARBITRARY stdin, not the index, so it must
        not satisfy the commit gate. The stamp is diff-scoped (the hook re-derives the
        cached diff and compares), but the marker is mtime-only and would otherwise be
        forgeable this way — so both are gated on real-index provenance.
    """
    if ok and staged and not diff_from_stdin:
        _write_review_stamp(cwd, diff)
        _touch_review_marker()


def _mode_review_board(
    board: list[BoardReviewer], prompt: str, diff: str, cwd: Path, timeout: int,
    staged: bool, pool_size: int, outcome_sink: list[FailoverOutcome] | None,
    diff_from_stdin: bool = False, visual_images: tuple[Path, ...] = (),
    exact_board: bool = False,
) -> int:
    """Board path: a priority-ordered FAILOVER pool of role-lensed reviewers.

    `board` is the FULL priority-ordered board (highest priority first). The active
    pool is the top-`pool_size` AVAILABLE seats by priority (startup failover — a
    higher-priority seat whose backend isn't reachable is skipped and the next-priority
    seat pulled up); the remaining available seats are the RESERVE. The pool runs in
    parallel, and any seat that fails DURING the run (backend error, timeout, empty or
    "unavailable" output) is replaced by the next-priority reserve until `pool_size`
    usable verdicts are produced or the reserve is exhausted (then it degrades, loudly).

    Output lists every seat that ran (successes and the failed seats that triggered a
    backfill, so the whole story is visible). Exit 0 (and the staged stamp) iff the pool
    refilled to `pool_size` usable verdicts — i.e. NOT degraded. A failed-then-replaced
    seat is an expected, HANDLED failover event, not a review failure: the run still
    delivered the target number of working verdicts, so it must succeed (otherwise the
    paywalled-Fable case would make every `review --staged` fail despite a full, healthy
    pool). Only a genuine shortfall (reserve exhausted before the pool refilled) is a
    failure. `outcome_sink`, when given, receives the FailoverOutcome so the CLI can
    report the models that actually ran."""
    if exact_board:
        pool, reserve = list(board), []
    else:
        pool, reserve = split_pool_reserve(board, pool_size, lambda r: backend_available(r.model))
    if not pool:
        print("[review-cli] board: no reviewers are available — configure at least one "
              "backend key/CLI (e.g. COMMANDCODE_API_KEY, GEMINI_API_KEY, codex/claude on "
              "PATH).", file=sys.stderr, flush=True)
        return 1

    outcome = run_board_with_failover(pool, reserve, prompt, diff, cwd, timeout, visual_images)
    if outcome_sink is not None:
        outcome_sink.append(outcome)

    print("\n\n---\n\n".join(format_result(r) for r in outcome.results))

    if outcome.degraded:
        print(f"[review-cli] board: degraded — only {len(outcome.usable)} of "
              f"{outcome.target} seats produced a usable verdict (reserve exhausted).",
              file=sys.stderr, flush=True)

    # Success gate: the pool refilled to its target of usable verdicts (NOT degraded).
    # A failed-then-replaced seat is handled, so it must not block — only a real
    # shortfall (reserve exhausted) does.
    ok = not outcome.degraded
    _stamp_if_staged_commit_review(ok, staged, diff_from_stdin, cwd, diff)
    return 0 if ok else 1


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    """Diff-mode-only options. `--retry` lives HERE, not on the global surface: in-seat retry
    applies only to the diff review path (the failover board + the flat `-m` panel), NOT to
    brainstorm/quorum/just-ask (which call run_panel and never use the retry wrapper). Keeping
    it off the top-level help avoids advertising a no-op flag outside diff review (AGENTS.md:
    the global list is only truly-global options; codex P1 on #46). The other shared options
    (-m / -C / --pool / --prompt / --staged / --visual …) come from the CLI's global surface."""
    parser.add_argument(
        "--retry", type=int, default=None, metavar="N",
        help=(
            "in-seat retries on a TRANSIENT failure (429/529/5xx/timeout/overloaded) before "
            "falling to the reserve (default from $REVIEW_RETRY_COUNT, else "
            f"{retry_default()}); applies to the failover board AND the flat -m panel. A "
            "SEAT-FATAL failure (auth/bad-model/501/refusal) is never retried. 0 disables it."
        ),
    )


def _handler(ctx: ModeContext) -> int:
    """Thin over `mode_review`: the CLI resolves the board / pool / outcome_sink and
    passes them through `ctx.extra` (only the failover-board path needs them).

    The board path passes `board` + `pool_size` + `outcome_sink`; the flat path
    (explicit -m with no configured board/models) passes board=None and NOTHING else —
    the call shape there is identical to the pre-redesign default review (a board=None
    call with no pool_size/outcome_sink), so consumers/stubs of the flat path stay
    compatible."""
    board = ctx.extra.get("board")
    # A diff piped on stdin (vs read from `git diff --cached`) must not satisfy the staged
    # commit gate even under --staged — see _stamp_if_staged_commit_review.
    diff_from_stdin = bool(ctx.extra.get("diff_from_stdin", False))
    base = (
        ctx.models, ctx.with_visual(ctx.args.prompt), ctx.diff, ctx.cwd, ctx.timeout,
        ctx.args.staged,
    )
    if board is None:
        return mode_review(
            *base, board=None, diff_from_stdin=diff_from_stdin,
            visual_images=_visual_images(ctx),
            effort_override=ctx.effort_override,
        )
    return mode_review(
        *base, board=board,
        pool_size=ctx.extra.get("pool_size", DEFAULT_POOL_SIZE),
        outcome_sink=ctx.extra.get("outcome_sink"),
        diff_from_stdin=diff_from_stdin,
        visual_images=_visual_images(ctx),
        exact_board=bool(ctx.extra.get("exact_board", False)),
    )


MODE = ModeSpec(
    name="review",
    # The diff-review SUBCOMMAND is `diff` (renamed from the stuttering `review review`).
    # The stable mode `name` / `stats_mode` stay "review" (the run-stats key and the
    # handler dispatch identity are unchanged); only the user-facing verb moved.
    subcommand="diff",
    diff_policy="require",
    stats_mode="review",
    summary="diff review across the reviewer board (requires a diff)",
    handler=_handler,
    add_arguments=_add_arguments,
    announce_logs=False,
)
