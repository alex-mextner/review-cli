#!/usr/bin/env python3
"""Composability tests — visual verification composition (§3). NO real backends.

Proves the architecture, not just the pixels:
  * `review visual <image>` is the canonical standalone visual command, while
    `--visual` still composes with text mode subcommands;
  * with a companion mode the image's visual context is THREADED INTO that mode's
    prompt (the composition seam) — asserted by capturing the mode call;
  * standalone (`review visual img` with no diff) runs the verdict pipeline and returns
    the mapped exit code;
  * cvGate runs in the companion path too (a broken render surfaces in the context).

The mode functions are monkeypatched WHERE THEY ARE DEFINED (the per-mode modules) so
the test never spawns codex/gemini/claude — the modes-subcommands redesign dispatches
through `modes/registry`, not a `cli.mode_*` attribute. The diff is forced empty /
supplied so no git is touched.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import visual_fixtures as vf  # noqa: E402
from reviewlib import cli  # noqa: E402
from reviewlib.features.visual import compose as _cmp  # noqa: E402
from reviewlib.modes import brainstorm as _brainstorm_mod  # noqa: E402
from reviewlib.modes import just_ask as _just_ask_mod  # noqa: E402
from reviewlib.modes import quorum as _quorum_mod  # noqa: E402
from reviewlib.modes import review as _review_mod  # noqa: E402

TASK_ARGS = ["--task", "TEST-1"]

# These composability tests assert the Stage-1 cvGate-described context threading. The
# Stage-2 per-mode fan-out would otherwise fire a REAL vision call here (a Gemini/
# Anthropic key is configured on a dev box). Force the fan-out to find NO vision backend
# so the seam degrades to the cvGate-described note (the exact behaviour these tests
# pin) and NO API is burned. The dedicated fan-out behaviour is covered, with a mocked
# vision call, in test_visual_fanout.py.
_cmp.select_vision_backend = lambda models: None


def _styled(tmp: str = "/tmp/visual-compose-styled.png") -> str:
    return str(vf.styled_render(Path(tmp)))


def _blank(tmp: str = "/tmp/visual-compose-blank.png") -> str:
    return str(vf.blank_white(Path(tmp)))


def test_visual_composes_with_brainstorm_subcommand():
    """--visual is composable with any mode subcommand: `review brainstorm "…" --task X
    --visual img` threads the image context into the brainstorm topic (the composition seam)."""
    captured = {}

    def fake_brainstorm(topic, *a, **k):
        captured["topic"] = topic
        return 0

    old = _brainstorm_mod.mode_brainstorm
    _brainstorm_mod.mode_brainstorm = fake_brainstorm
    try:
        rc = cli.main(["brainstorm", "should we ship X", *TASK_ARGS, "--visual", _styled(), "--no-ai", "-C", str(REPO_ROOT)])
    finally:
        _brainstorm_mod.mode_brainstorm = old
    assert rc == 0
    # The image context was threaded into the brainstorm topic.
    assert "ATTACHED RENDER" in captured["topic"], "visual context not folded into brainstorm topic"
    assert "should we ship X" in captured["topic"]


def test_visual_threads_into_quorum():
    captured = {}

    def fake_quorum(question, *a, **k):
        captured["question"] = question
        return 0

    old = _quorum_mod.mode_quorum
    _quorum_mod.mode_quorum = fake_quorum
    try:
        rc = cli.main(["quorum", "is this styled?", *TASK_ARGS, "--visual", _styled(), "--no-ai", "-C", str(REPO_ROOT)])
    finally:
        _quorum_mod.mode_quorum = old
    assert rc == 0
    assert "ATTACHED RENDER" in captured["question"]
    assert "is this styled?" in captured["question"]


def test_companion_cvgate_surfaces_passthrough_outcome():
    """cvGate fires in the companion path: a pass-through (styled) render's context note
    carries the cvGate outcome so the mode reasons with it (a ROLLBACK would instead
    block the mode — see test_companion_rollback_blocks_the_mode)."""
    captured = {}

    def fake_just_ask(question, *a, **k):
        captured["question"] = question
        return 0

    old = _just_ask_mod.mode_just_ask
    _just_ask_mod.mode_just_ask = fake_just_ask
    try:
        rc = cli.main(["just-ask", "describe", *TASK_ARGS, "--visual", _styled(), "--no-ai", "-C", str(REPO_ROOT)])
    finally:
        _just_ask_mod.mode_just_ask = old
    assert rc == 0
    assert "cvGate pre-filter outcome: pass_through" in captured["question"]
    assert "ATTACHED RENDER" in captured["question"]


def _clean_repo() -> str:
    """A throwaway clean git repo so --visual standalone sees NO diff (a dirty cwd
    would route to the diff-review companion — the codex-P1 routing)."""
    import subprocess
    import tempfile

    d = tempfile.mkdtemp(prefix="visual-clean-repo-")
    subprocess.run(["git", "init", "-q"], cwd=d, capture_output=True)
    return d


def test_visual_subcommand_standalone_runs_pipeline_and_maps_exit():
    """`review visual <image>` is the canonical standalone verdict pipeline. A blank
    image with --no-ai --strict must exit 10 (rollback)."""
    rc = cli.main(["visual", _blank(), "--no-ai", "--strict", "-C", str(REPO_ROOT)])
    assert rc == 10, f"blank standalone --strict must exit 10, got {rc}"

    # A styled pass-through under --no-ai is human_review → non-strict exit 0.
    rc2 = cli.main(["visual", _styled(), "--no-ai", "-C", str(REPO_ROOT)])
    assert rc2 == 0


def test_visual_subcommand_with_diff_threads_visual_into_review():
    """`review visual <image> --task X --diff` includes the working-tree diff and runs the
    normal review companion with the screenshot folded into the prompt."""
    captured = {}

    def fake_review(models, prompt, diff, cwd, timeout, staged, board=None, **kw):
        captured["prompt"] = prompt
        captured["diff"] = diff
        captured["board"] = board
        return 0

    old = _review_mod.mode_review
    old_stdin = cli._read_stdin_if_piped
    old_git = cli._git_diff
    old_is_repo = cli._is_git_repo
    old_cfg = cli.load_config
    _review_mod.mode_review = fake_review
    cli._read_stdin_if_piped = lambda: None
    cli._git_diff = lambda cwd, staged: "diff --git a/x b/x\n+change\n"
    cli._is_git_repo = lambda cwd: True
    cli.load_config = lambda: {"models": ["codex"]}
    try:
        rc = cli.main(["visual", _styled(), *TASK_ARGS, "--diff", "--no-ai", "-C", str(REPO_ROOT)])
    finally:
        _review_mod.mode_review = old
        cli._read_stdin_if_piped = old_stdin
        cli._git_diff = old_git
        cli._is_git_repo = old_is_repo
        cli.load_config = old_cfg
    assert rc == 0
    assert "ATTACHED RENDER" in captured["prompt"], "visual companion must carry visual context"
    assert "+change" in captured["diff"]


def test_companion_rollback_blocks_the_mode():
    """A blank render rolls back the cvGate pre-filter; the companion mode must be
    BLOCKED, not run (codex P2). The mode function must never be called, and the exit
    code is 10 under --strict / non-zero otherwise."""
    called = {"n": 0}

    def fake_just_ask(*a, **k):
        called["n"] += 1
        return 0

    old = _just_ask_mod.mode_just_ask
    _just_ask_mod.mode_just_ask = fake_just_ask
    try:
        rc = cli.main(["just-ask", "describe", *TASK_ARGS, "--visual", _blank(), "--strict", "-C", str(REPO_ROOT)])
        rc_advisory = cli.main(["just-ask", "describe", *TASK_ARGS, "--visual", _blank(), "-C", str(REPO_ROOT)])
    finally:
        _just_ask_mod.mode_just_ask = old
    assert called["n"] == 0, "the mode must NOT run when the visual pre-filter rolls back"
    assert rc == 10, f"--strict pre-filter rollback must exit 10, got {rc}"
    assert rc_advisory == 1, f"non-strict pre-filter rollback must exit non-zero, got {rc_advisory}"


def test_companion_unreadable_image_is_usage_exit_1():
    """An unreadable/missing companion --visual image is a USAGE error (exit 1), NOT a
    --strict content block (exit 10) — matches the standalone map (codex P2)."""
    called = {"n": 0}

    def fake_just_ask(*a, **k):
        called["n"] += 1
        return 0

    old = _just_ask_mod.mode_just_ask
    _just_ask_mod.mode_just_ask = fake_just_ask
    try:
        rc = cli.main(["just-ask", "x", *TASK_ARGS, "--visual", "/tmp/does-not-exist-zzz.png", "--strict", "-C", str(REPO_ROOT)])
    finally:
        _just_ask_mod.mode_just_ask = old
    assert called["n"] == 0, "the mode must not run on an unreadable image"
    assert rc == 1, f"unreadable companion image must be usage exit 1 even under --strict, got {rc}"


def test_default_review_with_diff_threads_visual():
    """`review diff --task X --visual` with a piped diff routes to the diff-review companion
    with the image as context — not the standalone pipeline (--visual rides the diff mode)."""
    captured = {}

    def fake_review(models, prompt, diff, cwd, timeout, staged, board=None, **kw):
        captured["prompt"] = prompt
        captured["diff"] = diff
        captured["board"] = board
        return 0

    old = _review_mod.mode_review
    old_stdin = cli._read_stdin_if_piped
    _review_mod.mode_review = fake_review
    cli._read_stdin_if_piped = lambda: "diff --git a/x b/x\n+change\n"
    try:
        rc = cli.main(["diff", *TASK_ARGS, "--visual", _styled(), "--no-ai", "-C", str(REPO_ROOT)])
    finally:
        _review_mod.mode_review = old
        cli._read_stdin_if_piped = old_stdin
    assert rc == 0
    assert "ATTACHED RENDER" in captured["prompt"], "diff-review companion must carry visual context"
    assert "+change" in captured["diff"]


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
