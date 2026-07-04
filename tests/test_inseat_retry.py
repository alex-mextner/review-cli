"""Unit tests for IN-SEAT RETRY + retryable/seat-fatal classification (reviewlib.retry).

Today the board has reserve-replace failover but throws a seat away on the FIRST failure.
This adds the missing layer: retry the SAME seat with backoff+jitter on a TRANSIENT failure
(429 / 529 / 5xx / timeout / overloaded) BEFORE falling to the reserve, and go STRAIGHT to
the reserve on a SEAT-FATAL failure (auth / bad model / 501 / refusal) that no retry can fix.

These tests, all OFFLINE (no model call, no network — backends are stubbed by per-call
behaviour scripts and the clock/RNG are injected), prove:
  (a) classify_failure: each transient shape (429/529/5xx/timeout/overloaded/throttle/quota)
      reads RETRYABLE, each fatal shape (auth/403/501/bad-model/refusal) reads SEAT_FATAL,
      and an unclassifiable failure fails CLOSED to SEAT_FATAL (-> straight to the reserve);
  (b) the error CHANNEL discipline: a long real review BODY mentioning "503"/"rate limit" is
      NOT misread as transient (the body is not the channel); the short rc=0 "unavailable"
      SENTINEL body IS in-channel;
  (c) a transient-then-succeeds seat is RETRIED in-seat and recovers (no reserve needed);
  (d) a SEAT-FATAL seat returns immediately — ZERO retries (the reserve takes over);
  (e) the configured retry count is RESPECTED (env $REVIEW_RETRY_COUNT, the budget bounds the
      number of attempts), and a persistent transient failure exhausts exactly the budget;
  (f) the retry is logged DURABLY (a retry-event file under the run log dir), not stderr-only;
  (g) backoff is present and GROWS (exponential), and JITTER perturbs it (two seeds differ);
  (h) the panel integration (run_board_with_failover) retries a transient pool seat in-seat
      and keeps the pool full WITHOUT consuming the reserve; a seat-fatal one DOES fall to
      the reserve; fail-loud-on-empty is preserved (an unrecoverable seat is handed back, not
      faked into a verdict).

Plain-script harness (mirrors tests/test_failover_pool.py): each test_* is run by __main__.
"""
from __future__ import annotations

import os
import random
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import reviewlib.panel as panel  # noqa: E402
import reviewlib.retry as retry  # noqa: E402
from reviewlib.backends import ReviewResult  # noqa: E402
from reviewlib.config import DEFAULT_BOARD, split_pool_reserve  # noqa: E402
from reviewlib.panel import run_board_with_failover  # noqa: E402
from reviewlib.process import log_dir  # noqa: E402
from reviewlib.retry import (  # noqa: E402
    FailureClass,
    classify_failure,
    compute_backoff,
    retry_count,
    run_seat_with_retry,
)

PROMPT = "Review this diff."


# A `tmp_log` argument names a FRESH run-log dir for the test (so write_retry_log / log_dir
# write somewhere isolated and assertable). Under pytest it is a fixture (defined below);
# under the plain __main__ harness the runner injects the same value. Both paths route through
# _make_tmp_log so the two can never drift.
try:
    import pytest

    @pytest.fixture
    def tmp_log():
        restore = _make_tmp_log()
        try:
            yield None
        finally:
            restore()
except ImportError:  # pytest absent: the plain __main__ harness supplies tmp_log itself
    pass


def _result(rc: int = 0, out: str = "ok", err: str = "", cmd: str = "fake") -> ReviewResult:
    return ReviewResult(model="m", command=cmd, returncode=rc, stdout=out, stderr=err)


# === (a) classify_failure: transient vs seat-fatal ===============================
def test_classify_429_is_retryable():
    assert classify_failure(_result(1, "", "HTTP 429 Too Many Requests")) is FailureClass.RETRYABLE

def test_classify_529_overloaded_is_retryable():
    assert classify_failure(_result(1, "", "Error 529: overloaded")) is FailureClass.RETRYABLE

def test_classify_5xx_gateway_is_retryable():
    for code in ("500", "502", "503", "504", "520", "524"):
        assert classify_failure(_result(1, "", f"upstream returned {code}")) is FailureClass.RETRYABLE, code

def test_classify_timeout_exit_code_is_retryable():
    """A process timeout (exit 124, the `timeout`/backstop code) is transient by code alone —
    even with no throttle keyword in the partial buffer."""
    assert classify_failure(_result(124, "partial...", "")) is FailureClass.RETRYABLE

