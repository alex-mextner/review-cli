"""In-seat retry: classify a backend failure, then retry the SAME seat on a TRANSIENT one.

Why this exists
---------------
The failover board (panel.run_board_with_failover) already does RESERVE-REPLACE: a seat
that fails is backfilled from the next-priority reserve. But that throws away the seat on
the FIRST failure, which is wasteful for a TRANSIENT error (a 429 rate-limit / a 529
overloaded / a gateway 5xx / a timeout) where the same model would have answered on a
second try a second later. This module adds the missing layer: retry the SAME seat with
exponential backoff + jitter while the failure looks transient, and only fall to the
reserve once retries are exhausted OR the failure is SEAT-FATAL (auth / bad model / a 501
not-implemented / a refusal) — a class no retry can fix, so we don't waste the budget.

The classification contract is MIRRORED from agent-tools (the single ecosystem source of
truth, lib/contracts/models.yaml `fallback_chain:` + agent-hooks/model-error-fallback/
`fallback_chain.is_transient_model_error`). agent-tools is not importable as a package from
this repo (review-cli ships with a single `pyyaml` dep), so the regex set is copied here
verbatim rather than imported. Keep the two IN SYNC: if a provider changes its throttle
wording, update BOTH. A divergence means the cross-harness fallback hook and review-cli's
in-seat retry would disagree about what "transient" means.

A failure is RETRYABLE only when it reads as transient on the error CHANNEL: stderr, plus a
short stdout when the exit code is NON-ZERO (a failed CLI often writes its error to stdout).
The backend COMMAND line is excluded (its argv carries incidental numeric tokens), and a
SUCCESSFUL run's stdout is excluded — a long review that merely mentions "rate limit" or
"503" while describing the code must not be misread as a throttle (the agent-tools
error-channel discipline); the rc=0 "unavailable" sentinel is handled separately as an
administrative seat-fatal state. Default: NOT retryable (fail closed toward the reserve — a
misclassified fatal that we retry only wastes latency; a misclassified transient that we
DON'T retry still gets reserve-replaced, so the run is never stranded).
"""
from __future__ import annotations

import os
import random
import re
import sys
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from .backends import ReviewResult
from .panel import _UNAVAILABLE_MAX_LEN, result_is_usable
from .process import write_retry_log

# ── transient classification (MIRRORED from agent-tools fallback_chain.py) ────────────────
# The closed vocabulary of error SIGNALS that count as TRANSIENT (retry the same seat). These
# are byte-identical to agent-tools/agent-hooks/model-error-fallback/fallback_chain.py
# TRANSIENT_PATTERNS — the ecosystem's one source of truth for "what can a retry / a different
# provider's quota fix right now". A failure NOT matching these is SEAT-FATAL: switching or
# retrying won't help (a wrong answer, a failing test, an auth error, a 501).
_TRANSIENT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b429\b",
        # 5xx transient family: 500/502/503/504 gateway-class, 520-524 (Cloudflare gateway),
        # and 529 (Anthropic overloaded). A bare numeric code still counts (e.g. "HTTP 529").
        r"\b5(?:0[0234]|2[0-4]|29)\b",
        r"rate[\s_-]?limit",
        r"temporarily (?:limiting|unavailable)",
        r"overloaded",
        r"server (?:error|temporarily)",
        r"service unavailable",
        r"too many requests",
        # NOTE: `quota` is DELIBERATELY NOT here, unlike the agent-tools cross-harness chain.
        # There "transient" means "a DIFFERENT provider's quota could serve this now", so a
        # quota error is worth falling to another harness. In-seat retry hits the SAME provider
        # key — an exhausted quota will NOT replenish inside the retry window — so for THIS
        # layer a quota error is effectively seat-fatal: retrying just burns the budget + the
        # backoff before the reserve takes over. It is classified SEAT_FATAL below so the
        # reserve (a different seat / key) is reached immediately. (rate-limit DOES recover in
        # seconds, so it stays transient; quota does not.)
        # "at/over/out-of/insufficient capacity" only — NOT a bare "capacity" and NOT "no
        # capacity", so a refusal ("I have no capacity to help with that") is not misread as a
        # transient outage. The leading \b stops "at capacity" matching inside "great capacity".
        r"\b(?:at|over|out of|insufficient)\s+capacity",
        r"\bthrottl",  # throttle / throttled / throttling
    )
)

