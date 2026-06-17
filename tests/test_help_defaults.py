#!/usr/bin/env python3
"""`--help` must show the ACTUAL effective default of each configurable option.

ROADMAP (CTO 2026-06-16, "Help must show ACTUAL defaults — esp. --model"): every
configurable option in `review --help` shows its EFFECTIVE default value, not a vague
description. Pinned here:

  * `--model` shows what runs when you DON'T pass -m — the active reviewer board by
    default, OR the configured `models:` list (the config cascade is reflected);
  * `--moderator` shows the auto-pick priority chain (opus -> codex -> gemini), not a bare
    "moderator backend";
  * `--pool` / `--timeout` / `--vision-timeout` print their concrete numeric defaults.

Driven through the real top-level parser's help text. The config-cascade case isolates
HOME to a temp dir (CONFIG_PATH = ~/.config/review-cli/config.yaml resolves via Path.home())
so it never reads the developer's real config.
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

from reviewlib import cli  # noqa: E402


def _top_level_help() -> str:
    return cli._build_top_level_parser().format_help()


def _mode_help(subcommand: str) -> str:
    from reviewlib.modes.registry import get_mode

    return cli._build_mode_parser(get_mode(subcommand)).format_help()


def _model_line(help_text: str) -> str:
    """Extract just the `-m, --model …` option block (up to the next option), whitespace-
    normalized. The `--show-board` flag's own description also says "active reviewer board",
    so a whole-help substring check would false-positive — scope to the --model default."""
    norm = " ".join(help_text.split())
    start = norm.index("-m, --model")
    end = norm.index("-C, --cwd", start)
    return norm[start:end]


def test_model_help_shows_the_board_default_when_no_config_models():
    # No configured models -> the default is the active board; the --model LINE must say so
    # and point at --show-board. Scope to the --model line: the --show-board flag's own
    # description also names the board, so a whole-help check would false-positive (codex).
    saved = cli.load_config
    cli.load_config = lambda: {}
    try:
        line = _model_line(_top_level_help())
    finally:
        cli.load_config = saved
    assert "active reviewer board" in line, line
    assert "--show-board" in line, line


def test_model_help_reflects_configured_models():
    # A `models:` list in config.yaml IS the effective default (the flat panel) — the help
    # must reflect that exact list, not the generic board phrasing.
    saved = cli.load_config
    cli.load_config = lambda: {"models": ["codex", "gemini"]}
    try:
        text = _top_level_help()
    finally:
        cli.load_config = saved
    assert "config.yaml models" in text, text
    assert "codex" in text and "gemini" in text, text


def test_model_default_is_mode_aware_not_board_for_panel_modes():
    """The `--model` default is MODE-AWARE (codex review): only `review diff` runs the
    reviewer board. `review just-ask`/`quorum`/`brainstorm` must NOT claim the board as
    their default — they use config models / DEFAULT_MODELS (brainstorm: brainstorm_models)."""
    saved = cli.load_config
    cli.load_config = lambda: {}
    try:
        for sub in ("just-ask", "quorum", "brainstorm"):
            # Scope to the --model line: the --show-board flag's own text also names the
            # board, so a whole-help check would false-positive.
            line = _model_line(_mode_help(sub))
            assert "active reviewer board" not in line, (sub, line)
            # The built-in defaults are named instead (codex is in DEFAULT_MODELS).
            assert "built-in defaults" in line, (sub, line)
        # And the diff subcommand DOES claim the board on its --model line.
        assert "active reviewer board" in _model_line(_mode_help("diff")), _model_line(_mode_help("diff"))
    finally:
        cli.load_config = saved


def test_brainstorm_model_default_reflects_brainstorm_models():
    """`review brainstorm --help` reflects `brainstorm_models:` (its real precedence top),
    not `models:` or the board."""
    saved = cli.load_config
    cli.load_config = lambda: {"brainstorm_models": ["fable5"], "models": ["codex"]}
    try:
        line = _model_line(_mode_help("brainstorm"))
    finally:
        cli.load_config = saved
    assert "brainstorm_models" in line, line
    assert "fable5" in line or "claude:claude-fable-5" in line, line


def test_moderator_help_shows_the_autopick_chain():
    text = _top_level_help()
    assert "--moderator" in text
    # The concrete priority chain, not a vague "moderator backend".
    assert "claude:claude-opus-4-8" in text, text
    assert "codex" in text and "gemini" in text, text


def test_numeric_defaults_are_concrete():
    text = _top_level_help()
    assert "default 4" in text, text          # --pool
    assert "1200" in text and "240" in text, text  # --timeout (review / panel)
    assert "default 60" in text, text          # --vision-timeout


def test_model_help_does_not_crash_on_unreadable_config():
    # --help must never raise even if load_config blows up; it falls back to the board phrasing.
    saved = cli.load_config

    def _boom():
        raise RuntimeError("bad config")

    cli.load_config = _boom
    try:
        line = _model_line(_top_level_help())
    finally:
        cli.load_config = saved
    assert "active reviewer board" in line, line


def test_model_help_does_not_crash_on_percent_in_config_model_id():
    """A `%` in a config model id must NOT crash `review --help` (codex): the default is
    interpolated into an argparse `help=`, where `%` is formatting syntax, so it must be
    escaped. Build help for the diff and brainstorm parsers with `%`-containing config and
    assert it renders (no ValueError) and the literal `%` survives."""
    from reviewlib.modes.registry import get_mode

    saved = cli.load_config
    cli.load_config = lambda: {"models": ["bad%model"], "brainstorm_models": ["b%s"]}
    try:
        for sub in ("diff", "brainstorm", "just-ask"):
            # Must not raise "badly formed help string".
            text = cli._build_mode_parser(get_mode(sub)).format_help()
            assert "bad%model" in text or "b%s" in text, (sub, text)
        # The top-level overview too.
        assert cli._build_top_level_parser().format_help()
    finally:
        cli.load_config = saved


def test_help_end_to_end_via_cli_shows_model_default():
    # End-to-end through the real CLI (isolated HOME so no dev config leaks): bare `review
    # --help` exits 0 and the printed help carries the --model default.
    with tempfile.TemporaryDirectory() as home:
        env = dict(os.environ)
        env["HOME"] = home
        import subprocess

        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "bin" / "review"), "--help"],
            capture_output=True, text=True, timeout=60, env=env,
        )
        assert proc.returncode == 0, (proc.returncode, proc.stderr)
        assert "active reviewer board" in proc.stdout, proc.stdout


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
    print(f"\n{'FAILED' if failures else 'OK'}: {failures} failure(s)")
    sys.exit(1 if failures else 0)
