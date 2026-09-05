"""Panel orchestration: parallel multi-backend runs, result formatting.

Extracted verbatim from the original single-file `bin/review` (Stage 0
decomposition — zero behaviour change).
"""

from __future__ import annotations

import concurrent.futures
import os
import sys
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

from .backends import (
    ReviewResult,
    _UNAVAILABLE_MARKERS,
    _UNAVAILABLE_MAX_LEN,
    backend_available,
    call_backend,
    resolve_backend,
    review_with_images,
)
from .config import MODERATOR_CANDIDATES, BoardReviewer
from .process import set_board_deadline, write_retry_log

# Overall wall-clock BUDGET (seconds, from the start of a board run) that
# run_board_with_failover clamps its reserve-promotion idle timeouts against via
# process.set_board_deadline — so a run wrapped in an external `timeout N` degrades
# gracefully instead of being SIGKILLed mid reserve-promotion (review-cli#221: a
# promoted reserve's default 20-minute idle floor can outlive a caller's 15-minute
# external wrapper). Unset by default (None = pre-existing unclamped behaviour,
# unchanged) since not every caller runs under an external timeout.
_BOARD_DEADLINE_BUDGET_ENV = "REVIEW_BOARD_DEADLINE_SECONDS"


def _board_deadline_budget() -> int | None:
    raw = os.environ.get(_BOARD_DEADLINE_BUDGET_ENV)
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


# A backend can report rc=0 with a NON-empty body that is actually an "unavailable"
# notice rather than a real answer (e.g. the paywalled Fable returns rc=0 with
# "Claude Fable 5 is currently unavailable. Learn more: …"). The cheap availability
# probe (key/CLI present) cannot see that, so mid-run failover must treat such a body
# as a failed seat and backfill it.
#
# HEURISTIC, intentionally CONSERVATIVE — a false positive is costly (it brands a real
# verdict as failed, spends an extra reserve call, and can flip a clean `review --staged`
# to a non-zero exit that blocks a commit). So the markers match only the SPECIFIC
# "<model> is currently/temporarily unavailable" notice shape, NOT generic phrases a real
# review uses ("`X` is not available before py3.11"). The check fires only when the WHOLE
# body is short (a one-liner notice, see `_UNAVAILABLE_MAX_LEN`). These shapes may need
# updating if a provider changes its unavailability wording.
#
# `_UNAVAILABLE_MARKERS` / `_UNAVAILABLE_MAX_LEN` are imported FROM `.backends` (above),
# not defined here — backends.py is now the single canonical source (this module already
# imports several other names from backends, so this direction has no circularity; the
# reverse would). retry.py imports `_UNAVAILABLE_MAX_LEN` FROM THIS module unchanged —
# re-exporting it here keeps that import working without touching retry.py. glm/Opus
# review finding (2026-08 seat-cooldown feature, round 2): a prior version of
# backends.py kept its OWN private copy of just one of these four markers, so a Fable
# response shaped like one of the other three was classified paywall everywhere EXCEPT
# the cooldown-recording check — silently defeating the cooldown for that wording. A
# single shared source makes that class of drift impossible, not just commented-against.

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
#
# INVARIANT this module-global relies on: only ONE top-level panel operation
# (run_panel / run_panel_with_retry / run_board_with_failover / run_moderator) runs at a time
# per process. Every mode invokes them SEQUENTIALLY; the parallelism is WITHIN a single panel
# call (its worker threads), and the suppress toggle is set ONCE on the calling thread around
# that call, never by the workers — so the workers never race it. Two CONCURRENT top-level
# panels would; no mode does that. Move the flag to a contextvar before adding one that does.
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


