#!/usr/bin/env python3
"""`reviewlib.seat_cooldown` — cross-invocation cooldown cache for a chronically
unavailable claude seat (Fable), and its wiring into `reviewlib.backends.review_claude`.

Bug this fixes (2026-08 token-burn investigation): each `review` invocation is a fresh
process, so a Fable seat that is out of session quota still paid for one full `claude-p`
dispatch attempt on EVERY invocation before falling to the reserve — real evidence: 4,322
of 6,383 recorded runs dispatched Fable and it failed, most with an explicit session-limit
notice. This module's cache lets a later invocation, within a bounded window, skip the
real dispatch and return the SAME rc=0 "is currently unavailable" sentinel shape the rest
of the codebase (failover, in-seat retry, the dashboard) already knows how to handle.

Both layers are tested: the store itself (record/read/expire/clear, corrupt-file
tolerance) and `review_claude`'s use of it (skips the real dispatch while cooling down,
records a cooldown after a genuine chronic failure, does NOT cache a transient/auth
failure).
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import backends  # noqa: E402
from reviewlib import seat_cooldown as sc  # noqa: E402
from reviewlib.backends import ReviewResult  # noqa: E402

MODEL = "claude:claude-fable-5"


def _with_store(fn):
    """Point $REVIEW_SEAT_COOLDOWN_FILE AND $REVIEW_LOG_DIR at fresh temp locations for
    the duration of `fn` — the wiring tests below drive the real `review_claude`, whose
    cooldown-skip path calls `_emit_rest_log` (a real sidecar-log write via
    `reviewlib.process.log_dir()`); without redirecting it too, a test run would write
    into the developer's real ~/Library/Logs/review-cli (verified: it did, once).

    Also forces $REVIEW_CLAUDE_MODE=cli: the wiring tests below patch
    `backends.review_claude_cli`, but `review_claude()` routes to the UNPATCHED
    `review_claude_api` instead whenever `REVIEW_CLAUDE_MODE=api` is exported OR the
    host has an Anthropic key configured with no claude CLI on PATH (`_anthropic_api_
    config`) — on such a host these tests would silently POST to the live Anthropic API
    (kimi review finding). Forcing `cli` here makes the dispatch path deterministic
    regardless of the host's ambient env.

    Also CLEARS $REVIEW_SEAT_COOLDOWN_SECONDS: several tests here assume the default
    TTL and record explicit ttl_seconds values, but `active_cooldown` also re-checks
    this var on every read — a developer who exported `REVIEW_SEAT_COOLDOWN_SECONDS=0`
    (the module's own documented un-stick escape hatch) would otherwise see these tests
    fail (codex review finding)."""
    with tempfile.TemporaryDirectory() as d:
        saved_store = os.environ.get("REVIEW_SEAT_COOLDOWN_FILE")
        saved_logs = os.environ.get("REVIEW_LOG_DIR")
        saved_mode = os.environ.get("REVIEW_CLAUDE_MODE")
        saved_ttl = os.environ.get("REVIEW_SEAT_COOLDOWN_SECONDS")
        os.environ["REVIEW_SEAT_COOLDOWN_FILE"] = str(Path(d) / "seat-cooldown.json")
        os.environ["REVIEW_LOG_DIR"] = str(Path(d) / "logs")
        os.environ["REVIEW_CLAUDE_MODE"] = "cli"
        os.environ.pop("REVIEW_SEAT_COOLDOWN_SECONDS", None)
        try:
            return fn()
        finally:
            for key, saved in (
                ("REVIEW_SEAT_COOLDOWN_FILE", saved_store),
                ("REVIEW_LOG_DIR", saved_logs),
                ("REVIEW_CLAUDE_MODE", saved_mode),
                ("REVIEW_SEAT_COOLDOWN_SECONDS", saved_ttl),
            ):
                if saved is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = saved


# ---- the store itself -------------------------------------------------------------------
def test_no_cooldown_recorded_returns_none():
    def _run():
        assert sc.active_cooldown(MODEL) is None

    _with_store(_run)


def test_active_cooldown_never_raises_when_cooldown_path_raises_runtimeerror():
    """Opus review finding, round 3: `cooldown_path()` calls `Path.home()`, which raises
    `RuntimeError` (NOT `OSError`) when no home directory can be resolved. `active_
    cooldown`'s `try: data = _load(cooldown_path()) except OSError: return None` catches
    `cooldown_path()`'s own exception too (it's evaluated inside the try), but a
    `RuntimeError` used to fall straight through the too-narrow `except OSError` —
    taking down `review_claude`/`review_with_images`'s hot-path dispatch, exactly the
    "never raises, fail-open" violation `record_cooldown`/`clear_cooldown` were already
    fixed for. No `_with_store` here on purpose — this pins the raw `cooldown_path()`
    failure directly, not the env-overridden path."""
    saved = sc.cooldown_path

    def _boom():
        raise RuntimeError("simulated: could not determine home directory")

    sc.cooldown_path = _boom
    try:
        assert sc.active_cooldown(MODEL) is None  # must not raise
    finally:
        sc.cooldown_path = saved


def test_record_then_active_within_window():
    def _run():
        sc.record_cooldown(MODEL, "session limit", now=1000.0, ttl_seconds=600.0)
        result = sc.active_cooldown(MODEL, now=1100.0)  # 100s later, still within 600s
        assert result is not None
        assert result["reason"] == "session limit"
        assert abs(result["remaining_seconds"] - 500.0) < 0.01

    _with_store(_run)


def test_cooldown_expires_after_ttl():
    def _run():
        sc.record_cooldown(MODEL, "session limit", now=1000.0, ttl_seconds=600.0)
        assert (
            sc.active_cooldown(MODEL, now=1700.0) is None
        )  # 700s later, past the window

    _with_store(_run)


def test_cooldown_is_per_model():
    def _run():
        sc.record_cooldown(MODEL, "session limit", now=1000.0, ttl_seconds=600.0)
        assert sc.active_cooldown("claude:claude-opus-4-8", now=1100.0) is None

    _with_store(_run)


def test_clear_cooldown_removes_it():
    def _run():
        sc.record_cooldown(MODEL, "session limit", now=1000.0, ttl_seconds=600.0)
        sc.clear_cooldown(MODEL)
        assert sc.active_cooldown(MODEL, now=1100.0) is None

    _with_store(_run)


def test_ttl_le_zero_disables_recording():
    def _run():
        sc.record_cooldown(MODEL, "session limit", now=1000.0, ttl_seconds=0.0)
        assert sc.active_cooldown(MODEL, now=1000.5) is None

    _with_store(_run)


def test_nan_ttl_env_falls_back_to_default_not_crash():
    """codex review finding: nan/inf both pass `<= 0` checks in Python
    (`float("nan") <= 0` and `float("inf") <= 0` are both False), so a malformed
    $REVIEW_SEAT_COOLDOWN_SECONDS could otherwise persist a non-finite `until` and
    later crash `int(remaining_seconds)` downstream. Pins that both fall back to the
    documented default instead."""

    def _run():
        for bad in ("nan", "inf", "-inf"):
            saved = os.environ.get("REVIEW_SEAT_COOLDOWN_SECONDS")
            os.environ["REVIEW_SEAT_COOLDOWN_SECONDS"] = bad
            try:
                assert sc._ttl_seconds() == sc.DEFAULT_COOLDOWN_SECONDS, bad
            finally:
                if saved is None:
                    os.environ.pop("REVIEW_SEAT_COOLDOWN_SECONDS", None)
                else:
                    os.environ["REVIEW_SEAT_COOLDOWN_SECONDS"] = saved

    _with_store(_run)


def test_non_finite_until_in_store_reads_as_no_cooldown_not_crash():
    """codex review finding, with a REPRODUCED live crash: `_ttl_seconds()` rejects a
    non-finite *env* value before it is ever written, but Python's `json` module
    accepts the non-standard NaN/Infinity/-Infinity literals on READ too — so a
    hand-edited or otherwise corrupted store file can still carry a non-finite `until`.
    Before this fix, `isinstance(float("nan"), float)` is True and `nan <= now` is
    False, so a NaN `until` passed straight through and handed the caller
    `remaining_seconds=nan` — which then crashed `int(remaining_seconds)` in
    `backends._cooldown_skip_result` (confirmed live: `ValueError: cannot convert
    float NaN to integer`). Pins that active_cooldown now treats ANY non-finite
    `until` as "no cooldown", never a crash."""
    import json

    def _run():
        for bad in (float("nan"), float("inf"), float("-inf")):
            path = sc.cooldown_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({MODEL: {"until": bad, "reason": "corrupt"}}),
                encoding="utf-8",
            )
            assert sc.active_cooldown(MODEL) is None, bad

    _with_store(_run)


def test_corrupt_store_reads_as_no_cooldown():
    def _run():
        Path(sc.cooldown_path()).parent.mkdir(parents=True, exist_ok=True)
        Path(sc.cooldown_path()).write_text("{not valid json", encoding="utf-8")
        assert sc.active_cooldown(MODEL) is None  # never raises

    _with_store(_run)


def test_non_utf8_store_reads_as_no_cooldown_not_a_crash():
    """codex review finding: `Path.read_text(encoding="utf-8")` raises
    `UnicodeDecodeError` on a non-UTF-8 file — a `ValueError` subclass, NOT an
    `OSError` — so the original `except OSError` in `_load` let it propagate through
    every claude dispatch instead of failing open like every other corruption class."""

    def _run():
        path = sc.cooldown_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff\xfe\x00\x01garbage-not-utf8")
        assert sc.active_cooldown(MODEL) is None  # never raises

    _with_store(_run)


def test_reason_is_truncated_and_persisted():
    def _run():
        long_reason = "x" * 5000
        sc.record_cooldown(MODEL, long_reason, now=1000.0, ttl_seconds=600.0)
        result = sc.active_cooldown(MODEL, now=1001.0)
        assert len(result["reason"]) <= sc._REASON_MAX_LEN

    _with_store(_run)


def test_record_and_clear_cooldown_never_raise_on_a_non_oserror_write_failure():
    """Opus review finding: `_write` re-raises ANY `BaseException` after its temp-file
    cleanup (`except BaseException: tmp.unlink(...); raise`) — not just `OSError` — but
    `record_cooldown`/`clear_cooldown` used to catch only `OSError`. Both run on the hot
    path immediately after a genuine `review_claude` dispatch, so a non-`OSError` write
    failure (simulated here as a `TypeError`) would have propagated and taken down the
    review, contradicting the "never raises" contract both docstrings promise. Pins that
    a `TypeError` from `_write` is swallowed by both functions, same as an `OSError`
    already was."""

    def _run():
        saved = sc._write

        def _boom(path, data):
            raise TypeError("simulated non-OSError write failure")

        sc._write = _boom
        try:
            sc.record_cooldown(MODEL, "session limit")  # must not raise
            sc.clear_cooldown(MODEL)  # must not raise either
        finally:
            sc._write = saved
        assert sc.active_cooldown(MODEL) is None  # the write never actually landed

    _with_store(_run)


def test_concurrent_writes_from_multiple_threads_never_corrupt_the_store():
    """Opus/codex review finding: the previous temp filename
    (``f"{path.name}.tmp{os.getpid()}"``) was unique per-PROCESS, not per-CALL — a board
    dispatches its seats in parallel THREADS within one process, so two claude-provider
    seats recording a cooldown in the same round raced on the IDENTICAL tmp path
    (interleaved writes -> possibly corrupt JSON; the second ``os.replace`` on an
    already-moved tmp raised ``FileNotFoundError``, silently dropping that write via the
    caller's ``except OSError``). `tempfile.mkstemp` gives every call its OWN unique,
    exclusively-created temp file, so `os.replace`'s atomicity now genuinely holds under
    concurrency — pins that N concurrent `record_cooldown` calls, from real threads
    sharing one store file, always leave the store as VALID, PARSEABLE JSON (never
    truncated/interleaved garbage) and at least one cooldown survives (the mechanism
    works, it isn't silently deadlocked/broken).

    NOT pinned here (a separate, pre-existing, explicitly-accepted limitation — Fable
    review finding: "read-modify-write is also lock-free... acceptable for best-effort"):
    the OUTER read-modify-write (`_load` then `_write`) is not itself locked, so under
    concurrent calls for DIFFERENT models, a later write can still overwrite an earlier
    one's entry — a "last-writer-wins" lost update, not corruption. That is within this
    module's documented contract (best-effort; a lost cooldown just costs one more real
    dispatch next run, never a broken review) and is not what this test guards."""
    import json
    import threading

    def _run():
        models = [f"claude:m{i}" for i in range(12)]
        threads = [
            threading.Thread(target=sc.record_cooldown, args=(m, "session limit"))
            for m in models
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        # No crash, no leaked .tmp* files, and the store is valid JSON — the collision
        # class this fix addresses, not the separate lost-update race documented above.
        leftover_tmp = [p for p in sc.cooldown_path().parent.glob("*.tmp*")]
        assert leftover_tmp == [], leftover_tmp
        raw = sc.cooldown_path().read_text(encoding="utf-8")
        data = json.loads(raw)  # never raises ValueError — proves no interleaved write
        assert any(sc.active_cooldown(m) is not None for m in models), data

    _with_store(_run)


# ---- wiring into review_claude ------------------------------------------------------------
def _patched(obj, name, value):
    saved = getattr(obj, name)
    setattr(obj, name, value)
    return saved


def test_cooldown_skip_result_contract_with_panel_and_retry():
    """Fable review finding: the entire design of `_cooldown_skip_result` rests on the
    unverified claim that its synthetic rc=0 body is recognized by
    `panel.result_is_usable` and `retry.classify_failure` "via the SAME code path... zero
    new integration surface" — no test asserted this directly (only that the body
    *contains* a substring). If `panel._UNAVAILABLE_MARKERS` ever required a MORE
    specific phrase than the generic `"is currently unavailable"` it actually is today
    (e.g. the seat's own display name, which the synthetic body does NOT contain — it
    uses the raw `model` id instead), a cooldown skip would be misclassified as a
    SUCCESSFUL rc=0 result: a flat `review diff -m fable` during a cooldown window would
    return the cache notice as the "review" and could pass a commit gate. Pins the
    actual cross-module contract explicitly, so a future change to either side's
    matching logic fails this test instead of silently breaking the cooldown feature."""
    from reviewlib.panel import result_is_usable
    from reviewlib.retry import FailureClass, classify_failure

    def _run():
        sc.record_cooldown(MODEL, "session limit", now=None, ttl_seconds=600.0)
        cooldown = sc.active_cooldown(MODEL)
        result = backends._cooldown_skip_result(MODEL, 0, cooldown)
        assert result_is_usable(result) is False, result.stdout
        assert classify_failure(result) == FailureClass.SEAT_FATAL

    _with_store(_run)


def test_cooldown_skip_body_stays_usable_with_a_long_model_and_reason():
    """glm review finding: `_cooldown_skip_result`'s synthesized stdout used to have NO
    length bound — with a long `model` id (unvalidated, from `-m`/a board config entry)
    and/or a `reason` near `seat_cooldown._REASON_MAX_LEN=200`, the body could exceed
    `panel._UNAVAILABLE_MAX_LEN` (400). Past that bound `result_is_usable` stops scanning
    for the sentinel markers entirely and returns `True` — silently turning a cached skip
    (that never ran a real review) into a "successful" rc=0 verdict able to satisfy the
    flat path's `ok` and the `--commit`/`--staged` commit gate. Pins that the contract
    this whole design rests on (`_cooldown_skip_result`'s body is ALWAYS recognised as
    the sentinel) holds even at both length extremes, not just the common short case
    `test_cooldown_skip_result_contract_with_panel_and_retry` above already covers."""
    from reviewlib.panel import result_is_usable
    from reviewlib.retry import FailureClass, classify_failure

    long_model = "claude:" + ("x" * 500)  # pathological, but not impossible via config
    long_reason = (
        "y" * sc._REASON_MAX_LEN
    )  # the store's own max persisted reason length

    def _run():
        sc.record_cooldown(long_model, long_reason, now=None, ttl_seconds=600.0)
        cooldown = sc.active_cooldown(long_model)
        result = backends._cooldown_skip_result(long_model, 0, cooldown)
        assert len(result.stdout) <= backends._UNAVAILABLE_MAX_LEN, len(result.stdout)
        # The marker phrase itself must survive truncation — it's the one substring
        # every downstream consumer actually keys on.
        assert backends._UNAVAILABLE_MARKERS[0] in result.stdout
        assert result_is_usable(result) is False, result.stdout
        assert classify_failure(result) == FailureClass.SEAT_FATAL

    _with_store(_run)


def test_cooldown_skip_body_stays_usable_with_a_pathological_remaining_seconds():
    """Opus review finding, round 4: `remaining` used to be embedded as-is, treated as
    FIXED overhead the `model`/`reason` truncation budgets are computed against — but it
    is not bounded anywhere. `_ttl_seconds()` rejects a non-finite TTL (NaN/inf), but a
    pathologically large yet still-FINITE one (approaching float64's ~1.8e308 ceiling)
    survives that guard and renders as a many-hundred-digit `remaining`, which ALONE can
    exceed `_UNAVAILABLE_MAX_LEN` — at which point even truncating `model` to nothing
    (the function's own last resort) cannot bring the body back under the bound. Pins
    that `_bounded_cooldown_skip_body`'s "guaranteed <= max_len" contract holds even for
    an absurd `remaining` value, not just an absurd `model`/`reason` (the case the
    sibling test above already covers)."""
    from reviewlib.panel import result_is_usable

    pathological_remaining = int(
        1e300
    )  # ~301 digits — alone exceeds _UNAVAILABLE_MAX_LEN
    body = backends._bounded_cooldown_skip_body(
        "claude:claude-fable-5", "session limit", pathological_remaining
    )
    assert len(body) <= backends._UNAVAILABLE_MAX_LEN, len(body)
    assert backends._UNAVAILABLE_MARKERS[0] in body
    fake_result = ReviewResult(
        model="claude:claude-fable-5",
        command="seat-cooldown skip (claude)",
        returncode=0,
        stdout=body,
        stderr="",
    )
    assert result_is_usable(fake_result) is False, body


def test_review_claude_skips_real_dispatch_while_cooling_down():
    def _run():
        sc.record_cooldown(MODEL, "session limit", now=None, ttl_seconds=600.0)
        saved_cli = _patched(
            backends,
            "review_claude_cli",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("real CLI dispatch was NOT skipped")
            ),
        )
        saved_unpaid = _patched(
            backends, "unpaid_provider_result", lambda *a, **k: None
        )
        try:
            result = backends.review_claude(MODEL, "prompt", "diff", Path("."), 60)
        finally:
            backends.review_claude_cli = saved_cli
            backends.unpaid_provider_result = saved_unpaid
        assert result.returncode == 0
        assert "is currently unavailable" in result.stdout
        assert "cached:" in result.stdout

    _with_store(_run)


def test_review_claude_records_cooldown_after_sentinel_response():
    def _run():
        assert sc.active_cooldown(MODEL) is None
        saved_cli = _patched(
            backends,
            "review_claude_cli",
            lambda *a, **k: ReviewResult(
                model=MODEL,
                command="claude-p",
                returncode=0,
                stdout="Claude Fable 5 is currently unavailable. Learn more: https://x",
                stderr="",
            ),
        )
        saved_unpaid = _patched(
            backends, "unpaid_provider_result", lambda *a, **k: None
        )
        try:
            backends.review_claude(MODEL, "prompt", "diff", Path("."), 60)
        finally:
            backends.review_claude_cli = saved_cli
            backends.unpaid_provider_result = saved_unpaid
        assert sc.active_cooldown(MODEL) is not None

    _with_store(_run)


def test_review_claude_records_cooldown_for_every_unavailable_marker_wording():
    """glm/Opus review finding (round 2): `_chronic_unavailable_reason` used to
    recognise only ONE of `backends._UNAVAILABLE_MARKERS`'s four wordings ("is currently
    unavailable"), even though every OTHER consumer of that tuple (`panel.
    result_is_usable`, `retry._is_rc0_sentinel`, the dashboard's HEALTH_PAYWALL) already
    treated all four as equally chronic. A Fable response shaped like one of the other
    three ("is temporarily unavailable", "model is unavailable", "currently not
    available") was therefore failover-replaced and classified paywall everywhere except
    HERE, so `record_cooldown` silently never fired for it and the seat kept paying for a
    real dispatch on every invocation — the exact burn this feature exists to stop. Pins
    that a cooldown is now recorded for ALL FOUR canonical wordings, not just the first."""
    for marker in backends._UNAVAILABLE_MARKERS:

        def _run(marker=marker):
            assert sc.active_cooldown(MODEL) is None
            saved_cli = _patched(
                backends,
                "review_claude_cli",
                lambda *a, **k: ReviewResult(
                    model=MODEL,
                    command="claude-p",
                    returncode=0,
                    stdout=f"Claude Fable 5 {marker}. Learn more: https://x",
                    stderr="",
                ),
            )
            saved_unpaid = _patched(
                backends, "unpaid_provider_result", lambda *a, **k: None
            )
            try:
                backends.review_claude(MODEL, "prompt", "diff", Path("."), 60)
            finally:
                backends.review_claude_cli = saved_cli
                backends.unpaid_provider_result = saved_unpaid
            assert sc.active_cooldown(MODEL) is not None, marker
            sc.clear_cooldown(MODEL)

        _with_store(_run)


def test_review_claude_records_cooldown_after_session_limit_response():
    def _run():
        saved_cli = _patched(
            backends,
            "review_claude_cli",
            lambda *a, **k: ReviewResult(
                model=MODEL,
                command="claude-p",
                returncode=1,
                stdout="",
                stderr="You've hit your session limit · resets 7:30pm (Europe/Belgrade)",
            ),
        )
        saved_unpaid = _patched(
            backends, "unpaid_provider_result", lambda *a, **k: None
        )
        try:
            backends.review_claude(MODEL, "prompt", "diff", Path("."), 60)
        finally:
            backends.review_claude_cli = saved_cli
            backends.unpaid_provider_result = saved_unpaid
        assert sc.active_cooldown(MODEL) is not None

    _with_store(_run)


def test_review_claude_does_not_cache_a_plain_auth_failure():
    """A bare auth/bad-key failure is a POSSIBLY-transient misconfiguration (a key that
    gets rotated moments later) — seat_cooldown must stay narrow to the two CHRONIC
    signals and never cache this class (see the module's docstring)."""

    def _run():
        saved_cli = _patched(
            backends,
            "review_claude_cli",
            lambda *a, **k: ReviewResult(
                model=MODEL,
                command="claude-p",
                returncode=1,
                stdout="",
                stderr="Error: authentication failed",
            ),
        )
        saved_unpaid = _patched(
            backends, "unpaid_provider_result", lambda *a, **k: None
        )
        try:
            backends.review_claude(MODEL, "prompt", "diff", Path("."), 60)
        finally:
            backends.review_claude_cli = saved_cli
            backends.unpaid_provider_result = saved_unpaid
        assert sc.active_cooldown(MODEL) is None

    _with_store(_run)


def test_review_claude_does_not_cache_a_long_real_review():
    """A genuine long review must never be mistaken for the short administrative
    sentinel, even if it happens to contain the word 'unavailable' somewhere in prose."""

    def _run():
        long_body = (
            "This function is unavailable on Windows due to a platform check.\n"
            + ("ok " * 200)
        )
        saved_cli = _patched(
            backends,
            "review_claude_cli",
            lambda *a, **k: ReviewResult(
                model=MODEL,
                command="claude-p",
                returncode=0,
                stdout=long_body,
                stderr="",
            ),
        )
        saved_unpaid = _patched(
            backends, "unpaid_provider_result", lambda *a, **k: None
        )
        try:
            backends.review_claude(MODEL, "prompt", "diff", Path("."), 60)
        finally:
            backends.review_claude_cli = saved_cli
            backends.unpaid_provider_result = saved_unpaid
        assert sc.active_cooldown(MODEL) is None

    _with_store(_run)


def test_review_claude_does_not_cache_a_successful_review_mentioning_session_limit():
    """codex/kimi P1 finding: the ORIGINAL quota-marker check scanned ANY body for
    'session limit'/'usage-credits' regardless of exit code — so a genuine, USABLE rc=0
    review whose prose happens to mention 'session limit' (very plausible for a review
    of review-cli's OWN diff, which literally introduces that phrase) would falsely
    cache a 10-minute cooldown for a perfectly healthy seat. Pins the fix: the quota
    branch only fires on a NON-zero exit (mirrors retry.py's _error_channel)."""

    def _run():
        long_body = (
            "This diff adds a cooldown for the session limit / usage-credits case. "
            + ("Looks correct. " * 100)
        )
        saved_cli = _patched(
            backends,
            "review_claude_cli",
            lambda *a, **k: ReviewResult(
                model=MODEL,
                command="claude-p",
                returncode=0,
                stdout=long_body,
                stderr="",
            ),
        )
        saved_unpaid = _patched(
            backends, "unpaid_provider_result", lambda *a, **k: None
        )
        try:
            result = backends.review_claude(MODEL, "prompt", "diff", Path("."), 60)
        finally:
            backends.review_claude_cli = saved_cli
            backends.unpaid_provider_result = saved_unpaid
        assert result.returncode == 0
        assert result.stdout == long_body  # the real review is returned unmodified
        assert sc.active_cooldown(MODEL) is None

    _with_store(_run)


def test_review_claude_does_not_cache_a_short_rc0_body_mentioning_session_limit():
    """A short rc=0 body that mentions 'session limit' but is NOT the administrative
    sentinel phrase is still a real (if terse) answer, not a chronic-failure signal."""

    def _run():
        saved_cli = _patched(
            backends,
            "review_claude_cli",
            lambda *a, **k: ReviewResult(
                model=MODEL,
                command="claude-p",
                returncode=0,
                stdout="Fine — no session limit concerns here.",
                stderr="",
            ),
        )
        saved_unpaid = _patched(
            backends, "unpaid_provider_result", lambda *a, **k: None
        )
        try:
            backends.review_claude(MODEL, "prompt", "diff", Path("."), 60)
        finally:
            backends.review_claude_cli = saved_cli
            backends.unpaid_provider_result = saved_unpaid
        assert sc.active_cooldown(MODEL) is None

    _with_store(_run)


# ---- wiring into review_with_images (the --visual path) -----------------------------------
def test_review_with_images_skips_real_dispatch_while_cooling_down():
    """kimi P2 finding: --visual dispatches straight to review_claude_cli_with_images,
    bypassing review_claude() entirely — it must consult seat_cooldown itself."""

    def _run():
        sc.record_cooldown(MODEL, "session limit", now=None, ttl_seconds=600.0)
        saved_images = _patched(
            backends,
            "review_claude_cli_with_images",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("real CLI-with-images dispatch was NOT skipped")
            ),
        )
        saved_unpaid = _patched(
            backends, "unpaid_provider_result", lambda *a, **k: None
        )
        try:
            result = backends.review_with_images(
                MODEL, "prompt", "diff", Path("."), 60, images=(Path("fake.png"),)
            )
        finally:
            backends.review_claude_cli_with_images = saved_images
            backends.unpaid_provider_result = saved_unpaid
        assert result.returncode == 0
        assert "is currently unavailable" in result.stdout
        assert "cached:" in result.stdout

    _with_store(_run)


def test_review_with_images_records_cooldown_after_sentinel_response():
    def _run():
        assert sc.active_cooldown(MODEL) is None
        saved_images = _patched(
            backends,
            "review_claude_cli_with_images",
            lambda *a, **k: ReviewResult(
                model=MODEL,
                command="claude-p",
                returncode=0,
                stdout="Claude Fable 5 is currently unavailable. Learn more: https://x",
                stderr="",
            ),
        )
        saved_unpaid = _patched(
            backends, "unpaid_provider_result", lambda *a, **k: None
        )
        try:
            backends.review_with_images(
                MODEL, "prompt", "diff", Path("."), 60, images=(Path("fake.png"),)
            )
        finally:
            backends.review_claude_cli_with_images = saved_images
            backends.unpaid_provider_result = saved_unpaid
        assert sc.active_cooldown(MODEL) is not None

    _with_store(_run)


# ---- the SAME sentinel-shape gap in just-ask / quorum (codex review finding) --------------
# `mode_review`'s flat/board paths already reject a cooldown-skip's rc=0 "is currently
# unavailable" sentinel via `result_is_usable` — but `mode_just_ask` and `mode_quorum` had
# their OWN independent `returncode == 0` exit-code checks, so `review just-ask -m fable`
# (or quorum) during a cooldown window used to exit 0 with the cache-hit notice reported as
# a real answer. These drive the mode functions directly with a patched `run_panel`/
# `run_moderator` (no real backend, no seat_cooldown store needed) — the point is the
# MODE-LEVEL exit-code gate, not the cooldown cache itself.
def _sentinel_result(model: str) -> ReviewResult:
    return ReviewResult(
        model=model,
        command="seat-cooldown skip (claude)",
        returncode=0,
        stdout=f"{model} is currently unavailable (cached: session limit; skip expires "
        "in 300s — reviewlib.seat_cooldown).",
        stderr="",
    )


def test_just_ask_rc0_sentinel_result_is_not_reported_as_success():
    import reviewlib.modes.just_ask as just_ask_mod

    saved_run_panel = just_ask_mod.run_panel
    just_ask_mod.run_panel = lambda jobs, cwd, timeout: [
        _sentinel_result(j.model) for j in jobs
    ]
    try:
        rc = just_ask_mod.mode_just_ask("q?", ["fable"], "", Path("."), 60)
    finally:
        just_ask_mod.run_panel = saved_run_panel
    assert rc == 1, rc


def test_quorum_rc0_sentinel_expert_result_is_not_reported_as_success():
    import reviewlib.modes.quorum as quorum_mod

    saved_run_panel = quorum_mod.run_panel
    saved_run_moderator = quorum_mod.run_moderator
    quorum_mod.run_panel = lambda jobs, cwd, timeout: [
        _sentinel_result(j.model) for j in jobs
    ]
    quorum_mod.run_moderator = lambda moderators, prompt, cwd, timeout: ReviewResult(
        model=moderators[0],
        command="mod",
        returncode=0,
        stdout="a real summary",
        stderr="",
    )
    try:
        rc = quorum_mod.mode_quorum("q?", ["fable"], "", Path("."), 60, ["glm-5.2"])
    finally:
        quorum_mod.run_panel = saved_run_panel
        quorum_mod.run_moderator = saved_run_moderator
    assert rc == 1, rc


def test_quorum_rc0_sentinel_moderator_result_is_not_reported_as_success():
    """The moderator's OWN result can equally be a cached-skip sentinel (the moderator
    is itself a claude seat) — pins that `ok` checks the moderator with the same
    predicate, not just the experts."""
    import reviewlib.modes.quorum as quorum_mod

    saved_run_panel = quorum_mod.run_panel
    saved_run_moderator = quorum_mod.run_moderator
    quorum_mod.run_panel = lambda jobs, cwd, timeout: [
        ReviewResult(
            model=j.model,
            command="fake",
            returncode=0,
            stdout="a real expert answer",
            stderr="",
        )
        for j in jobs
    ]
    quorum_mod.run_moderator = lambda moderators, prompt, cwd, timeout: (
        _sentinel_result(moderators[0])
    )
    try:
        rc = quorum_mod.mode_quorum("q?", ["codex"], "", Path("."), 60, ["fable"])
    finally:
        quorum_mod.run_panel = saved_run_panel
        quorum_mod.run_moderator = saved_run_moderator
    assert rc == 1, rc


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failures else 0)
