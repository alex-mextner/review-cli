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
    BoardConfigError,
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
        # The tests seat goes DIRECT to z.ai (his GLM subscription), not commandcode.
        ("zai:glm-5.2", "tests", "GLM"),
        # 8th seat: gpt-5.5 on a tight public-API/contracts lens.
        ("commandcode:gpt-5.5", "contracts", "GPT-5.5"),
    ]
    got = [(r.model, r.role, r.display) for r in DEFAULT_BOARD]
    assert got == expected, got


def test_default_board_has_eight_seats():
    assert len(DEFAULT_BOARD) == 8, len(DEFAULT_BOARD)


def test_tests_seat_routes_to_zai_backend_with_glm52():
    """The tests seat must route to the z.ai backend (his subscription), model glm-5.2 —
    NOT the commandcode gateway. resolve_backend(zai:glm-5.2) -> review_zai."""
    seat = next(r for r in DEFAULT_BOARD if r.role == "tests")
    assert seat.model == "zai:glm-5.2", seat.model
    assert backends.resolve_backend(seat.model) is backends.review_zai
    # The backend sends model id glm-5.2 on the wire (suffix after `zai:`).
    assert seat.model.split(":", 1)[1] == "glm-5.2"


def test_contracts_seat_has_a_lens():
    seat = next(r for r in DEFAULT_BOARD if r.role == "contracts")
    assert seat.model == "commandcode:gpt-5.5", seat.model
    assert seat.display == "GPT-5.5"
    assert "contracts" in REVIEW_ROLES
    lens = REVIEW_ROLES["contracts"].lower()
    assert "public api" in lens or "api shape" in lens, lens
    assert "backward" in lens or "compat" in lens, lens


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


# === cost-safety: PRESENT-but-all-malformed `board:` ERRORS (does not silently
# fall back to the paid DEFAULT_BOARD). Absent -> default; partial -> keep valid. ==
def test_present_board_all_malformed_raises_not_silent_default():
    """A non-empty `board:` whose entries are ALL malformed must ERROR, NOT
    silently run the paid 8-model DEFAULT_BOARD."""
    cfgs = [
        {"board": ["not-a-mapping", "still-not"]},   # no mappings at all
        {"board": [{"role": "correctness"}, {"role": "security"}]},  # all missing model
        {"board": [{"model": "   "}, {"model": ""}]},  # all blank models
        {"board": [{"model": 42}, "x", {"no": "model"}]},  # mixed garbage, none usable
    ]
    for cfg in cfgs:
        try:
            load_board(cfg)
        except BoardConfigError:
            pass  # expected
        else:
            raise AssertionError(f"expected BoardConfigError for {cfg!r}")


def test_absent_board_key_uses_default_not_error():
    """Absent `board:` -> DEFAULT_BOARD (no error): no preference was expressed."""
    assert [r.model for r in load_board({})] == [r.model for r in DEFAULT_BOARD]
    # An explicitly empty list is "no preference" too -> default, not an error.
    assert [r.model for r in load_board({"board": []})] == [r.model for r in DEFAULT_BOARD]


def test_partial_malformed_board_keeps_valid_entries_no_error():
    """SOME valid + SOME malformed -> keep the valid ones, warn on bad (no error)."""
    cfg = {"board": [
        "not-a-mapping",
        {"role": "correctness"},          # missing model -> skipped
        {"model": "codex", "role": "correctness"},   # valid
        {"model": "gemini", "role": "security"},     # valid
    ]}
    board = load_board(cfg)
    assert [r.model for r in board] == ["codex", "gemini"], [r.model for r in board]


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

    # round_no is the 6th positional arg run_panel passes to every backend (HYP-742
    # dashboard threading); a board reviewer fake must accept it or run_panel turns the
    # TypeError into an internal 127.
    def _fake_backend(model, prompt, diff, cwd, timeout, round_no=0):
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

    def _fake_backend(model, prompt, diff, cwd, timeout, round_no=0):
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