def test_classify_rate_limit_words_are_retryable():
    for phrase in ("rate limit exceeded", "rate-limited", "too many requests",
                   "service unavailable", "server temporarily busy",
                   "request throttled", "we are at capacity right now"):
        assert classify_failure(_result(1, "", phrase)) is FailureClass.RETRYABLE, phrase

def test_transient_mentioning_auth_or_billing_word_still_retries():
    """The seat-fatal anchors are TIGHT: a real TRANSIENT that merely contains the word
    'authentication'/'billing'/'forbidden' (not its failure phrasing) must NOT be mis-eaten as
    fatal — it has to keep the cheap in-seat retry, or the feature loses its value."""
    for phrase in (
        "503 authentication service temporarily unavailable",
        "502 billing gateway timeout",
        "429 rate limit on the billing region",
        "api key abc123 rate limited, retry later",  # provider echoes the key id in a 429
    ):
        assert classify_failure(_result(1, "", phrase)) is FailureClass.RETRYABLE, phrase

def test_classify_quota_and_billing_are_seat_fatal_for_in_seat():
    """Unlike the agent-tools cross-harness chain (where a quota error is worth falling to a
    DIFFERENT provider's quota), in-seat retry hits the SAME key — an exhausted quota / billing
    failure won't replenish in the retry window, so it is SEAT_FATAL here: fall to the reserve
    at once, don't burn the budget on a dead key."""
    for phrase in ("quota exceeded", "monthly quota reached", "insufficient credit",
                   "insufficient balance", "billing hard limit reached"):
        assert classify_failure(_result(1, "", phrase)) is FailureClass.SEAT_FATAL, phrase

def test_classify_auth_is_seat_fatal():
    for phrase in ("HTTP 401 Unauthorized", "403 Forbidden", "authentication failed",
                   "invalid api key", "permission denied"):
        assert classify_failure(_result(1, "", phrase)) is FailureClass.SEAT_FATAL, phrase

def test_classify_bad_model_is_seat_fatal():
    for phrase in ("unknown model 'foo'", "model not found", "unsupported model",
                   "the model does not exist"):
        assert classify_failure(_result(1, "", phrase)) is FailureClass.SEAT_FATAL, phrase

def test_classify_501_not_implemented_is_seat_fatal():
    assert classify_failure(_result(1, "", "HTTP 501 Not Implemented")) is FailureClass.SEAT_FATAL

def test_classify_refusal_capacity_is_not_transient():
    """A refusal that says 'no capacity' / 'great capacity' must NOT read as a transient
    outage (it would burn the reserve on a non-transient failure)."""
    for phrase in ("I have no capacity to help with that",
                   "this requires great capacity for nuance"):
        assert classify_failure(_result(0, phrase, "")) is FailureClass.SEAT_FATAL, phrase

def test_classify_unclassifiable_fails_closed_to_seat_fatal():
    """An empty/mystery failure defaults to SEAT_FATAL — fail closed toward the reserve, never
    spin the retry budget on a failure we can't read."""
    assert classify_failure(_result(1, "", "")) is FailureClass.SEAT_FATAL
    assert classify_failure(_result(1, "", "internal weirdness, no http code")) is FailureClass.SEAT_FATAL

def test_seat_fatal_wins_over_incidental_transient_substring():
    """An auth failure whose text ALSO contains a transient-looking number still classifies
    SEAT_FATAL (the fatal check runs first) — switching/retrying can't fix bad credentials."""
    assert classify_failure(_result(1, "", "401 unauthorized (after 3 rate limit retries)")) is FailureClass.SEAT_FATAL


# === (b) error-channel discipline ===============================================
def test_long_review_body_mentioning_503_is_not_in_channel():
    """A LONG, usable review body that mentions '503' / 'rate limit' while describing the code
    is a real verdict — it must not even reach classification (it is usable), and its body is
    not the error channel."""
    body = "x" * 600 + " the handler should retry on 503 and rate limit responses"
    res = ReviewResult(model="m", command="fake", returncode=0, stdout=body, stderr="")
    assert panel.result_is_usable(res)  # usable -> never classified as a failure at all
    # And even if forced through classification, the long body is NOT in the error channel:
    assert "503" not in retry._error_channel(res)

def test_rc0_sentinel_body_is_seat_fatal_not_retried():
    """The rc=0 short 'unavailable' SENTINEL (paywalled/disabled Fable) is an ADMINISTRATIVE,
    chronic state — NOT a throttle. It must classify SEAT_FATAL (immediate reserve), even
    though the notice text contains 'temporarily unavailable' which the transient set would
    otherwise match. A chronically-unavailable model must not pay retries + backoff every run
    (reviewC #1/#2)."""
    for body in (
        "This model is temporarily unavailable. Please try again later.",
        "Claude Fable 5 is currently unavailable. Learn more: https://x",
    ):
        res = ReviewResult(model="m", command="fake", returncode=0, stdout=body, stderr="")
        assert not panel.result_is_usable(res)
        assert classify_failure(res) is FailureClass.SEAT_FATAL, body

