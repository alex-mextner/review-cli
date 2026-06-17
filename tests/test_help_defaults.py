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

import os
import re
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import cli  # noqa: E402


def _canon_optstrings(help_text: str) -> str:
    """Canonicalize argparse's short/long option rendering so assertions are stable across
    Python versions. Python <=3.12 renders a metavar after EVERY option string
    (`-m MODEL, --model MODEL`); 3.13+ shows it once after the last (`-m, --model MODEL`).
    Collapse the <=3.12 form to the 3.13+ form so `-m, --model` matches on both."""
    # `-m MODEL, --model` -> `-m, --model`  (drop the metavar that sits between a short
    # option and the following `, --long`). The metavar is an uppercase/〈〉 token.
    return re.sub(r"(-\w) [A-Z][A-Z0-9_]*(, --)", r"\1\2", help_text)


def _top_level_help() -> str:
    return cli._build_top_level_parser().format_help()


def _mode_help(subcommand: str) -> str:
    from reviewlib.modes.registry import get_mode

    return cli._build_mode_parser(get_mode(subcommand)).format_help()


def _model_line(help_text: str) -> str:
    """Extract just the `-m, --model …` option block (up to the next option), whitespace-
    normalized. The `--show-board` flag's own description also says "active reviewer board",
    so a whole-help substring check would false-positive — scope to the --model default."""
    norm = " ".join(_canon_optstrings(help_text).split())
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
    # --moderator is scoped to the panel modes (quorum / brainstorm) now, so its help lives
    # on those subcommands — assert the concrete auto-pick chain there, not the bare phrasing.
    text = _mode_help("quorum")
    assert "--moderator" in text
    assert "claude:claude-opus-4-8" in text, text
    assert "codex" in text and "gemini" in text, text


def test_numeric_defaults_are_concrete():
    # --pool / --timeout are global (top-level); --vision-timeout is a visual flag scoped to
    # the subcommands. Assert each shows its concrete numeric default on the right surface.
    top = _top_level_help()
    assert "default 4" in top, top              # --pool
    assert "1200" in top and "240" in top, top  # --timeout (review / panel)
    assert "default 60" in _mode_help("diff"), _mode_help("diff")  # --vision-timeout (visual)


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


def _option_strings(parser_help: str) -> str:
    """The OPTIONS section of a help text (between 'options:' and the next top-level
    heading / EOF), whitespace-normalized — so a flag named only in the subcommand epilog
    or a description doesn't count as 'an option on this parser'."""
    norm = _canon_optstrings(parser_help)
    start = norm.index("options:")
    # Stop at the subcommands epilog (top-level) if present.
    end = norm.find("\nsubcommands:", start)
    section = norm[start:] if end == -1 else norm[start:end]
    return section


# --- Subcommand-only options belong in the SUBCOMMAND help, not the global list (ROADMAP). -
VISUAL_FLAGS = ("--visual", "--before", "--intent", "--expect", "--check", "--json",
                "--strict", "--no-ai", "--no-local-model", "--vision-timeout", "--project")


def test_visual_flags_absent_from_global_help_present_on_subcommands():
    opts = _option_strings(_top_level_help())
    for flag in VISUAL_FLAGS:
        assert flag not in opts, (flag, "leaked into the GLOBAL option list")
    # …and present on every mode parser (the --visual feature rides any subcommand).
    for sub in ("diff", "brainstorm", "just-ask", "quorum"):
        mode_opts = _option_strings(_mode_help(sub))
        for flag in VISUAL_FLAGS:
            assert flag in mode_opts, (sub, flag, "missing from the subcommand help")


def test_prompt_is_scoped_to_the_diff_review():
    assert "--prompt" not in _option_strings(_top_level_help()), "prompt leaked to global"
    assert "--prompt" in _option_strings(_mode_help("diff")), "diff review needs --prompt"
    for sub in ("just-ask", "quorum", "brainstorm"):
        assert "--prompt" not in _option_strings(_mode_help(sub)), (sub, "must not have --prompt")


def test_moderator_is_scoped_to_panel_modes():
    assert "--moderator" not in _option_strings(_top_level_help()), "moderator leaked to global"
    for sub in ("quorum", "brainstorm"):
        assert "--moderator" in _option_strings(_mode_help(sub)), (sub, "panel mode needs --moderator")
    for sub in ("diff", "just-ask"):
        assert "--moderator" not in _option_strings(_mode_help(sub)), (sub, "must not have --moderator")


def test_subcommand_only_flags_set_covers_every_mode_only_flag():
    """`_SUBCOMMAND_ONLY_FLAGS` (the pre-parse guard's list) must stay COMPLETE: every long
    option that exists on a mode parser but NOT on the top-level parser must be in the set —
    else a verb-less `review --that-flag` would hit argparse's opaque "unrecognized
    arguments" instead of the friendly `review diff` pointer (gemini review). This guards
    against a future mode-only flag being added without updating the set."""
    from reviewlib.modes.registry import iter_modes

    def _long_opts(parser) -> set[str]:
        opts: set[str] = set()
        for action in parser._actions:  # noqa: SLF001 — introspection is the point
            for s in action.option_strings:
                if s.startswith("--"):
                    opts.add(s)
        return opts

    global_opts = _long_opts(cli._build_top_level_parser())
    mode_only: set[str] = set()
    for mode in iter_modes():
        mode_only |= _long_opts(cli._build_mode_parser(mode)) - global_opts
    mode_only.discard("--help")  # argparse's own, present everywhere
    missing = mode_only - cli._SUBCOMMAND_ONLY_FLAGS
    assert not missing, (
        f"these mode-only flags are missing from _SUBCOMMAND_ONLY_FLAGS (the verb-less guard "
        f"would drop the friendly pointer for them): {sorted(missing)}"
    )


def test_global_help_lists_only_truly_global_options():
    opts = _option_strings(_top_level_help())
    for flag in ("-m, --model", "-C, --cwd", "-o, --output", "--timeout",
                 "--list-defaults", "--show-board", "--pool"):
        assert flag in opts, (flag, "missing from the global option list")
    # The mode/diff-source-only flags must NOT be global.
    for flag in ("--diff", "--staged", "--prompt", "--moderator"):
        assert flag not in opts, (flag, "leaked into the GLOBAL option list")


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
