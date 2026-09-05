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
import time
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
        assert sc.active_cooldown(MODEL, access_method="test") is None

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
        assert sc.active_cooldown(MODEL, access_method="test") is None  # must not raise
    finally:
        sc.cooldown_path = saved


def test_record_then_active_within_window():
    def _run():
        sc.record_cooldown(
            MODEL, "session limit", now=1000.0, ttl_seconds=600.0, access_method="test"
        )
        result = sc.active_cooldown(
            MODEL, now=1100.0, access_method="test"
        )  # 100s later, still within 600s
        assert result is not None
        assert result["reason"] == "session limit"
        assert abs(result["remaining_seconds"] - 500.0) < 0.01

    _with_store(_run)


def test_cooldown_expires_after_ttl():
    def _run():
        sc.record_cooldown(
            MODEL, "session limit", now=1000.0, ttl_seconds=600.0, access_method="test"
        )
        assert (
            sc.active_cooldown(MODEL, now=1700.0, access_method="test") is None
        )  # 700s later, past the window

    _with_store(_run)


def test_cooldown_is_per_model():
    def _run():
        sc.record_cooldown(
            MODEL, "session limit", now=1000.0, ttl_seconds=600.0, access_method="test"
        )
        assert (
            sc.active_cooldown(
                "claude:claude-opus-4-8", now=1100.0, access_method="test"
            )
            is None
        )

    _with_store(_run)


def test_clear_cooldown_removes_it():
    def _run():
        sc.record_cooldown(
            MODEL, "session limit", now=1000.0, ttl_seconds=600.0, access_method="test"
        )
        sc.clear_cooldown(MODEL, access_method="test")
        assert sc.active_cooldown(MODEL, now=1100.0, access_method="test") is None

    _with_store(_run)


def test_ttl_le_zero_disables_recording():
    def _run():
        sc.record_cooldown(
            MODEL, "session limit", now=1000.0, ttl_seconds=0.0, access_method="test"
        )
        assert sc.active_cooldown(MODEL, now=1000.5, access_method="test") is None

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
                json.dumps({MODEL: {"test": {"until": bad, "reason": "corrupt"}}}),
                encoding="utf-8",
            )
            assert sc.active_cooldown(MODEL, access_method="test") is None, bad

    _with_store(_run)


def test_corrupt_store_reads_as_no_cooldown():
    def _run():
        Path(sc.cooldown_path()).parent.mkdir(parents=True, exist_ok=True)
        Path(sc.cooldown_path()).write_text("{not valid json", encoding="utf-8")
        assert sc.active_cooldown(MODEL, access_method="test") is None  # never raises

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
        assert sc.active_cooldown(MODEL, access_method="test") is None  # never raises

    _with_store(_run)


def test_reason_is_truncated_and_persisted():
    def _run():
        long_reason = "x" * 5000
        sc.record_cooldown(
            MODEL, long_reason, now=1000.0, ttl_seconds=600.0, access_method="test"
        )
        result = sc.active_cooldown(MODEL, now=1001.0, access_method="test")
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
            sc.record_cooldown(
                MODEL, "session limit", access_method="test"
            )  # must not raise
            sc.clear_cooldown(MODEL, access_method="test")  # must not raise either
        finally:
            sc._write = saved
        assert (
            sc.active_cooldown(MODEL, access_method="test") is None
        )  # the write never actually landed

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
            threading.Thread(
                target=sc.record_cooldown,
                args=(m, "session limit"),
                kwargs={"access_method": "test"},
            )
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
        assert any(
            sc.active_cooldown(m, access_method="test") is not None for m in models
        ), data

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
        sc.record_cooldown(
            MODEL, "session limit", now=None, ttl_seconds=600.0, access_method="test"
        )
        cooldown = sc.active_cooldown(MODEL, access_method="test")
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
        sc.record_cooldown(
            long_model,
            long_reason,
            now=None,
            ttl_seconds=600.0,
            access_method="test",
        )
        cooldown = sc.active_cooldown(long_model, access_method="test")
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


