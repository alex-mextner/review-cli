"""Wiring test: `review_opencode` must record a seat_cooldown entry when
`_run_streamed` reaps a seat for TRUE SILENCE (the explicit `.true_silenced`
attribute — reviewlib.process's true_silence_timeout, reviewlib.model_behavior's
per-model threshold), and must NOT record one for any other outcome (ordinary
success, ordinary idle-timeout, a real error, or a child that happens to exit with
the SAME returncode 125 on its own for unrelated reasons) — those already have their
own unrelated handling, and a bare returncode match must never be conflated with a
genuine true-silence reap (round-2 review finding, codex + Fable).

Mirrors the mocking style of tests/test_opencode_realrepo.py (patch `_run_streamed` +
`_which`, hermetic — no real opencode binary needed) and tests/test_seat_cooldown.py's
`_with_store` isolation (a fresh $REVIEW_SEAT_COOLDOWN_FILE per test, so this never
touches a developer's real cooldown store).
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import backends as review_backends  # noqa: E402
from reviewlib import seat_cooldown as sc  # noqa: E402

# An UNWATCHED opencode seat: for a model under the zai/glm stall watchdog
# (`_opencode_model_needs_stall_watchdog`) the liveness bound is the sole owner of the
# zero-output signal and `true_silence_timeout` is deliberately NOT forwarded (see
# `_run_opencode_with_stall_retry`; pinned by
# tests/test_opencode_realrepo.py::test_stall_watchdog_owns_zero_output_for_watched_model),
# so the generic true-silence plumbing is exercised on a seat that still carries it.
MODEL = "oc:moonshotai/kimi-k2.5"


def _with_store(fn):
    """Isolates BOTH the cooldown store AND the dashboard log dir for every test that
    uses this helper (codex review finding, review-cli#243 round 13): the skip path
    (`_cooldown_skip_result` -> `_emit_rest_log`) writes a REAL sidecar log via
    `process.log_dir()` whenever a test's flow actually reaches a cooldown skip --
    without isolating `REVIEW_LOG_DIR` too, several tests here injected a synthetic
    `oc:zai/glm-5.2` cooldown-skip event into the developer's REAL dashboard log dir on
    every run, corrupting exactly the seat-health stats this feature exists to
    surface. Fixed once, at the shared helper, rather than patching only the
    individual tests caught failing -- any future test using `_with_store` is
    protected too."""
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as log_dir:
        saved_store = os.environ.get("REVIEW_SEAT_COOLDOWN_FILE")
        saved_ttl = os.environ.get("REVIEW_SEAT_COOLDOWN_SECONDS")
        saved_log_dir = os.environ.get("REVIEW_LOG_DIR")
        os.environ["REVIEW_SEAT_COOLDOWN_FILE"] = str(Path(d) / "seat-cooldown.json")
        os.environ.pop("REVIEW_SEAT_COOLDOWN_SECONDS", None)
        os.environ["REVIEW_LOG_DIR"] = log_dir
        try:
            return fn()
        finally:
            for key, saved in (
                ("REVIEW_SEAT_COOLDOWN_FILE", saved_store),
                ("REVIEW_SEAT_COOLDOWN_SECONDS", saved_ttl),
                ("REVIEW_LOG_DIR", saved_log_dir),
            ):
                if saved is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = saved


class _FakeProc:
    """Mirrors the real `_run_streamed` return shape: `.true_silenced` is the
    authoritative signal, independent of `.returncode` — a fake can therefore set
    a returncode of 125 WITHOUT true_silenced=True, exercising exactly the collision
    scenario the round-2 review fix closed."""

    def __init__(
        self,
        returncode: int,
        stdout: str = "",
        stderr: str = "",
        *,
        true_silenced: bool = False,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.true_silenced = true_silenced


@contextlib.contextmanager
def _stub_run_streamed(
    returncode: int, *, true_silenced: bool, runs_in_repo: bool = False
):
    """Patch `_run_streamed` to return a fixed fake, and `_which`/the project-config
    probe so the call reaches `_run_streamed` deterministically without a real
    opencode binary (mirrors tests/test_opencode_realrepo.py's `_capture_opencode`).
    `captured["calls"]` counts real `_run_streamed` invocations — a test can assert it
    stayed at 0 when a cooldown should have short-circuited the dispatch entirely."""
    orig_run = review_backends._run_streamed
    orig_which = review_backends._which
    orig_ensure = review_backends._ensure_opencode_readonly_agent
    orig_runs_in_repo = review_backends._opencode_runs_in_repo
    captured: dict[str, object] = {"calls": 0, "true_silence_timeout": None}

    def _fake(*args, **kwargs):
        captured["calls"] += 1
        captured["true_silence_timeout"] = kwargs.get("true_silence_timeout")
        return _FakeProc(
            returncode,
            stdout="partial or full output",
            stderr="",
            true_silenced=true_silenced,
        )

    review_backends._run_streamed = _fake  # type: ignore[assignment]
    review_backends._which = lambda name: f"/fake/bin/{name}"  # type: ignore[assignment]
    review_backends._ensure_opencode_readonly_agent = lambda *a, **k: None  # type: ignore[assignment]
    review_backends._opencode_runs_in_repo = lambda cwd: runs_in_repo  # type: ignore[assignment]
    try:
        yield captured
    finally:
        review_backends._run_streamed = orig_run
        review_backends._which = orig_which
        review_backends._ensure_opencode_readonly_agent = orig_ensure
        review_backends._opencode_runs_in_repo = orig_runs_in_repo


def test_true_silence_records_a_cooldown():
    def _run():
        assert sc.active_cooldown(MODEL, access_method="opencode") is None
        with _stub_run_streamed(125, true_silenced=True) as captured:
            result = review_backends.review_opencode(
                MODEL, "prompt", "diff", Path("."), 60
            )
        assert result.returncode == 125
        assert sc.active_cooldown(MODEL, access_method="opencode") is not None
        # The registry-driven value must actually reach _run_streamed, not silently
        # stay None (which would disable the check) OR a hardcoded stand-in that just
        # happens to be truthy (Fable review finding, round 3: an earlier version of
        # this test only asserted "is not None", which a hardcoded constant at the
        # call site would also satisfy).
        from reviewlib import model_behavior

        assert captured[
            "true_silence_timeout"
        ] == model_behavior.true_silence_timeout_seconds(MODEL)

    _with_store(_run)


def test_env_override_disable_actually_reaches_run_streamed():
    """$REVIEW_TRUE_SILENCE_SECONDS=0 must propagate all the way through
    review_opencode to _run_streamed as None (disabling the check), not just be
    correct in reviewlib.model_behavior's own unit tests in isolation."""

    def _run():
        saved = os.environ.get("REVIEW_TRUE_SILENCE_SECONDS")
        os.environ["REVIEW_TRUE_SILENCE_SECONDS"] = "0"
        try:
            with _stub_run_streamed(0, true_silenced=False) as captured:
                review_backends.review_opencode(MODEL, "prompt", "diff", Path("."), 60)
            assert captured["true_silence_timeout"] is None
        finally:
            if saved is None:
                os.environ.pop("REVIEW_TRUE_SILENCE_SECONDS", None)
            else:
                os.environ["REVIEW_TRUE_SILENCE_SECONDS"] = saved

    _with_store(_run)


def test_a_genuine_child_exit_125_without_true_silence_does_not_record_a_cooldown():
    """round-2 review finding (codex + Fable): 125 is a real exit code some CLIs/
    wrappers use on their own for unrelated reasons. A child that exits 125 by
    itself — WITHOUT `_run_streamed` having actually reaped it for true silence —
    must never be misdiagnosed as a stuck seat and wrongly benched."""

    def _run():
        assert sc.active_cooldown(MODEL, access_method="opencode") is None
        with _stub_run_streamed(125, true_silenced=False):
            result = review_backends.review_opencode(
                MODEL, "prompt", "diff", Path("."), 60
            )
        assert result.returncode == 125
        assert sc.active_cooldown(MODEL, access_method="opencode") is None, (
            "a genuine (non-true-silence) exit 125 wrongly recorded a cooldown"
        )

    _with_store(_run)


def test_true_silence_wiring_covers_the_in_repo_branch_too():
    """codex review finding (round 1): the other test only exercised the FALLBACK
    (not-a-git-repo) `_run_streamed` call site inside review_opencode — the in-repo
    early-return branch has its own separate `true_silence_timeout=`/
    `_record_true_silence_if_needed` plumbing that could silently drift out of sync."""

    def _run():
        assert sc.active_cooldown(MODEL, access_method="opencode") is None
        with _stub_run_streamed(125, true_silenced=True, runs_in_repo=True) as captured:
            result = review_backends.review_opencode(
                MODEL, "prompt", "diff", Path("."), 60
            )
        assert result.returncode == 125
        assert sc.active_cooldown(MODEL, access_method="opencode") is not None
        assert captured["true_silence_timeout"] is not None

    _with_store(_run)


def test_ordinary_success_does_not_record_a_cooldown():
    def _run():
        assert sc.active_cooldown(MODEL, access_method="opencode") is None
        with _stub_run_streamed(0, true_silenced=False):
            result = review_backends.review_opencode(
                MODEL, "prompt", "diff", Path("."), 60
            )
        assert result.returncode == 0
        assert sc.active_cooldown(MODEL, access_method="opencode") is None

    _with_store(_run)


def test_ordinary_idle_timeout_does_not_record_a_true_silence_cooldown():
    """rc 124 (the pre-existing idle-timeout code, true_silenced always False for
    that path) is a DIFFERENT failure shape from true-silence — it must not trip
    this new cooldown path (it may still be handled elsewhere via the existing
    chronic-unavailable/failover machinery, which this test does not need to
    exercise)."""

    def _run():
        assert sc.active_cooldown(MODEL, access_method="opencode") is None
        with _stub_run_streamed(124, true_silenced=False):
            result = review_backends.review_opencode(
                MODEL, "prompt", "diff", Path("."), 60
            )
        assert result.returncode == 124
        assert sc.active_cooldown(MODEL, access_method="opencode") is None

    _with_store(_run)


def test_a_recorded_cooldown_is_actually_consulted_on_the_next_call():
    """codex review finding (round 1, P1): recording a cooldown is useless if the NEXT
    call never checks it. After a true-silence trip, a second `review_opencode` call
    for the SAME model must skip the real dispatch entirely (0 further `_run_streamed`
    calls) and return the synthetic cooldown-skip sentinel instead."""

    def _run():
        with _stub_run_streamed(125, true_silenced=True) as first_call:
            first = review_backends.review_opencode(
                MODEL, "prompt", "diff", Path("."), 60
            )
        assert first.returncode == 125
        assert first_call["calls"] == 1
        assert sc.active_cooldown(MODEL, access_method="opencode") is not None

        with _stub_run_streamed(0, true_silenced=False) as second_call:
            second = review_backends.review_opencode(
                MODEL, "prompt", "diff", Path("."), 60
            )
        assert second_call["calls"] == 0, (
            "review_opencode dispatched a real call despite an active cooldown"
        )
        assert second.returncode == 0  # the synthetic cooldown-skip sentinel

    _with_store(_run)


def test_true_silence_cooldown_crosses_the_oc_and_opencode_alias_spellings():
    """codex review finding (review-cli#243 round 22): `resolve_backend` routes BOTH
    `oc:provider/model` and `opencode:provider/model` to `review_opencode` (config.py's
    `_agentic` docstring calls these "both spellings" of the SAME canonical seat, and
    the dashboard attributes both to the same `oc:` row) -- but before this fix, the
    cooldown store was keyed by whichever RAW spelling was passed in, so a true-silence
    trip via one spelling would not protect the next call made via the other. A trip
    on the `opencode:` alias must be consulted (and skip the real dispatch) on the
    NEXT call made via the canonical `oc:` spelling, and vice versa."""
    OC_MODEL = "oc:zai/glm-5.2"
    OPENCODE_ALIAS = "opencode:zai/glm-5.2"

    def _run():
        with _stub_run_streamed(125, true_silenced=True) as first_call:
            first = review_backends.review_opencode(
                OPENCODE_ALIAS, "prompt", "diff", Path("."), 60
            )
        assert first.returncode == 125
        assert first_call["calls"] == 1
        # The cooldown must be recorded under the CANONICAL `oc:` key, not the raw
        # `opencode:` alias that was actually passed in.
        assert sc.active_cooldown(OC_MODEL, access_method="opencode") is not None
        assert sc.active_cooldown(OPENCODE_ALIAS, access_method="opencode") is None

        with _stub_run_streamed(0, true_silenced=False) as second_call:
            second = review_backends.review_opencode(
                OC_MODEL, "prompt", "diff", Path("."), 60
            )
        assert second_call["calls"] == 0, (
            "a true-silence trip via the opencode: alias did not protect the next "
            "call made via the canonical oc: spelling"
        )
        assert second.returncode == 0  # the synthetic cooldown-skip sentinel

    _with_store(_run)


def test_review_opencode_reports_the_callers_own_model_on_both_dispatch_and_skip():
    """(codex review finding, review-cli#243 round 26, P1 -- supersedes an earlier,
    now-WRONG round-23 version of this test) Every returned `ReviewResult.model` must
    equal the caller's OWN requested `model` string, unchanged, on EVERY exit path
    (real dispatch, cooldown-skip, unpaid, preflight) -- NEVER the internally
    canonicalized `oc:` form. `reviewlib/modes/review.py`'s flat-diff path builds
    `by_model = {result.model: result for result in results}` and looks up
    `by_model[model]` for each ORIGINALLY REQUESTED model string; if a dispatch
    silently rewrote `.model` to a canonical spelling, `review diff -m
    opencode:zai/glm-5.2` would KeyError after a real dispatch completed -- a crash on
    a real, basic CLI invocation. A round-23 version of this test asserted the
    OPPOSITE (canonical `.model` on both paths) to fix a genuine inconsistency
    between dispatch and skip -- the correct fix for that inconsistency is BOTH paths
    consistently reporting the caller's own string, not both reporting canonical."""
    OPENCODE_ALIAS = "opencode:zai/glm-5.2"
    CANONICAL = "oc:zai/glm-5.2"

    def _run():
        with _stub_run_streamed(0, true_silenced=False):
            dispatched = review_backends.review_opencode(
                OPENCODE_ALIAS, "prompt", "diff", Path("."), 60
            )
        assert dispatched.model == OPENCODE_ALIAS, (
            f"real dispatch reported {dispatched.model!r}, not the caller's own "
            f"{OPENCODE_ALIAS!r} -- this would KeyError in review.py's by_model lookup"
        )

        sc.record_cooldown(CANONICAL, "true-silence timeout", access_method="opencode")
        with _stub_run_streamed(0, true_silenced=False):
            skipped = review_backends.review_opencode(
                OPENCODE_ALIAS, "prompt", "diff", Path("."), 60
            )
        assert skipped.model == OPENCODE_ALIAS, (
            f"cooldown-skip reported {skipped.model!r}, not the caller's own "
            f"{OPENCODE_ALIAS!r}"
        )
        assert dispatched.model == skipped.model, (
            "dispatch and skip reported DIFFERENT .model ids for the same seat"
        )

    _with_store(_run)


def test_cooldown_skip_is_attributed_to_opencode_not_claude():
    """codex review finding (round 2, P1): the synthetic cooldown-skip result used to
    be hard-coded to attribute EVERY skip to "claude" in the sidecar log/dashboard —
    an opencode true-silence skip was therefore mislabeled as a Claude/Fable cached-
    paywall event, corrupting attribution and hiding the real `oc:*` seat's health."""

    def _run():
        with _stub_run_streamed(125, true_silenced=True):
            review_backends.review_opencode(MODEL, "prompt", "diff", Path("."), 60)
        assert sc.active_cooldown(MODEL, access_method="opencode") is not None

        with _stub_run_streamed(0, true_silenced=False) as second_call:
            skip_result = review_backends.review_opencode(
                MODEL, "prompt", "diff", Path("."), 60
            )
        assert second_call["calls"] == 0  # confirms this really is the skip path
        assert "opencode" in skip_result.command
        assert "claude" not in skip_result.command.lower()

    _with_store(_run)


def test_cooldown_skip_sidecar_log_resolves_to_the_real_board_seat():
    """codex review finding (round 4, P2): attributing the skip's `command` string to
    "opencode" (the previous test) is not the same as the dashboard resolving it to
    the SPECIFIC board seat. `model_id_for_call` (reviewlib/dashboard/parser.py) only
    maps an opencode call to `oc:<provider/model>` when argv0 carries an `-m <model>`
    token, the same shape `review_opencode`'s own real dispatch already writes. The
    skip's sidecar log must carry that token too, or the skip silently falls into the
    generic `opencode` bucket -- present in the log, but invisible on the actual
    `oc:zai/glm-5.2` seat's health/error row, defeating the point of attributing the
    skip to a specific backend at all."""

    def _run():
        with tempfile.TemporaryDirectory() as log_dir:
            saved_log_dir = os.environ.get("REVIEW_LOG_DIR")
            os.environ["REVIEW_LOG_DIR"] = log_dir
            try:
                with _stub_run_streamed(125, true_silenced=True):
                    review_backends.review_opencode(
                        MODEL, "prompt", "diff", Path("."), 60
                    )
                assert sc.active_cooldown(MODEL, access_method="opencode") is not None

                with _stub_run_streamed(0, true_silenced=False) as second_call:
                    review_backends.review_opencode(
                        MODEL, "prompt", "diff", Path("."), 60
                    )
                assert second_call["calls"] == 0  # confirms this is the skip path

                from reviewlib.dashboard import parser as p

                logs = sorted(Path(log_dir).glob("*-opencode-r*.log"))
                assert logs, "no sidecar log written for the cooldown-skip call"
                call = p.parse_call_log(logs[-1])
                assert call is not None
                assert p.model_id_for_call(call) == MODEL, (
                    f"cooldown skip resolved to {p.model_id_for_call(call)!r}, "
                    f"not the real seat {MODEL!r} -- it fell into the generic "
                    "opencode bucket instead of the seat's own health row"
                )
            finally:
                if saved_log_dir is None:
                    os.environ.pop("REVIEW_LOG_DIR", None)
                else:
                    os.environ["REVIEW_LOG_DIR"] = saved_log_dir

    _with_store(_run)


def test_a_non_true_silence_cooldown_reason_is_also_honored():
    """Fable review finding (round 2): the new cooldown gate in review_opencode
    honors ANY active cooldown, not just true-silence ones — matching review_claude's
    own established pattern (it too consults active_cooldown broadly, not only for
    its own recorded reason). This proves that consistency is real: a cooldown
    recorded for a DIFFERENT reason is still correctly honored by the new gate."""

    def _run():
        assert sc.active_cooldown(MODEL, access_method="opencode") is None
        sc.record_cooldown(MODEL, "unavailable sentinel", access_method="opencode")
        assert sc.active_cooldown(MODEL, access_method="opencode") is not None

        with _stub_run_streamed(0, true_silenced=False) as captured:
            result = review_backends.review_opencode(
                MODEL, "prompt", "diff", Path("."), 60
            )
        assert captured["calls"] == 0, (
            "review_opencode dispatched a real call despite an unrelated active cooldown"
        )
        assert result.returncode == 0  # the synthetic cooldown-skip sentinel

    _with_store(_run)


def test_repeated_true_silence_escalates_the_cooldown_instead_of_resetting_it():
    """codex review finding (round 1, P2): passing an explicit ttl_seconds made every
    occurrence look like fail_count=1 to record_cooldown, silently disabling this
    repo's escalating cooldown schedule (review-cli#230). A SECOND true-silence trip
    for the same model must escalate `fail_count` and lengthen the cooldown window,
    not repeat the same flat TTL.

    Drives `review_opencode` itself (not `record_cooldown` directly) so this proves
    the actual WIRING (`_record_true_silence_if_needed`'s call shape), matching what
    codex's P2 finding was about; the wall-clock EXPIRY/escalation-window arithmetic
    itself already has dedicated coverage in tests/test_seat_cooldown.py."""

    def _run():
        with _stub_run_streamed(125, true_silenced=True):
            review_backends.review_opencode(MODEL, "prompt", "diff", Path("."), 60)
        data = json.loads(sc.cooldown_path().read_text(encoding="utf-8"))
        first_fail_count = data[MODEL]["opencode"]["fail_count"]
        first_window = data[MODEL]["opencode"]["until"] - data[MODEL]["opencode"]["recorded_at"]
        assert first_fail_count == 1

        # A SECOND real true-silence trip, driven the same way _record_true_silence_if_
        # needed does it (no explicit ttl_seconds) — record_cooldown reads the still-
        # persisted prior entry's fail_count to escalate, regardless of whether that
        # prior cooldown has expired yet, so this needs no wall-clock manipulation.
        review_backends._record_true_silence_if_needed(
            MODEL, _FakeProc(125, stdout="partial output", true_silenced=True)
        )
        data2 = json.loads(sc.cooldown_path().read_text(encoding="utf-8"))
        second_window = data2[MODEL]["opencode"]["until"] - data2[MODEL]["opencode"]["recorded_at"]
        assert data2[MODEL]["opencode"]["fail_count"] == 2, (
            "second true-silence trip did not escalate fail_count"
        )
        assert second_window > first_window, (
            "second true-silence trip did not escalate to a longer cooldown window"
        )

    _with_store(_run)


def test_true_silence_then_empty_rc0_body_does_not_clear_the_cooldown():
    """Opus review finding (round 14): the clear-path guard is `returncode == 0 and
    proc.stdout.strip()` -- an EMPTY (or whitespace-only) rc=0 body must NOT count as
    recovery (a silently-disabled/framing-only response, not a real verdict), so the
    cooldown must stay active. Every prior clear-path test used a non-empty body;
    nothing pinned the `.strip()` guard's own effect until this test."""

    def _run():
        review_backends._record_true_silence_if_needed(
            MODEL, _FakeProc(125, stdout="", true_silenced=True)
        )
        assert sc.active_cooldown(MODEL, access_method="opencode") is not None

        review_backends._record_true_silence_if_needed(
            MODEL, _FakeProc(0, stdout="   \n  ", true_silenced=False)
        )
        assert sc.active_cooldown(MODEL, access_method="opencode") is not None, (
            "an empty/whitespace-only rc=0 body wrongly cleared the cooldown"
        )

    _with_store(_run)


def test_true_silence_then_success_then_true_silence_resets_fail_count():
    """codex review finding (round 3, P2): a genuine success must clear the
    true-silence cooldown history the same way review_claude's own success path does
    (backends.py:288) -- mirrors clear_cooldown's own documented contract, which names
    this as one of backends.py's two intended call sites. Without this, a seat that
    RECOVERS after one true-silence trip, then goes true-silent again later, wrongly
    resumes escalation at fail_count=2 (30 minutes) instead of restarting cleanly at
    fail_count=1 (10 minutes)."""

    def _run():
        # First true-silence trip: fail_count=1.
        review_backends._record_true_silence_if_needed(
            MODEL, _FakeProc(125, stdout="", true_silenced=True)
        )
        data = json.loads(sc.cooldown_path().read_text(encoding="utf-8"))
        assert data[MODEL]["opencode"]["fail_count"] == 1

        # A genuine success in between: must clear the cooldown entirely, not just
        # let it passively expire.
        review_backends._record_true_silence_if_needed(
            MODEL, _FakeProc(0, stdout="a real review verdict", true_silenced=False)
        )
        assert sc.active_cooldown(MODEL, access_method="opencode") is None, (
            "a genuine success after a true-silence trip did not clear the cooldown"
        )

        # A THIRD true-silence trip, after the recovery: fail_count must restart at 1,
        # not continue escalating from the pre-recovery history.
        review_backends._record_true_silence_if_needed(
            MODEL, _FakeProc(125, stdout="", true_silenced=True)
        )
        data2 = json.loads(sc.cooldown_path().read_text(encoding="utf-8"))
        assert data2[MODEL]["opencode"]["fail_count"] == 1, (
            "true-silence after a genuine success did not reset fail_count to 1"
        )

    _with_store(_run)


def test_true_silence_then_rc0_quota_body_then_true_silence_preserves_escalation():
    """codex review finding (round 12, P1): a rc=0 body that LOOKS like a genuine
    success but is actually a chronic-unavailable-shaped sentinel (matching the SAME
    4 marker phrases review_claude's own clear-gate already checks via
    _chronic_unavailable_reason) must NOT be treated as recovery -- it must record a
    cooldown (escalating, same as any other chronic failure) instead of wrongly
    clearing the true-silence escalation history. Mirrors
    test_true_silence_then_success_then_true_silence_resets_fail_count's shape, but
    the "recovery" in the middle is a sentinel body, not a real verdict, so
    fail_count must ESCALATE across all three trips, never reset."""

    def _run():
        # First true-silence trip: fail_count=1.
        review_backends._record_true_silence_if_needed(
            MODEL, _FakeProc(125, stdout="", true_silenced=True)
        )
        data = json.loads(sc.cooldown_path().read_text(encoding="utf-8"))
        assert data[MODEL]["opencode"]["fail_count"] == 1

        # rc=0 with a SHORT body matching a known chronic-unavailable marker phrase --
        # NOT a real review, must NOT clear the cooldown.
        review_backends._record_true_silence_if_needed(
            MODEL,
            _FakeProc(
                0, stdout="GLM-5.2 is currently unavailable", true_silenced=False
            ),
        )
        assert sc.active_cooldown(MODEL, access_method="opencode") is not None, (
            "a chronic-unavailable-shaped rc=0 body wrongly cleared the cooldown"
        )
        data_mid = json.loads(sc.cooldown_path().read_text(encoding="utf-8"))
        assert data_mid[MODEL]["opencode"]["fail_count"] == 2, (
            "the sentinel body did not itself record a (escalating) cooldown entry"
        )

        # A THIRD true-silence trip: must escalate from 2, not reset to 1 -- the
        # sentinel body in between was never a genuine recovery.
        review_backends._record_true_silence_if_needed(
            MODEL, _FakeProc(125, stdout="", true_silenced=True)
        )
        data2 = json.loads(sc.cooldown_path().read_text(encoding="utf-8"))
        assert data2[MODEL]["opencode"]["fail_count"] == 3, (
            "escalation was wrongly reset by the sentinel-body 'recovery'"
        )

    _with_store(_run)


def test_nonzero_exit_with_quota_marker_in_stderr_records_a_cooldown():
    """(Opus review finding, review-cli#243 round 19) The round-14 docstring on
    _record_true_silence_if_needed explicitly claims a NON-ZERO exit whose stderr/
    short-stdout matches _CHRONIC_QUOTA_MARKERS ("session limit" / "usage-credits" /
    "usage credits") ALSO records a cooldown via _chronic_unavailable_reason -- this
    is the one branch that claim covers with zero test coverage until now (every
    other branch of the function -- true-silence, bare-125, rc=0 success clears,
    rc=124 no-record, rc=0 sentinel records, empty rc=0 no-clear -- already has a
    dedicated test)."""

    def _run():
        assert sc.active_cooldown(MODEL, access_method="opencode") is None
        review_backends._record_true_silence_if_needed(
            MODEL,
            _FakeProc(
                1, stdout="", stderr="session limit reached", true_silenced=False
            ),
        )
        assert sc.active_cooldown(MODEL, access_method="opencode") is not None, (
            "a non-zero exit with a quota-marker stderr did not record a cooldown"
        )
        data = json.loads(sc.cooldown_path().read_text(encoding="utf-8"))
        assert data[MODEL]["opencode"]["reason"] == "session limit / usage credits"

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
