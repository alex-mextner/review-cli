#!/usr/bin/env python3
"""Unit tests for the reviewer board (HYP-741).

A board assigns each reviewer model its OWN role/lens. These tests prove, all
offline (no model call, no network — backends are monkeypatched / forced
unavailable):
  (a) DEFAULT_BOARD matches the directive table byte-exact (model -> role);
  (b) config.yaml `board:` parsing — valid entries, unknown-role fallback to the
      generic prompt (no crash), bad entries skipped, empty/absent -> DEFAULT_BOARD;
  (c) role-lens injection — an available reviewer's PanelJob prompt is
      `base_prompt + lens` and its label is `"<display> [<role>]"`;
  (d) graceful skip — an unavailable reviewer is dropped (not crashed) and surfaced
      in the `skipped` list; an all-unavailable board returns no jobs;
  (e) mode_review board path runs in parallel and returns 0 on success / 1 on a
      failed reviewer, and only an explicit -m disables the board in the CLI.

Runs as a plain script (mirrors tests/test_provider_keys.py): each `test_*` is
invoked by the __main__ block, no pytest required.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import reviewlib.backends as backends  # noqa: E402
from reviewlib.backends import ReviewResult  # noqa: E402
from reviewlib.config import (  # noqa: E402
    DEFAULT_BOARD,
    REVIEW_ROLES,
    BoardReviewer,
    load_board,
)
from reviewlib.panel import build_board_jobs  # noqa: E402

DEFAULT_PROMPT = "Review this diff."


# === DEFAULT_BOARD shape (byte-exact model ids from the directive) ==============
def test_default_board_matches_directive_table():
    expected = [
        ("claude:claude-opus-4-8", "architect", "Opus"),
        ("codex", "correctness", "Codex"),
        ("gemini", "consistency", "Gemini"),
        ("commandcode:deepseek/deepseek-v4-pro", "performance", "DeepSeek"),
        ("commandcode:moonshotai/Kimi-K2.7-Code", "quality", "Kimi"),
        ("commandcode:Qwen/Qwen3.7-Max", "security", "Qwen"),
        ("commandcode:zai-org/GLM-5.1", "tests", "GLM"),
    ]
    got = [(r.model, r.role, r.display) for r in DEFAULT_BOARD]
    assert got == expected, got


def test_every_default_role_has_a_lens():
    for reviewer in DEFAULT_BOARD:
        assert reviewer.role in REVIEW_ROLES, reviewer.role
        assert reviewer.role_lens.strip(), reviewer.role


def test_roles_are_non_overlapping_focus_sentences():
    # Each lens names its own focus word — a cheap guard that they aren't identical.
    focuses = {role: lens.split(":", 1)[0] for role, lens in REVIEW_ROLES.items()}
    assert len(set(focuses.values())) == len(REVIEW_ROLES), focuses


# === config.yaml board parsing ==================================================
def test_no_board_key_falls_back_to_default():
    assert [r.model for r in load_board({})] == [r.model for r in DEFAULT_BOARD]
    assert [r.model for r in load_board({"models": ["codex"]})] == [r.model for r in DEFAULT_BOARD]


def test_empty_or_wrong_typed_board_falls_back_to_default():
    for bad in ([], {"board": []}, {"board": "codex"}, {"board": 42}):
        cfg = bad if isinstance(bad, dict) else {"board": bad}
        assert len(load_board(cfg)) == len(DEFAULT_BOARD), cfg


def test_board_config_overrides_default():
    cfg = {"board": [
        {"model": "codex", "role": "correctness"},
        {"model": "gemini", "role": "security", "name": "G"},
    ]}
    board = load_board(cfg)
    assert len(board) == 2
    assert board[0].model == "codex" and board[0].role == "correctness"
    # Explicit name honored; default name derived from the model tail otherwise.
    assert board[1].display == "G"
    assert board[0].display == "codex"


def test_board_alias_in_model_is_expanded():
    # `glm46` is an alias for `zai:glm-4.6` (config._expand_alias).
    board = load_board({"board": [{"model": "glm46", "role": "tests"}]})
    assert board[0].model == "zai:glm-4.6", board[0].model


def test_unknown_role_keeps_reviewer_with_generic_prompt():
    board = load_board({"board": [{"model": "codex", "role": "made-up-role"}]})
    assert len(board) == 1
    assert board[0].role == "made-up-role"
    # Unknown role -> empty lens -> the job uses the generic prompt (no crash).
    assert board[0].role_lens == ""


def test_bad_entries_are_skipped_not_crashed():
    cfg = {"board": [
        "not-a-mapping",
        {"role": "correctness"},  # missing model
        {"model": "   "},  # blank model
        {"model": "codex", "role": "correctness"},  # the only valid one
    ]}
    board = load_board(cfg)
    assert [r.model for r in board] == ["codex"], [r.model for r in board]


def test_role_omitted_is_general_with_generic_prompt():
    board = load_board({"board": [{"model": "codex"}]})
    assert board[0].role == ""
    assert board[0].role_lens == ""


# === role-lens injection into PanelJobs =========================================
class _AvailabilityPatch:
    """Force backend_available to a fixed predicate so the board path is offline."""

    def __init__(self, available_models: set[str]):
        self._available = available_models

    def __enter__(self):
        self._old = backends.backend_available

        def _fake(model: str) -> bool:
            return model in self._available

        backends.backend_available = _fake
        # build_board_jobs imported the name into reviewlib.panel; patch there too.
        import reviewlib.panel as panel

        self._old_panel = panel.backend_available
        panel.backend_available = _fake
        return self

    def __exit__(self, *exc):
        backends.backend_available = self._old
        import reviewlib.panel as panel

        panel.backend_available = self._old_panel
        return False


def test_build_board_jobs_injects_lens_and_label():
    board = [BoardReviewer("codex", "performance", "Codex")]
    with _AvailabilityPatch({"codex"}):
        jobs, skipped = build_board_jobs(board, DEFAULT_PROMPT, "+x")
    assert skipped == []
    assert len(jobs) == 1
    job = jobs[0]
    assert job.model == "codex"
    assert job.diff == "+x"
    assert job.label == "Codex [performance]"
    # The lens is appended to the base prompt.
    assert job.prompt.startswith(DEFAULT_PROMPT + "\n\n")
    assert REVIEW_ROLES["performance"] in job.prompt


def test_build_board_jobs_unknown_role_uses_generic_prompt():
    board = [BoardReviewer("codex", "", "Codex")]
    with _AvailabilityPatch({"codex"}):
        jobs, _ = build_board_jobs(board, DEFAULT_PROMPT, "")
    assert jobs[0].prompt == DEFAULT_PROMPT  # no lens appended
    assert jobs[0].label == "Codex [general]"


# === graceful skip of unavailable reviewers =====================================
def test_unavailable_reviewers_are_skipped_not_crashed():
    board = list(DEFAULT_BOARD)
    with _AvailabilityPatch({"codex", "gemini"}):  # only two reachable
        jobs, skipped = build_board_jobs(board, DEFAULT_PROMPT, "+x")
    assert {j.model for j in jobs} == {"codex", "gemini"}
    assert {r.model for r in skipped} == {
        r.model for r in board if r.model not in {"codex", "gemini"}
    }
    assert len(jobs) + len(skipped) == len(board)


def test_all_unavailable_board_returns_no_jobs():
    with _AvailabilityPatch(set()):
        jobs, skipped = build_board_jobs(list(DEFAULT_BOARD), DEFAULT_PROMPT, "+x")
    assert jobs == []
    assert len(skipped) == len(DEFAULT_BOARD)


# === mode_review board path (parallel run, exit codes) ==========================
def test_mode_review_board_runs_and_succeeds():
    from reviewlib.modes import review as review_mod

    calls: list[tuple[str, str]] = []

    def _fake_backend(model, prompt, diff, cwd, timeout):
        calls.append((model, prompt))
        return ReviewResult(model=model, command="fake", returncode=0, stdout="ok", stderr="")

    board = [
        BoardReviewer("codex", "correctness", "Codex"),
        BoardReviewer("gemini", "consistency", "Gemini"),
    ]
    old_resolve = review_mod.resolve_backend
    review_mod.resolve_backend = lambda _m: _fake_backend
    # also patch the one build_board_jobs uses (reviewlib.panel.resolve_backend)
    import reviewlib.panel as panel

    old_panel_resolve = panel.resolve_backend
    panel.resolve_backend = lambda _m: _fake_backend
    try:
        with _AvailabilityPatch({"codex", "gemini"}):
            rc = review_mod.mode_review([], DEFAULT_PROMPT, "+x", REPO_ROOT, 5, False, board=board)
    finally:
        review_mod.resolve_backend = old_resolve
        panel.resolve_backend = old_panel_resolve
    assert rc == 0, rc
    # Each reviewer got a role-lensed prompt.
    prompts = {model: prompt for model, prompt in calls}
    assert REVIEW_ROLES["correctness"] in prompts["codex"]
    assert REVIEW_ROLES["consistency"] in prompts["gemini"]


def test_mode_review_board_fails_when_a_reviewer_fails():
    from reviewlib.modes import review as review_mod

    def _fake_backend(model, prompt, diff, cwd, timeout):
        rc = 0 if model == "codex" else 1
        return ReviewResult(model=model, command="fake", returncode=rc, stdout="x", stderr="boom")

    board = [
        BoardReviewer("codex", "correctness", "Codex"),
        BoardReviewer("gemini", "consistency", "Gemini"),
    ]
    import reviewlib.panel as panel

    old_panel_resolve = panel.resolve_backend
    panel.resolve_backend = lambda _m: _fake_backend
    try:
        with _AvailabilityPatch({"codex", "gemini"}):
            rc = review_mod.mode_review([], DEFAULT_PROMPT, "+x", REPO_ROOT, 5, False, board=board)
    finally:
        panel.resolve_backend = old_panel_resolve
    assert rc == 1, rc


def test_mode_review_board_with_no_available_reviewers_returns_1():
    from reviewlib.modes import review as review_mod

    with _AvailabilityPatch(set()):
        rc = review_mod.mode_review([], DEFAULT_PROMPT, "+x", REPO_ROOT, 5, False, board=list(DEFAULT_BOARD))
    assert rc == 1, rc


# === CLI wiring: explicit -m disables the board, --no-board too =================
def test_cli_explicit_models_disable_board():
    """An explicit -m must run the flat legacy panel (board=None), NOT the board."""
    from reviewlib import cli

    captured: dict = {}

    def _fake_mode_review(models, prompt, diff, cwd, timeout, staged, board=None):
        captured["models"] = models
        captured["board"] = board
        return 0

    old = cli.mode_review
    cli.mode_review = _fake_mode_review
    # Avoid touching a real config file / git diff: feed the diff via stdin and
    # point the env file at nothing so no provider key resolves.
    old_stdin = sys.stdin
    try:
        os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
        import io

        sys.stdin = io.StringIO("+added line\n")
        # explicit -m codex -> board must be None
        cli.main(["-m", "codex", "-C", str(REPO_ROOT)])
        assert captured["board"] is None, captured["board"]
        assert captured["models"] == ["codex"], captured["models"]
        # --no-board with no -m -> still None, models = the legacy default/config list
        sys.stdin = io.StringIO("+added line\n")
        cli.main(["--no-board", "-C", str(REPO_ROOT)])
        assert captured["board"] is None, captured["board"]
    finally:
        cli.mode_review = old
        sys.stdin = old_stdin


def test_cli_default_path_activates_board():
    """No -m, no --no-board -> the board is passed into mode_review."""
    from reviewlib import cli

    captured: dict = {}

    def _fake_mode_review(models, prompt, diff, cwd, timeout, staged, board=None):
        captured["board"] = board
        return 0

    old = cli.mode_review
    cli.mode_review = _fake_mode_review
    # Pin the board to DEFAULT_BOARD so the test is independent of the dev
    # machine's ~/.config/review-cli/config.yaml.
    old_load_board = cli.load_board
    cli.load_board = lambda _cfg: list(DEFAULT_BOARD)
    old_stdin = sys.stdin
    try:
        os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
        import io

        sys.stdin = io.StringIO("+added line\n")
        cli.main(["-C", str(REPO_ROOT)])
        assert captured["board"] is not None, "board should be active by default"
        assert len(captured["board"]) == len(DEFAULT_BOARD)
    finally:
        cli.mode_review = old
        cli.load_board = old_load_board
        sys.stdin = old_stdin


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
