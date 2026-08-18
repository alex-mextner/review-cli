#!/usr/bin/env python3
"""Unit tests for the operator-facing "panel/board padded" stderr notices and
the quorum duplicate-seat disclosure (reviewlib.cli._warn_if_panel_padded,
reviewlib.modes.review._warn_if_board_reused, reviewlib.modes.quorum's
`<model>#N` labelling + moderator note).

Covers, offline (no model call, no network):
  (a) the panel/board warnings fire iff a repeat is genuinely REUSE, not just
      "the output happens to contain a duplicate model" — a config board that
      legitimately lists the same model under two distinct roles must NOT
      false-positive (Fable/k3 review finding, review-cli#205 round 2);
  (b) quorum labels repeated models `<model>#1`/`<model>#2` in the transcript
      and adds a moderator disclosure note, so duplicate seats read as ONE
      opinion, not two independent ones.

Plain-script harness (mirrors tests/test_pool_reuse.py): each test_* is run
by __main__, and also pytest-discoverable.
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stderr
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib.backends import ReviewResult  # noqa: E402
from reviewlib.cli import _warn_if_panel_padded  # noqa: E402
from reviewlib.config import BoardReviewer  # noqa: E402
from reviewlib.modes import quorum as q_mod  # noqa: E402
from reviewlib.modes.review import _warn_if_board_reused  # noqa: E402

REPO_DIR = REPO_ROOT


def _captured_stderr(fn, *args) -> str:
    buf = io.StringIO()
    with redirect_stderr(buf):
        fn(*args)
    return buf.getvalue()


def test_panel_padded_warning_fires_on_real_reuse():
    out = _captured_stderr(_warn_if_panel_padded, ["fable", "glm", "fable", "glm"])
    assert "panel padded" in out
    assert "fable x2" in out


def test_panel_padded_warning_silent_when_all_distinct():
    out = _captured_stderr(_warn_if_panel_padded, ["fable", "glm", "sol"])
    assert out == ""


def test_board_reused_warning_fires_on_real_padding():
    board = [
        BoardReviewer("fable", "architect", "Fable"),
        BoardReviewer("glm", "performance", "GLM"),
    ]
    # A genuine padded pool: the second "Fable" seat is a NEW object (as
    # select_pool_with_reuse's `replace()` extras always are), not one of
    # `board`'s own seat objects.
    from dataclasses import replace

    pool = [board[0], replace(board[0], role="security")]
    out = _captured_stderr(_warn_if_board_reused, board, pool)
    assert "board padded" in out
    assert (
        "fable x1" in out or "fable x2" in out
    )  # exact count phrasing, either is fine


def test_board_reused_warning_silent_on_legit_duplicate_model_board():
    # A config board that legitimately lists the SAME model under two
    # DIFFERENT roles as two DISTINCT seats — no reuse happened, the pool is
    # just both of those original seats verbatim.
    board = [
        BoardReviewer("fable", "architect", "Fable-A"),
        BoardReviewer("fable", "security", "Fable-B"),
    ]
    pool = list(board)  # both ORIGINAL objects, unchanged — not a replace() copy
    out = _captured_stderr(_warn_if_board_reused, board, pool)
    assert out == ""


def test_board_reused_warning_silent_when_pool_equals_board():
    board = [
        BoardReviewer("fable", "architect", "Fable"),
        BoardReviewer("sol", "consistency", "Sol"),
    ]
    out = _captured_stderr(_warn_if_board_reused, board, list(board))
    assert out == ""


# --- quorum's <model>#N labelling + moderator disclosure --------------------------


def test_quorum_labels_duplicate_seats_and_notes_them_to_the_moderator():
    captured: dict = {}

    def _fake_run_panel(jobs, cwd, timeout):
        captured["labels"] = [j.label for j in jobs]
        return [
            ReviewResult(
                model=j.label or j.model,
                command="fake",
                returncode=0,
                stdout=f"answer from {j.label or j.model}",
                stderr="",
            )
            for j in jobs
        ]

    def _fake_run_moderator(candidates, prompt, cwd, timeout, diff="", round_no=0):
        captured["mod_prompt"] = prompt
        return ReviewResult(
            model=candidates[0],
            command="fake",
            returncode=0,
            stdout="summary",
            stderr="",
        )

    saved_panel, saved_mod = q_mod.run_panel, q_mod.run_moderator
    q_mod.run_panel = _fake_run_panel
    q_mod.run_moderator = _fake_run_moderator
    try:
        rc = q_mod.mode_quorum(
            "question",
            ["fable", "glm", "fable"],  # fable padded in twice
            "",
            REPO_DIR,
            5,
            ["mod"],
        )
    finally:
        q_mod.run_panel = saved_panel
        q_mod.run_moderator = saved_mod
    assert rc == 0, rc
    # glm has no duplicate, so its PanelJob.label stays None (run_panel falls
    # back to job.model for the transcript) -- only the repeated fable seats
    # get an explicit "#N" label.
    assert captured["labels"] == ["fable#1", None, "fable#2"]
    assert "### Expert: fable#1" in captured["mod_prompt"]
    assert "### Expert: fable#2" in captured["mod_prompt"]
    assert "fable#1" in captured["mod_prompt"]
    assert "fable#2" in captured["mod_prompt"]
    assert "SINGLE opinion" in captured["mod_prompt"]


def test_quorum_no_labels_or_note_when_models_are_distinct():
    captured: dict = {}

    def _fake_run_panel(jobs, cwd, timeout):
        captured["labels"] = [j.label for j in jobs]
        return [
            ReviewResult(
                model=j.model, command="fake", returncode=0, stdout="ok", stderr=""
            )
            for j in jobs
        ]

    def _fake_run_moderator(candidates, prompt, cwd, timeout, diff="", round_no=0):
        captured["mod_prompt"] = prompt
        return ReviewResult(
            model=candidates[0],
            command="fake",
            returncode=0,
            stdout="summary",
            stderr="",
        )

    saved_panel, saved_mod = q_mod.run_panel, q_mod.run_moderator
    q_mod.run_panel = _fake_run_panel
    q_mod.run_moderator = _fake_run_moderator
    try:
        q_mod.mode_quorum("question", ["fable", "glm"], "", REPO_DIR, 5, ["mod"])
    finally:
        q_mod.run_panel = saved_panel
        q_mod.run_moderator = saved_mod
    assert captured["labels"] == [None, None]
    assert "SINGLE opinion" not in captured["mod_prompt"]


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
