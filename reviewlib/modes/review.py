"""Plain diff review: run the diff across every selected backend in parallel.

Originally extracted verbatim from `bin/review:main()` (Stage 0 decomposition).
The reviewer-board path (HYP-741) is layered on top: when a board is passed, each
reviewer gets its own role-lens prompt + label, but the parallel run, result
formatting, and staged-stamp behaviour are otherwise identical.

The failover pool (priority + availability) is layered on the board path: the board
is a PRIORITY-ordered list, the active pool is the top-N AVAILABLE seats (startup
failover), and a seat that fails DURING the run is replaced by the next-priority
reserve (mid-run failover) so the run still yields N working verdicts when possible.
"""
from __future__ import annotations

import concurrent.futures
import sys
from pathlib import Path

from ..backends import ReviewResult, backend_available, resolve_backend
from ..config import DEFAULT_POOL_SIZE, BoardReviewer, split_pool_reserve
from ..install import _write_review_stamp
from ..panel import (
    FailoverOutcome,
    _tally_result,
    format_result,
    run_board_with_failover,
)


def mode_review(
    models: list[str], prompt: str, diff: str, cwd: Path, timeout: int, staged: bool,
    board: list[BoardReviewer] | None = None, pool_size: int = DEFAULT_POOL_SIZE,
    outcome_sink: list[FailoverOutcome] | None = None,
) -> int:
    if not diff.strip():
        print("No diff to review.", file=sys.stderr)
        return 1

    if board:
        return _mode_review_board(
            board, prompt, diff, cwd, timeout, staged, pool_size, outcome_sink,
        )

    results: list[ReviewResult] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(models)) as pool:
        futures = {
            pool.submit(resolve_backend(model), model, prompt, diff, cwd, timeout): model
            for model in models
        }
        for future in concurrent.futures.as_completed(futures):
            model = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                results.append(ReviewResult(model=model, command="internal", returncode=127, stdout="", stderr=str(exc)))
            # Feed the run-stats per-call tally (no-op outside a CLI-driven run). This
            # plain `-m` path runs its own executor instead of run_panel, so it must
            # tally here for the recorded ok/fail counts to be accurate.
            _tally_result(results[-1].returncode)

    by_model = {result.model: result for result in results}
    print("\n\n---\n\n".join(format_result(by_model[model]) for model in models))
    ok = all(result.returncode == 0 for result in results)
    # Only stamp staged reviews — the commit gate verifies the STAGED diff, so an
    # unstaged/piped review must not satisfy it (and must not block later).
    if ok and staged:
        _write_review_stamp(cwd, diff)
    return 0 if ok else 1


def _mode_review_board(
    board: list[BoardReviewer], prompt: str, diff: str, cwd: Path, timeout: int,
    staged: bool, pool_size: int, outcome_sink: list[FailoverOutcome] | None,
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
    pool, reserve = split_pool_reserve(board, pool_size, lambda r: backend_available(r.model))
    if not pool:
        print("[review-cli] board: no reviewers are available — configure at least one "
              "backend key/CLI (e.g. COMMANDCODE_API_KEY, GEMINI_API_KEY, codex/claude on "
              "PATH).", file=sys.stderr, flush=True)
        return 1

    outcome = run_board_with_failover(pool, reserve, prompt, diff, cwd, timeout)
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
    if ok and staged:
        _write_review_stamp(cwd, diff)
    return 0 if ok else 1
