#!/usr/bin/env python3
"""Mode SUBCOMMAND dispatch + the mode registry (the modes-subcommands redesign).

The review modes moved from `--` flags into first-class SUBCOMMANDS backed by a
plugin-directory-style mode registry (mirrors features/visual/registry). These tests pin:

  * each subcommand (review / brainstorm / just-ask / quorum, + the `ask` alias)
    dispatches to the RIGHT mode handler;
  * a bare `review …` (no recognized subcommand) defaults to the REVIEW mode (the §4
    ergonomics) and prints the one-line migration hint to stderr — but never hard-errors;
  * `review brainstorm "…" --diff` composes the working-tree diff as grounding;
  * the removed mode FLAGS (--brainstorm/--quorum/--just-ask) now error helpfully (exit 2)
    and point at the subcommand;
  * the registry contract (get_mode / known_subcommands / default_mode / iter_modes).

All offline: the mode handlers are stubbed WHERE THEY ARE DEFINED (the per-mode modules)
so no backend is spawned; the diff is faked so no real git is touched.
"""
from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import cli  # noqa: E402
from reviewlib.modes import brainstorm as _brainstorm_mod  # noqa: E402
from reviewlib.modes import just_ask as _just_ask_mod  # noqa: E402
from reviewlib.modes import quorum as _quorum_mod  # noqa: E402
from reviewlib.modes import registry as _registry  # noqa: E402
from reviewlib.modes import review as _review_mod  # noqa: E402


# --- A small harness that stubs every mode handler + diff acquisition + config. -------
def _run(argv: list[str], *, diff: str = "") -> dict:
    """Run `cli.main(argv)` with all four mode handlers + _git_diff + load_config stubbed.
    Returns {"mode": <name of the mode whose handler ran>, "text": <the first positional
    arg the handler saw>, "rc": <exit code>, "stderr": <captured>}."""
    captured: dict = {"mode": None, "text": None}

    def _mk(name):
        def _fake(*a, **k):
            captured["mode"] = name
            captured["text"] = a[0] if a else None
            captured["args"] = a
            return 0
        return _fake

    saved = {
        "review": _review_mod.mode_review,
        "brainstorm": _brainstorm_mod.mode_brainstorm,
        "just-ask": _just_ask_mod.mode_just_ask,
        "quorum": _quorum_mod.mode_quorum,
        "git": cli._git_diff,
        "cfg": cli.load_config,
        "stdin": cli._read_stdin_if_piped,
    }
    _review_mod.mode_review = _mk("review")
    _brainstorm_mod.mode_brainstorm = _mk("brainstorm")
    _just_ask_mod.mode_just_ask = _mk("just-ask")
    _quorum_mod.mode_quorum = _mk("quorum")
    cli._git_diff = lambda cwd, staged: diff
    cli.load_config = lambda: {"models": ["codex"]}  # explicit models -> no real board
    cli._read_stdin_if_piped = lambda: None
    old_env = os.environ.get("GEMINI_ENV_FILE")
    os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
    err = io.StringIO()
    try:
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            captured["rc"] = cli.main(argv)
    finally:
        _review_mod.mode_review = saved["review"]
        _brainstorm_mod.mode_brainstorm = saved["brainstorm"]
        _just_ask_mod.mode_just_ask = saved["just-ask"]
        _quorum_mod.mode_quorum = saved["quorum"]
        cli._git_diff = saved["git"]
        cli.load_config = saved["cfg"]
        cli._read_stdin_if_piped = saved["stdin"]
        if old_env is None:
            os.environ.pop("GEMINI_ENV_FILE", None)
        else:
            os.environ["GEMINI_ENV_FILE"] = old_env
    captured["stderr"] = err.getvalue()
    return captured


# --- Each subcommand dispatches to the right mode. ------------------------------------
def test_review_subcommand_dispatches_to_review():
    cap = _run(["review", "-C", str(REPO_ROOT)], diff="diff --git a/x b/x\n+y\n")
    assert cap["mode"] == "review", cap
    assert cap["rc"] == 0


def test_brainstorm_subcommand_dispatches_to_brainstorm():
    cap = _run(["brainstorm", "design the cache", "-C", str(REPO_ROOT)])
    assert cap["mode"] == "brainstorm", cap
    assert cap["text"] == "design the cache", cap


def test_just_ask_subcommand_dispatches_to_just_ask():
    cap = _run(["just-ask", "is this safe?", "-C", str(REPO_ROOT)])
    assert cap["mode"] == "just-ask", cap
    assert cap["text"] == "is this safe?", cap