# The shell exit code our streamed runner stamps on a PROCESS timeout (mirrors `timeout`'s
# 124, see process._run_streamed / backstop.BACKSTOP_EXIT_CODE). A timeout is the canonical
# transient failure even when the partial buffer carries no throttle keyword, so it is
# retryable by exit code alone.
_TIMEOUT_EXIT_CODE = 124

# How many times a TIMEOUT (exit 124) may be retried in-seat, regardless of the larger retry
# budget. A timeout is expensive: each retry is ANOTHER full per-call `timeout` wait, so an
# unbounded budget would make a genuinely-hung seat wait (budget+1) full timeouts before the
# reserve takes over (3x the per-call timeout at the default budget). One re-attempt covers a
# one-off slow response; a seat that times out TWICE is treated as down and reserve-replaced
# at once. A non-timeout transient (a fast 429/5xx) still gets the full budget — its retries
# are cheap (they fail in milliseconds, not a whole timeout). The wall-clock cap below is the
# backstop for a SLOW transient (a 503 returned just before the per-call timeout, rc != 124).
_MAX_TIMEOUT_RETRIES = 1

# Wall-clock cap (seconds) on the whole in-seat retry LOOP — the backstop for a SLOW transient
# the exit-124 cap can't catch: a backend that holds the connection and returns a 503/429 in
# the BODY just shy of the per-call timeout (rc != 124) would otherwise cost ~a full timeout
# per retry, up to budget x timeout (~10x at the ceiling). Once the cumulative time spent in
# the retry loop crosses this, we stop retrying and hand the seat to the reserve, regardless of
# remaining budget. Generous enough to clear a normal throttle (a few short backoffs + fast
# retries) but well under a single long per-call timeout, so a slow-failing seat reserves
# promptly. Overridable via $REVIEW_RETRY_MAX_SECONDS (tests force it tiny / large).
_DEFAULT_RETRY_MAX_SECONDS = 90.0

# A SEAT-FATAL error channel short-circuits the transient check (checked BEFORE the transient
# patterns): an auth / not-implemented / refusal / bad-model failure can never be fixed by a
# retry, so it goes STRAIGHT to the reserve. These anchors are kept TIGHT on purpose: a
# fatal-first check that matched a bare word ("authentication", "billing", "forbidden")
# anywhere would mis-eat a real TRANSIENT that merely mentions it — e.g. "503 authentication
# service temporarily unavailable" or "502 billing gateway timeout" would skip the cheap retry
# and burn a reserve seat, undercutting the whole feature. So the broad words require their
# FAILURE qualifier (authentication failed / billing limit), and the unqualified signals are
# the unambiguous status codes / phrases that are never transient.
_SEAT_FATAL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b401\b",
        r"\b403\b",
        r"\b501\b",  # not implemented — a permanent capability gap, never a throttle
        r"unauthorized",
        # authentication: only the FAILURE phrasings, not the bare word (which appears in
        # "authentication service temporarily unavailable", a transient).
        r"authentication (?:failed|error|required)",
        # api-key: only the FAILURE phrasings (invalid/missing/expired/incorrect/bad), not the
        # bare "api key" — some providers echo the key id in a rate-limit message ("api key
        # ... rate limited"), which is transient and must keep the cheap retry (reviewC).
        r"invalid[\s_-]?(?:api[\s_-]?)?key",
        r"(?:missing|expired|incorrect|bad|no)\s+api[\s_-]?key",
        r"api[\s_-]?key\s+(?:is\s+)?(?:invalid|missing|expired|incorrect|not[\s_-]?found)",
        r"\bforbidden\b",  # word-anchored; 403 already covers the status code
        r"permission denied",
        r"(?:unknown|invalid|unsupported|unrecognized|no such)\s+model",
        r"model[\s_-]?not[\s_-]?found",
        r"\bmodel\b.*does not exist",  # anchored to a MODEL, not a bare "does not exist"
        r"not[\s_-]?implemented",
        # `quota` / "billing"/"insufficient credit": the SAME key won't replenish in the retry
        # window (see the transient-list note), so for IN-SEAT retry these are seat-fatal —
        # fall to the reserve at once rather than retry the dead key. `quota`/`billing` BOTH
        # require their failure qualifier (exceeded/exhausted/reached/limit), not the bare word:
        # many providers say "429 Too Many Requests — quota resets shortly" for a RECOVERABLE
        # RPM/TPM rate-limit, and "502 billing gateway timeout" is a transient — neither must be
        # mis-eaten as fatal (reviewD). Only an EXHAUSTED billing quota is seat-fatal.
        r"\bquota\s+(?:exceeded|exhausted|reached|limit)",
        r"(?:exceeded|exhausted|reached|out of|over)\s+(?:your\s+)?quota",
        r"insufficient (?:credit|balance|funds)",
        r"billing (?:limit|hard[\s_-]?limit|exceeded|error|problem)",
    )
)


