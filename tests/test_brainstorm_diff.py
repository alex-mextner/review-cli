#!/usr/bin/env python3
"""Unit tests for brainstorm + diff grounding (board redesign).

`--brainstorm` can now ALSO take the working-tree / --staged / piped diff into
account: when a diff is present every persona PanelJob (and the moderator) sees it
as constant grounding context, so you can brainstorm ABOUT a specific change. With
NO diff the classic pure-ideation behaviour is unchanged. These tests prove, all
offline (run_panel / run_moderator are stubbed — no model call, no network):

  (a) with a diff, EVERY persona job carries that diff (PanelJob.diff) and a prompt
      note that points the model at the ```diff``` block;
  (b) the moderator turns + the final synthesis also receive the diff;
  (c) with NO diff, persona jobs carry diff="" and no diff-note is injected — the
      pure-ideation path is byte-for-byte the old behaviour.

Same harness style as tests/test_reviewer_board.py: plain test_* functions invoked
by the __main__ block; backends/panel funcs are stubbed by reassigning module globals.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import reviewlib.modes.brainstorm as bs  # noqa: E402
from reviewlib.backends import ReviewResult  # noqa: E402

SAMPLE_DIFF = "diff --git a/x b/x\n@@\n-old\n+new\n"


class _Capture:
    """Stub run_panel + run_moderator on the brainstorm module, capturing the jobs
    and moderator (prompt, diff) so a test can assert what the personas/moderator saw.
    Forces a single round (moderator says STOP) so the test is fast and deterministic."""

    def __init__(self):
        self.persona_jobs = []          # every PanelJob across rounds
        self.moderator_calls = []       # list of (prompt, diff)

    def __enter__(self):
        self._old_panel = bs.run_panel
        self._old_mod = bs.run_moderator

        def _fake_run_panel(jobs, cwd, timeout):
            self.persona_jobs.extend(jobs)
            return [ReviewResult(model=j.label or j.model, command="fake",
                                 returncode=0, stdout="idea", stderr="") for j in jobs]

        def _fake_run_moderator(candidates, prompt, cwd, timeout, diff="", round_no=0):
            self.moderator_calls.append((prompt, diff))
            # End the loop immediately (min_rounds is clamped to 5, but the test sets
            # rounds low and we still emit STOP — the loop only breaks at >= min_rounds,
            # so keep output non-empty + rc0 and let min_rounds gate the count).
            return ReviewResult(model=candidates[0] if candidates else "mod",
                                command="fake", returncode=0,
                                stdout="summary\nDECISION: STOP", stderr="")

        bs.run_panel = _fake_run_panel
        bs.run_moderator = _fake_run_moderator
        return self

    def __exit__(self, *exc):
        bs.run_panel = self._old_panel
        bs.run_moderator = self._old_mod
        return False


def _run(diff: str) -> _Capture:
    cap = _Capture()
    with cap:
        # rounds=1/max_rounds=1 -> clamped to min 5 internally, but the persona-job
        # capture only needs >= 1 round; assertions look at the FIRST round's jobs.
        rc = bs.mode_brainstorm(
            "How should we cache?", ["codex", "gemini"], REPO_ROOT, 5,
            ["mod"], rounds=1, max_rounds=1, diff=diff,
        )
    assert rc == 0, rc
    return cap


def test_brainstorm_with_diff_feeds_every_persona_job():
    cap = _run(SAMPLE_DIFF)
    assert cap.persona_jobs, "no persona jobs ran"
    # Every persona job carries the grounding diff and the diff-note in its prompt.
    for job in cap.persona_jobs:
        assert job.diff == SAMPLE_DIFF, "persona job missing the grounding diff"
        assert "```diff```" in job.prompt or "diff``` block" in job.prompt, job.prompt
        assert "ABOUT this change" in job.prompt, "diff-note not injected into persona prompt"


def test_brainstorm_with_diff_feeds_moderator_and_synthesis():
    cap = _run(SAMPLE_DIFF)
    assert cap.moderator_calls, "moderator never ran"
    # Every moderator turn (round summaries + final synthesis) gets the diff too.
    for prompt, diff in cap.moderator_calls:
        assert diff == SAMPLE_DIFF, "moderator did not receive the grounding diff"
    # The FINAL moderator call is the synthesis; its prompt must carry the diff-note so
    # a regression on that separate code line is caught (GLM finding 9).
    synth_prompt, _synth_diff = cap.moderator_calls[-1]
    assert "FULL TRANSCRIPT" in synth_prompt, "last moderator call is not the synthesis"
    assert "ABOUT this change" in synth_prompt, "synthesis prompt missing the diff-note"


def test_brainstorm_without_diff_is_pure_ideation():
    cap = _run("")
    assert cap.persona_jobs, "no persona jobs ran"
    for job in cap.persona_jobs:
        assert job.diff == "", "no-diff brainstorm must carry empty PanelJob.diff"
        # No diff-note injected -> classic ideation prompt.
        assert "ABOUT this change" not in job.prompt
        assert "```diff" not in job.prompt
        # Byte-for-byte: with diff_note == "", the prompt must keep the exact old shape
        # "...Do not edit files.\n\nTOPIC:" — a spacing regression would slip through a
        # mere substring check (GLM finding 11).
        assert "Do not edit files.\n\nTOPIC:" in job.prompt, repr(job.prompt[:200])
    for _prompt, diff in cap.moderator_calls:
        assert diff == "", "no-diff brainstorm must not pass a diff to the moderator"


def test_brainstorm_diff_default_is_empty_backward_compatible():
    """mode_brainstorm(diff=...) defaults to "" so existing callers are unaffected."""
    cap = _Capture()
    with cap:
        rc = bs.mode_brainstorm(
            "topic", ["codex"], REPO_ROOT, 5, ["mod"], rounds=1, max_rounds=1,
        )
    assert rc == 0, rc
    for job in cap.persona_jobs:
        assert job.diff == ""
        assert "ABOUT this change" not in job.prompt


# === run_moderator signature pin: the real callee MUST accept diff= ===============
def test_run_moderator_accepts_diff_kwarg():
    """The grounded brainstorm calls run_moderator(..., diff=diff, ...). The persona/
    moderator tests stub run_moderator, so a regression that drops the `diff` parameter
    from the REAL signature would go unnoticed (GLM finding 13). Pin it here against the
    real function so the grounded path can't silently start raising TypeError in prod."""
    import inspect

    from reviewlib.panel import run_moderator

    params = inspect.signature(run_moderator).parameters
    assert "diff" in params, list(params)


