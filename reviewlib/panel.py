"""Panel orchestration: parallel multi-backend runs, result formatting.

Extracted verbatim from the original single-file `bin/review` (Stage 0
decomposition — zero behaviour change).
"""
from __future__ import annotations

import concurrent.futures
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from .backends import ReviewResult, backend_available, resolve_backend
from .config import MODERATOR_CANDIDATES, BoardReviewer

# Optional per-call success/fail tally for the run-stats record. The CLI installs a
# collector (`begin_call_tally`) before a run and reads it after, so every mode that
# funnels its backend calls through `run_panel` / `run_single` / `run_moderator`
# contributes real per-call counts WITHOUT changing any mode's signature. None means
# "no run is being tallied" (the default — e.g. tests calling run_panel directly), so
# the hook is a zero-cost no-op outside a CLI-driven run.
_TALLY_LOCK = threading.Lock()
_call_tally: dict[str, int] | None = None
# When True, run_panel's auto-tally is suppressed: run_moderator owns the count for its
# candidate loop and tallies exactly ONE outcome with the moderator success criterion
# (rc 0 AND non-empty stdout), so an empty-but-rc0 candidate the mode REJECTS is not
# miscounted as ok and failed fallbacks aren't over-counted (codex P2).
_suppress_autotally = False


def begin_call_tally() -> None:
    """Start counting per-call ok/fail for the current run. CLI-only; idempotent."""
    global _call_tally
    with _TALLY_LOCK:
        _call_tally = {"ok": 0, "fail": 0}


def end_call_tally() -> dict[str, int]:
    """Stop counting and return ``{"ok": n, "fail": n}`` for the run just finished."""
    global _call_tally
    with _TALLY_LOCK:
        tally = _call_tally or {"ok": 0, "fail": 0}
        _call_tally = None
        return dict(tally)


def _tally_ok(success: bool) -> None:
    """Record one call outcome against the active tally (no-op outside a CLI run)."""
    with _TALLY_LOCK:
        if _call_tally is None:
            return
        _call_tally["ok" if success else "fail"] += 1


def _tally_result(returncode: int) -> None:
    """Auto-tally a panel call by exit code, unless suppressed (moderator path)."""
    with _TALLY_LOCK:
        if _call_tally is None or _suppress_autotally:
            return
        _call_tally["ok" if returncode == 0 else "fail"] += 1


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


def run_moderator(candidates: list[str], prompt: str, cwd: Path, timeout: int, diff: str = "", round_no: int = 0) -> ReviewResult:
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

    # A moderator turn is ONE logical call in the run-stats, no matter how many
    # candidates it falls through. Suppress run_panel's per-candidate auto-tally and
    # tally exactly one outcome below, judged by the SAME success criterion this
    # function uses (rc 0 AND non-empty output) — so an empty-but-rc0 candidate the
    # mode rejects is counted as a fail, not an ok (codex P2).
    global _suppress_autotally
    with _TALLY_LOCK:
        prev_suppress = _suppress_autotally
        _suppress_autotally = True
    try:
        result = _run_moderator_inner(candidates, prompt, cwd, timeout, diff, round_no)
    finally:
        with _TALLY_LOCK:
            _suppress_autotally = prev_suppress
    _tally_ok(result.returncode == 0 and bool(result.stdout.strip()))
    return result


def _run_moderator_inner(candidates: list[str], prompt: str, cwd: Path, timeout: int, diff: str, round_no: int = 0) -> ReviewResult:
    last: ReviewResult | None = None
    for index, model in enumerate(candidates):
        result = run_single(model, prompt, cwd, timeout, diff=diff, round_no=round_no)
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
    # The brainstorm round this job belongs to (1-based). Threaded into the backend so
    # the per-call log is stamped `-r{N}` instead of always `-r0` — the dashboard parser
    # infers brainstorm mode from round>=1 (HYP-742 finding 3). 0 = single-shot
    # review/just-ask/quorum (no rounds).
    round_no: int = 0


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
            pool.submit(
                resolve_backend(job.model), job.model, job.prompt, job.diff, cwd, timeout,
                job.round_no,
            ): index
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
            _tally_result(results[index].returncode)
    return [r for r in results if r is not None]


def run_single(model: str, prompt: str, cwd: Path, timeout: int, diff: str = "", round_no: int = 0) -> ReviewResult:
    return run_panel([PanelJob(model=model, prompt=prompt, diff=diff, round_no=round_no)], cwd, timeout)[0]
