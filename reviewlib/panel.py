"""Panel orchestration: parallel multi-backend runs, result formatting.

Extracted verbatim from the original single-file `bin/review` (Stage 0
decomposition — zero behaviour change).
"""
from __future__ import annotations

import concurrent.futures
import sys
from dataclasses import dataclass
from pathlib import Path

from .backends import ReviewResult, backend_available, resolve_backend
from .config import MODERATOR_CANDIDATES, BoardReviewer


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


def pick_moderators(explicit: str | None, panel: list[str]) -> list[str]:
    """Ordered moderator candidates (highest priority first).

    explicit (``--moderator``) goes first when given, then the configured
    MODERATOR_CANDIDATES filtered to backends that pass the cheap availability
    probe, then panel[0] as a guaranteed last resort. Order-preserving de-dup.
    ``run_moderator`` walks this list at run time and falls back to the next
    candidate when one FAILS — so even an explicitly requested moderator, or one
    that passes the availability probe but dies at run time (e.g. an
    Anthropic-disabled model), never leaves the panel without a synthesis.
    """
    ordered: list[str] = []
    if explicit:
        ordered.append(explicit)
    ordered.extend(c for c in MODERATOR_CANDIDATES if backend_available(c))
    if panel:
        ordered.append(panel[0])
    seen: set[str] = set()
    deduped = [c for c in ordered if not (c in seen or seen.add(c))]
    return deduped or list(panel[:1]) or ["codex"]


def pick_moderator(explicit: str | None, panel: list[str]) -> str:
    """Single best moderator (first of pick_moderators). Kept for callers that
    only need one; new code should prefer pick_moderators + run_moderator."""
    return pick_moderators(explicit, panel)[0]


def run_moderator(candidates: list[str], prompt: str, cwd: Path, timeout: int, diff: str = "") -> ReviewResult:
    """Run the moderator `prompt` against `candidates` in priority order.

    Returns the first result that succeeds (exit 0 with non-empty output). On a
    failure (non-zero exit OR empty output — the dead-moderator hang surfaces as
    a timeout exit, and a silently-disabled model as empty output) it logs and
    falls back to the next candidate. If every candidate fails, returns the last
    result so the caller still surfaces an error rather than crashing.

    Each call retries from the top of the list: the common path (the first
    candidate works) costs one run, and a persistently dead top candidate only
    adds its own failure latency per moderator turn — acceptable for the rare
    Anthropic-disabled case, and self-healing the moment the model comes back.
    """
    if isinstance(candidates, str):  # tolerate a single-model string from older callers
        candidates = [candidates]
    last: ReviewResult | None = None
    for index, model in enumerate(candidates):
        result = run_single(model, prompt, cwd, timeout, diff=diff)
        if result.returncode == 0 and result.stdout.strip():
            if index > 0:
                print(f"[review-cli] moderator fell back to {model} "
                      f"(higher-priority candidate(s) failed)", file=sys.stderr, flush=True)
            return result
        reason = f"exit {result.returncode}" if result.returncode != 0 else "empty output"
        nxt = "trying next" if index + 1 < len(candidates) else "no more candidates"
        print(f"[review-cli] moderator {model} failed ({reason}); {nxt}", file=sys.stderr, flush=True)
        last = result
    if last is None:
        return ReviewResult(model="(none)", command="moderator", returncode=127,
                            stdout="", stderr="no moderator candidates")
    if last.returncode == 0 and not last.stdout.strip():
        # Every candidate "succeeded" with empty output. Surface as a failure so
        # quorum/brainstorm don't report success for a synthesis that isn't there.
        return ReviewResult(model=last.model, command=last.command, returncode=1,
                            stdout=last.stdout, stderr=last.stderr or "moderator produced no output")
    return last


@dataclass(frozen=True)
class PanelJob:
    model: str
    prompt: str
    diff: str = ""
    label: str | None = None


def build_board_jobs(
    board: list[BoardReviewer], base_prompt: str, diff: str,
) -> tuple[list[PanelJob], list[BoardReviewer]]:
    """Turn a reviewer board into PanelJobs, skipping unavailable reviewers.

    Each reachable reviewer becomes one PanelJob whose prompt is
    `base_prompt + "\\n\\n" + role_lens` (the generic prompt alone when the role is
    unknown / blank) and whose label is `"<display> [<role>]"` so the result block
    shows who reviewed with which lens. A reviewer whose backend isn't available
    (no key / no CLI) is SKIPPED — `backend_available` is the same cheap probe the
    moderator selection uses — and returned in the second tuple element so the
    caller can log the degradation. The board never crashes on a dead backend; it
    just shrinks. Returns ([] , skipped) when nothing is reachable; the caller
    decides how to surface that."""
    jobs: list[PanelJob] = []
    skipped: list[BoardReviewer] = []
    for reviewer in board:
        if not backend_available(reviewer.model):
            skipped.append(reviewer)
            continue
        lens = reviewer.role_lens
        prompt = f"{base_prompt}\n\n{lens}" if lens else base_prompt
        role_tag = reviewer.role or "general"
        jobs.append(PanelJob(
            model=reviewer.model, prompt=prompt, diff=diff,
            label=f"{reviewer.display} [{role_tag}]",
        ))
    return jobs, skipped


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
