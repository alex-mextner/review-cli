"""Plain diff review: run the diff across every selected backend in parallel.

Extracted verbatim from the body that was inline in the original
`bin/review:main()` (Stage 0 decomposition — zero behaviour change).
"""
from __future__ import annotations

import concurrent.futures
import sys
from pathlib import Path

from ..backends import ReviewResult, resolve_backend
from ..install import _write_review_stamp
from ..panel import format_result


def mode_review(models: list[str], prompt: str, diff: str, cwd: Path, timeout: int, staged: bool) -> int:
    if not diff.strip():
        print("No diff to review.", file=sys.stderr)
        return 1

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
