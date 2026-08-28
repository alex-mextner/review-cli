#!/usr/bin/env python3
"""Unit tests for `reviewlib.modes.review._report_pool_shortfall` — the STDOUT notice
that names every unavailable board seat whenever the live pool comes up short of the
requested `pool_size`.

WHY this exists: `_mode_review_board`'s pre-dispatch pool selection
(`select_pool_and_reserve_with_reuse`) silently returns fewer seats than requested when
enough higher-priority board seats are unavailable and the usage-limit-based reuse
padding doesn't apply (it only compensates near-usage-limit exclusion, not raw
unavailability — see `select_pool_with_reuse`'s docstring). Before this notice, that
shrink had ZERO visible signal anywhere an agent or human would actually see it: not in
stdout (what gets pasted into a PR/TG report), and the existing `outcome.degraded`
stderr warning only covers a DIFFERENT, rarer case (reserve exhausted DURING dispatch,
after the pool already started running). Root-caused 2026-08-28 (Alex: "где glm5.2?!" —
a report said "reviewed via codex + Fable" with no mention that Opus/GLM were
configured but silently dropped).

Offline (no model call, no network — `backend_unavailable_reason` is monkeypatched).
Plain-script harness (mirrors tests/test_reuse_warnings.py): each test_* is run by
__main__, and also pytest-discoverable.
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib.config import (  # noqa: E402
    BoardReviewer,
    select_pool_and_reserve_with_reuse,
)
from reviewlib.modes import review as review_mod  # noqa: E402


def _captured_stdout(fn, *args, **kwargs) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*args, **kwargs)
    return buf.getvalue()


def _patched_reason(reasons: dict[str, str | None]):
    """try/finally monkeypatch of the module-level name `_report_pool_shortfall` calls
    (NOT the pytest `monkeypatch` fixture — mirrors tests/test_reviewer_board.py's
    manual-patch pattern so this runs unchanged under the standalone __main__ runner)."""
    saved = review_mod.backend_unavailable_reason
    review_mod.backend_unavailable_reason = lambda model: reasons.get(model)
    return saved


def test_shortfall_warning_fires_and_names_the_missing_seat():
    board = [
        BoardReviewer("claude:claude-opus-4-8", "correctness", "Opus"),
        BoardReviewer("codex:gpt-5.6-terra", "consistency", "Codex"),
        BoardReviewer("claude:claude-fable-5", "architect", "Fable"),
    ]
    pool = [board[0], board[2]]  # Codex silently dropped
    reserve: list[BoardReviewer] = []
    saved = _patched_reason(
        {"codex:gpt-5.6-terra": "codex: `codex` CLI not found on PATH"}
    )
    try:
        out = _captured_stdout(
            review_mod._report_pool_shortfall,
            board,
            pool,
            reserve=reserve,
            pool_size=3,
        )
    finally:
        review_mod.backend_unavailable_reason = saved
    assert "pool came up short" in out
    assert "requested 3" in out and "only 2 live" in out
    assert "Codex (codex:gpt-5.6-terra)" in out
    assert "CLI not found on PATH" in out
    # Seats that DID run must not be listed as missing.
    assert "Opus (" not in out
    assert "Fable (" not in out


def test_shortfall_warning_silent_when_pool_meets_target():
    board = [
        BoardReviewer("claude:claude-opus-4-8", "correctness", "Opus"),
        BoardReviewer("claude:claude-fable-5", "architect", "Fable"),
    ]
    out = _captured_stdout(
        review_mod._report_pool_shortfall, board, board, reserve=[], pool_size=2
    )
    assert out == ""


def test_shortfall_warning_silent_in_all_seats_mode_when_the_whole_board_ran():
    # pool_size <= 0 means "run every available seat" — silent iff the pool actually
    # covers the whole board (the real "no shortfall" case for that mode).
    board = [BoardReviewer("claude:claude-opus-4-8", "correctness", "Opus")]
    for pool_size in (0, -1):
        out = _captured_stdout(
            review_mod._report_pool_shortfall,
            board,
            board,
            reserve=[],
            pool_size=pool_size,
        )
        assert out == "", (pool_size, out)


def test_shortfall_warning_fires_in_all_seats_mode_when_a_seat_is_missing():
    """`pool_size <= 0` ("all seats") must NOT be treated as "no shortfall concept
    applies" — the real target in that mode is `len(board)`. Without this, a `--pool 0`
    run with a seat down reproduces the exact silent-shrink incident this notice exists
    to kill, just under the "all seats" configuration (k3 review finding, round 2)."""
    board = [
        BoardReviewer("claude:claude-opus-4-8", "correctness", "Opus"),
        BoardReviewer("codex:gpt-5.6-terra", "consistency", "Codex"),
    ]
    pool = [board[0]]  # Codex down, no reserve to backfill it
    saved = _patched_reason(
        {"codex:gpt-5.6-terra": "codex: `codex` CLI not found on PATH"}
    )
    try:
        out = _captured_stdout(
            review_mod._report_pool_shortfall, board, pool, reserve=[], pool_size=0
        )
    finally:
        review_mod.backend_unavailable_reason = saved
    assert "requested 2 seats" in out, out
    assert "Codex (codex:gpt-5.6-terra)" in out


def test_shortfall_warning_does_not_list_a_healthy_reserve_seat():
    """A seat sitting in `reserve` (healthy, just outranked by priority) must NOT be
    reported as a gap — only seats absent from BOTH `pool` and `reserve` are genuinely
    unavailable."""
    board = [
        BoardReviewer("claude:claude-opus-4-8", "correctness", "Opus"),
        BoardReviewer("codex:gpt-5.6-terra", "consistency", "Codex"),
        BoardReviewer("omp:kimi-code/k3", "general", "k3"),
    ]
    pool = [board[0]]  # only Opus ran
    reserve = [board[1]]  # Codex is healthy reserve, just not needed
    saved = _patched_reason({"omp:kimi-code/k3": "omp: `omp` CLI not found on PATH"})
    try:
        out = _captured_stdout(
            review_mod._report_pool_shortfall,
            board,
            pool,
            reserve=reserve,
            pool_size=2,
        )
    finally:
        review_mod.backend_unavailable_reason = saved
    assert "Codex" not in out  # healthy reserve, not a gap
    assert "k3 (omp:kimi-code/k3)" in out  # genuinely unavailable


def test_shortfall_warning_clamps_requested_to_board_size():
    """`--pool 8` against a 4-seat board with 1 seat down must NOT print "requested 8,
    only 3 live -- 1 unavailable" (5 of the 8 never existed on the board — k3 review
    finding, round 1: an unaccounted-for gap between the raw ask and the board's real
    capacity makes the notice's own arithmetic look broken). It must report against
    what the board could ever deliver: `min(pool_size, len(board))` == 4."""
    board = [
        BoardReviewer("claude:claude-opus-4-8", "correctness", "Opus"),
        BoardReviewer("codex:gpt-5.6-terra", "consistency", "Codex"),
        BoardReviewer("claude:claude-fable-5", "architect", "Fable"),
        BoardReviewer("omp:kimi-code/k3", "general", "k3"),
    ]
    pool = [board[0], board[2], board[3]]  # Codex down, 3 of 4 live
    saved = _patched_reason(
        {"codex:gpt-5.6-terra": "codex: `codex` CLI not found on PATH"}
    )
    try:
        out = _captured_stdout(
            review_mod._report_pool_shortfall, board, pool, reserve=[], pool_size=8
        )
    finally:
        review_mod.backend_unavailable_reason = saved
    assert "requested 4 seats" in out, out
    assert "board has 4" in out, out
    assert "requested 8" not in out


def test_shortfall_warning_wired_to_real_selector_output():
    """End-to-end through the REAL `select_pool_and_reserve_with_reuse` (config.py),
    not a hand-built pool/reserve — the gap all three reviewers flagged: nothing proved
    the selector actually returns original board object references for the pool/reserve
    it hands back, which `_report_pool_shortfall`'s `id()`-identity check depends on. If
    the selector ever started returning `replace()`-copies for every seat instead of the
    originals, this test (unlike the hand-built ones above) would catch it."""
    board = [
        BoardReviewer("claude:claude-opus-4-8", "correctness", "Opus"),
        BoardReviewer("codex:gpt-5.6-terra", "consistency", "Codex"),
        BoardReviewer("claude:claude-fable-5", "architect", "Fable"),
        BoardReviewer("omp:kimi-code/k3", "general", "k3"),
    ]

    def _available(seat: BoardReviewer) -> bool:
        return seat.model != "codex:gpt-5.6-terra" and seat.model != "omp:kimi-code/k3"

    pool, reserve = select_pool_and_reserve_with_reuse(board, 3, _available)
    saved = _patched_reason(
        {
            "codex:gpt-5.6-terra": "codex: `codex` CLI not found on PATH",
            "omp:kimi-code/k3": "omp: `omp` CLI not found on PATH",
        }
    )
    try:
        out = _captured_stdout(
            review_mod._report_pool_shortfall,
            board,
            pool,
            reserve=reserve,
            pool_size=3,
        )
    finally:
        review_mod.backend_unavailable_reason = saved
    # Opus and Fable genuinely ran — must never be reported as missing.
    assert "Opus (" not in out
    assert "Fable (" not in out
    # Codex and k3 were unavailable per the real selector's own decision.
    assert "Codex (codex:gpt-5.6-terra)" in out, out
    assert "k3 (omp:kimi-code/k3)" in out, out


def test_shortfall_notice_fires_through_the_real_mode_review_board_call_site():
    """Locks the CALL SITE, not just the function: every reviewer across this PR's
    review rounds (Opus, Fable, k3, GLM) independently flagged that nothing proved
    `_mode_review_board` actually invokes `_report_pool_shortfall` -- a future refactor
    could drop that one line, move it past the `if not pool: return 1` early exit, or
    guard it behind a condition, and all the direct-call tests above would stay green
    while the feature silently reverts to the pre-fix silence. Drives the real
    `mode_review` -> `_mode_review_board` path with a 2-seat board (one available, one
    not) and a fake dispatch backend (no network/subprocess), and asserts the notice
    text actually reaches stdout."""
    from reviewlib.modes import review as review_mod

    board = [
        BoardReviewer("codex", "correctness", "Codex"),
        BoardReviewer("missing-provider:model", "tests", "Missing"),
    ]

    def _fake_backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
        return review_mod.ReviewResult(
            model=model,
            command="fake",
            returncode=0,
            stdout=f"ok from {model}",
            stderr="",
        )

    import reviewlib.panel as panel_mod

    saved_resolve = panel_mod.resolve_backend
    saved_available = review_mod.backend_available
    saved_reason = review_mod.backend_unavailable_reason
    panel_mod.resolve_backend = lambda _m: _fake_backend
    review_mod.backend_available = lambda model: model == "codex"
    review_mod.backend_unavailable_reason = lambda model: (
        None if model == "codex" else "patched unavailable (test)"
    )
    try:
        out = _captured_stdout(
            review_mod.mode_review,
            [],
            "Review this diff.",
            "+x",
            REPO_ROOT,
            5,
            False,
            board=board,
            pool_size=len(board),
        )
    finally:
        panel_mod.resolve_backend = saved_resolve
        review_mod.backend_available = saved_available
        review_mod.backend_unavailable_reason = saved_reason
    assert "pool came up short" in out, out
    assert "Missing (missing-provider:model)" in out, out
    assert "patched unavailable (test)" in out, out


TESTS = [
    test_shortfall_warning_fires_and_names_the_missing_seat,
    test_shortfall_warning_silent_when_pool_meets_target,
    test_shortfall_warning_silent_in_all_seats_mode_when_the_whole_board_ran,
    test_shortfall_warning_fires_in_all_seats_mode_when_a_seat_is_missing,
    test_shortfall_warning_does_not_list_a_healthy_reserve_seat,
    test_shortfall_warning_clamps_requested_to_board_size,
    test_shortfall_warning_wired_to_real_selector_output,
    test_shortfall_notice_fires_through_the_real_mode_review_board_call_site,
]


if __name__ == "__main__":
    failures = 0
    for test in TESTS:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
    if failures:
        print(f"{failures} failure(s)")
        sys.exit(1)
    print("all tests passed")