def test_real_stderr_throttle_still_retries():
    """A REAL transient on the ERROR channel (stderr / non-zero exit) still retries — the
    sentinel-is-fatal rule only covers the rc=0 administrative notice, not a genuine 503."""
    assert classify_failure(_result(1, "", "503 service temporarily unavailable")) is FailureClass.RETRYABLE
    assert classify_failure(_result(1, "", "429 rate limit")) is FailureClass.RETRYABLE

def test_nonzero_exit_stdout_error_is_in_channel():
    """A backend that streams to stdout and, on FAILURE, writes the error to stdout with rc!=0
    and EMPTY stderr (a common CLI pattern) must still be classified — a short non-zero stdout
    is an error body, not a review, so it joins the channel and a transient there retries
    (reviewD #1)."""
    assert classify_failure(_result(1, "503 service unavailable", "")) is FailureClass.RETRYABLE
    assert classify_failure(_result(1, "429 too many requests", "")) is FailureClass.RETRYABLE
    # ...but a SUCCESSFUL (rc=0) long review mentioning 503 is usable and never classified.
    long_review = "x" * 600 + " consider retrying on 503"
    assert panel.result_is_usable(ReviewResult("m", "c", 0, long_review, ""))

def test_429_with_quota_word_still_retries():
    """'429 Too Many Requests — quota resets shortly' is a RECOVERABLE RPM/TPM limit, not an
    exhausted billing quota — the bare-`quota` fatal anchor must not eat it (reviewD #2)."""
    for phrase in (
        "429 Too Many Requests, quota resets shortly",
        "rate limit hit, per-minute quota will reset",
    ):
        assert classify_failure(_result(1, "", phrase)) is FailureClass.RETRYABLE, phrase

def test_command_line_is_not_in_error_channel():
    """The backend COMMAND must NOT feed classification: an incidental numeric token in argv
    (a port, a `--timeout 503`, a model version) must not false-match a transient pattern and
    burn the retry budget. With an empty stderr/body, such a command stays unclassifiable ->
    SEAT_FATAL (straight to the reserve, no wasted retry)."""
    res = ReviewResult(model="m", command="codex exec -m gpt-503 --timeout 429", returncode=1, stdout="", stderr="")
    assert "503" not in retry._error_channel(res)
    assert "429" not in retry._error_channel(res)
    assert classify_failure(res) is FailureClass.SEAT_FATAL