def test_cli_config_models_beat_board():
    """An explicit `models:` in config.yaml OVERRIDES the default board: the user
    configured exact models, so the paid board must NOT run (cost-safety). The
    precedence is explicit -m > config `models:` > default board."""
    from reviewlib import cli

    captured: dict = {}

    def _fake_mode_review(models, prompt, diff, cwd, timeout, staged, board=None):
        captured["models"] = models
        captured["board"] = board
        return 0

    old = cli.mode_review
    cli.mode_review = _fake_mode_review
    # Inject a config with an explicit `models:` list (and a `board:` too, to prove
    # the board is ignored when models: is configured). load_board must NOT be hit.
    old_load_config = cli.load_config
    cli.load_config = lambda: {"models": ["codex", "gemini"], "board": list(DEFAULT_BOARD)}
    old_load_board = cli.load_board

    def _boom(_cfg):
        raise AssertionError("load_board must not be called when config models: is set")

    cli.load_board = _boom
    old_stdin = sys.stdin
    try:
        os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
        import io

        sys.stdin = io.StringIO("+added line\n")
        cli.main(["-C", str(REPO_ROOT)])
        assert captured["board"] is None, captured["board"]
        # The configured models flow through (alias-expanded, here identity).
        assert captured["models"] == ["codex", "gemini"], captured["models"]
    finally:
        cli.mode_review = old
        cli.load_config = old_load_config
        cli.load_board = old_load_board
        sys.stdin = old_stdin


def test_cli_empty_config_models_does_not_disable_board():
    """An "effectively empty" `models:` — absent, [], or only blank/whitespace
    entries — is NOT a real preference, so it must NOT disable the board NOR feed
    blank model names to the panel (codex P2). The board still runs in every case."""
    from reviewlib import cli

    for empty_models in ([], ["", "  ", "\t"]):
        captured: dict = {}

        def _fake_mode_review(models, prompt, diff, cwd, timeout, staged, board=None):
            captured["board"] = board
            captured["models"] = models
            return 0

        old = cli.mode_review
        cli.mode_review = _fake_mode_review
        old_load_config = cli.load_config
        cli.load_config = lambda em=empty_models: {"models": em}
        old_load_board = cli.load_board
        cli.load_board = lambda _cfg: list(DEFAULT_BOARD)
        old_stdin = sys.stdin
        try:
            os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
            import io

            sys.stdin = io.StringIO("+added line\n")
            cli.main(["-C", str(REPO_ROOT)])
            assert captured["board"] is not None, f"{empty_models!r} must not disable the board"
            # And no blank model name leaked into the (unused) flat models list.
            assert all(m.strip() for m in captured["models"]), captured["models"]
        finally:
            cli.mode_review = old
            cli.load_config = old_load_config
            cli.load_board = old_load_board
            sys.stdin = old_stdin


def test_cli_all_malformed_board_errors_nonzero():
    """A present-but-all-malformed `board:` makes the CLI exit non-zero with a
    message, not silently run the paid default board."""
    from reviewlib import cli

    called = {"mode_review": False}

    def _fake_mode_review(*_a, **_k):
        called["mode_review"] = True
        return 0

    old = cli.mode_review
    cli.mode_review = _fake_mode_review
    old_load_config = cli.load_config
    cli.load_config = lambda: {"board": ["not-a-mapping", {"role": "x"}]}
    old_stdin = sys.stdin
    try:
        os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
        import io

        sys.stdin = io.StringIO("+added line\n")
        rc = cli.main(["-C", str(REPO_ROOT)])
        assert rc != 0, rc
        assert called["mode_review"] is False, "must not run the panel on a bad board"
    finally:
        cli.mode_review = old
        cli.load_config = old_load_config
        sys.stdin = old_stdin