class FailureClass(Enum):
    """How a non-usable seat result should be handled by the failover loop.

    RETRYABLE — a TRANSIENT failure that the SAME key can recover from in seconds: a 429
                rate-limit, a 529/5xx overload, an "overloaded"/"service unavailable" notice,
                or a process timeout (the timeout retry is capped — see _MAX_TIMEOUT_RETRIES).
                Retry the SAME seat with backoff before falling to the reserve.
    SEAT_FATAL — no retry of the SAME key can fix it: auth / bad-model / 501 / refusal, AND an
                exhausted quota / billing limit (the same key won't replenish in the retry
                window — unlike the agent-tools cross-harness chain, which can fall to a
                different provider's quota). Go straight to the reserve. (Also the DEFAULT for
                an unclassifiable failure — fail closed toward the reserve, which still keeps
                the pool full.)
    """

    RETRYABLE = "retryable"
    SEAT_FATAL = "seat-fatal"


def _is_rc0_sentinel(result: ReviewResult) -> bool:
    """True when the failure is the rc=0 "unavailable" SENTINEL body (the paywalled/disabled
    case): exit 0, a SHORT non-review stdout, and an empty stderr.

    This is an ADMINISTRATIVE unavailability (a model paywalled or turned off), NOT a throttle:
    the provider answered cleanly (rc=0) with a one-line "…is currently/temporarily
    unavailable" notice instead of a review. Retrying it in-seat is pointless — it is chronic,
    not transient, so the SAME model will return the SAME notice on every retry. So this is
    classified SEAT_FATAL (immediate reserve), NOT retried — even though the notice text
    contains words like "temporarily unavailable" that the transient set would otherwise match
    (reviewC: a chronically-paywalled model must not pay 2 retries + backoff every single run).

    The short-body rule reuses result_is_usable's own rejection + the panel sentinel length
    bound (imported, not copied), so this and the panel's sentinel detector never disagree."""
    if result.returncode != 0 or (result.stderr or "").strip():
        return False
    body = (result.stdout or "").strip()
    return bool(body) and len(body) <= _UNAVAILABLE_MAX_LEN and not result_is_usable(result)


