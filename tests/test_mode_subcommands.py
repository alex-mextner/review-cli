#!/usr/bin/env python3
"""Mode SUBCOMMAND dispatch + the mode registry (the modes-subcommands redesign).

The review modes moved from `--` flags into first-class SUBCOMMANDS backed by a
plugin-directory-style mode registry (mirrors features/visual/registry). These tests pin:

  * each subcommand (diff / brainstorm / just-ask / quorum, + the `ask` alias)
    dispatches to the RIGHT mode handler;
  * the diff review is `review diff` (renamed from the stuttering `review review`); a
    bare `review` (no subcommand) prints HELP and exits 0 — it does NOT run a diff review;
  * the removed verb `review review` prints a one-line "use `review diff`" pointer (exit 2);
  * `review brainstorm "…" --diff` composes the working-tree diff as grounding;
  * the removed mode FLAGS (--brainstorm/--quorum/--just-ask) now error helpfully (exit 2)
    and point at the subcommand;
  * the no-replacement removed FLAGS (--mcp/--ln) fail LOUD with a structured
    what/why/how-to-fix error (exit 2) instead of argparse's opaque "unrecognized
    arguments" — so a stale `review --mcp` MCP registration is diagnosable;
  * the registry contract (get_mode / known_subcommands / diff_mode / iter_modes).

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
def _run(argv: list[str], *, diff: str = "", stdin: str | None = None) -> dict:
    """Run `cli.main(argv)` with all four mode handlers + _git_diff + load_config stubbed.
    `stdin` (default None) is what `_read_stdin_if_piped` returns — a non-None string
    simulates a diff piped in (`git diff | review`). Returns {"mode": <name of the mode
    whose handler ran>, "text": <the first positional arg the handler saw>, "rc": <exit
    code>, "stderr": <captured>}."""
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
        "is_git_repo": cli._is_git_repo,
        "cfg": cli.load_config,
        "stdin": cli._read_stdin_if_piped,
    }
    _review_mod.mode_review = _mk("review")
    _brainstorm_mod.mode_brainstorm = _mk("brainstorm")
    _just_ask_mod.mode_just_ask = _mk("just-ask")
    _quorum_mod.mode_quorum = _mk("quorum")
    cli._git_diff = lambda cwd, staged: diff
    cli._is_git_repo = lambda cwd: True
    cli.load_config = lambda: {"models": ["codex"]}  # deterministic one-seat config board
    cli._read_stdin_if_piped = lambda: stdin
    old_env = os.environ.get("GEMINI_ENV_FILE")
    old_task = os.environ.get("REVIEW_TASK_CODE")
    os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
    os.environ["REVIEW_TASK_CODE"] = "TEST-1"
    err = io.StringIO()
    out = io.StringIO()
    try:
        with redirect_stderr(err), redirect_stdout(out):
            try:
                captured["rc"] = cli.main(argv)
            except SystemExit as exc:
                # A bare `review` / `review --flag …` (no verb) prints help and exits via
                # SystemExit, exactly like `review --help` — capture its code as the rc the
                # real CLI process would surface (argparse normalizes None -> 0).
                captured["rc"] = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    finally:
        _review_mod.mode_review = saved["review"]
        _brainstorm_mod.mode_brainstorm = saved["brainstorm"]
        _just_ask_mod.mode_just_ask = saved["just-ask"]
        _quorum_mod.mode_quorum = saved["quorum"]
        cli._git_diff = saved["git"]
        cli._is_git_repo = saved["is_git_repo"]
        cli.load_config = saved["cfg"]
        cli._read_stdin_if_piped = saved["stdin"]
        if old_env is None:
            os.environ.pop("GEMINI_ENV_FILE", None)
        else:
            os.environ["GEMINI_ENV_FILE"] = old_env
        if old_task is None:
            os.environ.pop("REVIEW_TASK_CODE", None)
        else:
            os.environ["REVIEW_TASK_CODE"] = old_task
    captured["stderr"] = err.getvalue()
    captured["stdout"] = out.getvalue()
    return captured


# --- Each subcommand dispatches to the right mode. ------------------------------------
def test_diff_subcommand_dispatches_to_review():
    cap = _run(["diff", "-C", str(REPO_ROOT)], diff="diff --git a/x b/x\n+y\n")
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


# --- Bare `review` (no subcommand) prints HELP — it does NOT run a diff review. -------
def test_bare_review_prints_help_and_does_not_run_diff():
    """A bare `review` (no args) must print the HELP/usage to stdout and exit 0 WITHOUT
    dispatching any mode handler (the old "bare review = diff review" default was the
    mistake this migration fixes)."""
    cap = _run([], diff="diff --git a/x b/x\n+y\n")
    assert cap["mode"] is None, ("no mode handler must run on bare review", cap)
    assert cap["rc"] == 0, cap
    # The help/overview was printed (it names the subcommands + the diff pointer).
    out = cap["stdout"]
    assert "subcommands:" in out, out
    assert "review diff" in out, out


def test_flags_without_subcommand_print_help_and_point_at_diff():
    """`review -C <repo>` (flags, no verb) is the old diff-default invocation. It must NOT
    silently run a diff review: it prints help + a `review diff` pointer and exits 2 (the
    user passed args meaning to DO something)."""
    cap = _run(["-C", str(REPO_ROOT)], diff="diff --git a/x b/x\n+y\n")
    assert cap["mode"] is None, ("no mode handler must run", cap)
    assert cap["rc"] == 2, cap
    assert "review diff" in cap["stderr"], cap["stderr"]
    assert "--task CODE" in cap["stderr"], cap["stderr"]


def test_staged_without_subcommand_points_at_diff():
    """`review --staged` (no verb) is the old pre-commit muscle-memory command. `--staged`
    is scoped to the subcommands now, so a pre-parse guard catches it and emits the friendly
    `review diff` pointer (exit 2, no mode run) — NOT argparse's opaque "unrecognized
    arguments", which would drop the migration guidance (codex review)."""
    cap = _run(["--staged", "-C", str(REPO_ROOT)], diff="diff --git a/s b/s\n+z\n")
    assert cap["mode"] is None, cap
    assert cap["rc"] == 2, cap
    assert "review diff" in cap["stderr"], cap["stderr"]
    assert "--task CODE" in cap["stderr"], cap["stderr"]
    assert "unrecognized arguments" not in cap["stderr"], cap["stderr"]