def test_ask_alias_dispatches_to_just_ask():
    cap = _run(["ask", "aliased question", "-C", str(REPO_ROOT)])
    assert cap["mode"] == "just-ask", cap
    assert cap["text"] == "aliased question", cap


def test_quorum_subcommand_dispatches_to_quorum():
    cap = _run(["quorum", "ship it?", "-C", str(REPO_ROOT)])
    assert cap["mode"] == "quorum", cap
    assert cap["text"] == "ship it?", cap


# --- Bare `review …` (no subcommand) defaults to the review mode (§4). ----------------
def test_no_subcommand_defaults_to_review():
    # The hint is printed once per process; reset the latch so this test is self-
    # contained regardless of which tests ran before it (the hint is process-global).
    cli._DEFAULT_HINT_SHOWN = False
    cap = _run(["-C", str(REPO_ROOT)], diff="diff --git a/x b/x\n+y\n")
    assert cap["mode"] == "review", cap
    assert cap["rc"] == 0
    # And it prints the one-line migration hint to stderr (but does NOT hard-error).
    assert "review review" in cap["stderr"], cap["stderr"]
    assert "subcommands" in cap["stderr"], cap["stderr"]


def test_no_subcommand_with_staged_still_review():
    cap = _run(["--staged", "-C", str(REPO_ROOT)], diff="diff --git a/s b/s\n+z\n")
    assert cap["mode"] == "review", cap
    assert cap["rc"] == 0


def test_meta_flag_without_subcommand_does_not_nag():
    """A meta query (--list-defaults) without a subcommand must NOT print the hint — it
    is not a diff-review dispatch (the hint is for the default review path only)."""
    err = io.StringIO()
    out = io.StringIO()
    saved_cfg = cli.load_config
    cli.load_config = lambda: {}
    try:
        with redirect_stderr(err), redirect_stdout(out):
            rc = cli.main(["--list-defaults"])
    finally:
        cli.load_config = saved_cfg
    assert rc == 0
    assert "review review" not in err.getvalue(), err.getvalue()


# --- brainstorm composes with --diff grounding. --------------------------------------
def test_brainstorm_with_diff_flag_grounds_on_working_tree_diff():
    """`review brainstorm "…" --diff` (or --staged) picks up the working-tree diff as
    OPTIONAL grounding and feeds it to the handler."""
    grounding = "diff --git a/g b/g\n@@\n+grounded\n"
    captured: dict = {}

    def _fake(topic, models, cwd, timeout, moderators, rounds, max_rounds, diff=""):
        captured["diff"] = diff
        return 0

    saved = _brainstorm_mod.mode_brainstorm
    saved_git = cli._git_diff
    saved_cfg = cli.load_config
    saved_stdin = cli._read_stdin_if_piped
    _brainstorm_mod.mode_brainstorm = _fake
    cli._git_diff = lambda cwd, staged: grounding
    cli.load_config = lambda: {"models": ["codex"]}
    cli._read_stdin_if_piped = lambda: None
    try:
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            rc = cli.main(["brainstorm", "topic", "--diff", "-C", str(REPO_ROOT)])
    finally:
        _brainstorm_mod.mode_brainstorm = saved
        cli._git_diff = saved_git
        cli.load_config = saved_cfg
        cli._read_stdin_if_piped = saved_stdin
    assert rc == 0
    assert captured["diff"] == grounding, captured


def test_just_ask_does_not_auto_grab_diff_but_diff_flag_opts_in():
    """just-ask has 'none' diff policy: WITHOUT --diff/--staged/pipe it gets no diff;
    WITH --diff it opts in to the working-tree diff as context."""
    grounding = "diff --git a/g b/g\n@@\n+ctx\n"

    def run(argv):
        captured: dict = {}

        def _fake(question, models, diff, cwd, timeout):
            captured["diff"] = diff
            return 0

        saved = _just_ask_mod.mode_just_ask
        saved_git = cli._git_diff
        saved_cfg = cli.load_config
        saved_stdin = cli._read_stdin_if_piped
        _just_ask_mod.mode_just_ask = _fake
        cli._git_diff = lambda cwd, staged: grounding
        cli.load_config = lambda: {"models": ["codex"]}
        cli._read_stdin_if_piped = lambda: None
        try:
            with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                rc = cli.main(argv)
        finally:
            _just_ask_mod.mode_just_ask = saved
            cli._git_diff = saved_git
            cli.load_config = saved_cfg
            cli._read_stdin_if_piped = saved_stdin
        return rc, captured.get("diff")

    rc, diff = run(["just-ask", "Q", "-C", str(REPO_ROOT)])
    assert rc == 0 and diff == "", ("no --diff -> no context", diff)
    rc, diff = run(["just-ask", "Q", "--diff", "-C", str(REPO_ROOT)])
    assert rc == 0 and diff == grounding, ("--diff -> working-tree context", diff)