def recount_round_by_usability(results: list[ReviewResult]) -> None:
    """Correct the active run-stats tally for a round that `run_panel` already auto-tallied.

    `run_panel` auto-tallies each call by EXIT CODE (rc 0 == ok). For a brainstorm persona
    round that is wrong for a seat that exits 0 with EMPTY / "unavailable" output — a dead or
    credential-less backend — which `result_is_usable` rejects but the rc-only tally counted
    as ok. Left uncorrected, a fully-dead rc=0 round records `ok=N, fail=0`, which both lies
    in the run-stats and poisons the ETA average for that pool size. This reclassifies each
    such seat from ok->fail so the recorded tally matches `result_is_usable` (the same
    judgement the dead-panel guard and the failover board use). A no-op outside a CLI run
    (no active tally) and for a round whose seats were all genuinely usable."""
    with _TALLY_LOCK:
        if _call_tally is None:
            return
        for result in results:
            if result.returncode == 0 and not result_is_usable(result):
                # run_panel counted this rc=0 seat as ok; it is not a real verdict. MOVE the
                # count ok->fail (a reclassification — the total stays constant). Only when
                # there IS an ok to move: run_panel always counted this seat as ok, so the
                # guard holds in practice, but keeping fail++ inside it means a broken
                # invariant can never INFLATE the total (ok+fail > calls made) — it would
                # only under-count, never lie upward.
                if _call_tally["ok"] > 0:
                    _call_tally["ok"] -= 1
                    _call_tally["fail"] += 1


def result_is_usable(result: ReviewResult) -> bool:
    """Did this reviewer produce a REAL verdict (vs a failed/empty/unavailable seat)?

    Mid-run failover backfills a seat when this returns False. Three failure shapes:
      * non-zero exit (backend error / timeout / internal crash);
      * empty output (a silently-disabled model often returns rc=0 with nothing);
      * an "unavailable" SENTINEL body — rc=0 with a short notice like "Claude Fable 5
        is currently unavailable" instead of a review (the paywalled-but-keyed case the
        cheap probe can't detect). Only a SHORT body is checked for the markers so a
        genuine long review that mentions availability isn't misclassified."""
    if result.returncode != 0:
        return False
    body = result.stdout.strip()
    if not body:
        return False
    if len(body) <= _UNAVAILABLE_MAX_LEN:
        low = body.lower()
        if any(marker in low for marker in _UNAVAILABLE_MARKERS):
            return False
    return True


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