def test_visual_without_subcommand_points_at_visual():
    """`review --visual shot.png` (no verb) points at the canonical `review visual` form,
    not an opaque argparse error."""
    cap = _run(["--visual", "shot.png", "-C", str(REPO_ROOT)])
    assert cap["mode"] is None, cap
    assert cap["rc"] == 2, cap
    assert "review visual" in cap["stderr"], cap["stderr"]
    assert "review diff --visual" not in cap["stderr"], cap["stderr"]


def test_piped_diff_without_subcommand_fails_loud_not_silent_noop():
    """`git diff | review` (a piped diff, no subcommand) used to run a diff review; a bare
    `review` no longer does. Silently exiting 0 would turn it into an undetectable no-op
    SUCCESS (codex P1) — so it must FAIL LOUD (exit 2) pointing at `git diff | review diff`,
    and run no mode handler."""
    cap = _run([], stdin="diff --git a/x b/x\n+y\n")
    assert cap["mode"] is None, ("no mode handler must run", cap)
    assert cap["rc"] == 2, cap
    assert "review diff" in cap["stderr"], cap["stderr"]
    assert "--task CODE" in cap["stderr"], cap["stderr"]
    assert "piped in" in cap["stderr"], cap["stderr"]


def test_removed_review_verb_points_at_diff():
    """The old stuttering `review review` verb is gone: it prints a one-line `review diff`
    pointer (exit 2), like the removed mode flags — it does NOT run a diff review."""
    cap = _run(["review", "-C", str(REPO_ROOT)], diff="diff --git a/x b/x\n+y\n")
    assert cap["mode"] is None, ("review review must not dispatch the diff handler", cap)
    assert cap["rc"] == 2, cap
    assert "review diff" in cap["stderr"], cap["stderr"]
    assert "--task CODE" in cap["stderr"], cap["stderr"]
    assert "no longer a subcommand" in cap["stderr"], cap["stderr"]