# === (c) transient-then-succeeds is retried in-seat ============================
class _Script:
    """A seat runner driven by a list of (rc, out, err) outcomes, one per call."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def __call__(self) -> ReviewResult:
        rc, out, err = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        return ReviewResult(model="m", command="fake", returncode=rc, stdout=out, stderr=err)


def _no_sleep(_seconds: float) -> None:
    return None


def test_transient_retried_then_succeeds(tmp_log):
    """A seat that fails transiently once then succeeds is RETRIED in-seat and recovers — the
    runner is called exactly twice, and the final result is the usable one."""
    script = _Script([(1, "", "429 rate limit"), (0, "real verdict", "")])
    out = run_seat_with_retry("m", script, max_retries=3, rng=random.Random(1), sleeper=_no_sleep)
    assert out.returncode == 0 and out.stdout == "real verdict"
    assert script.calls == 2  # initial + ONE retry


# === (d) seat-fatal -> zero retries (straight to reserve) =======================
def test_seat_fatal_does_not_retry(tmp_log):
    """A SEAT-FATAL failure returns immediately — the runner is called ONCE, no retry, so the
    caller (failover) reserve-replaces it without burning the budget."""
    script = _Script([(1, "", "401 unauthorized"), (0, "should-never-reach", "")])
    out = run_seat_with_retry("m", script, max_retries=5, rng=random.Random(1), sleeper=_no_sleep)
    assert out.returncode == 1 and "unauthorized" in out.stderr
    assert script.calls == 1  # ZERO retries on a fatal


# === (e) retry count respected ==================================================
def test_retry_count_respected_exhausts_budget(tmp_log):
    """A persistently-transient seat is retried EXACTLY `budget` times then handed back failed
    (fail-loud — never faked into a verdict). budget=3 -> 1 initial + 3 retries = 4 calls."""
    script = _Script([(1, "", "503 service unavailable")])  # always transient-fails
    out = run_seat_with_retry("m", script, max_retries=3, rng=random.Random(1), sleeper=_no_sleep)
    assert out.returncode == 1  # still failed: handed back for the reserve, not fabricated
    assert script.calls == 4

def test_timeout_retry_is_capped(tmp_log):
    """A TIMEOUT (exit 124) retry costs a full per-call timeout, so it is capped independently
    of the larger budget: with budget=5, a seat that keeps timing out is retried only ONCE
    (cap=1) then handed back for the reserve — NOT 5 times (which would wait 6 full timeouts on
    a hung seat). Initial + 1 timeout retry = 2 calls."""
    script = _Script([(124, "partial", "")])  # always times out
    out = run_seat_with_retry("m", script, max_retries=5, rng=random.Random(1), sleeper=_no_sleep)
    assert out.returncode == 124
    assert script.calls == 2, script.calls  # initial + exactly ONE timeout retry

def test_wall_clock_cap_stops_a_slow_transient(tmp_log):
    """The wall-clock cap bounds a SLOW transient the exit-124 sub-cap can't see: a backend
    that returns a 503 in the BODY just shy of the per-call timeout (rc != 124) would otherwise
    cost ~budget x timeout. With a fake clock that advances 40s per call and a 90s cap, the seat
    is retried only until ~90s elapsed, then handed back — NOT the full budget of 8."""
    clock = {"t": 0.0}

    def _clock():
        return clock["t"]

    def _slow_runner_factory():
        def _r():
            clock["t"] += 40.0  # each failed call burns ~40s of wall time
            return ReviewResult("m", "fake", 1, "", "503 service unavailable")
        return _r

    out = run_seat_with_retry(
        "m", _slow_runner_factory(), max_retries=8, max_seconds=90.0,
        rng=random.Random(1), sleeper=_no_sleep, clock=_clock,
    )
    assert out.returncode == 1  # still failed -> reserve takes over (fail-loud)
    # initial (t->40) + retry1 (t->80) ; before retry2 elapsed=80<90 so retry2 runs (t->120);
    # before retry3 elapsed=120>=90 -> stop. So 3 calls, NOT 9. The cap bit well before budget.
    text = "\n".join(p.read_text() for p in log_dir().glob("*-retry*.log"))
    assert "kind=retry-time-exhausted" in text, text

def test_retry_max_seconds_env_override():
    """retry_max_seconds() reads $REVIEW_RETRY_MAX_SECONDS at call time, symmetric with
    retry_count(): blank/garbage -> default, a number -> that number, <=0 passes through
    (disable handled at the call site)."""
    saved = os.environ.get("REVIEW_RETRY_MAX_SECONDS")
    try:
        os.environ["REVIEW_RETRY_MAX_SECONDS"] = "30"
        assert retry.retry_max_seconds() == 30.0
        os.environ["REVIEW_RETRY_MAX_SECONDS"] = "0"
        assert retry.retry_max_seconds() == 0.0
        os.environ["REVIEW_RETRY_MAX_SECONDS"] = "-5"
        assert retry.retry_max_seconds() == -5.0
        os.environ["REVIEW_RETRY_MAX_SECONDS"] = "garbage"
        assert retry.retry_max_seconds() == retry._DEFAULT_RETRY_MAX_SECONDS
        os.environ["REVIEW_RETRY_MAX_SECONDS"] = "  "
        assert retry.retry_max_seconds() == retry._DEFAULT_RETRY_MAX_SECONDS
        del os.environ["REVIEW_RETRY_MAX_SECONDS"]
        assert retry.retry_max_seconds() == retry._DEFAULT_RETRY_MAX_SECONDS
    finally:
        if saved is None:
            os.environ.pop("REVIEW_RETRY_MAX_SECONDS", None)
        else:
            os.environ["REVIEW_RETRY_MAX_SECONDS"] = saved

def test_zero_sleep_retries_do_not_clobber_each_others_logs(tmp_log):
    """Under zero backoff (the test path, or a future 0-delay config) two retry events for the
    SAME model can land in the same microsecond. The filename seq discriminator must keep them
    as SEPARATE files — assert the NUMBER of retry-log files equals the number of retry events,
    not 1 (which would mean an O_TRUNC clobber)."""
    script = _Script([(1, "", "503"), (1, "", "503"), (0, "ok", "")])
    run_seat_with_retry("same-model", script, max_retries=3, rng=random.Random(1), sleeper=_no_sleep)
    files = list(log_dir().glob("*-retry*.log"))
    assert len(files) == 2, [f.name for f in files]  # two retry events -> two distinct files

def test_wall_clock_cap_disabled_with_nonpositive(tmp_log):
    """max_seconds<=0 disables the wall-clock cap — only the count cap applies then."""
    script = _Script([(1, "", "503")])
    out = run_seat_with_retry("m", script, max_retries=3, max_seconds=0.0,
                              rng=random.Random(1), sleeper=_no_sleep)
    assert out.returncode == 1
    assert script.calls == 4  # full budget used, the wall-clock cap did not fire

def test_fast_transient_still_uses_full_budget_even_after_a_timeout(tmp_log):
    """A cheap (non-timeout) transient still gets the full budget — the timeout cap only bounds
    timeouts. Here a single timeout is retried (cap allows 1), then a 429 keeps retrying until
    the seat recovers, within the budget."""
    script = _Script([(124, "", ""), (1, "", "429"), (1, "", "429"), (0, "ok", "")])
    out = run_seat_with_retry("m", script, max_retries=5, rng=random.Random(1), sleeper=_no_sleep)
    assert out.returncode == 0 and out.stdout == "ok"
    assert script.calls == 4  # initial timeout + 3 retries (1 timeout-retry + 2 fast 429s)

def test_retry_count_zero_disables_retry(tmp_log):
    script = _Script([(1, "", "429"), (0, "x", "")])
    out = run_seat_with_retry("m", script, max_retries=0, rng=random.Random(1), sleeper=_no_sleep)
    assert out.returncode == 1
    assert script.calls == 1  # no retry at all

def test_cli_retry_flag_overrides_existing_env(tmp_log):
    """The CLI `--retry N` must WIN over a pre-existing $REVIEW_RETRY_COUNT (and clamp). Drives
    the real cli._dispatch export path (parse -> export) in an empty git repo (the run exits
    cleanly with 'No diff to review' AFTER the export) and asserts the resolved budget."""
    import subprocess
    import tempfile

    from reviewlib import cli

    repo = tempfile.mkdtemp(prefix="review-retry-cli-")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    saved = os.environ.get("REVIEW_RETRY_COUNT")
    try:
        for env_val, flag_val, want in [("1", "7", 7), ("9", "-4", 0), ("2", "9999", retry.max_retry_count())]:
            os.environ["REVIEW_RETRY_COUNT"] = env_val
            cli._dispatch(["diff", "--task", "TEST-1", "--staged", "--retry", flag_val, "-C", repo])
            assert retry_count() == want, f"env={env_val} --retry={flag_val} -> {retry_count()}, want {want}"
    finally:
        if saved is None:
            os.environ.pop("REVIEW_RETRY_COUNT", None)
        else:
            os.environ["REVIEW_RETRY_COUNT"] = saved


def test_retry_count_env_override(tmp_log):
    """retry_count() reads $REVIEW_RETRY_COUNT at call time; clamps negative->0, caps the
    ceiling, ignores garbage."""
    saved = os.environ.get("REVIEW_RETRY_COUNT")
    try:
        os.environ["REVIEW_RETRY_COUNT"] = "5"
        assert retry_count() == 5
        os.environ["REVIEW_RETRY_COUNT"] = "-3"
        assert retry_count() == 0
        os.environ["REVIEW_RETRY_COUNT"] = "9999"
        assert retry_count() == retry.max_retry_count()
        os.environ["REVIEW_RETRY_COUNT"] = "not-a-number"
        assert retry_count() == retry.retry_default()
        del os.environ["REVIEW_RETRY_COUNT"]
        assert retry_count() == retry.retry_default()
    finally:
        if saved is None:
            os.environ.pop("REVIEW_RETRY_COUNT", None)
        else:
            os.environ["REVIEW_RETRY_COUNT"] = saved


# === (f) durable logging ========================================================
def test_retry_logged_durably(tmp_log):
    """A retry writes a DURABLE retry-event log file under the run log dir (not stderr-only)."""
    script = _Script([(1, "", "429 rate limit"), (0, "ok", "")])
    run_seat_with_retry("seat-x", script, max_retries=2, rng=random.Random(1), sleeper=_no_sleep)
    logs = list(log_dir().glob("*-retry*.log"))
    assert logs, "no durable retry-event log written"
    text = "\n".join(p.read_text() for p in logs)
    assert "RETRY-EVENT" in text
    assert "kind=retry" in text
    assert "429" in text  # the failing channel detail is persisted

def test_seat_fatal_logged_durably(tmp_log):
    """A seat-fatal failure also leaves a durable record (kind=seat-fatal) so a post-mortem
    sees WHY a seat went straight to the reserve."""
    script = _Script([(1, "", "403 forbidden")])
    run_seat_with_retry("seat-y", script, max_retries=2, rng=random.Random(1), sleeper=_no_sleep)
    text = "\n".join(p.read_text() for p in log_dir().glob("*-retry*.log"))
    assert "kind=seat-fatal" in text

def test_timeout_exhausted_has_its_own_log_kind(tmp_log):
    """A capped-out TIMEOUT is logged as kind=timeout-exhausted, NOT seat-fatal — so the
    dashboard/post-mortem can tell 'we stopped retrying a hung seat' from a real auth/501
    seat-fatal."""
    script = _Script([(124, "partial", "")])  # always times out
    run_seat_with_retry("seat-t", script, max_retries=3, rng=random.Random(1), sleeper=_no_sleep)
    text = "\n".join(p.read_text() for p in log_dir().glob("*-retry*.log"))
    assert "kind=timeout-exhausted" in text
    assert "kind=seat-fatal" not in text


# === (g) backoff + jitter present ===============================================
def test_backoff_grows_exponentially():
    """Without jitter (a zero-spread RNG) the delay grows by the backoff factor and caps."""
    class _Zero(random.Random):
        def uniform(self, a, b):  # no jitter -> deterministic schedule
            return 0.0
    rng = _Zero()
    d1 = compute_backoff(1, rng)
    d2 = compute_backoff(2, rng)
    d3 = compute_backoff(3, rng)
    assert d1 > 0
    assert d2 > d1 and d3 > d2  # strictly growing (exponential)
    # The cap holds for a large attempt index.
    assert compute_backoff(20, rng) <= 8.0 + 1e-9

def test_jitter_perturbs_backoff():
    """Jitter is real: two different RNG seeds give different delays for the SAME attempt."""
    a = compute_backoff(3, random.Random(1))
    b = compute_backoff(3, random.Random(99999))
    assert a != b, (a, b)

def test_jitter_stays_within_bounds():
    """Jittered delay never exceeds the cap and is never negative, across many seeds."""
    for seed in range(200):
        d = compute_backoff(3, random.Random(seed))
        assert 0.0 <= d <= 8.0 + 1e-9, (seed, d)


# === (h) panel integration: retry before reserve ===============================
class _FakeBackends:
    """Stub panel.resolve_backend with per-model behaviour scripts (a LIST of (rc, out) the
    model cycles through on successive calls; the last entry repeats). Records dispatches."""

    def __init__(self, behaviour=None):
        self.behaviour = behaviour or {}
        self.counts: dict[str, int] = {}
        self.dispatched: list[str] = []

    def __enter__(self):
        self._old = panel.resolve_backend

        def _resolve(_model: str):
            def _backend(model, prompt, diff, cwd, timeout, round_no=0):
                self.dispatched.append(model)
                seq = self.behaviour.get(model)
                if seq is None:
                    return ReviewResult(model=model, command="fake", returncode=0, stdout=f"ok {model}", stderr="")
                i = self.counts.get(model, 0)
                rc, out, err = seq[min(i, len(seq) - 1)]
                self.counts[model] = i + 1
                return ReviewResult(model=model, command="fake", returncode=rc, stdout=out, stderr=err)
            return _backend

        panel.resolve_backend = _resolve
        return self

    def __exit__(self, *exc):
        panel.resolve_backend = self._old
        return False


def _avail(models):
    return lambda r: r.model in models


def test_panel_transient_seat_retried_no_reserve_used(tmp_log):
    """A pool seat that fails transiently then recovers is RETRIED in-seat; the pool stays
    full and the reserve is NOT consumed."""
    os.environ["REVIEW_RETRY_COUNT"] = "3"
    try:
        board = list(DEFAULT_BOARD)
        pool, reserve = split_pool_reserve(board, 4, _avail({r.model for r in board}))
        # pool[1] throttles once, then succeeds — retried in-seat, no reserve needed.
        behaviour = {pool[1].model: [(1, "", "429 rate limit"), (0, "recovered verdict", "")]}
        with _FakeBackends(behaviour) as fb:
            outcome = run_board_with_failover(pool, reserve, PROMPT, "+x", REPO_ROOT, 5)
        assert len(outcome.usable) == 4
        assert not outcome.degraded
        # No reserve seat was dispatched (the retry kept the pool full).
        assert reserve[0].model not in fb.dispatched, fb.dispatched
        # The retried seat is in the usable pool under its own id.
        assert pool[1].model in outcome.usable_models
    finally:
        os.environ.pop("REVIEW_RETRY_COUNT", None)


def test_panel_parallel_retries_tally_one_per_logical_seat(tmp_log):
    """The run-stats tally records EXACTLY ONE outcome per logical seat even when SEVERAL pool
    seats retry transiently in parallel — a retried-then-recovered seat is one `ok`, never a
    `fail`+`ok` and never double-counted. This is the test that pins the tally against the
    cross-thread suppression race (a per-thread global toggle would mis-suppress here)."""
    os.environ["REVIEW_RETRY_COUNT"] = "3"
    try:
        board = list(DEFAULT_BOARD)
        pool, reserve = split_pool_reserve(board, 4, _avail({r.model for r in board}))
        # THREE of the four seats throttle once then recover — all retried in parallel.
        behaviour = {
            pool[0].model: [(1, "", "429 rate limit"), (0, "ok0", "")],
            pool[1].model: [(1, "", "503 overloaded"), (0, "ok1", "")],
            pool[2].model: [(1, "", "529 overloaded"), (0, "ok2", "")],
        }
        panel.begin_call_tally()
        with _FakeBackends(behaviour) as fb:
            outcome = run_board_with_failover(pool, reserve, PROMPT, "+x", REPO_ROOT, 5)
        tally = panel.end_call_tally()
        assert len(outcome.usable) == 4
        assert not outcome.degraded
        # Exactly 4 logical seats, all ok; the retried attempts are NOT counted as fails.
        assert tally == {"ok": 4, "fail": 0}, tally
        # No reserve was needed (every seat recovered on its own retry).
        assert reserve[0].model not in fb.dispatched
    finally:
        os.environ.pop("REVIEW_RETRY_COUNT", None)


def test_panel_tally_one_per_seat_across_reserve_replace(tmp_log):
    """The run-stats tally stays ONE-per-logical-seat across a MULTI-ROUND reserve-replace (the
    path where run_panel_with_retry runs again in the backfill round under suppression). A
    seat-fatal pool seat = 1 fail (the dead seat) + 1 ok (its reserve), never double-counted —
    this pins the final-tally loop against a suppression-aware regression on the backfill
    round, not just the all-recover-in-one-round case."""
    os.environ["REVIEW_RETRY_COUNT"] = "2"
    try:
        board = list(DEFAULT_BOARD)
        pool, reserve = split_pool_reserve(board, 4, _avail({r.model for r in board}))
        # pool[2] is seat-fatal (no retry) -> reserve[0] backfills it in round 2.
        behaviour = {pool[2].model: [(1, "", "401 unauthorized")]}
        panel.begin_call_tally()
        with _FakeBackends(behaviour):
            outcome = run_board_with_failover(pool, reserve, PROMPT, "+x", REPO_ROOT, 5)
        tally = panel.end_call_tally()
        assert len(outcome.usable) == 4 and not outcome.degraded
        # 4 logical seats: the dead pool seat is 1 fail, its reserve replacement is 1 ok, the
        # other 3 pool seats are ok -> 4 ok + 1 fail. NOT inflated by the backfill round.
        assert tally == {"ok": 4, "fail": 1}, tally
    finally:
        os.environ.pop("REVIEW_RETRY_COUNT", None)


def test_panel_seat_fatal_falls_to_reserve(tmp_log):
    """A SEAT-FATAL pool seat is NOT retried — it falls straight to the reserve, which keeps
    the pool full."""
    os.environ["REVIEW_RETRY_COUNT"] = "3"
    try:
        board = list(DEFAULT_BOARD)
        pool, reserve = split_pool_reserve(board, 4, _avail({r.model for r in board}))
        behaviour = {pool[3].model: [(1, "", "401 unauthorized")]}  # fatal, every call
        with _FakeBackends(behaviour) as fb:
            outcome = run_board_with_failover(pool, reserve, PROMPT, "+x", REPO_ROOT, 5)
        assert len(outcome.usable) == 4
        assert not outcome.degraded
        # The fatal seat was tried ONCE (no in-seat retry) and the reserve backfilled it.
        assert fb.dispatched.count(pool[3].model) == 1, fb.dispatched
        assert reserve[0].model in fb.dispatched
        assert reserve[0].model in outcome.usable_models
        assert pool[3].model not in outcome.usable_models
    finally:
        os.environ.pop("REVIEW_RETRY_COUNT", None)


def test_panel_retry_then_reserve_when_unrecovered(tmp_log):
    """A pool seat that stays transiently down through its whole retry budget is reserve-
    replaced AFTER the retries are exhausted (retry FIRST, reserve as the fallback) — and the
    promotion is recorded durably."""
    os.environ["REVIEW_RETRY_COUNT"] = "2"
    try:
        board = list(DEFAULT_BOARD)
        pool, reserve = split_pool_reserve(board, 4, _avail({r.model for r in board}))
        behaviour = {pool[0].model: [(1, "", "503 service unavailable")]}  # always down
        with _FakeBackends(behaviour) as fb:
            outcome = run_board_with_failover(pool, reserve, PROMPT, "+x", REPO_ROOT, 5)
        assert len(outcome.usable) == 4
        assert not outcome.degraded
        # The down seat was tried initial + 2 retries = 3 times before the reserve took over.
        assert fb.dispatched.count(pool[0].model) == 3, fb.dispatched
        assert reserve[0].model in outcome.usable_models
        # The reserve promotion is in the durable log.
        text = "\n".join(p.read_text() for p in log_dir().glob("*-retry*.log"))
        assert "kind=promote" in text
    finally:
        os.environ.pop("REVIEW_RETRY_COUNT", None)


# === nested-suppression tally invariant ========================================
def test_tally_result_is_suppression_aware(tmp_log):
    """run_panel_with_retry's final `for result in results: _tally_result(...)` loop is a no-op
    under the failover loop's suppression — that holds ONLY if `_tally_result` itself respects
    `_suppress_autotally`. Pin that invariant directly: a suppressed _tally_result must NOT
    count; the failover loop's own _tally_ok is the single source of the count."""
    panel.begin_call_tally()
    with panel._TALLY_LOCK:
        prev = panel._suppress_autotally
        panel._suppress_autotally = True
    try:
        panel._tally_result(0)  # would-be ok
        panel._tally_result(1)  # would-be fail
    finally:
        with panel._TALLY_LOCK:
            panel._suppress_autotally = prev
    # Both suppressed -> zero counted. Then an UN-suppressed call DOES count.
    panel._tally_result(0)
    tally = panel.end_call_tally()
    assert tally == {"ok": 1, "fail": 0}, tally


