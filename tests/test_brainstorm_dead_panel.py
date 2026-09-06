#!/usr/bin/env python3
"""Unit tests: brainstorm FAILS LOUD when the panel backends return empty.

Regression guard for the hollow-converge bug (ROADMAP, CTO 2026-06-16): a brainstorm whose
every panel seat returned empty ("(no output)") used to run all its rounds, let the moderator
rubber-stamp DECISION: STOP over a transcript of nothing, print an EMPTY synthesis, and exit 0
as if it worked — wasting ~20 min of "it's still thinking". The fix detects a dead round (most/
all seats produced no usable output) and aborts with a clear error + a stable non-zero exit
(EXIT_DEAD_PANEL), instead of a hollow STOP.

These tests exercise the REAL `mode_brainstorm` loop, `_round_is_dead`, and `result_is_usable`
(the production judgement). Only the network boundary is stubbed: `panel.resolve_backend` is
replaced so the backend call returns canned `ReviewResult`s (dead or alive) with no model
call / no network. `REVIEW_LOG_DIR` points the discussion log at a throwaway temp dir.

Same plain-function + __main__ harness as the rest of tests/.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import reviewlib.panel as panel  # noqa: E402
from reviewlib.backends import ReviewResult  # noqa: E402
from reviewlib.modes.brainstorm import (  # noqa: E402
    EXIT_DEAD_PANEL,
    _round_is_dead,
    mode_brainstorm,
)


def _result(
    model: str, *, stdout: str = "", returncode: int = 0, stderr: str = ""
) -> ReviewResult:
    return ReviewResult(
        model=model,
        command=f"fake:{model}",
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


class _StubBackends:
    """Stub `panel.resolve_backend` so every model resolves to a canned backend. `outcome`
    decides what each persona call returns: 'dead' -> empty stdout (a silently-disabled
    backend), 'error' -> non-zero exit, 'alive' -> real output. The moderator/synthesis
    prompts (when reached) always return usable text so a NON-dead run still completes.

    `dead_from_round` (optional) flips persona calls to DEAD only from that round onward, so
    a test can model "rounds 1..N-1 productive, round N flakes" — the partial-success case
    where the dead-panel guard must NOT abort (good rounds already accumulated)."""

    def __init__(self, outcome: str, *, dead_from_round: int | None = None):
        self.outcome = outcome
        self.dead_from_round = dead_from_round
        self.persona_calls = 0
        self.moderator_calls = 0

    def __enter__(self):
        self._old = panel.resolve_backend
        from reviewlib.modes.brainstorm import (
            MODERATOR_PROMPT_LEADIN,
            SYNTHESIS_PROMPT_MARKER,
        )

        def _fake_backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
            if SYNTHESIS_PROMPT_MARKER in prompt or MODERATOR_PROMPT_LEADIN in prompt:
                self.moderator_calls += 1
                body = (
                    "Moderator summary.\nDECISION: CONTINUE"
                    if MODERATOR_PROMPT_LEADIN in prompt
                    else "FINAL SYNTHESIS: ship idea-A."
                )
                return _result(model, stdout=body)
            self.persona_calls += 1
            if self.dead_from_round is not None and round_no >= self.dead_from_round:
                return _result(model, stdout="")  # flake from this round on
            if self.outcome == "dead":
                return _result(model, stdout="")  # rc 0, empty -> not usable
            if self.outcome == "error":
                return _result(model, stdout="", returncode=1, stderr="backend died")
            return _result(model, stdout=f"idea from {model} r{round_no}")

        panel.resolve_backend = lambda _model: _fake_backend
        return self

    def __exit__(self, *exc):
        panel.resolve_backend = self._old
        return False


def _run_brainstorm(
    outcome: str,
    *,
    rounds: int = 5,
    max_rounds: int = 8,
    dead_from_round: int | None = None,
):
    """Run a real brainstorm with a stubbed backend boundary, in a temp log dir. Returns
    (exit_code, stdout_text, stub)."""
    with tempfile.TemporaryDirectory() as tmp:
        old_log = os.environ.get("REVIEW_LOG_DIR")
        os.environ["REVIEW_LOG_DIR"] = tmp
        buf = io.StringIO()
        try:
            with (
                _StubBackends(outcome, dead_from_round=dead_from_round) as stub,
                redirect_stdout(buf),
            ):
                rc = mode_brainstorm(
                    "How should we cache?",
                    ["m1", "m2", "m3"],
                    REPO_ROOT,
                    5,
                    ["mod"],
                    rounds=rounds,
                    max_rounds=max_rounds,
                )
        finally:
            if old_log is None:
                os.environ.pop("REVIEW_LOG_DIR", None)
            else:
                os.environ["REVIEW_LOG_DIR"] = old_log
        return rc, buf.getvalue(), stub


# === _round_is_dead unit (the production judgement) ===============================
def test_round_is_dead_all_empty():
    assert _round_is_dead([_result("a"), _result("b"), _result("c")]) is True


def test_round_is_dead_all_errored():
    rs = [_result("a", returncode=1), _result("b", returncode=1)]
    assert _round_is_dead(rs) is True


def test_round_is_dead_majority_empty():
    # 1 usable of 3 -> dead (strict majority unusable).
    rs = [_result("a", stdout="real"), _result("b"), _result("c")]
    assert _round_is_dead(rs) is True


def test_round_is_alive_half_usable():
    # 1 usable of 2 -> NOT dead (a single flaky backend must not abort the run).
    rs = [_result("a", stdout="real"), _result("b")]
    assert _round_is_dead(rs) is False


def test_round_is_alive_all_usable():
    rs = [_result("a", stdout="x"), _result("b", stdout="y")]
    assert _round_is_dead(rs) is False


def test_round_is_dead_empty_list():
    assert _round_is_dead([]) is True


# === end-to-end: a dead panel aborts loud with the stable exit code ==============
def test_dead_panel_aborts_with_dead_panel_exit_code():
    rc, _out, stub = _run_brainstorm("dead")
    assert rc == EXIT_DEAD_PANEL, (
        f"expected EXIT_DEAD_PANEL={EXIT_DEAD_PANEL}, got {rc}"
    )
    # It must abort on the FIRST dead round — exactly one round of persona calls (3 seats),
    # NOT all 5 min-rounds, and it must NOT spend a moderator call on a dead round.
    assert stub.persona_calls == 3, (
        f"expected 1 dead round (3 seats), got {stub.persona_calls} calls"
    )
    assert stub.moderator_calls == 0, "must not call the moderator on a dead round"


def test_dead_panel_errored_backends_also_abort():
    rc, _out, _stub = _run_brainstorm("error")
    assert rc == EXIT_DEAD_PANEL, f"errored backends must also abort, got {rc}"


def test_dead_panel_prints_actionable_error():
    """The abort must print a clear, actionable error (stderr) — what happened, why, how to
    fix — not a silent empty synthesis. Captured here off stderr."""
    err = io.StringIO()
    from contextlib import redirect_stderr

    with tempfile.TemporaryDirectory() as tmp:
        old_log = os.environ.get("REVIEW_LOG_DIR")
        os.environ["REVIEW_LOG_DIR"] = tmp
        try:
            with (
                _StubBackends("dead"),
                redirect_stderr(err),
                redirect_stdout(io.StringIO()),
            ):
                mode_brainstorm(
                    "topic",
                    ["m1", "m2", "m3"],
                    REPO_ROOT,
                    5,
                    ["mod"],
                    rounds=5,
                    max_rounds=8,
                )
        finally:
            if old_log is None:
                os.environ.pop("REVIEW_LOG_DIR", None)
            else:
                os.environ["REVIEW_LOG_DIR"] = old_log
    text = err.getvalue()
    assert "brainstorm aborted" in text, text
    assert "no usable output" in text, text
    assert "dead or credential-less" in text, text
    assert "fix:" in text.lower(), "error must tell the user how to fix it"


def test_dead_panel_does_not_print_a_hollow_synthesis():
    """The whole point: a dead run must NOT emit a 'Final synthesis' block as if it worked."""
    _rc, out, _stub = _run_brainstorm("dead")
    assert "# Final synthesis" not in out, "a dead panel must not print a synthesis"


def test_live_panel_still_completes_normally():
    """A healthy panel is unaffected: it runs to its min rounds, synthesizes, and exits 0
    (the dead-panel guard must not fire on a productive run)."""
    rc, out, stub = _run_brainstorm("alive", rounds=5, max_rounds=5)
    assert rc == 0, f"a live brainstorm must succeed, got {rc}"
    assert "# Final synthesis" in out, "a live brainstorm must print its synthesis"
    assert stub.persona_calls == 5 * 3, (
        f"expected 5 rounds x 3 seats, got {stub.persona_calls}"
    )


def test_dead_round_after_productive_rounds_does_not_abort(capfd=None):
    """A later round flaking (transient) must NOT discard the productive earlier rounds.

    The dead-panel guard fires ONLY before any usable round has accumulated (`not
    transcript_blocks`) — its target is "every seat dead from round 1". If round 1 was
    productive and a later round flakes, the run must keep its good round and still reach a
    synthesis (the pre-existing graceful behavior), NOT abort with EXIT_DEAD_PANEL and throw
    the good work away (claude-opus review). Mid-run transient resilience (retry/reserve-swap)
    is a separate ROADMAP item; this only guards the dead-panel path against that regression."""
    # Round 1 alive, round 2 onward dead. The run must NOT abort — it keeps round 1.
    rc, out, stub = _run_brainstorm("alive", rounds=1, max_rounds=3, dead_from_round=2)
    assert rc != EXIT_DEAD_PANEL, (
        f"a dead round AFTER a productive round must not abort with EXIT_DEAD_PANEL, got {rc}"
    )
    assert rc == 0, f"the run should complete (synthesize the good round), got {rc}"
    assert "# Final synthesis" in out, (
        "the accumulated good round must still be synthesized"
    )
    # The guard did NOT fire on round 1 (it ran at least one productive round before any
    # dead round) — i.e. it did not bail on the first round the way the all-dead case does.
    assert stub.persona_calls > 3, (
        f"expected >1 round to run (round 1 productive, not aborted), got "
        f"{stub.persona_calls} persona calls"
    )
    # The productive round-1 content must actually be present in the output the user sees —
    # proving the synthesis isn't hollow / the good round wasn't silently dropped.
    assert "idea from m1 r1" in out, (
        "round-1 productive content must survive into the output"
    )


def test_midrun_collapse_stops_on_dead_round_not_min_rounds():
    """A mid-run collapse STOPS the loop on the dead round and synthesizes the good rounds —
    it must NOT keep hammering the now-dead backends for the remaining min_rounds (the
    "~20 min wasted on dead backends" the CTO hit).

    Round 1 alive, round 2 dead, with rounds>=5. Before the fix the loop ignored the dead
    round and ran to round 5 (min_rounds) before STOP could fire — 5 rounds x 3 seats of dead
    backends. After the fix the dead round 2 itself ends the loop: exactly round 1 (3 seats)
    + round 2 (3 dead seats) = 6 persona calls, then a synthesis over round 1."""
    rc, out, stub = _run_brainstorm("alive", rounds=5, max_rounds=8, dead_from_round=2)
    assert rc == 0, f"the collapse must synthesize the good round and exit 0, got {rc}"
    assert "# Final synthesis" in out, "the good round 1 must still be synthesized"
    # The dead round ends the loop — round 1 (3) + the dead round 2 (3) only. NOT 5 min-rounds
    # (15) of dead backends.
    assert stub.persona_calls == 6, (
        f"a mid-run collapse must stop on the dead round, not run to min_rounds; expected "
        f"6 persona calls (round 1 + dead round 2), got {stub.persona_calls}"
    )
    # The moderator is NOT consulted on the dead round (round 2). The stub counts a moderator
    # call for BOTH a per-round summary AND the final synthesis, so a clean collapse is exactly
    # 2: round 1's summary + the final synthesis. A round-2 moderator turn would make it 3 —
    # so this pins "no moderator on the dead round" (the rubber-stamp-STOP vector is gone).
    assert stub.moderator_calls == 2, (
        f"the moderator must not be called on the dead round (expected 2 = round-1 summary + "
        f"final synthesis), got {stub.moderator_calls}"
    )


def test_midrun_collapse_not_hollow_even_if_moderator_would_stop_over_dead_round():
    """The hollow-STOP vector (CTO 2026-06-16): the moderator stamps DECISION: STOP over a
    DEAD round and the run "converges" on a transcript of "(no output)". The fix removes the
    moderator from the dead-round decision entirely — the dead round itself ends the loop
    BEFORE any moderator turn — so even a STOP-happy moderator cannot manufacture a hollow
    convergence. Proven here with a moderator that ALWAYS says STOP."""
    with tempfile.TemporaryDirectory() as tmp:
        old_log = os.environ.get("REVIEW_LOG_DIR")
        os.environ["REVIEW_LOG_DIR"] = tmp
        buf = io.StringIO()
        try:
            stub = _StubBackends("alive", dead_from_round=2)
            # Override the moderator to ALWAYS stamp STOP (the rubber-stamp moderator).
            orig_enter = stub.__enter__

            def _enter_stop_happy(_self=stub, _orig=orig_enter):
                _orig()
                from reviewlib.modes.brainstorm import (
                    MODERATOR_PROMPT_LEADIN,
                    SYNTHESIS_PROMPT_MARKER,
                )

                def _fake(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
                    if SYNTHESIS_PROMPT_MARKER in prompt:
                        _self.moderator_calls += 1
                        return _result(model, stdout="FINAL SYNTHESIS: ship idea-A.")
                    if MODERATOR_PROMPT_LEADIN in prompt:
                        _self.moderator_calls += 1
                        return _result(model, stdout="Summary.\nDECISION: STOP")
                    _self.persona_calls += 1
                    if round_no >= 2:
                        return _result(model, stdout="")  # dead from round 2
                    return _result(model, stdout=f"idea from {model} r{round_no}")

                panel.resolve_backend = lambda _m: _fake
                return _self

            stub.__enter__ = _enter_stop_happy  # type: ignore[method-assign]
            with stub, redirect_stdout(buf):
                rc = mode_brainstorm(
                    "topic",
                    ["m1", "m2", "m3"],
                    REPO_ROOT,
                    5,
                    ["mod"],
                    rounds=5,
                    max_rounds=8,
                )
        finally:
            if old_log is None:
                os.environ.pop("REVIEW_LOG_DIR", None)
            else:
                os.environ["REVIEW_LOG_DIR"] = old_log
    out = buf.getvalue()
    # The run still completes with a synthesis over the REAL round 1 (rc 0) — not a dead-panel
    # abort (round 1 was productive) and not a hollow synthesis.
    assert rc == 0, (
        f"a productive round 1 + dead round 2 must synthesize and exit 0, got {rc}"
    )
    assert "# Final synthesis" in out
    assert "idea from m1 r1" in out, (
        "the real round-1 ideas must be in the synthesized output"
    )
    # Crucially, the loop stopped on the dead round 2, NOT after a moderator STOP-over-dead —
    # only round 1's persona+moderator and round 2's dead personas ran.
    assert "# Round 3" not in out, (
        "the dead round must end the loop, not let STOP run more rounds"
    )


def test_dead_panel_writes_partial_discussion_log():
    """The dead round we DID run must be on disk (diagnosable + resumable), and the log must
    record the abort — not silently vanish."""
    with tempfile.TemporaryDirectory() as tmp:
        old_log = os.environ.get("REVIEW_LOG_DIR")
        os.environ["REVIEW_LOG_DIR"] = tmp
        try:
            with _StubBackends("dead"), redirect_stdout(io.StringIO()):
                mode_brainstorm(
                    "topic",
                    ["m1", "m2", "m3"],
                    REPO_ROOT,
                    5,
                    ["mod"],
                    rounds=5,
                    max_rounds=8,
                )
            logs = list(Path(tmp).glob("*brainstorm.md"))
            assert logs, "no discussion log written"
            body = logs[0].read_text()
            assert "# Round 1" in body, "the dead round must be logged for diagnosis"
            assert "ABORTED: dead panel" in body, "the log must record the abort"
            # The dead round must carry NO nonce'd structural round sentinel — otherwise the
            # session parser counts it as a completed round and a resume skips it (codex P2).
            assert "review:round 1 nonce=" not in body, (
                "a dead round must NOT write a structural round sentinel (it would be counted "
                "as completed and skipped on resume)"
            )
        finally:
            if old_log is None:
                os.environ.pop("REVIEW_LOG_DIR", None)
            else:
                os.environ["REVIEW_LOG_DIR"] = old_log


def test_dead_panel_log_has_zero_completed_rounds_for_resume():
    """REGRESSION (codex P2): a dead-panel discussion log must parse as having ZERO completed
    rounds, so `review sessions --resume` re-runs the dead round (round 1) instead of seeding
    its '(no output)' transcript and continuing from round 2. Exercises the REAL session parser
    against the REAL log the abort writes."""
    import reviewlib.sessions as sessions

    with tempfile.TemporaryDirectory() as tmp:
        old_log = os.environ.get("REVIEW_LOG_DIR")
        os.environ["REVIEW_LOG_DIR"] = tmp
        try:
            with _StubBackends("dead"), redirect_stdout(io.StringIO()):
                mode_brainstorm(
                    "topic",
                    ["m1", "m2", "m3"],
                    REPO_ROOT,
                    5,
                    ["mod"],
                    rounds=5,
                    max_rounds=8,
                )
            logs = list(Path(tmp).glob("*brainstorm.md"))
            assert logs, "no discussion log written"
            session = sessions.parse_log(logs[0])
            assert session.completed_rounds == 0, (
                f"dead-panel log parsed as {session.completed_rounds} completed rounds — a "
                "resume would skip the dead round instead of re-running it"
            )
        finally:
            if old_log is None:
                os.environ.pop("REVIEW_LOG_DIR", None)
            else:
                os.environ["REVIEW_LOG_DIR"] = old_log


def test_dead_panel_run_stats_count_dead_seats_as_failures():
    """REGRESSION (codex P2): a rc=0 EMPTY dead panel must record fail counts, not `ok=N`.
    `run_panel` auto-tallies by exit code, so a dead-but-rc0 round would otherwise log as all
    ok and poison the ETA average. The abort path calls `recount_round_by_usability`, so the
    active tally reflects `result_is_usable`. Exercises the REAL tally (begin/end_call_tally)
    around a real dead-panel brainstorm run."""
    import reviewlib.panel as panel

    with tempfile.TemporaryDirectory() as tmp:
        old_log = os.environ.get("REVIEW_LOG_DIR")
        os.environ["REVIEW_LOG_DIR"] = tmp
        panel.begin_call_tally()
        try:
            with _StubBackends("dead"), redirect_stdout(io.StringIO()):
                mode_brainstorm(
                    "topic",
                    ["m1", "m2", "m3"],
                    REPO_ROOT,
                    5,
                    ["mod"],
                    rounds=5,
                    max_rounds=8,
                )
        finally:
            tally = panel.end_call_tally()
            if old_log is None:
                os.environ.pop("REVIEW_LOG_DIR", None)
            else:
                os.environ["REVIEW_LOG_DIR"] = old_log
    # 3 dead persona seats -> all counted as fail, none as ok.
    assert tally["ok"] == 0, f"dead seats wrongly counted as ok: {tally}"
    assert tally["fail"] == 3, f"expected 3 dead seats counted as fail: {tally}"


def test_recount_round_by_usability_reclassifies_only_dead_rc0_seats():
    """Unit: `recount_round_by_usability` moves rc=0-but-empty seats ok->fail and leaves a
    genuinely-usable rc=0 seat and an already-failed (rc!=0) seat alone."""
    import reviewlib.panel as panel

    panel.begin_call_tally()
    # Simulate run_panel's auto-tally: 2 rc=0 seats counted ok, 1 rc=1 seat counted fail.
    panel._tally_result(0)
    panel._tally_result(0)
    panel._tally_result(1)
    results = [
        _result("a", stdout="real verdict here"),  # usable -> stays ok
        _result("b", stdout=""),  # rc0 empty -> ok->fail
        _result("c", returncode=1),  # already fail -> untouched
    ]
    panel.recount_round_by_usability(results)
    tally = panel.end_call_tally()
    assert tally == {"ok": 1, "fail": 2, "prompt_tokens": 0, "output_tokens": 0}, tally


def test_recount_is_a_noop_with_no_active_tally():
    """The docstring advertises a no-op outside a CLI run (no active tally). Calling it
    without `begin_call_tally` must not raise and must not create state."""
    import reviewlib.panel as panel

    # Ensure no active tally (a prior test may have left one closed already; end is safe).
    panel.end_call_tally()
    # No exception, and nothing to assert on state (there is none) — the contract is "safe".
    panel.recount_round_by_usability([_result("a", stdout="")])


def test_recount_never_inflates_the_total_when_no_ok_to_move():
    """A reclassification must keep ok+fail == calls-made: never invent a fail when there
    is no ok left to move (a corrupted/empty tally must not be made to LIE upward). Here the
    tally starts with ZERO ok (nothing run_panel counted ok) but the round has a dead rc=0
    seat — recount must leave the total untouched, not push fail past the real call count."""
    import reviewlib.panel as panel

    panel.begin_call_tally()
    panel._tally_result(1)  # one real failure; ok stays 0
    results = [_result("a", stdout="")]  # rc0 empty -> would reclassify, but ok==0
    panel.recount_round_by_usability(results)
    tally = panel.end_call_tally()
    # ok+fail must still equal the 1 call actually made — no phantom fail.
    assert tally["ok"] + tally["fail"] == 1, tally
    assert tally == {"ok": 0, "fail": 1, "prompt_tokens": 0, "output_tokens": 0}, tally


# === codex review finding (2026-08 seat-cooldown feature): a cached-skip sentinel
# (rc=0, non-empty "is currently unavailable" body) is NOT the same failure shape
# `_StubBackends` above models ("dead" = rc0/empty, "error" = rc!=0) — it needs its
# own stub so a persona/moderator can return that THIRD shape specifically. =========
_SENTINEL_BODY = (
    "claude:claude-fable-5 is currently unavailable (cached: session limit)."
)


def test_normal_round_with_one_sentinel_persona_corrects_the_tally():
    """A round with 2 usable personas + 1 cached-cooldown sentinel is NOT a dead round
    (`_round_is_dead` needs a STRICT MAJORITY unusable — 1-of-3 stays alive), so the
    old dead-round-only `recount_round_by_usability` call never ran for it: the
    sentinel seat stayed counted `ok` by `run_panel`'s bare exit-code auto-tally,
    contradicting the sentinel contract the rest of this feature enforces. Pins that
    the tally is now corrected for every round, not only a dead one."""
    import reviewlib.panel as panel

    def _fake_backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
        from reviewlib.modes.brainstorm import (
            MODERATOR_PROMPT_LEADIN,
            SYNTHESIS_PROMPT_MARKER,
        )

        if SYNTHESIS_PROMPT_MARKER in prompt or MODERATOR_PROMPT_LEADIN in prompt:
            return _result(model, stdout="Moderator summary.\nDECISION: STOP")
        if model == "m3":
            return _result(model, stdout=_SENTINEL_BODY)  # rc=0, non-empty, sentinel
        return _result(model, stdout=f"idea from {model} r{round_no}")

    old_resolve = panel.resolve_backend
    panel.resolve_backend = lambda _model: _fake_backend
    with tempfile.TemporaryDirectory() as tmp:
        old_log = os.environ.get("REVIEW_LOG_DIR")
        os.environ["REVIEW_LOG_DIR"] = tmp
        panel.begin_call_tally()
        try:
            with redirect_stdout(io.StringIO()):
                rc = mode_brainstorm(
                    "topic",
                    ["m1", "m2", "m3"],
                    REPO_ROOT,
                    5,
                    ["mod"],
                    rounds=1,
                    max_rounds=1,
                )
        finally:
            tally = panel.end_call_tally()
            panel.resolve_backend = old_resolve
            if old_log is None:
                os.environ.pop("REVIEW_LOG_DIR", None)
            else:
                os.environ["REVIEW_LOG_DIR"] = old_log
    assert rc == 0, rc  # the round itself succeeded (2/3 usable, not dead)
    # 2 real personas ok, 1 sentinel persona reclassified fail, moderator+synthesis ok.
    assert tally["fail"] >= 1, tally
    assert tally["ok"] + tally["fail"] >= 3, tally


def test_moderator_sentinel_result_does_not_get_promoted_or_reported_as_success():
    """codex review finding: `mode_brainstorm`'s own moderator-promotion (`if
    mod_result.returncode == 0`) and final-exit-code check (`synth.returncode == 0`)
    used to accept a cached-cooldown sentinel exactly like a real answer — a bare
    returncode check, not `result_is_usable`. Pins the end-to-end fix: when EVERY
    moderator candidate returns the sentinel shape, the brainstorm reports FAILURE
    (not a silent 0-exit success with the cache notice standing in for real
    synthesis) — `run_moderator`'s own fallback already covers the "not promoted"
    half; this covers `mode_brainstorm`'s consumption of that result."""
    import reviewlib.panel as panel

    def _fake_backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
        from reviewlib.modes.brainstorm import (
            MODERATOR_PROMPT_LEADIN,
            SYNTHESIS_PROMPT_MARKER,
        )

        if SYNTHESIS_PROMPT_MARKER in prompt or MODERATOR_PROMPT_LEADIN in prompt:
            return _result(
                model, stdout=_SENTINEL_BODY
            )  # every moderator call cooling down
        return _result(model, stdout=f"idea from {model} r{round_no}")

    old_resolve = panel.resolve_backend
    panel.resolve_backend = lambda _model: _fake_backend
    with tempfile.TemporaryDirectory() as tmp:
        old_log = os.environ.get("REVIEW_LOG_DIR")
        os.environ["REVIEW_LOG_DIR"] = tmp
        try:
            with redirect_stdout(io.StringIO()):
                rc = mode_brainstorm(
                    "topic",
                    ["m1", "m2"],
                    REPO_ROOT,
                    5,
                    ["mod"],
                    rounds=1,
                    max_rounds=1,
                )
        finally:
            panel.resolve_backend = old_resolve
            if old_log is None:
                os.environ.pop("REVIEW_LOG_DIR", None)
            else:
                os.environ["REVIEW_LOG_DIR"] = old_log
    assert rc == 1, rc  # NOT a hollow success


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