def test_removed_review_verb_points_at_diff_after_leading_model_option():
    """Leading global options must not hide the removed `review` verb diagnostic."""
    cap = _run(["-m", "fable5", "review", "-C", str(REPO_ROOT)], diff="diff --git a/x b/x\n+y\n")
    assert cap["mode"] is None, ("review -m fable review must not dispatch", cap)
    assert cap["rc"] == 2, cap
    assert "review diff" in cap["stderr"], cap["stderr"]
    assert "no longer a subcommand" in cap["stderr"], cap["stderr"]


def test_meta_flag_without_subcommand_works():
    """A meta query (--list-defaults) without a subcommand still works (exit 0) and prints
    the defaults, not the help."""
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
    assert "codex" in out.getvalue(), out.getvalue()


def test_visual_list_defaults_works_without_image():
    out = io.StringIO()
    saved_cfg = cli.load_config
    cli.load_config = lambda: {}
    try:
        with redirect_stderr(io.StringIO()), redirect_stdout(out):
            rc = cli.main(["visual", "--list-defaults"])
    finally:
        cli.load_config = saved_cfg
    assert rc == 0
    text = out.getvalue()
    assert "claude:claude-opus-4-8" in text, text
    assert "oc:zai/glm-4.5v" in text, text


# --- brainstorm composes with --diff grounding. --------------------------------------
def test_brainstorm_with_diff_flag_grounds_on_working_tree_diff():
    """`review brainstorm "…" --diff` (or --staged) picks up the working-tree diff as
    OPTIONAL grounding and feeds it to the handler."""
    grounding = "diff --git a/g b/g\n@@\n+grounded\n"
    captured: dict = {}

    def _fake(topic, models, cwd, timeout, moderators, rounds, max_rounds, diff="", **_k):
        captured["diff"] = diff
        return 0

    saved = _brainstorm_mod.mode_brainstorm
    saved_git = cli._git_diff
    saved_is_git_repo = cli._is_git_repo
    saved_cfg = cli.load_config
    saved_stdin = cli._read_stdin_if_piped
    _brainstorm_mod.mode_brainstorm = _fake
    cli._git_diff = lambda cwd, staged: grounding
    cli._is_git_repo = lambda cwd: True
    cli.load_config = lambda: {"models": ["codex"]}
    cli._read_stdin_if_piped = lambda: None
    try:
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            rc = cli.main(["brainstorm", "topic", "--task", "TEST-1", "--diff", "-C", str(REPO_ROOT)])
    finally:
        _brainstorm_mod.mode_brainstorm = saved
        cli._git_diff = saved_git
        cli._is_git_repo = saved_is_git_repo
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

        def _fake(question, models, diff, cwd, timeout, *_a, **_k):
            captured["diff"] = diff
            return 0

        saved = _just_ask_mod.mode_just_ask
        saved_git = cli._git_diff
        saved_is_git_repo = cli._is_git_repo
        saved_cfg = cli.load_config
        saved_stdin = cli._read_stdin_if_piped
        _just_ask_mod.mode_just_ask = _fake
        cli._git_diff = lambda cwd, staged: grounding
        cli._is_git_repo = lambda cwd: True
        cli.load_config = lambda: {"models": ["codex"]}
        cli._read_stdin_if_piped = lambda: None
        try:
            with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                rc = cli.main(argv)
        finally:
            _just_ask_mod.mode_just_ask = saved
            cli._git_diff = saved_git
            cli._is_git_repo = saved_is_git_repo
            cli.load_config = saved_cfg
            cli._read_stdin_if_piped = saved_stdin
        return rc, captured.get("diff")

    rc, diff = run(["just-ask", "Q", "--task", "TEST-1", "-C", str(REPO_ROOT)])
    assert rc == 0 and diff == "", ("no --diff -> no context", diff)
    rc, diff = run(["just-ask", "Q", "--task", "TEST-1", "--diff", "-C", str(REPO_ROOT)])
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
    assert "--task CODE" in err, err
    assert "no longer a flag" in err, err


def test_removed_quorum_flag_errors_with_pointer():
    rc, err = _capture_main(["--quorum", "x", "-C", str(REPO_ROOT)])
    assert rc == 2, rc
    assert "review quorum" in err, err
    assert "--task CODE" in err, err