def test_real_run_moderator_forwards_diff_to_backend():
    """End-to-end transport: the REAL run_moderator must actually FORWARD `diff` down to
    the backend call (not just accept the kwarg and drop it). Stub only at the
    panel.run_single layer and assert the diff reaches it (GLM finding 13)."""
    import reviewlib.panel as panel
    from reviewlib.backends import ReviewResult

    seen: dict = {}

    def _fake_run_single(model, prompt, cwd, timeout, diff="", round_no=0):
        seen["diff"] = diff
        return ReviewResult(model=model, command="fake", returncode=0, stdout="ok", stderr="")

    old = panel.run_single
    panel.run_single = _fake_run_single
    try:
        panel.run_moderator(["mod"], "summarize", REPO_ROOT, 5, diff=SAMPLE_DIFF, round_no=1)
    finally:
        panel.run_single = old
    assert seen.get("diff") == SAMPLE_DIFF, repr(seen.get("diff"))


# === CLI-level diff acquisition for --brainstorm ==================================
# These monkeypatch cli._git_diff with a SENTINEL (instead of relying on the dev tree
# being dirty) so the assertions hold regardless of checkout cleanliness (GLM finding 2).
def _capture_cli_brainstorm_diff(argv: list[str], *, stdin_text: str | None,
                                 git_diff) -> dict:
    """Run `cli.main(argv)` with mode_brainstorm + cli._git_diff stubbed, returning
    {"diff": <passed to mode_brainstorm>, "git_called": bool}. `stdin_text=None` -> a
    TTY (no pipe, `_read_stdin_if_piped` returns None); a string ("" or non-empty) ->
    a pipe with that content. `git_diff` is a callable used to fake _git_diff."""
    import io
    import os

    from reviewlib import cli

    captured: dict = {"git_called": False}

    def _fake_brainstorm(topic, models, cwd, timeout, moderators, rounds, max_rounds, diff=""):
        captured["diff"] = diff
        return 0

    def _wrapped_git_diff(cwd, staged):
        captured["git_called"] = True
        return git_diff(cwd, staged)

    old_mb = cli.mode_brainstorm
    old_git = cli._git_diff
    old_load_config = cli.load_config
    old_env = os.environ.get("GEMINI_ENV_FILE")
    cli.mode_brainstorm = _fake_brainstorm
    cli._git_diff = _wrapped_git_diff
    cli.load_config = lambda: {}  # no config models, deterministic
    old_stdin = sys.stdin
    try:
        os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
        if stdin_text is None:
            class _Tty(io.StringIO):
                def isatty(self):
                    return True

            sys.stdin = _Tty("")
        else:
            sys.stdin = io.StringIO(stdin_text)  # StringIO.isatty() -> False (a pipe)
        cli.main(argv)
    finally:
        cli.mode_brainstorm = old_mb
        cli._git_diff = old_git
        cli.load_config = old_load_config
        sys.stdin = old_stdin
        # Restore the env var instead of leaking it across tests (GLM finding 15).
        if old_env is None:
            os.environ.pop("GEMINI_ENV_FILE", None)
        else:
            os.environ["GEMINI_ENV_FILE"] = old_env
    return captured