def _error_channel(result: ReviewResult) -> str:
    """The text used for transient/fatal classification — the ERROR CHANNEL.

    stderr always; PLUS a SHORT stdout when the exit code is NON-ZERO. Many CLIs stream their
    answer to stdout and, on failure, write the error text to stdout (not stderr) while exiting
    non-zero with an empty stderr — so a stderr-only channel would miss a real "503"/"429" and
    silently never retry that backend (reviewD). A non-zero exit already MEANS failure, so its
    short stdout is an error body, not a review.

    TWO things stay excluded:
      * a SUCCESSFUL run's stdout (rc==0) — never folded here. rc==0 is either a real review
        (usable; doesn't reach classification) or the administrative SENTINEL (handled up front
        in `classify_failure` via `_is_rc0_sentinel`, before the patterns). So a long review
        mentioning "503"/"rate limit" is never misread as a throttle (the agent-tools
        error-channel discipline);
      * the backend COMMAND line — its argv carries incidental numeric tokens (a port,
        `--timeout 503`, a model version) that would false-match a pattern.

    The non-zero stdout is length-bounded (`_UNAVAILABLE_MAX_LEN`) so a long partial body (e.g.
    a timed-out review that streamed a lot before dying) isn't scanned wholesale for an
    incidental keyword — an error notice is a short line, not paragraphs."""
    parts = [(result.stderr or "").strip()]
    if result.returncode != 0:
        body = (result.stdout or "").strip()
        if body and len(body) <= _UNAVAILABLE_MAX_LEN:
            parts.append(body)
    return "\n".join(p for p in parts if p)


def classify_failure(result: ReviewResult) -> FailureClass:
    """Classify a NON-USABLE seat result as RETRYABLE (transient) or SEAT_FATAL.

    Precondition: the caller has already decided this seat is not usable (result_is_usable is
    False) — this only decides HOW to recover (retry the seat vs fall to the reserve). Order:
      1. a process TIMEOUT (exit 124) is transient by code (capped separately in the loop);
      2. the rc=0 "unavailable" SENTINEL is an ADMINISTRATIVE/chronic state (paywall/disabled),
         NOT a throttle — SEAT_FATAL, so a chronically-unavailable model goes STRAIGHT to the
         reserve every run instead of paying retries it can never clear (reviewC);
      3. a SEAT-FATAL channel (auth / bad-model / 501 / refusal) wins over an incidental
         transient-looking substring;
      4. the transient patterns (a real stderr throttle);
      5. otherwise SEAT_FATAL — fail closed; an unclassifiable failure falls to the reserve,
         never spins the retry budget on a mystery."""
    if result.returncode == _TIMEOUT_EXIT_CODE:
        return FailureClass.RETRYABLE
    if _is_rc0_sentinel(result):
        return FailureClass.SEAT_FATAL
    channel = _error_channel(result)
    if any(p.search(channel) for p in _SEAT_FATAL_PATTERNS):
        return FailureClass.SEAT_FATAL
    if any(p.search(channel) for p in _TRANSIENT_PATTERNS):
        return FailureClass.RETRYABLE
    return FailureClass.SEAT_FATAL


# ── retry configuration (env / flag / config, read at call time) ──────────────────────────
# How many EXTRA in-seat attempts a transient failure gets before falling to the reserve. 0
# disables in-seat retry (straight to reserve-replace, the legacy behaviour). The default is
# small on purpose: a couple of retries clears a brief throttle spike without lengthening a
# genuinely-down seat's path to the reserve. Overridable via $REVIEW_RETRY_COUNT or a `--retry
# N` flag (the CLI resolves the flag and exports the env so this one reader sees both).
_DEFAULT_RETRY_COUNT = 2
_MAX_RETRY_COUNT = 10  # a sane ceiling so a typo'd env can't pin a dead seat for minutes

# Backoff schedule: delay before the Nth retry = base * factor**(N-1), capped, then jittered.
# Small base — a throttle clears in well under a second usually, and the board already runs
# under the 4h backstop, so we keep each seat's retry path short.
_BASE_DELAY_SECONDS = 0.5
_BACKOFF_FACTOR = 2.0
_MAX_DELAY_SECONDS = 8.0
_JITTER_FRACTION = 0.25  # ±25% of the computed delay, so concurrent seats don't sync-retry


def retry_default() -> int:
    """The built-in default in-seat retry budget (used in `--retry` help text). Separate from
    `retry_count()` so the help string shows the BUILT-IN default, not the env-overridden one."""
    return _DEFAULT_RETRY_COUNT


def max_retry_count() -> int:
    """The hard ceiling on the retry budget (a typo'd env / flag clamps here)."""
    return _MAX_RETRY_COUNT