# === flat `-m` path also retries (not just the board) ==========================
def test_flat_m_path_retries_transient_seat(tmp_log):
    """An explicit `-m` review (the flat path, no board) ALSO retries a seat on a transient
    failure — `--retry`/$REVIEW_RETRY_COUNT is not board-only. Stubs `review.resolve_backend`
    (the flat path's dispatch point) so a seat throttles once then recovers, and asserts the
    seat was dispatched twice and the run passed (exit 0)."""
    import reviewlib.modes.review as review_mode

    os.environ["REVIEW_RETRY_COUNT"] = "2"
    saved = review_mode.resolve_backend
    counts = {"m1": 0}
    try:
        def _resolve(_model):
            def _b(model, prompt, diff, cwd, timeout, round_no=0):
                i = counts["m1"]
                counts["m1"] += 1
                if model == "m1" and i == 0:
                    return ReviewResult(model, "fake", 1, "", "429 rate limit")
                return ReviewResult(model, "fake", 0, f"verdict {model}", "")
            return _b
        review_mode.resolve_backend = _resolve
        rc = review_mode.mode_review(["m1"], "prompt", "+x", REPO_ROOT, 5, staged=False)
        assert rc == 0, rc                 # recovered on retry -> the run passes
        assert counts["m1"] == 2           # initial transient fail + one retry
    finally:
        review_mode.resolve_backend = saved
        os.environ.pop("REVIEW_RETRY_COUNT", None)


# === harness ====================================================================
def _make_tmp_log():
    """A per-test fresh log dir, exported via $REVIEW_LOG_DIR so write_retry_log / log_dir
    point at it. Returns a restore callable."""
    import tempfile

    saved = os.environ.get("REVIEW_LOG_DIR")
    tmp = tempfile.mkdtemp(prefix="review-retry-log-")
    os.environ["REVIEW_LOG_DIR"] = tmp

    def _restore():
        if saved is None:
            os.environ.pop("REVIEW_LOG_DIR", None)
        else:
            os.environ["REVIEW_LOG_DIR"] = saved

    return _restore


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        # Tests that DECLARE a `tmp_log` PARAMETER get a fresh log dir; the rest run bare.
        # co_varnames[:co_argcount] is the declared args only (not every local), so a test
        # with an unrelated local named tmp_log is never mis-detected.
        code = fn.__code__
        wants_log = "tmp_log" in code.co_varnames[: code.co_argcount]
        restore = _make_tmp_log() if wants_log else (lambda: None)
        try:
            fn(None) if wants_log else fn()
            print(f"PASS {name}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
        finally:
            restore()
    sys.exit(1 if failures else 0)