def _git_ok(_cwd, _staged):
    return "diff --git a/w b/w\n@@\n+grounded\n"


def _git_raises(_cwd, _staged):
    raise RuntimeError("not a git repository")


def test_cli_brainstorm_picks_up_working_tree_diff():
    """--brainstorm (no pipe) feeds the working-tree diff into mode_brainstorm — the
    happy path (GLM finding 3), proven via a sentinel _git_diff, not tree dirtiness."""
    cap = _capture_cli_brainstorm_diff(
        ["--brainstorm", "topic", "-C", str(REPO_ROOT)], stdin_text=None, git_diff=_git_ok,
    )
    assert cap["git_called"] is True
    assert cap["diff"] == _git_ok(None, None), repr(cap["diff"])


def test_cli_brainstorm_staged_picks_up_staged_diff():
    """--brainstorm --staged feeds the staged diff (git diff --cached) as grounding."""
    seen: dict = {}

    def _git(cwd, staged):
        seen["staged"] = staged
        return "diff --git a/s b/s\n@@\n+staged\n"

    cap = _capture_cli_brainstorm_diff(
        ["--brainstorm", "topic", "--staged", "-C", str(REPO_ROOT)], stdin_text=None, git_diff=_git,
    )
    assert seen["staged"] is True, "staged flag not forwarded to git diff"
    assert cap["diff"] == "diff --git a/s b/s\n@@\n+staged\n", repr(cap["diff"])


def test_cli_brainstorm_staged_nonrepo_degrades_to_ideation():
    """--staged --brainstorm against a NON-repo (git diff raises) must degrade to pure
    ideation (diff == ""), NOT raise — the docs promise graceful degradation (codex P2)."""
    cap = _capture_cli_brainstorm_diff(
        ["--staged", "--brainstorm", "topic", "-C", str(REPO_ROOT)], stdin_text=None, git_diff=_git_raises,
    )
    assert cap["git_called"] is True
    assert cap["diff"] == "", repr(cap["diff"])


def test_cli_brainstorm_nonempty_pipe_takes_precedence_over_worktree():
    """A non-empty piped diff is used as-is and git diff is NOT consulted (precedence)."""
    piped = "diff --git a/z b/z\n@@\n+zzz\n"
    cap = _capture_cli_brainstorm_diff(
        ["--brainstorm", "topic", "-C", str(REPO_ROOT)], stdin_text=piped, git_diff=_git_ok,
    )
    assert cap["git_called"] is False, "non-empty pipe must win without probing the tree"
    assert cap["diff"] == piped, repr(cap["diff"])


def test_cli_brainstorm_empty_pipe_falls_back_to_worktree():
    """An empty / `< /dev/null` stdin reads as None, so brainstorm still probes the
    working-tree diff — matching every other mode and the documented
    `--brainstorm "Q" < /dev/null` convention (an empty redirect must NOT suppress
    grounding). The 2nd-pass board review (codex) flagged the opposite gating as a
    regression for non-interactive runners; this pins the chosen behaviour."""
    cap = _capture_cli_brainstorm_diff(
        ["--brainstorm", "topic", "-C", str(REPO_ROOT)], stdin_text="", git_diff=_git_ok,
    )
    assert cap["git_called"] is True, "empty pipe must fall back to the working-tree diff"
    assert cap["diff"] == _git_ok(None, None), repr(cap["diff"])


def test_cli_default_review_staged_nonrepo_still_hard_fails():
    """The needs_diff formula change ((... ) and brainstorm is None) must NOT relax the
    DEFAULT review: `review --staged` (no brainstorm) against a non-repo must still
    hard-fail (git diff raises, uncaught), not degrade (GLM finding 5)."""
    import io
    import os

    from reviewlib import cli

    old_git = cli._git_diff
    old_load_config = cli.load_config
    old_mr = cli.mode_review
    old_env = os.environ.get("GEMINI_ENV_FILE")
    cli._git_diff = _git_raises
    cli.load_config = lambda: {}
    cli.mode_review = lambda *_a, **_k: 0  # must never be reached
    old_stdin = sys.stdin
    try:
        os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"

        class _Tty(io.StringIO):
            def isatty(self):
                return True

        sys.stdin = _Tty("")
        raised = False
        try:
            cli.main(["--staged", "-C", str(REPO_ROOT)])
        except RuntimeError:
            raised = True  # default --staged review hard-fails on a non-repo, as before
        assert raised, "default --staged review must still hard-fail on a non-repo"
    finally:
        cli._git_diff = old_git
        cli.load_config = old_load_config
        cli.mode_review = old_mr
        sys.stdin = old_stdin
        if old_env is None:
            os.environ.pop("GEMINI_ENV_FILE", None)
        else:
            os.environ["GEMINI_ENV_FILE"] = old_env


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