def retry_count() -> int:
    """The configured in-seat retry budget, read at CALL time so an env override applies.

    $REVIEW_RETRY_COUNT wins; a missing/blank/non-integer value falls back to the default; a
    negative value clamps to 0 (disabled); a value above the ceiling clamps down. The CLI's
    `--retry N` flag is wired by EXPORTING $REVIEW_RETRY_COUNT before the run, so this single
    reader honours flag, env, and default with one precedence rule (flag==env here)."""
    raw = os.environ.get("REVIEW_RETRY_COUNT")
    if raw is None or not raw.strip():
        return _DEFAULT_RETRY_COUNT
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_RETRY_COUNT
    return max(0, min(value, _MAX_RETRY_COUNT))


def retry_max_seconds() -> float:
    """Wall-clock cap (seconds) on the whole in-seat retry loop, read at CALL time.

    $REVIEW_RETRY_MAX_SECONDS wins; a missing/blank/non-numeric value falls back to the
    default; a non-positive value disables the wall-clock cap (only the count + timeout caps
    apply then). This is the backstop for a SLOW transient that the exit-124 cap can't catch
    (a 503 returned just before the per-call timeout, rc != 124)."""
    raw = os.environ.get("REVIEW_RETRY_MAX_SECONDS")
    if raw is None or not raw.strip():
        return _DEFAULT_RETRY_MAX_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_RETRY_MAX_SECONDS
    return value  # <= 0 disables the wall-clock cap (handled at the call site)


def compute_backoff(attempt: int, rng: random.Random) -> float:
    """Backoff (seconds) to sleep BEFORE the given 1-based retry attempt.

    `base * factor**(attempt-1)`, capped at the max, then jittered symmetrically by ±jitter
    so parallel seats that all throttled at once don't retry in lockstep (a thundering herd
    back onto the same rate-limited provider). The jitter is drawn from the CALLER's owned
    `rng` so a seeded test gets a reproducible schedule and one seat can't perturb another."""
    raw = _BASE_DELAY_SECONDS * (_BACKOFF_FACTOR ** (attempt - 1))
    capped = min(raw, _MAX_DELAY_SECONDS)
    spread = rng.uniform(-_JITTER_FRACTION, _JITTER_FRACTION)
    jittered = capped * (1.0 + spread)
    return max(0.0, min(jittered, _MAX_DELAY_SECONDS))


@dataclass(frozen=True)
class SeatAttempt:
    """One in-seat retry event, recorded DURABLY (run log) and surfaced to stderr.

    `attempt` is the 1-based retry index (attempt 1 = the first RETRY, i.e. the second call to
    the seat). `delay` is the backoff slept before it. `prior` is the failing ReviewResult
    that triggered this retry (its rc + a trimmed error channel go into the log)."""

    model: str
    attempt: int
    max_attempts: int
    delay: float
    prior: ReviewResult