def test_cli_standalone_visual_ignores_malformed_board():
    """(codex P2) Standalone `review --visual image` (no diff) does NOT use the reviewer
    board, so a present-but-malformed `board:` must NOT block it — board validation runs
    only on the board path, after the standalone pipeline has had its chance."""
    from reviewlib import cli
    import reviewlib.features.visual.visual_cli as visual_cli

    reached = {"standalone": False}

    def _fake_standalone(*_a, **_k):
        reached["standalone"] = True
        return 0

    old_standalone = visual_cli.run_visual_standalone
    visual_cli.run_visual_standalone = _fake_standalone
    old_load_config = cli.load_config
    cli.load_config = lambda: {"board": ["not-a-mapping"]}  # malformed board, irrelevant here
    old_stdin = sys.stdin
    try:
        os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
        import io

        # No piped diff and the cwd is not inside a diff-producing repo path we control;
        # _git_diff degrades to "" for --visual, so this routes to the standalone pipeline.
        sys.stdin = io.StringIO("")
        rc = cli.main(["--visual", "/tmp/does-not-exist-zzz.png", "-C", "/tmp"])
        assert rc == 0, rc
        assert reached["standalone"] is True, "standalone visual must run despite the malformed board"
    finally:
        visual_cli.run_visual_standalone = old_standalone
        cli.load_config = old_load_config
        sys.stdin = old_stdin


def test_cli_all_malformed_board_fails_before_visual_fanout():
    """(codex P2) On the default-review path, an all-malformed `board:` must error
    BEFORE the --visual fan-out, so a doomed config never spends a paid vision call."""
    from reviewlib import cli
    import reviewlib.features.visual.compose as compose

    fanout_calls = {"n": 0}

    def _fake_build(*_a, **_k):
        fanout_calls["n"] += 1
        raise AssertionError("visual fan-out must not run when the board config is invalid")

    old_mode_review = cli.mode_review
    cli.mode_review = lambda *_a, **_k: 0
    old_load_config = cli.load_config
    cli.load_config = lambda: {"board": ["not-a-mapping"]}
    old_build = compose.build_mode_visual_context
    compose.build_mode_visual_context = _fake_build
    old_stdin = sys.stdin
    try:
        os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
        import io

        sys.stdin = io.StringIO("+added line\n")
        rc = cli.main(["--visual", "/tmp/does-not-exist-zzz.png", "-C", str(REPO_ROOT)])
        assert rc == 2, rc
        assert fanout_calls["n"] == 0, "fan-out ran before the board was validated"
    finally:
        cli.mode_review = old_mode_review
        cli.load_config = old_load_config
        compose.build_mode_visual_context = old_build
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
    # Pin the board to DEFAULT_BOARD AND stub load_config to an empty dict so the
    # test is independent of the dev machine's ~/.config/review-cli/config.yaml —
    # which DOES set `models:`, and a configured `models:` now (correctly) disables
    # the board. The true default path has neither -m nor config models nor board.
    old_load_board = cli.load_board
    cli.load_board = lambda _cfg: list(DEFAULT_BOARD)
    old_load_config = cli.load_config
    cli.load_config = lambda: {}
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
        cli.load_config = old_load_config
        sys.stdin = old_stdin


def test_cli_list_defaults_reports_normalized_config_models(capfd=None):
    """(codex P3) `--list-defaults` must report the SAME normalized models the review
    path uses: comma-joined entries split, blanks dropped, aliases expanded."""
    import io
    import contextlib

    from reviewlib import cli

    old_load_config = cli.load_config
    cli.load_config = lambda: {"models": ["codex, fable5", "  ", ""]}
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["--list-defaults"])
        out = buf.getvalue().strip().splitlines()
    finally:
        cli.load_config = old_load_config
    assert rc == 0, rc
    # "codex, fable5" splits into two; blanks dropped; fable5 -> claude:claude-fable-5.
    assert out == ["codex", "claude:claude-fable-5"], out


def test_cli_list_defaults_empty_config_uses_code_defaults():
    """An effectively-empty config `models:` -> --list-defaults shows the code DEFAULT_MODELS."""
    import io
    import contextlib

    from reviewlib import cli
    from reviewlib.config import DEFAULT_MODELS, _expand_alias

    old_load_config = cli.load_config
    cli.load_config = lambda: {"models": ["", "  "]}
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["--list-defaults"])
        out = buf.getvalue().strip().splitlines()
    finally:
        cli.load_config = old_load_config
    assert out == [_expand_alias(m) for m in DEFAULT_MODELS], out


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