def test_review_claude_cli_cooldown_does_not_shadow_the_api_transport():
    """review-cli#187, the headline claim: a cooldown recorded from the CLI transport
    must NOT skip a dispatch that resolves to the (independently healthy) API
    transport for the SAME model, and vice versa -- before this fix, the store was
    keyed by model alone, so a CLI-recorded cooldown silently starved the API route
    too even though switching transport is a legitimate immediate fix."""

    def _run():
        sc.record_cooldown(
            MODEL, "session limit", now=None, ttl_seconds=600.0, access_method="cli"
        )
        api_calls = []
        saved_api = _patched(
            backends,
            "review_claude_api",
            lambda *a, **k: (
                api_calls.append(1)
                or ReviewResult(
                    model=MODEL,
                    command="api",
                    returncode=0,
                    stdout="real answer",
                    stderr="",
                )
            ),
        )
        saved_unpaid = _patched(
            backends, "unpaid_provider_result", lambda *a, **k: None
        )
        saved_mode = os.environ.get("REVIEW_CLAUDE_MODE")
        os.environ["REVIEW_CLAUDE_MODE"] = "api"
        try:
            result = backends.review_claude(MODEL, "prompt", "diff", Path("."), 60)
        finally:
            backends.review_claude_api = saved_api
            backends.unpaid_provider_result = saved_unpaid
            if saved_mode is None:
                os.environ.pop("REVIEW_CLAUDE_MODE", None)
            else:
                os.environ["REVIEW_CLAUDE_MODE"] = saved_mode
        # The API transport was actually dispatched -- the CLI cooldown did not skip it.
        assert api_calls == [1], "API transport was wrongly skipped by a CLI cooldown"
        assert result.returncode == 0
        assert result.stdout == "real answer"

    _with_store(_run)


def test_review_claude_skips_real_dispatch_while_cooling_down():
    def _run():
        sc.record_cooldown(
            MODEL, "session limit", now=None, ttl_seconds=600.0, access_method="cli"
        )
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


def test_review_claude_skip_does_not_escalate_or_reclear_the_cooldown():
    """review-cli#221 round-4 review finding (Fable): the skip path deliberately
    mimics the chronic-unavailable sentinel (rc=0, 'is currently unavailable'), which
    would MATCH `_chronic_unavailable_reason` if it ever fell through to the tail
    record/clear block instead of returning early. If that ever happened post-
    escalation, EVERY invocation during an active cooldown would increment fail_count
    and ratchet the window toward the 8h cap with no real dispatch ever occurring — a
    self-reinforcing one-way trap. The sibling test above proves the real CLI is never
    called; this proves the STORE itself is untouched by the skip (fail_count doesn't
    move, `until` doesn't move) — both `record_cooldown` (would bump fail_count) and
    `clear_cooldown` (would delete the entry, since a skip's rc=0 body could otherwise
    misread as `elif returncode == 0`) must be unreachable from this path."""

    def _run():
        # Real wall-clock timestamps, NOT a fixed 1970 epoch: the dispatch-time
        # `active_cooldown(model)` check INSIDE `review_claude` calls with no `now=`
        # override, so it always checks against real time — an entry recorded at a
        # fixed past epoch would already read as expired there, letting the real CLI
        # dispatch through and defeating the whole point of this test (the same
        # pitfall k3's round-4 finding caught in a sibling test above).
        t0 = time.time()
        sc.record_cooldown(MODEL, "hang", now=t0)
        sc.record_cooldown(MODEL, "hang", now=t0)
        before = sc.active_cooldown(MODEL, now=t0)
        assert before["fail_count"] == 2
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
        after = sc.active_cooldown(MODEL, now=t0)
        assert after is not None, "the skip must not have cleared the cooldown"
        assert after["fail_count"] == 2, "the skip must not have escalated fail_count"
        assert after["until"] == before["until"], (
            "the skip must not have touched `until`"
        )

    _with_store(_run)