def test_removed_just_ask_flag_errors_with_pointer():
    rc, err = _capture_main(["--just-ask", "x", "-C", str(REPO_ROOT)])
    assert rc == 2, rc
    assert "review just-ask" in err, err
    assert "--task CODE" in err, err


def test_removed_flag_equals_form_errors():
    rc, err = _capture_main(["--brainstorm=topic", "-C", str(REPO_ROOT)])
    assert rc == 2, rc
    assert "review brainstorm" in err, err
    assert "--task CODE" in err, err


# --- Flags removed WITH NO replacement (--mcp / --ln) fail LOUD, not opaque. ----------
# `review --mcp` used to hit argparse's bare `unrecognized arguments: --mcp` — useless to
# a user whose stale `~/.claude/mcp/mcp.json` still spawns it. Now it gets a structured
# what/why/how-to-fix error so the dead MCP registration is diagnosable (ROADMAP §9).
def test_removed_mcp_flag_errors_with_structured_pointer():
    rc, err = _capture_main(["--mcp", "-C", str(REPO_ROOT)])
    assert rc == 2, rc
    assert "`--mcp` was removed" in err, err
    # The 3-part structured error: what / why / how-to-fix.
    assert "why:" in err and "fix:" in err, err
    # It must NOT be the opaque argparse message.
    assert "unrecognized arguments" not in err, err


def test_removed_mcp_flag_names_the_registration_to_remove():
    """The fix line must point at the concrete stale registration to delete — the whole
    reason this exists is that a dead `mcp.json`/rig entry keeps spawning `review --mcp`."""
    rc, err = _capture_main(["--mcp", "-C", str(REPO_ROOT)])
    assert rc == 2, rc
    assert "mcp.json" in err, err
    assert "rig apply" in err, err


def test_removed_mcp_flag_equals_form_errors():
    rc, err = _capture_main(["--mcp=stdio", "-C", str(REPO_ROOT)])
    assert rc == 2, rc
    assert "`--mcp` was removed" in err, err


def test_removed_ln_flag_errors_with_pointer():
    rc, err = _capture_main(["--ln", "-C", str(REPO_ROOT)])
    assert rc == 2, rc
    assert "`--ln` was removed" in err, err


def test_removed_ln_flag_equals_form_errors():
    rc, err = _capture_main(["--ln=5", "-C", str(REPO_ROOT)])
    assert rc == 2, rc
    assert "`--ln` was removed" in err, err


def test_removed_quorum_check_flag_points_to_check():
    """`review task CODE --quorum-check` was renamed to `--check` (collided with the
    unrelated `review quorum` panel). It must fail LOUD with a pointer to the new spelling,
    not argparse's opaque `unrecognized arguments`."""
    rc, err = _capture_main(["task", "ABC-1", "--quorum-check"])
    assert rc == 2, rc
    assert "`--quorum-check` was removed" in err, err
    assert "--check" in err, err
    assert "unrecognized arguments" not in err, err


def test_removed_flag_caught_after_valid_global_args():
    """A stale launcher can put `--mcp` AFTER valid args (e.g. `-C <dir> --mcp`). The
    scan walks the whole pre-`--` argv, so it is still caught — not left to argparse."""
    rc, err = _capture_main(["-C", str(REPO_ROOT), "--mcp"])
    assert rc == 2, rc
    assert "`--mcp` was removed" in err, err


def test_removed_flag_caught_before_a_subcommand():
    """`--mcp brainstorm x` (removed flag before a verb) is still intercepted with the
    structured error, not parsed as a brainstorm run."""
    rc, err = _capture_main(["--mcp", "brainstorm", "x"])
    assert rc == 2, rc
    assert "`--mcp` was removed" in err, err


def test_removed_no_replacement_flag_after_double_dash_is_not_intercepted():
    """A `--mcp` AFTER `--` is a positional value, not the removed flag — the reject scan
    stops at `--`, so the structured removed-flag error must NOT fire. Driven through the
    `diff` subcommand (the diff review mode has no positional, so argparse rejects the
    stray token with a usage SystemExit)."""
    err = io.StringIO()
    raised = False
    with redirect_stderr(err), redirect_stdout(io.StringIO()):
        try:
            cli.main(["diff", "--task", "TEST-1", "--", "--mcp"])
        except SystemExit:
            raised = True  # argparse usage error on the stray positional — acceptable
    assert "`--mcp` was removed" not in err.getvalue(), err.getvalue()
    assert raised, "argparse should reject the stray positional after --"