def run_moderator(
    candidates: list[str],
    prompt: str,
    cwd: Path,
    timeout: int,
    diff: str = "",
    round_no: int = 0,
) -> ReviewResult:
    """Run the moderator `prompt` against `candidates` in priority order.

    Returns the first result that is USABLE (`result_is_usable`: exit 0, non-empty,
    and not a short "is currently unavailable" sentinel body). On a failure
    (non-zero exit, empty output, or the unavailable sentinel — a cooling-down
    claude candidate returns exactly that rc=0 sentinel shape, codex review finding:
    a bare "rc==0 and non-empty" check accepted it as a real moderator summary, so a
    cooling-down FIRST candidate silently blocked fallback to a healthy one, and
    `quorum`/`brainstorm` could report the cache-hit notice as the actual synthesis)
    it logs and falls back to the next candidate. If every candidate fails, returns
    the last result so the caller still surfaces an error rather than crashing.

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
    # codex review finding (2026-08 seat-cooldown feature): tallied by the SAME
    # predicate `_run_moderator_inner` uses to accept/reject a candidate below
    # (`result_is_usable`, not a bare `rc==0 and non-empty` check) — a cached-skip
    # sentinel is rc=0 and non-empty, so the old check would have tallied it "ok".
    _tally_ok(result_is_usable(result))
    return result


def _run_moderator_inner(
    candidates: list[str],
    prompt: str,
    cwd: Path,
    timeout: int,
    diff: str,
    round_no: int = 0,
) -> ReviewResult:
    last: ReviewResult | None = None
    for index, model in enumerate(candidates):
        result = run_single(model, prompt, cwd, timeout, diff=diff, round_no=round_no)
        if result_is_usable(result):
            if index > 0:
                print(
                    f"[review-cli] moderator fell back to {model} "
                    f"(higher-priority candidate(s) failed)",
                    file=sys.stderr,
                    flush=True,
                )
            return result
        if result.returncode != 0:
            reason = f"exit {result.returncode}"
        elif not result.stdout.strip():
            reason = "empty output"
        else:
            reason = "unavailable sentinel"  # rc=0, non-empty, but result_is_usable rejected it
        nxt = "trying next" if index + 1 < len(candidates) else "no more candidates"
        print(
            f"[review-cli] moderator {model} failed ({reason}); {nxt}",
            file=sys.stderr,
            flush=True,
        )
        last = result
    if last is None:
        return ReviewResult(
            model="(none)",
            command="moderator",
            returncode=127,
            stdout="",
            stderr="no moderator candidates",
        )
    if last.returncode == 0 and not last.stdout.strip():
        # Every candidate "succeeded" with empty output. Surface as a failure so
        # quorum/brainstorm don't report success for a synthesis that isn't there.
        return ReviewResult(
            model=last.model,
            command=last.command,
            returncode=1,
            stdout=last.stdout,
            stderr=last.stderr or "moderator produced no output",
        )
    return last


@dataclass(frozen=True)
class PanelJob:
    model: str
    prompt: str
    diff: str = ""
    label: str | None = None
    images: tuple[Path, ...] = ()
    # The brainstorm round this job belongs to (1-based). Threaded into the backend so
    # the per-call log is stamped `-r{N}` instead of always `-r0` — the dashboard parser
    # infers brainstorm mode from round>=1 (HYP-742 finding 3). 0 = single-shot
    # review/just-ask/quorum (no rounds).
    round_no: int = 0
    effort: str | None = None


def build_board_job(
    reviewer: BoardReviewer,
    base_prompt: str,
    diff: str,
    images: tuple[Path, ...] = (),
) -> PanelJob:
    """One role-lensed PanelJob for a reviewer (no availability check).

    The prompt is `base_prompt + "\\n\\n" + role_lens` (the generic prompt alone
    when the role is unknown / blank) and the label is
    `"<display> [<role>]"` so the result block shows who reviewed with which lens."""
    lens = reviewer.role_lens
    parts = [base_prompt]
    if lens:
        parts.append(lens)
    prompt = "\n\n".join(parts)
    role_tag = reviewer.role or "general"
    return PanelJob(
        model=reviewer.model,
        prompt=prompt,
        diff=diff,
        label=f"{reviewer.display} [{role_tag}]",
        images=images,
        effort=reviewer.effort,
    )


def build_board_jobs(
    board: list[BoardReviewer],
    base_prompt: str,
    diff: str,
    images: tuple[Path, ...] = (),
) -> tuple[list[PanelJob], list[BoardReviewer]]:
    """Turn a reviewer board into PanelJobs, skipping unavailable reviewers.

    Each reachable reviewer becomes one PanelJob (see build_board_job). A reviewer
    whose backend isn't available (no key / no CLI) is SKIPPED — `backend_available`
    is the same cheap probe the moderator selection uses — and returned in the second
    tuple element so the caller can log the degradation. The board never crashes on a
    dead backend; it just shrinks. Returns ([], skipped) when nothing is reachable;
    the caller decides how to surface that."""
    jobs: list[PanelJob] = []
    skipped: list[BoardReviewer] = []
    for reviewer in board:
        if not backend_available(reviewer.model):
            skipped.append(reviewer)
            continue
        jobs.append(build_board_job(reviewer, base_prompt, diff, images))
    return jobs, skipped


@dataclass(frozen=True)
class FailoverOutcome:
    """The result of a failover board run.

    `results` are the per-seat ReviewResults in run order (every seat that actually
    ran — successes AND the failed seats that triggered a backfill, so the user sees
    the whole story). `usable` is the subset that produced a real verdict (see
    result_is_usable) — its length is the honest pool_size for run-stats. `degraded`
    is True when fewer than `target` usable verdicts were produced (the reserve was
    exhausted before the pool refilled)."""

    results: list[ReviewResult]
    usable: list[ReviewResult]
    target: int
    degraded: bool
    # The BARE model ids (e.g. `zai:glm-5.2`, not the `"GLM [quality]"` label) of the
    # seats that produced a usable verdict, in the order they completed. This is the
    # honest run-stats pool: a backfilled reserve appears here under its real model id,
    # so record_run keys the ETA/history on what actually ran, never a display label.
    usable_models: list[str]


def run_board_with_failover(
    pool: list[BoardReviewer],
    reserve: list[BoardReviewer],
    base_prompt: str,
    diff: str,
    cwd: Path,
    timeout: int,
    images: tuple[Path, ...] = (),
) -> FailoverOutcome:
    """Run the priority `pool` and backfill failed seats from `reserve` (mid-run failover).

    The pool runs in parallel (run_panel). Any seat whose result is NOT usable
    (result_is_usable: non-zero exit, empty output, or an "unavailable" sentinel body)
    is a failed seat; for each, the next-priority RESERVE reviewer is promoted and run,
    repeating until `len(pool)` usable verdicts are collected or the reserve is exhausted
    (then it degrades gracefully — `FailoverOutcome.degraded` is True). The target count
    is the number of pool seats handed in (already sized to --pool and to availability by
    the caller's startup failover).

    Each LOGICAL seat is tallied exactly once against the run-stats (its FINAL outcome),
    so the per-call ok/fail counts and pool_size reflect the models that actually produced
    verdicts — not the failed attempts the failover replaced. Backfill rounds run
    sequentially (one reserve promotion per failed seat per round); the common path (the
    whole pool succeeds) costs a single parallel round."""
    target = len(pool)
    all_results: list[ReviewResult] = []
    usable: list[ReviewResult] = []
    usable_models: list[str] = []
    reserve_queue = list(reserve)

    # If REVIEW_BOARD_DEADLINE_SECONDS names an overall wall-clock budget, arm the
    # process-wide deadline for its duration so a reserve promoted late in this run gets
    # a clamped (but never starved-to-zero) idle timeout instead of the full default
    # floor — see process.idle_timeout_seconds / review-cli#221. Cleared in the finally
    # below regardless of outcome, so it can never leak into an unrelated later call.
    budget = _board_deadline_budget()
    set_board_deadline(time.monotonic() + budget if budget is not None else None)

    # The failover loop owns the run-stats tally: suppress run_panel's per-call auto-tally
    # so a failed-then-replaced seat isn't double-counted, and record exactly one outcome
    # per logical seat below (its final usable/unusable verdict).
    global _suppress_autotally
    with _TALLY_LOCK:
        prev_suppress = _suppress_autotally
        _suppress_autotally = True
    try:
        current = list(pool)
        while current:
            jobs = [build_board_job(r, base_prompt, diff, images) for r in current]
            # In-seat retry FIRST: each seat retries the SAME model on a TRANSIENT failure
            # (429/529/5xx/timeout/overloaded) with backoff+jitter, before we fall to the
            # reserve below. A SEAT-FATAL failure (auth/bad-model/501/refusal) skips the
            # retries and returns at once, so the reserve backfill stays immediate for the
            # class no retry can fix. run_panel_with_retry preserves job order, so the zip
            # below still lines each result up with its reviewer.
            round_results = run_panel_with_retry(jobs, cwd, timeout)
            # run_panel preserves job order, so each result lines up with its reviewer —
            # zip recovers the BoardReviewer (and its bare model id) behind a labelled
            # result. `strict=True` is a real guard (NOT a stripped-under-`-O` assert): if
            # run_panel ever returned a short list, this raises instead of silently
            # dropping a seat.
            next_round: list[BoardReviewer] = []
            for reviewer, result in zip(current, round_results, strict=True):
                all_results.append(result)
                if result_is_usable(result):
                    usable.append(result)
                    usable_models.append(reviewer.model)
                    _tally_ok(True)
                else:
                    # This seat failed — count it as a fail and try to backfill it from
                    # the next-priority reserve. The replacement runs in the NEXT round.
                    _tally_ok(False)
                    if reserve_queue:
                        promoted = reserve_queue.pop(0)
                        print(
                            f"[review-cli] board: {result.model} failed — promoting "
                            f"reserve {promoted.display} [{promoted.role or 'general'}] "
                            f"({promoted.model})",
                            file=sys.stderr,
                            flush=True,
                        )
                        # Durable record of the promotion (not stderr-only): a post-mortem /
                        # the dashboard can reconstruct WHICH seat failed and WHICH reserve
                        # backfilled it, with the failing seat's exit code + error channel.
                        write_retry_log(
                            f"{result.model}->{promoted.model}",
                            kind="promote",
                            attempt=0,
                            max_attempts=1,
                            delay=0.0,
                            result=result,
                        )
                        next_round.append(promoted)
            current = next_round
    finally:
        with _TALLY_LOCK:
            _suppress_autotally = prev_suppress
        set_board_deadline(None)

    degraded = len(usable) < target
    return FailoverOutcome(
        results=all_results,
        usable=usable,
        target=target,
        degraded=degraded,
        usable_models=usable_models,
    )


def run_panel(jobs: list[PanelJob], cwd: Path, timeout: int) -> list[ReviewResult]:
    """Run jobs in parallel, returning results in the SAME order as `jobs`."""
    results: list[ReviewResult | None] = [None] * len(jobs)
    if not jobs:
        return []

    def _run_job(job: PanelJob) -> ReviewResult:
        if job.images:
            return review_with_images(
                job.model,
                job.prompt,
                job.diff,
                cwd,
                timeout,
                job.round_no,
                job.images,
                effort=job.effort,
            )
        return call_backend(
            resolve_backend(job.model),
            job.model,
            job.prompt,
            job.diff,
            cwd,
            timeout,
            job.round_no,
            effort=job.effort,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {pool.submit(_run_job, job): index for index, job in enumerate(jobs)}
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
                results[index] = ReviewResult(
                    model=model,
                    command="internal",
                    returncode=127,
                    stdout="",
                    stderr=str(exc),
                )
            _tally_result(results[index].returncode)
    return [r for r in results if r is not None]


def run_panel_with_retry(
    jobs: list[PanelJob], cwd: Path, timeout: int
) -> list[ReviewResult]:
    """Run jobs in parallel like `run_panel`, but each seat RETRIES itself on a transient
    failure before giving up — same-order results out.

    Each job is dispatched on its own pool thread, and inside that thread the seat is run via
    `reviewlib.retry.run_seat_with_retry`: it calls the seat, and on a TRANSIENT failure
    (429/529/5xx/timeout/overloaded) sleeps a jittered backoff and retries the SAME model, up
    to the configured budget (`REVIEW_RETRY_COUNT` / `--retry`). A SEAT-FATAL failure (auth /
    bad model / 501 / refusal) returns immediately so the board's reserve-replace can take
    over without burning the retry budget. The seat's per-call dispatch reuses `run_panel`
    (one job), so the live per-call log files and label-wrapping are unchanged; only the
    retry orchestration is added.

    The retry import is LAZY (function-local) to avoid an import cycle: `reviewlib.retry`
    imports `panel.result_is_usable`, so importing it at module top would be circular.

    The inner per-attempt `run_panel([job])` calls would each auto-tally; that is suppressed
    ONCE around the whole parallel dispatch (a single-threaded toggle, NOT a per-thread one —
    the parallel seats must never race on the `_suppress_autotally` global). The caller
    (`run_board_with_failover` / a future direct user) owns the one-per-logical-seat tally;
    when NOT already suppressed, each seat's FINAL outcome is tallied once below, matching
    `run_panel`'s one-call-one-tally contract."""
    # Use the SAME unpaid predicate backend_available uses (runtime_provider_marked_unpaid),
    # so the pool guard's liveness view and the failover chain's drop set never disagree.
    from .backends import (
        runtime_provider_marked_unpaid as provider_marked_unpaid,
    )  # lazy
    from .provider_failover import (  # lazy: keeps panel import light
        forget_working_provider,
        is_default_provider_selection,
        provider_chain,
        remember_working_provider,
    )
    from .retry import (
        run_seat_with_retry,
    )  # lazy: breaks the panel<->retry import cycle

    if not jobs:
        return []
    results: list[ReviewResult | None] = [None] * len(jobs)

    def _seat(job: PanelJob) -> ReviewResult:
        # PROVIDER-FAILOVER (mid-review switchover): a logical model can be served by several
        # providers. Try them in order — each provider first gets the SAME-provider transient
        # retry (run_seat_with_retry: backoff on 429/5xx/DNS/timeout/reset), and if it is STILL
        # unusable we switch this SAME model to its NEXT provider and the review CONTINUES on
        # the working one. The seat only fails (-> board reserve-replace) when EVERY provider
        # is exhausted. The provider that produces the verdict is cached as last-working (tried
        # first next run); a total failure rotates the cache. Unpaid providers are dropped from
        # the chain up front (never dispatched) — distinct from this call-time failover.
        label = job.label or job.model
        chain = provider_chain(
            job.model,
            available=backend_available,
            unpaid=provider_marked_unpaid,
        )

        # Auto-tally stays suppressed (single-threaded) by the wrapper below; each inner
        # dispatch does NOT touch the global, so parallel seats can't race on it.
        def _attempt(seat_job: PanelJob) -> ReviewResult:
            def _once() -> ReviewResult:
                return run_panel([seat_job], cwd, timeout)[0]

            return run_seat_with_retry(label, _once)

        # The last-working cache only helps a model with ALTERNATES (so a chronically-flaky
        # first provider stops costing a failover each run). A single-provider seat (codex,
        # gemini, a plain claude id) has nothing to rotate, so skip the lock-serialized
        # load+atomic-rename write entirely instead of accumulating no-op `{"codex":"codex"}`
        # entries.
        #
        # The WRITE side must be gated the SAME way `provider_chain`'s cache-reorder READ is
        # (`is_default_provider_selection`): an explicit alternate pin (`-m
        # commandcode:zai-org/GLM-5.2`) succeeding/failing must not train/clear the shared
        # logical-key cache entry a later BARE alias request (`-m glm52`) reads — otherwise
        # a one-off pin silently rebiases (or wipes) default routing (review of #157: "cache
        # write isn't gated the way the read is").
        cache_eligible = len(chain) > 1 and is_default_provider_selection(job.model)
        last: ReviewResult | None = None
        for idx, provider_model in enumerate(chain):
            result = _attempt(
                replace(job, model=provider_model)
                if provider_model != job.model
                else job
            )
            if result_is_usable(result):
                if cache_eligible:
                    remember_working_provider(job.model, provider_model)
                return result
            last = result
            if idx + 1 < len(chain):
                print(
                    f"[review-cli] seat {label}: provider {provider_model} failed — "
                    f"switching to {chain[idx + 1]} (review continues)",
                    file=sys.stderr,
                    flush=True,
                )
                write_retry_log(
                    f"{provider_model}=>{chain[idx + 1]}",
                    kind="provider-failover",
                    attempt=idx,
                    max_attempts=len(chain),
                    delay=0.0,
                    result=result,
                )
        if cache_eligible:
            forget_working_provider(job.model)
        return last if last is not None else _attempt(job)

    # Suppress the inner auto-tally ONCE, on THIS thread, around the whole parallel run, then
    # tally each seat's final outcome once afterward. A single toggle here (vs one per worker
    # thread) is the whole point: the parallel `_once` dispatches must not toggle the shared
    # global concurrently. Restored in `finally` so a direct caller's tally state is intact.
    global _suppress_autotally
    with _TALLY_LOCK:
        prev_suppress = _suppress_autotally
        _suppress_autotally = True
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = {pool.submit(_seat, job): index for index, job in enumerate(jobs)}
            for future in concurrent.futures.as_completed(futures):
                index = futures[future]
                model = jobs[index].label or jobs[index].model
                try:
                    results[index] = future.result()
                except Exception as exc:  # noqa: BLE001 - report, never crash the panel
                    results[index] = ReviewResult(
                        model=model,
                        command="internal",
                        returncode=127,
                        stdout="",
                        stderr=str(exc),
                    )
    finally:
        with _TALLY_LOCK:
            _suppress_autotally = prev_suppress

    # Tally each seat's FINAL outcome once, AFTER restoring the suppression state. Two callers,
    # two owners of the count:
    #   * run_board_with_failover sets _suppress_autotally=True for its WHOLE loop, so this
    #     `_tally_result` is suppressed (a no-op) and the failover loop counts each logical seat
    #     itself via _tally_ok(result_is_usable(...)) — that is the {"ok":N,"fail":M} the tests
    #     assert. The board owns the tally.
    #   * a DIRECT caller (suppression off) gets one real count per seat here, matching
    #     run_panel's one-call-one-tally contract.
    for result in results:
        if result is not None:
            _tally_result(result.returncode)
    return [r for r in results if r is not None]


def run_single(
    model: str, prompt: str, cwd: Path, timeout: int, diff: str = "", round_no: int = 0
) -> ReviewResult:
    return run_panel(
        [PanelJob(model=model, prompt=prompt, diff=diff, round_no=round_no)],
        cwd,
        timeout,
    )[0]