# --- The removed mode flags now error helpfully. -------------------------------------
def _capture_main(argv: list[str]) -> tuple[int, str]:
    err = io.StringIO()
    with redirect_stderr(err), redirect_stdout(io.StringIO()):
        rc = cli.main(argv)
    return rc, err.getvalue()


def test_removed_brainstorm_flag_errors_with_pointer():
    rc, err = _capture_main(["--brainstorm", "x", "-C", str(REPO_ROOT)])
    assert rc == 2, rc
    assert "review brainstorm" in err, err
    assert "no longer a flag" in err, err


def test_removed_quorum_flag_errors_with_pointer():
    rc, err = _capture_main(["--quorum", "x", "-C", str(REPO_ROOT)])
    assert rc == 2, rc
    assert "review quorum" in err, err


def test_removed_just_ask_flag_errors_with_pointer():
    rc, err = _capture_main(["--just-ask", "x", "-C", str(REPO_ROOT)])
    assert rc == 2, rc
    assert "review just-ask" in err, err


def test_removed_flag_equals_form_errors():
    rc, err = _capture_main(["--brainstorm=topic", "-C", str(REPO_ROOT)])
    assert rc == 2, rc
    assert "review brainstorm" in err, err


def test_removed_flag_after_double_dash_is_not_intercepted():
    """A `--quorum` AFTER `--` is NOT the removed flag — the reject scan stops at `--`.
    The review mode has no positional, so argparse rejects the extra token with a usage
    SystemExit, but crucially NOT with the removed-flag message (the scan never fired)."""
    err = io.StringIO()
    raised = False
    with redirect_stderr(err), redirect_stdout(io.StringIO()):
        try:
            cli.main(["review", "--", "--quorum"])
        except SystemExit:
            raised = True  # argparse usage error on the stray positional — acceptable
    assert "no longer a flag" not in err.getvalue(), err.getvalue()
    assert raised, "argparse should reject the stray positional after --"


# --- The mode registry contract. -----------------------------------------------------
def test_registry_known_subcommands():
    subs = _registry.known_subcommands()
    for verb in ("review", "brainstorm", "just-ask", "quorum", "ask"):
        assert verb in subs, (verb, subs)


def test_registry_get_mode_resolves_subcommand_and_alias():
    assert _registry.get_mode("brainstorm").name == "brainstorm"
    assert _registry.get_mode("ask").name == "just-ask"   # alias
    assert _registry.get_mode("not-a-mode") is None


def test_registry_default_mode_is_review():
    assert _registry.default_mode().name == "review"
    assert _registry.DEFAULT_MODE_NAME == "review"


def test_registry_iter_modes_are_self_describing():
    """Every registered mode exposes the ModeSpec contract fields (name / subcommand /
    diff_policy / handler) — the descriptor a plugin-dir mode would also expose."""
    from reviewlib.modes.contract import DIFF_POLICIES
    seen_review_first = _registry.iter_modes()[0].name == "review"
    assert seen_review_first, "review must be the first (default) mode"
    for mode in _registry.iter_modes():
        assert mode.name and mode.subcommand, mode
        assert mode.diff_policy in DIFF_POLICIES, mode
        assert callable(mode.handler), mode


def test_review_mode_declares_require_diff_policy():
    """The review mode's descriptor declares diff_policy 'require' (it is the diff-review
    that always needs a diff), unlike the panel modes ('none'/'optional')."""
    assert _registry.get_mode("review").diff_policy == "require"
    assert _registry.get_mode("just-ask").diff_policy == "none"
    assert _registry.get_mode("quorum").diff_policy == "none"
    assert _registry.get_mode("brainstorm").diff_policy == "optional"


def test_review_mode_empty_diff_returns_nonzero_no_diff_to_review():
    """End-to-end: a `review` with an EMPTY diff reaches the REAL mode_review and returns
    non-zero ('No diff to review') — the 'require' policy enforced, not just declared."""
    saved_cfg = cli.load_config
    saved_git = cli._git_diff
    saved_stdin = cli._read_stdin_if_piped
    cli.load_config = lambda: {"models": ["codex"]}  # explicit models -> flat path, no board
    cli._git_diff = lambda cwd, staged: ""             # empty diff
    cli._read_stdin_if_piped = lambda: None
    try:
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            rc = cli.main(["review", "-C", str(REPO_ROOT)])
    finally:
        cli.load_config = saved_cfg
        cli._git_diff = saved_git
        cli._read_stdin_if_piped = saved_stdin
    assert rc == 1, rc


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
