"""Panel orchestration: parallel multi-backend runs, result formatting.

Extracted verbatim from the original single-file `bin/review` (Stage 0
decomposition — zero behaviour change).
"""
from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass
from pathlib import Path

from .backends import ReviewResult, backend_available, resolve_backend
from .config import MODERATOR_CANDIDATES


def format_result(result: ReviewResult) -> str:
    status = "ok" if result.returncode == 0 else f"exit {result.returncode}"
    body = result.stdout.strip()
    err = result.stderr.strip()
    parts = [f"## {result.model} [{status}]", f"`{result.command}`"]
    if body:
        parts.append(body)
    if err:
        parts.append("stderr:\n" + err)
    return "\n\n".join(parts)


def pick_moderator(explicit: str | None, panel: list[str]) -> str:
    if explicit:
        return explicit
    for candidate in MODERATOR_CANDIDATES:
        if backend_available(candidate):
            return candidate
    return panel[0]


@dataclass(frozen=True)
class PanelJob:
    model: str
    prompt: str
    diff: str = ""
    label: str | None = None


def run_panel(jobs: list[PanelJob], cwd: Path, timeout: int) -> list[ReviewResult]:
    """Run jobs in parallel, returning results in the SAME order as `jobs`."""
    results: list[ReviewResult | None] = [None] * len(jobs)
    if not jobs:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {
            pool.submit(resolve_backend(job.model), job.model, job.prompt, job.diff, cwd, timeout): index
            for index, job in enumerate(jobs)
        }
        for future in concurrent.futures.as_completed(futures):
            index = futures[future]
            model = jobs[index].label or jobs[index].model
            try:
                base = future.result()
                results[index] = ReviewResult(
                    model=jobs[index].label or base.model,
                    command=base.command,
                    returncode=base.returncode,
                    stdout=base.stdout,
                    stderr=base.stderr,
                )
            except Exception as exc:  # noqa: BLE001 - report, never crash the panel
                results[index] = ReviewResult(model=model, command="internal", returncode=127, stdout="", stderr=str(exc))
    return [r for r in results if r is not None]


def run_single(model: str, prompt: str, cwd: Path, timeout: int, diff: str = "") -> ReviewResult:
    return run_panel([PanelJob(model=model, prompt=prompt, diff=diff)], cwd, timeout)[0]