def test_removed_flag_after_double_dash_is_not_intercepted():
    """A `--quorum` AFTER `--` is NOT the removed flag — the reject scan stops at `--`.
    The diff review mode has no positional, so argparse rejects the extra token with a
    usage SystemExit, but crucially NOT with the removed-flag message (the scan never
    fired)."""
    err = io.StringIO()
    raised = False
    with redirect_stderr(err), redirect_stdout(io.StringIO()):
        try:
            cli.main(["diff", "--task", "TEST-1", "--", "--quorum"])
        except SystemExit:
            raised = True  # argparse usage error on the stray positional — acceptable
    assert "no longer a flag" not in err.getvalue(), err.getvalue()
    assert raised, "argparse should reject the stray positional after --"


# --- The mode registry contract. -----------------------------------------------------
def test_registry_known_subcommands():
    subs = _registry.known_subcommands()
    for verb in ("diff", "visual", "brainstorm", "just-ask", "quorum", "ask"):
        assert verb in subs, (verb, subs)
    # The old stuttering verb is GONE from the subcommand set (it is a removed verb now).
    assert "review" not in subs, subs


def test_registry_get_mode_resolves_subcommand_and_alias():
    assert _registry.get_mode("brainstorm").name == "brainstorm"
    assert _registry.get_mode("ask").name == "just-ask"   # alias
    assert _registry.get_mode("diff").name == "review"    # diff verb -> the review mode
    assert _registry.get_mode("visual").name == "visual"  # canonical standalone visual
    assert _registry.get_mode("review") is None           # the old verb no longer resolves
    assert _registry.get_mode("not-a-mode") is None


def test_registry_diff_mode_is_the_review_mode():
    assert _registry.diff_mode().name == "review"
    assert _registry.diff_mode().subcommand == "diff"
    assert _registry.DIFF_MODE_NAME == "review"


def test_registry_removed_subcommands_maps_review_to_diff():
    assert _registry.REMOVED_SUBCOMMANDS.get("review") == "diff"


def test_registry_iter_modes_are_self_describing():
    """Every registered mode exposes the ModeSpec contract fields (name / subcommand /
    diff_policy / handler) — the descriptor a plugin-dir mode would also expose."""
    from reviewlib.modes.contract import DIFF_POLICIES
    seen_review_first = _registry.iter_modes()[0].name == "review"
    assert seen_review_first, "the diff-review mode must be the first registered mode"
    for mode in _registry.iter_modes():
        assert mode.name and mode.subcommand, mode
        assert mode.diff_policy in DIFF_POLICIES, mode
        assert callable(mode.handler), mode


def test_review_mode_declares_require_diff_policy():
    """The diff-review mode's descriptor declares diff_policy 'require' (it always needs a
    diff), unlike the panel modes ('none'/'optional')."""
    assert _registry.get_mode("diff").diff_policy == "require"
    assert _registry.get_mode("just-ask").diff_policy == "none"
    assert _registry.get_mode("quorum").diff_policy == "none"
    assert _registry.get_mode("brainstorm").diff_policy == "optional"
    assert _registry.get_mode("visual").diff_policy == "optional"


def test_diff_mode_empty_diff_returns_nonzero_no_diff_to_review():
    """End-to-end: a `review diff` with an EMPTY diff reaches the REAL mode_review and
    returns non-zero ('No diff to review') — the 'require' policy enforced, not just
    declared."""
    saved_cfg = cli.load_config
    saved_git = cli._git_diff
    saved_is_git_repo = cli._is_git_repo
    saved_stdin = cli._read_stdin_if_piped
    cli.load_config = lambda: {"models": ["codex"]}  # explicit models -> flat path, no board
    cli._git_diff = lambda cwd, staged: ""             # empty diff
    cli._is_git_repo = lambda cwd: True
    cli._read_stdin_if_piped = lambda: None
    try:
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            rc = cli.main(["diff", "--task", "TEST-1", "-C", str(REPO_ROOT)])
    finally:
        cli.load_config = saved_cfg
        cli._git_diff = saved_git
        cli._is_git_repo = saved_is_git_repo
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
