"""Plain diff review: run the diff across every selected backend in parallel.

Originally extracted verbatim from `bin/review:main()` (Stage 0 decomposition).
The reviewer-board path (HYP-741) is layered on top: when a board is passed, each
reviewer gets its own role-lens prompt + label, but the parallel run, result
formatting, and staged-stamp behaviour are otherwise identical.
"""
from __future__ import annotations

import concurrent.futures
import sys
from pathlib import Path

from ..backends import ReviewResult, resolve_backend
from ..config import BoardReviewer
from ..install import _write_review_stamp
from ..panel import build_board_jobs, format_result, run_panel


def mode_review(
    models: list[str], prompt: str, diff: str, cwd: Path, timeout: int, staged: bool,
    board: list[BoardReviewer] | None = None,
) -> int:
    if not diff.strip():
        print("No diff to review.", file=sys.stderr)
        return 1

    if board:
        return _mode_review_board(board, prompt, diff, cwd, timeout, staged)

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

    by_model = {result.model: result for result in results}
    print("\n\n---\n\n".join(format_result(by_model[model]) for model in models))
    ok = all(result.returncode == 0 for result in results)
    # Only stamp staged reviews — the commit gate verifies the STAGED diff, so an
    # unstaged/piped review must not satisfy it (and must not block later).
    if ok and staged:
        _write_review_stamp(cwd, diff)
    return 0 if ok else 1


def _mode_review_board(
    board: list[BoardReviewer], prompt: str, diff: str, cwd: Path, timeout: int, staged: bool,
) -> int:
    """Board path: one role-lensed PanelJob per AVAILABLE reviewer, run in parallel.

    Unavailable reviewers (no key / no CLI) are skipped and logged to stderr so the
    board degrades gracefully instead of crashing. Output order follows `jobs`
    (run_panel preserves it). The staged stamp is written only when EVERY job
    succeeded — same gate as the legacy path, so a board with a failed reviewer
    never silently satisfies the commit gate."""
    jobs, skipped = build_board_jobs(board, prompt, diff)
    for reviewer in skipped:
        print(f"[review-cli] board: skipping {reviewer.display} "
              f"[{reviewer.role or 'general'}] ({reviewer.model}) — backend unavailable "
              "(no key / not on PATH)", file=sys.stderr, flush=True)
    if not jobs:
        print("[review-cli] board: no reviewers are available — configure at least one "
              "backend key/CLI (e.g. COMMANDCODE_API_KEY, GEMINI_API_KEY, codex/claude on "
              "PATH).", file=sys.stderr, flush=True)
        return 1

    results = run_panel(jobs, cwd, timeout)
    print("\n\n---\n\n".join(format_result(r) for r in results))
    ok = all(r.returncode == 0 for r in results)
    if ok and staged:
        _write_review_stamp(cwd, diff)
    return 0 if ok else 1