def run_seat_with_retry(
    model: str,
    runner: Callable[[], ReviewResult],
    *,
    max_retries: int | None = None,
    max_seconds: float | None = None,
    rng: random.Random | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    on_retry: Callable[[SeatAttempt], None] | None = None,
) -> ReviewResult:
    """Run ONE seat, retrying the SAME seat on a TRANSIENT failure before giving up.

    `runner` is a zero-arg callable that performs one backend call for this seat and returns
    its ReviewResult (the panel passes a closure over its real `run_single`/job dispatch — so
    this module never has to know how a seat is actually run). The loop:

      1. call the seat;
      2. if the result is USABLE, return it (success — common path, one call);
      3. else classify the failure: SEAT_FATAL -> return immediately (let the caller fall to
         the reserve — no retry can fix auth/bad-model/501/refusal);
      4. RETRYABLE and budget left -> sleep a jittered backoff, log the retry DURABLY, retry;
      5. budget exhausted, OR the wall-clock cap is reached, OR a second timeout -> return the
         last (failed) result so the caller reserve-replaces it.

    THREE independent caps bound the cost so a slow-failing seat can't pin the run:
      * the retry COUNT (`max_retries` / `retry_count()`);
      * a TIMEOUT sub-cap (`_MAX_TIMEOUT_RETRIES`) — each timeout costs a whole per-call wait;
      * a WALL-CLOCK cap (`max_seconds` / `retry_max_seconds()`) — the backstop for a SLOW
        transient (a 503 returned just shy of the per-call timeout, rc != 124) the timeout
        sub-cap can't see. Checked BEFORE each retry so total time in the loop is bounded.

    Returns the FIRST usable result, or the LAST failed one when retries don't recover it
    (fail-loud-on-empty is preserved: an unusable result is handed back unchanged for the
    caller's reserve/degrade path — this layer never fabricates a verdict). `max_retries` /
    `max_seconds` default to the env-configured values; `rng`/`sleeper`/`clock` are injectable
    for deterministic tests."""
    budget = retry_count() if max_retries is None else max(0, max_retries)
    cap_seconds = retry_max_seconds() if max_seconds is None else max_seconds
    jitter_rng = rng if rng is not None else random.Random()
    max_attempts = budget + 1  # the initial call + `budget` retries
    timeout_retries = 0  # timeouts are capped separately (each costs a full per-call timeout)
    started = clock()

    result = runner()
    if result_is_usable(result):
        return result

    for attempt in range(1, budget + 1):
        if classify_failure(result) is FailureClass.SEAT_FATAL:
            # No retry can fix this class — hand the failed result back so the caller falls to
            # the reserve immediately, conserving the retry budget for genuine throttles.
            write_retry_log(model, kind="seat-fatal", attempt=attempt - 1,
                            max_attempts=max_attempts, delay=0.0, result=result)
            return result
        # Wall-clock backstop: if we've already spent the cap retrying (slow failed calls +
        # backoffs), stop — a SLOW transient must not cost ~budget x per-call timeout before
        # the reserve takes over. cap_seconds <= 0 disables this cap (count/timeout caps stay).
        if cap_seconds > 0 and (clock() - started) >= cap_seconds:
            write_retry_log(model, kind="retry-time-exhausted", attempt=attempt - 1,
                            max_attempts=max_attempts, delay=0.0, result=result)
            return result
        # A TIMEOUT retry costs another full per-call timeout, so cap it independently of the
        # (cheap) transient budget: after _MAX_TIMEOUT_RETRIES timeouts, stop retrying and let
        # the reserve take over rather than wait (budget+1) full timeouts on a hung seat. This
        # exit is a CAPPED TRANSIENT, not a fatal class — logged under its own kind so a
        # post-mortem / the dashboard can tell "we stopped retrying a hung seat" apart from a
        # real auth/501/refusal seat-fatal.
        if result.returncode == _TIMEOUT_EXIT_CODE:
            if timeout_retries >= _MAX_TIMEOUT_RETRIES:
                write_retry_log(model, kind="timeout-exhausted", attempt=attempt - 1,
                                max_attempts=max_attempts, delay=0.0, result=result)
                return result
            timeout_retries += 1
        delay = compute_backoff(attempt, jitter_rng)
        event = SeatAttempt(model=model, attempt=attempt, max_attempts=max_attempts,
                            delay=delay, prior=result)
        _announce_retry(event)
        write_retry_log(model, kind="retry", attempt=attempt, max_attempts=max_attempts,
                        delay=delay, result=result)
        if on_retry is not None:
            on_retry(event)
        sleeper(delay)
        result = runner()
        if result_is_usable(result):
            # We only reach here from inside the retry loop, so this IS a recovery.
            print(f"[review-cli] seat {model} recovered on retry {attempt}/{budget}",
                  file=sys.stderr, flush=True)
            return result
    return result


def _announce_retry(event: SeatAttempt) -> None:
    """Print a one-line retry notice to stderr (the human channel; the durable record is the
    run log). Trimmed so a long error channel doesn't flood stderr."""
    reason = (event.prior.stderr or event.prior.stdout or "").strip().splitlines()
    head = reason[0][:120] if reason else f"exit {event.prior.returncode}"
    print(f"[review-cli] seat {event.model} transient failure "
          f"(retry {event.attempt}/{event.max_attempts - 1} in {event.delay:.1f}s): {head}",
          file=sys.stderr, flush=True)