def test_review_claude_records_cooldown_after_sentinel_response():
    def _run():
        assert sc.active_cooldown(MODEL, access_method="cli") is None
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
        assert sc.active_cooldown(MODEL, access_method="cli") is not None

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
            assert sc.active_cooldown(MODEL, access_method="cli") is None
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
            assert sc.active_cooldown(MODEL, access_method="cli") is not None, marker
            sc.clear_cooldown(MODEL, access_method="cli")

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
        assert sc.active_cooldown(MODEL, access_method="cli") is not None

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
        assert sc.active_cooldown(MODEL, access_method="cli") is None

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
        assert sc.active_cooldown(MODEL, access_method="cli") is None

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
        assert sc.active_cooldown(MODEL, access_method="cli") is None

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
        assert sc.active_cooldown(MODEL, access_method="cli") is None

    _with_store(_run)


# ---- wiring into review_with_images (the --visual path) -----------------------------------
def test_review_with_images_skips_real_dispatch_while_cooling_down():
    """kimi P2 finding: --visual dispatches straight to review_claude_cli_with_images,
    bypassing review_claude() entirely — it must consult seat_cooldown itself."""

    def _run():
        sc.record_cooldown(
            MODEL, "session limit", now=None, ttl_seconds=600.0, access_method="cli"
        )
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


def test_review_with_images_skip_does_not_escalate_or_reclear_the_cooldown():
    """review-cli#221 round-4 review finding (Fable): the `review_claude` sibling test
    above proves the skip path can't escalate/clear the store for that call site --
    this mirrors it for --visual, which has the identical skip-then-tail-block shape
    (`active_cooldown` consult, early return, then `_chronic_unavailable_reason` /
    `record_cooldown`/`clear_cooldown` on a REAL dispatch only)."""

    def _run():
        t0 = time.time()
        sc.record_cooldown(MODEL, "hang", now=t0)
        sc.record_cooldown(MODEL, "hang", now=t0)
        before = sc.active_cooldown(MODEL, now=t0)
        assert before["fail_count"] == 2
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
        after = sc.active_cooldown(MODEL, now=t0)
        assert after is not None, "the skip must not have cleared the cooldown"
        assert after["fail_count"] == 2, "the skip must not have escalated fail_count"
        assert after["until"] == before["until"], (
            "the skip must not have touched `until`"
        )

    _with_store(_run)


def test_review_with_images_records_cooldown_after_sentinel_response():
    def _run():
        assert sc.active_cooldown(MODEL, access_method="cli") is None
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
        assert sc.active_cooldown(MODEL, access_method="cli") is not None

    _with_store(_run)


