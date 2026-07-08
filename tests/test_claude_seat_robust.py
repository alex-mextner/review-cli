#!/usr/bin/env python3
"""Robustness of the claude/opus review seat (review-cli#76).

The opus seat is the PRIMARY review-gate seat. It used to run through `claude-p`,
a TUI-scraper that spawns the interactive fullscreen `claude` under a PTY and
screen-scrapes the result. The scrape is lossy: spinner redraws and bare control
bytes smear into the captured output (or the scrape times out → empty stdout),
corrupting / blanking the verdict. The fix:

  1. Prefer `claude --print` (genuine headless print mode — no PTY, no TUI, clean
     stdout) over the `claude-p` scraper; fall back to `claude-p` only when the
     `claude` binary is absent.
  2. Spawn with a decoration-hostile env (TERM=dumb / NO_COLOR=1 / CI=1).
  3. Strip ANSI/OSC/control sequences from the captured stdout as belt-and-suspenders,
     so a stray sequence can never corrupt the parsed verdict.

These tests pin that contract without spawning a real model. Same plain-test harness
as tests/test_streaming.py — no pytest dependency.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import backends as _backends  # noqa: E402
from reviewlib.backends import ReviewResult  # noqa: E402
from reviewlib.process import strip_control_sequences  # noqa: E402


# --- the strip helper is robust to real TUI noise --------------------------------------

def test_strip_removes_csi_osc_and_c0_control_bytes():
    # A realistic smear: CSI colour codes, cursor moves, an OSC hyperlink, a spinner
    # frame (bare ESC), and stray C0 control bytes interleaved with the real verdict.
    noisy = (
        "\x1b[2J\x1b[H"                       # clear screen + home (CSI)
        "\x1b[38;5;213m## verdict\x1b[0m "    # coloured heading
        "\x1b]8;;https://x\x07link\x1b]8;;\x07"  # OSC hyperlink
        "\x07\x08[needs-changes]\x1bc"        # BEL, BS, then a lone ESC c (reset)
        "\x1b[1A\x1b[2K done\n"               # cursor-up + erase-line, real text, newline
    )
    clean = strip_control_sequences(noisy)
    assert clean == "## verdict link[needs-changes] done\n", repr(clean)
    # No escape or control bytes survive (newline/tab are intentionally kept).
    assert "\x1b" not in clean and "\x07" not in clean and "\x08" not in clean
    assert "\n" in clean  # structure preserved


def test_strip_parses_garbled_tui_transcript_to_correct_verdict():
    # A condensed sample of the kind of fullscreen-TUI bleed that historically reached
    # the seat: spinner frames + banner fragments + an erase-line, around the verdict.
    transcript = (
        "\x1b[2K\x1b[1G✶ Transmuting…\x1b[0m"      # spinner frame
        "\x1b[36mClaude Code v2.1.187\x1b[0m\n"               # banner
        "## claude:claude-opus-4-8 review\n"
        "The function subtracts instead of adding.\n"
        "\x1b[31m## verdict [needs-changes]\x1b[0m\n"          # coloured verdict
    )
    clean = strip_control_sequences(transcript)
    assert "## verdict [needs-changes]" in clean
    assert "## claude:claude-opus-4-8 review" in clean
    assert "\x1b" not in clean


def test_strip_is_idempotent_and_noop_on_clean_text():
    clean = "## verdict [ok]\nlooks fine\n"
    assert strip_control_sequences(clean) == clean
    assert strip_control_sequences(strip_control_sequences(clean)) == clean


def test_strip_drops_carriage_return_keeps_tab_and_newline():
    # CR is the TUI line-overwrite byte: a stray one must not splice an old redraw into a
    # verdict line. Tab and newline (real structure) survive.
    noisy = "old fragment\r## verdict [ok]\n\tindented note\n"
    clean = strip_control_sequences(noisy)
    assert "\r" not in clean
    assert clean == "old fragment## verdict [ok]\n\tindented note\n", repr(clean)
    assert "\t" in clean and "\n" in clean


# --- argv construction: direct `claude --print` path -----------------------------------

def test_direct_claude_argv_uses_print_mode_no_tui_flags():
    argv = _backends._claude_cli_argv(
        "/usr/local/bin/claude", direct=True, model="claude-opus-4-8",
        cwd=Path("/tmp/repo"), timeout=120,
    )
    # Genuine print mode, text output (the clean non-TUI path).
    assert "--print" in argv
    assert argv[argv.index("--output-format") + 1] == "text"
    # Read-only review surface preserved.
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert "--safe-mode" in argv
    assert "--disable-slash-commands" in argv
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--model") + 1] == "claude-opus-4-8"
    # Read-only is enforced by an EMPTY tool allowlist (all built-in tools off), not a
    # denylist — strictly stronger and avoids real `claude` warning on claude-p tool names.
    assert argv[argv.index("--tools") + 1] == ""
    assert "--disallowedTools" not in argv
    # The direct path does NOT carry claude-p-only flags (those drive the TUI wrapper).
    assert "--cwd" not in argv          # claude uses the process cwd
    assert "--timeout-sec" not in argv  # review-cli's own timeout governs the call
    assert "-p" not in argv             # claude-p's print toggle; here we use --print


def test_direct_claude_argv_omits_model_when_unspecified():
    argv = _backends._claude_cli_argv(
        "/usr/local/bin/claude", direct=True, model=None,
        cwd=Path("/tmp/repo"), timeout=120,
    )
    assert "--model" not in argv
    assert "--print" in argv


# --- argv construction: legacy claude-p fallback ---------------------------------------

def test_claude_p_fallback_argv_keeps_wrapper_surface():
    argv = _backends._claude_cli_argv(
        "/usr/local/bin/claude-p", direct=False, model="claude-opus-4-8",
        cwd=Path("/tmp/repo"), timeout=90,
    )
    # The wrapper needs its own --cwd / --tools '' / --timeout-sec / -p surface, but
    # review-cli disables claude-p's wall timer so _run_streamed owns the idle timeout.
    assert argv[argv.index("--cwd") + 1] == "/tmp/repo"
    assert argv[argv.index("--timeout-sec") + 1] == "0"
    assert "-p" in argv
    assert "--print" not in argv  # claude-p has no --print; -p is its print toggle
    # Same read-only guarantees as the direct path.
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    for tool in _backends._CLAUDE_DISALLOWED_TOOLS:
        assert tool in argv


def test_claude_p_fallback_timeout_disables_wrapper_wall_reap():
    argv = _backends._claude_cli_argv(
        "/usr/local/bin/claude-p", direct=False, model="claude-fable-5",
        cwd=Path("/tmp/repo"), timeout=90,
    )
    assert argv[argv.index("--timeout-sec") + 1] == "0"


# --- binary resolution prefers the direct print binary ---------------------------------

def test_binary_resolution_prefers_claude_over_claude_p():
    saved = _backends._which_optional

    def _which(name):
        return f"/bin/{name}" if name in ("claude", "claude-p") else None

    _backends._which_optional = _which
    try:
        path, direct = _backends._claude_cli_binary()
        assert path == "/bin/claude" and direct is True
    finally:
        _backends._which_optional = saved


def test_binary_resolution_falls_back_to_claude_p_when_claude_absent():
    saved = _backends._which_optional

    def _which(name):
        return "/bin/claude-p" if name == "claude-p" else None

    _backends._which_optional = _which
    try:
        path, direct = _backends._claude_cli_binary()
        assert path == "/bin/claude-p" and direct is False
    finally:
        _backends._which_optional = saved


# --- decoration-hostile env ------------------------------------------------------------

def test_claude_cli_env_disables_terminal_decoration():
    env = _backends._claude_cli_env()
    assert env["TERM"] == "dumb"
    assert env["NO_COLOR"] == "1"
    assert env["CI"] == "1"
    # Inherits the rest of the environment (PATH must survive so the child can exec).
    assert "PATH" in env


# --- end-to-end: the spawned argv + env are wired, and the verdict is stripped ----------

def test_review_claude_cli_strips_captured_verdict_and_wires_env():
    import tempfile

    captured = {}

    def _fake_run_streamed(argv, *, cwd, input_text, env, timeout, backend, round_no, announce):
        captured["argv"] = argv
        captured["env"] = env
        captured["timeout"] = timeout
        captured["backend"] = backend
        # Simulate a backend that leaked a coloured/cursor-noisy verdict into stdout.
        noisy = "\x1b[32m## claude:claude-opus-4-8 review\x1b[0m\nlgtm\n## verdict [ok]\n"
        return ReviewResult(model="x", command="x", returncode=0, stdout=noisy, stderr="")

    saved_run = _backends._run_streamed
    saved_which = _backends._which_optional
    saved_trust = _backends._ensure_workspace_trusted
    _backends._run_streamed = _fake_run_streamed
    _backends._which_optional = lambda name: "/bin/claude" if name == "claude" else None
    _backends._ensure_workspace_trusted = lambda cwd: None
    try:
        with tempfile.TemporaryDirectory() as d:
            res = _backends.review_claude_cli("claude:claude-opus-4-8", "review", "diff", Path(d), 30)
    finally:
        _backends._run_streamed = saved_run
        _backends._which_optional = saved_which
        _backends._ensure_workspace_trusted = saved_trust

    # The captured stdout is clean — no escape bytes — and the verdict survives.
    assert "\x1b" not in res.stdout
    assert "## verdict [ok]" in res.stdout
    assert res.returncode == 0
    # The direct print path was used, with the decoration-hostile env.
    assert "--print" in captured["argv"]
    assert captured["env"]["TERM"] == "dumb"
    assert captured["timeout"] == 30
    assert captured["backend"] == "claude"
    # The reported command reflects the direct binary, not claude-p.
    assert "claude --print" in res.command


def test_review_claude_cli_with_images_enables_scoped_read_and_refs_file():
    import tempfile

    captured = {}
    trusted: list[Path] = []

    def _fake_run_streamed(argv, *, cwd, input_text, env, timeout, backend, round_no, announce):
        captured["argv"] = argv
        captured["cwd"] = Path(cwd)
        captured["input_text"] = input_text
        captured["timeout"] = timeout
        add_dir = Path(argv[argv.index("--add-dir") + 1])
        refs = [part[1:] for part in input_text.split() if part.startswith("@")]
        assert add_dir.is_dir()
        assert refs and Path(refs[0]).is_file()
        assert Path(refs[0]).parent == add_dir
        assert Path(cwd) == add_dir
        return ReviewResult(model="x", command="x", returncode=0, stdout="I saw the pixels", stderr="")

    saved_run = _backends._run_streamed
    saved_which = _backends._which_optional
    saved_trust = _backends._ensure_workspace_trusted
    _backends._run_streamed = _fake_run_streamed
    _backends._which_optional = lambda name: "/bin/claude" if name == "claude" else None
    _backends._ensure_workspace_trusted = lambda cwd: trusted.append(Path(cwd))
    try:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            image = root / "shot.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\n")
            res = _backends.review_claude_cli_with_images(
                "claude:claude-opus-4-8", "review this screenshot", "", root, 30,
                images=(image,),
            )
    finally:
        _backends._run_streamed = saved_run
        _backends._which_optional = saved_which
        _backends._ensure_workspace_trusted = saved_trust

    argv = captured["argv"]
    assert argv[argv.index("--tools") + 1] == "Read"
    assert "--add-dir" in argv
    add_dir = Path(argv[argv.index("--add-dir") + 1])
    assert captured["cwd"] == add_dir
    assert root not in trusted
    assert add_dir in trusted
    assert captured["timeout"] == 30
    assert "=== RAW VISUAL ATTACHMENT ===" in captured["input_text"]
    assert "image @refs" in res.command
    assert res.stdout == "I saw the pixels"


def test_review_claude_cli_with_images_falls_back_when_no_image_can_be_staged():
    import tempfile

    captured = {}

    def _fake_review_claude_cli(model, prompt, diff, cwd, timeout, round_no=0):
        captured["model"] = model
        captured["prompt"] = prompt
        captured["diff"] = diff
        captured["cwd"] = Path(cwd)
        captured["timeout"] = timeout
        captured["round_no"] = round_no
        return ReviewResult(model=model, command="text-only", returncode=0, stdout="fallback", stderr="")

    saved_review = _backends.review_claude_cli
    saved_run = _backends._run_streamed
    saved_which = _backends._which_optional
    saved_trust = _backends._ensure_workspace_trusted
    _backends.review_claude_cli = _fake_review_claude_cli
    _backends._run_streamed = lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("image path should not run"))
    _backends._which_optional = lambda name: "/bin/claude" if name == "claude" else None
    _backends._ensure_workspace_trusted = lambda cwd: None
    try:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            missing = root / "missing.png"
            res = _backends.review_claude_cli_with_images(
                "claude:claude-opus-4-8", "review", "diff", root, 30, round_no=4,
                images=(missing,),
            )
    finally:
        _backends.review_claude_cli = saved_review
        _backends._run_streamed = saved_run
        _backends._which_optional = saved_which
        _backends._ensure_workspace_trusted = saved_trust

    assert res.command == "text-only"
    assert res.stdout == "fallback"
    assert captured["cwd"] == root
    assert captured["round_no"] == 4


def test_have_claude_cli_true_with_either_binary():
    saved = _backends._which_optional
    try:
        _backends._which_optional = lambda name: "/bin/claude" if name == "claude" else None
        assert _backends._have_claude_cli() is True
        _backends._which_optional = lambda name: "/bin/claude-p" if name == "claude-p" else None
        assert _backends._have_claude_cli() is True
        _backends._which_optional = lambda name: None
        assert _backends._have_claude_cli() is False
    finally:
        _backends._which_optional = saved


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