def test_review_with_images_clears_cooldown_on_genuine_success():
    """review-cli#221 round-3 review finding (Opus/k3): the `--visual` dispatch path
    got a `clear_cooldown` call mirroring `review_claude`'s, but had no wiring test of
    its own — only the pre-existing record-side test above. Proves the success-clears
    behavior on THIS call site specifically, not inferred from review_claude's test.

    Setup writes an ALREADY-EXPIRED entry directly (real `active_cooldown` uses real
    wall-clock `time.time()` internally on this call path, which the test can't
    control) that still carries escalation history (`fail_count: 2`) — expired means
    the real dispatch actually runs (not short-circuited to a skip result), and a
    genuine success on that dispatch must remove the stale history entirely, not just
    leave it to naturally fall out of `active_cooldown` once its `until` passes."""

    def _run():
        path = sc.cooldown_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"' + MODEL + '": {"until": 1.0, "reason": "hang", '
            '"recorded_at": 1.0, "fail_count": 2}}'
        )
        assert (
            sc.active_cooldown(MODEL) is None
        )  # already expired -> real dispatch runs

        saved_images = _patched(
            backends,
            "review_claude_cli_with_images",
            lambda *a, **k: ReviewResult(
                model=MODEL,
                command="claude-p",
                returncode=0,
                stdout="a real review body " * 10,
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
        # The stale entry must be gone entirely, not just still-expired: the NEXT
        # failure should start fresh at fail_count 1, not resume at 3.
        assert MODEL not in sc._load(path)

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


# ---- review-cli#221: escalating cooldown on repeated consecutive failures -------------
def test_escalation_first_failure_uses_default_ttl():
    def _run():
        sc.record_cooldown(MODEL, "hang", now=1000.0)
        cd = sc.active_cooldown(MODEL, now=1000.0)
        assert cd["remaining_seconds"] == sc.DEFAULT_COOLDOWN_SECONDS
        assert cd["fail_count"] == 1

    _with_store(_run)


def test_escalation_climbs_the_schedule_on_consecutive_failures():
    def _run():
        t = 1000.0
        expected = list(sc._ESCALATION_SCHEDULE)
        for i, ttl in enumerate(expected):
            sc.record_cooldown(MODEL, "hang", now=t)
            cd = sc.active_cooldown(MODEL, now=t)
            assert cd["remaining_seconds"] == ttl, (i, cd)
            assert cd["fail_count"] == i + 1
            t += 1.0  # well within the reset window, still climbing

    _with_store(_run)


def test_escalation_caps_at_the_schedule_ceiling():
    def _run():
        t = 1000.0
        last_t = t
        for _ in range(len(sc._ESCALATION_SCHEDULE) + 3):  # push well past the cap
            sc.record_cooldown(MODEL, "hang", now=t)
            last_t = t
            t += 1.0
        cd = sc.active_cooldown(MODEL, now=last_t)
        assert cd["remaining_seconds"] == sc._ESCALATION_SCHEDULE[-1]

    _with_store(_run)


def test_escalation_resets_after_a_long_quiet_period():
    def _run():
        sc.record_cooldown(MODEL, "hang", now=1000.0)
        sc.record_cooldown(MODEL, "hang", now=1001.0)
        cd = sc.active_cooldown(MODEL, now=1001.0)
        assert cd["fail_count"] == 2
        far_future = 1001.0 + sc._ESCALATION_RESET_SECONDS + 1.0
        sc.record_cooldown(MODEL, "hang", now=far_future)
        cd = sc.active_cooldown(MODEL, now=far_future)
        assert cd["fail_count"] == 1
        assert cd["remaining_seconds"] == sc.DEFAULT_COOLDOWN_SECONDS

    _with_store(_run)


def test_escalation_does_not_apply_when_ttl_seconds_is_explicit():
    """An explicit `ttl_seconds=` (what every OTHER test in this file uses) must behave
    exactly as before this feature — no escalation, no fail_count climbing regardless of
    how many times it's called."""

    def _run():
        for _ in range(5):
            sc.record_cooldown(MODEL, "hang", now=1000.0, ttl_seconds=600.0)
        cd = sc.active_cooldown(MODEL, now=1000.0)
        assert cd["remaining_seconds"] == 600.0
        # Round-4 review finding (k3): the docstring's "no fail_count climbing" half
        # of the claim was never actually asserted — a regression that kept the ttl
        # fixed but still incremented fail_count would have passed.
        assert cd["fail_count"] == 1

    _with_store(_run)


def test_escalation_does_not_apply_when_env_ttl_is_explicit():
    """`$REVIEW_SEAT_COOLDOWN_SECONDS` is a human explicitly asking for a specific
    window (or 0 to disable) — escalation must not override that."""

    def _run():
        os.environ["REVIEW_SEAT_COOLDOWN_SECONDS"] = "5"
        try:
            for i in range(4):
                sc.record_cooldown(MODEL, "hang", now=1000.0 + i)
            cd = sc.active_cooldown(MODEL, now=1003.0)
            assert cd["remaining_seconds"] == 5.0 - 0.0  # last record was at t=1003.0
        finally:
            os.environ.pop("REVIEW_SEAT_COOLDOWN_SECONDS", None)

    _with_store(_run)


def test_env_override_resets_fail_count_and_a_later_bare_failure_does_not_resume_it():
    """review-cli#221 round-4 review finding (Fable): the README claims an explicit
    override 'always resets fail_count to 1 on its next write' -- the existing test
    above only checks remaining_seconds, not fail_count, and never exercises the
    UNSET-afterward step. Drives the actual operator sequence the README argues for:
    escalate to fail_count 3, export an override and record once (should read back as
    1, per the override branch's `fail_count = 1` — see record_cooldown's docstring),
    unset the override, fail again (should be 2, resuming fresh escalation from the
    override-reset point — NOT 4, which would mean the override never actually reset
    anything and was silently ignored)."""

    def _run():
        sc.record_cooldown(MODEL, "hang", now=1000.0)
        sc.record_cooldown(MODEL, "hang", now=1001.0)
        sc.record_cooldown(MODEL, "hang", now=1002.0)
        assert sc.active_cooldown(MODEL, now=1002.0)["fail_count"] == 3

        os.environ["REVIEW_SEAT_COOLDOWN_SECONDS"] = "5"
        try:
            sc.record_cooldown(MODEL, "hang", now=1003.0)
        finally:
            os.environ.pop("REVIEW_SEAT_COOLDOWN_SECONDS", None)
        cd = sc.active_cooldown(MODEL, now=1003.0)
        assert cd["fail_count"] == 1, "the override write must reset fail_count to 1"
        assert cd["remaining_seconds"] == 5.0

        sc.record_cooldown(MODEL, "hang", now=1004.0)
        cd = sc.active_cooldown(MODEL, now=1004.0)
        assert cd["fail_count"] == 2, (
            "escalation must resume fresh from the override-reset point (2), not "
            "silently carry the pre-override history forward (4)"
        )

    _with_store(_run)


def test_active_cooldown_defaults_fail_count_to_1_for_pre_escalation_records():
    """A record written before this feature existed has no `fail_count` key — must read
    as 1, not crash or read as 0/None."""

    def _run():
        path = sc.cooldown_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"' + MODEL + '": {"until": 2000.0, "reason": "old"}}')
        cd = sc.active_cooldown(MODEL, now=1000.0)
        assert cd["fail_count"] == 1

    _with_store(_run)


def test_escalation_ignores_a_corrupt_fail_count_on_the_record_side():
    """Round-2 review finding: `active_cooldown`'s fail_count guard is tested, but
    `record_cooldown`'s OWN guard (`isinstance(prior_count, int) and prior_count >= 1`)
    is the one that actually indexes `_ESCALATION_SCHEDULE` — a hand-edited/corrupt
    store with a non-int or non-positive `fail_count` must not crash or index
    negative/wrong; it should read as "no valid prior count", same as missing."""

    def _run():
        path = sc.cooldown_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        for corrupt in ('"not-a-number"', "0", "-3", "true", "false"):
            path.write_text(
                '{"'
                + MODEL
                + '": {"until": 500.0, "reason": "old", "recorded_at": 999.0, '
                + '"fail_count": '
                + corrupt
                + "}}"
            )
            sc.record_cooldown(MODEL, "hang", now=1000.0)
            cd = sc.active_cooldown(MODEL, now=1000.0)
            assert cd["fail_count"] == 1, corrupt
            assert cd["remaining_seconds"] == sc.DEFAULT_COOLDOWN_SECONDS, corrupt

    _with_store(_run)


def test_escalation_does_not_index_negative_on_a_future_recorded_at():
    """Very-low-severity round-2 finding: a corrupt/hand-edited entry with a
    `recorded_at` AFTER `now` (clock skew or tampering) must not produce a negative
    reset-window delta. The actual (and correct, fail-open) behavior is to treat the
    entry as having no valid prior count at all — same as a missing/malformed
    fail_count — and start fresh at 1, NOT to keep climbing off the corrupt value."""

    def _run():
        path = sc.cooldown_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"' + MODEL + '": {"until": 500.0, "reason": "old", '
            '"recorded_at": 5000.0, "fail_count": 2}}'
        )
        sc.record_cooldown(MODEL, "hang", now=1000.0)  # now is BEFORE recorded_at
        cd = sc.active_cooldown(MODEL, now=1000.0)
        assert cd["fail_count"] == 1  # treated as no valid prior, not "reset to 3"

    _with_store(_run)


def test_escalation_rejects_a_bool_recorded_at_same_as_a_bool_fail_count():
    """Round-6 review finding (Opus): the `prior_count` guard was hardened to
    `type(x) is int` specifically to reject a corrupt store's bare JSON `true`/`false`
    (`isinstance(True, int)` is True in Python) — but the sibling `prior_recorded_at`
    guard still used `isinstance`, accepting a bool. `"recorded_at": true` almost
    always falls outside the 24h reset window in practice (`now - True` is huge), but
    the two guards in the same hardening block should reject the same corrupt shapes
    for the same reason, not merely happen to produce the same result by magnitude."""

    def _run():
        path = sc.cooldown_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # A `recorded_at` that's a bool but numerically WITHIN the reset window if it
        # were accepted as an int (True == 1) -- proves the guard rejects it on TYPE,
        # not merely because the numeric value happens to fall outside the window.
        path.write_text(
            '{"' + MODEL + '": {"until": 500.0, "reason": "old", '
            '"recorded_at": true, "fail_count": 2}}'
        )
        sc.record_cooldown(MODEL, "hang", now=1.0)
        cd = sc.active_cooldown(MODEL, now=1.0)
        assert cd["fail_count"] == 1, (
            "a bool recorded_at must not count as a valid prior"
        )

    _with_store(_run)


def test_success_clears_an_escalated_cooldown_review_cli_221():
    """Round-2 review finding (Opus): without a success-based reset, a seat that fails
    more often than once per 24h ratchets to the 8h cap and never comes back down, even
    if it succeeds constantly in between — the 24h-quiet-period reset alone can't catch
    this because only FAILURES touch the store. `clear_cooldown` must be reachable and
    fully reset the escalation history (this test proves the primitive; the wiring into
    backends.py's two real dispatch call sites is exercised by test_seat_cooldown's own
    wiring tests / manual read of backends.py, not re-simulated here)."""

    def _run():
        sc.record_cooldown(MODEL, "hang", now=1000.0)
        sc.record_cooldown(MODEL, "hang", now=1001.0)
        assert sc.active_cooldown(MODEL, now=1001.0)["fail_count"] == 2
        sc.clear_cooldown(MODEL)
        assert sc.active_cooldown(MODEL, now=1001.0) is None
        # A failure AFTER the clear starts fresh at fail_count 1, not 3.
        sc.record_cooldown(MODEL, "hang", now=1002.0)
        cd = sc.active_cooldown(MODEL, now=1002.0)
        assert cd["fail_count"] == 1
        assert cd["remaining_seconds"] == sc.DEFAULT_COOLDOWN_SECONDS

    _with_store(_run)


def test_clear_cooldown_deletes_escalated_entry_even_with_cooldowns_disabled():
    """Round-4 review finding (k3): `clear_cooldown` must NOT fast-path-return just
    because `$REVIEW_SEAT_COOLDOWN_SECONDS=0` is currently exported — that env value is
    the module's own documented un-stick hatch (an operator forcing a stuck, escalated
    seat to dispatch RIGHT NOW). If `clear_cooldown` bailed on the disable check, a
    genuine success while the hatch is active would never actually delete the stale
    escalated entry, so unsetting the var afterward would leave the seat cooling down
    for up to the 8h cap despite the just-observed recovery."""

    def _run():
        sc.record_cooldown(MODEL, "hang", now=1000.0)
        sc.record_cooldown(MODEL, "hang", now=1001.0)
        assert sc.active_cooldown(MODEL, now=1001.0)["fail_count"] == 2
        os.environ["REVIEW_SEAT_COOLDOWN_SECONDS"] = "0"
        try:
            sc.clear_cooldown(MODEL)
        finally:
            os.environ.pop("REVIEW_SEAT_COOLDOWN_SECONDS", None)
        assert sc.active_cooldown(MODEL, now=1001.0) is None

    _with_store(_run)


def test_wiring_review_claude_clears_cooldown_on_genuine_success():
    """The actual production wiring (backends.py's main review_claude path): a prior
    escalated cooldown for a model must be cleared when that model's NEXT real dispatch
    genuinely succeeds (rc=0), not merely 'was not chronic-shaped' (which also covers a
    real non-zero-exit failure that just isn't one of the two recognised chronic
    signals — that case must NOT clear escalation history).

    Round-4 review finding (k3): the original version of this test recorded entries at
    `now=1000.0/1001.0` (1970 — long expired relative to real wall-clock) and then
    asserted `active_cooldown(MODEL) is None` with NO `now=` (i.e. at real time) — an
    expired entry reads as None from `active_cooldown` regardless of whether
    `clear_cooldown` ever ran, so deleting the `clear_cooldown` call from
    `review_claude` entirely still left this test green. Fixed to assert store-level
    deletion instead (mirrors the correct sibling test for the --visual call site)."""

    def _run():
        sc.record_cooldown(MODEL, "hang", now=1000.0)
        sc.record_cooldown(MODEL, "hang", now=1001.0)
        assert sc.active_cooldown(MODEL, now=1001.0)["fail_count"] == 2

        saved = backends.review_claude_cli
        # Round-4 review finding (k3): every OTHER wiring test in this file stubs
        # unpaid_provider_result to None so dispatch is deterministic regardless of
        # the host's ambient gateway config — these two omitted it, so on a host where
        # it matches, review_claude would return the unpaid result before ever calling
        # the patched review_claude_cli, and both assertions below would pass for the
        # wrong reason (nothing exercised).
        saved_unpaid = _patched(
            backends, "unpaid_provider_result", lambda *a, **k: None
        )
        backends.review_claude_cli = lambda *a, **k: ReviewResult(
            model=MODEL,
            command="fake",
            returncode=0,
            stdout="a real review body " * 10,
            stderr="",
        )
        try:
            backends.review_claude(MODEL, "prompt", "", Path("."), 60, 0)
        finally:
            backends.review_claude_cli = saved
            backends.unpaid_provider_result = saved_unpaid
        assert MODEL not in sc._load(sc.cooldown_path()), (
            "genuine success must delete the stored entry, not just let it read as "
            "expired"
        )

    _with_store(_run)


def test_wiring_review_claude_non_chronic_failure_does_not_clear_cooldown():
    def _run():
        sc.record_cooldown(MODEL, "hang", now=1000.0)
        sc.record_cooldown(MODEL, "hang", now=1001.0)
        assert sc.active_cooldown(MODEL, now=1001.0)["fail_count"] == 2

        saved = backends.review_claude_cli
        saved_unpaid = _patched(
            backends, "unpaid_provider_result", lambda *a, **k: None
        )
        # returncode != 0, and the stderr doesn't match either chronic marker set —
        # a real but non-chronic failure (e.g. a one-off network blip).
        backends.review_claude_cli = lambda *a, **k: ReviewResult(
            model=MODEL,
            command="fake",
            returncode=1,
            stdout="",
            stderr="connection reset by peer",
        )
        try:
            backends.review_claude(MODEL, "prompt", "", Path("."), 60, 0)
        finally:
            backends.review_claude_cli = saved
            backends.unpaid_provider_result = saved_unpaid
        cd = sc.active_cooldown(MODEL, now=1001.0)
        assert cd is not None, "a non-chronic failure must not erase escalation history"
        assert cd["fail_count"] == 2

    _with_store(_run)


def test_wiring_review_claude_empty_rc0_body_does_not_clear_cooldown():
    """Round-4 review finding (k3): `panel.result_is_usable` treats returncode==0 with
    an EMPTY body as a failure shape too ("a silently-disabled model often returns
    rc=0 with nothing") — `returncode == 0` alone is not "genuine success". Without
    checking the body, a seat oscillating between chronic-quota failures (escalates)
    and empty-rc0 responses (clears) would never actually climb the schedule, pinned
    at 10 minutes forever despite being just as unusable on every dispatch."""

    def _run():
        sc.record_cooldown(MODEL, "hang", now=1000.0)
        sc.record_cooldown(MODEL, "hang", now=1001.0)
        assert sc.active_cooldown(MODEL, now=1001.0)["fail_count"] == 2

        saved = backends.review_claude_cli
        saved_unpaid = _patched(
            backends, "unpaid_provider_result", lambda *a, **k: None
        )
        # rc=0 but an EMPTY body -- result_is_usable's "silently-disabled model" shape,
        # not a genuine success and not the chronic sentinel shape either.
        backends.review_claude_cli = lambda *a, **k: ReviewResult(
            model=MODEL, command="fake", returncode=0, stdout="", stderr=""
        )
        try:
            backends.review_claude(MODEL, "prompt", "", Path("."), 60, 0)
        finally:
            backends.review_claude_cli = saved
            backends.unpaid_provider_result = saved_unpaid
        cd = sc.active_cooldown(MODEL, now=1001.0)
        assert cd is not None, "an empty-rc0 result must not clear escalation history"
        assert cd["fail_count"] == 2, "an empty-rc0 result must not itself escalate"

    _with_store(_run)


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
