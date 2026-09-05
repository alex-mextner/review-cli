#!/usr/bin/env python3
"""Unit tests for brainstorm + diff grounding (board redesign).

`--brainstorm` can now ALSO take the working-tree / --staged / piped diff into
account: when a diff is present it grounds the discussion. With NO diff the
classic pure-ideation behaviour is unchanged. These tests prove, all offline
(run_panel / run_moderator are stubbed — no model call, no network):

  (a) with a diff, ONLY the FIRST round of an invocation's persona jobs carry that
      diff (PanelJob.diff) and a prompt note pointing at the ```diff``` block —
      round 2+ personas rely on the shared transcript instead (Alex, 2026-08-28:
      incremental diff-grounding, don't re-send the diff every round);
  (b) the moderator turns + the final synthesis receive the diff on EVERY round
      (unaffected by (a) — they need it to judge convergence / write the
      recommendation);
  (c) with NO diff, persona jobs carry diff="" and no diff-note is injected — the
      pure-ideation path is byte-for-byte the old behaviour;
  (d) a RESUMED invocation (`start_round > 1`) with a freshly re-attached diff
      grounds its FIRST executed round (`round_no == start_round`), not literal
      round 1 — round 1 doesn't run in a resumed process at all.

Same harness style as tests/test_reviewer_board.py: plain test_* functions invoked
by the __main__ block; backends/panel funcs are stubbed by reassigning module globals.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import reviewlib.modes.brainstorm as bs  # noqa: E402
from reviewlib.backends import ReviewResult  # noqa: E402

# Same directory as this file (tests/) — Python auto-adds a directly-run script's own
# dir to sys.path, and pytest's classic (no __init__.py) import mode does the same for
# a collected test file, so this sibling import works under both invocation styles.
from _brainstorm_env_isolation import (  # noqa: E402
    with_isolated_brainstorm_paths as _with_isolated_brainstorm_paths,
)

SAMPLE_DIFF = "diff --git a/x b/x\n@@\n-old\n+new\n"


class _Capture:
    """Stub run_panel + run_moderator on the brainstorm module, capturing the jobs
    and moderator (prompt, diff) so a test can assert what the personas/moderator saw.
    Forces a single round (moderator says STOP) so the test is fast and deterministic."""

    def __init__(self):
        self.persona_jobs = []  # every PanelJob across rounds
        self.moderator_calls = []  # list of (prompt, diff)

    def __enter__(self):
        self._old_panel = bs.run_panel
        self._old_mod = bs.run_moderator

        def _fake_run_panel(jobs, cwd, timeout):
            self.persona_jobs.extend(jobs)
            return [
                ReviewResult(
                    model=j.label or j.model,
                    command="fake",
                    returncode=0,
                    stdout="idea",
                    stderr="",
                )
                for j in jobs
            ]

        def _fake_run_moderator(candidates, prompt, cwd, timeout, diff="", round_no=0):
            self.moderator_calls.append((prompt, diff))
            # End the loop immediately (min_rounds is clamped to 3, but the test sets
            # rounds low and we still emit STOP — the loop only breaks at >= min_rounds,
            # so keep output non-empty + rc0 and let min_rounds gate the count).
            return ReviewResult(
                model=candidates[0] if candidates else "mod",
                command="fake",
                returncode=0,
                stdout="summary\nDECISION: STOP",
                stderr="",
            )

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
        # rounds=1/max_rounds=1 -> clamped to min 3 internally (min_rounds floor).
        rc = _with_isolated_brainstorm_paths(
            lambda: bs.mode_brainstorm(
                "How should we cache?",
                ["codex", "gemini"],
                REPO_ROOT,
                5,
                ["mod"],
                rounds=1,
                max_rounds=1,
                diff=diff,
            )
        )
    assert rc == 0, rc
    return cap


def test_brainstorm_with_diff_feeds_round_one_persona_jobs_only():
    # Incremental diff-grounding (Alex, 2026-08-28): round 1 personas get the full
    # diff; round 2+ personas don't need it re-sent, they already have it via the
    # shared transcript. min_rounds clamps to 3, so this run produces 3 rounds.
    cap = _run(SAMPLE_DIFF)
    assert cap.persona_jobs, "no persona jobs ran"
    round1_jobs = [j for j in cap.persona_jobs if j.round_no == 1]
    later_jobs = [j for j in cap.persona_jobs if j.round_no != 1]
    assert round1_jobs, "no round-1 persona jobs captured"
    assert later_jobs, "no round-2+ persona jobs captured (min_rounds floor changed?)"
    for job in round1_jobs:
        assert job.diff == SAMPLE_DIFF, "round-1 persona job missing the grounding diff"
        assert "```diff```" in job.prompt or "diff``` block" in job.prompt, job.prompt
        assert "ABOUT this change" in job.prompt, (
            "diff-note not injected into round-1 persona prompt"
        )
    for job in later_jobs:
        assert job.diff == "", "round-2+ persona job should not re-carry the full diff"
        assert "```diff```" not in job.prompt, (
            "round-2+ prompt should not re-fence the diff"
        )
        assert "shown to earlier-round personas" in job.prompt, (
            "round-2+ persona prompt missing the earlier-round pointer note"
        )


def test_resumed_brainstorm_with_diff_grounds_its_first_executed_round():
    # Review finding (3 independent reviewers): gating on literal `round_no == 1`
    # silently dropped grounding for a RESUMED invocation, which never executes
    # round 1 in this process. A resume with a freshly re-attached diff must ground
    # its FIRST EXECUTED round (round_no == start_round), not round 1.
    cap = _Capture()
    with cap:
        rc = _with_isolated_brainstorm_paths(
            lambda: bs.mode_brainstorm(
                "How should we cache?",
                ["codex", "gemini"],
                REPO_ROOT,
                5,
                ["mod"],
                rounds=1,
                max_rounds=1,
                diff=SAMPLE_DIFF,
                seed_transcript=[
                    "## Round 1\nprior idea",
                    "## Round 2\nmore prior idea",
                ],
                seed_persona_index=6,
                start_round=3,
            )
        )
    assert rc == 0, rc
    assert cap.persona_jobs, "no persona jobs ran"
    round_numbers = {j.round_no for j in cap.persona_jobs}
    assert round_numbers == {3}, (
        "resumed run should only execute round 3 given rounds=1/max_rounds=1 "
        f"clamped to min_rounds=3 starting at start_round=3, got {round_numbers}"
    )
    for job in cap.persona_jobs:
        assert job.diff == SAMPLE_DIFF, (
            "the first executed round of a resume must carry the re-attached diff"
        )
        assert "```diff```" in job.prompt or "diff``` block" in job.prompt, job.prompt
        assert "ABOUT this change" in job.prompt, (
            "diff-note not injected into the resumed run's first-executed-round prompt"
        )
        assert "shown to earlier-round personas" not in job.prompt, (
            "the resumed run's first executed round should NOT claim the diff was "
            "already shown — this run has not shown it yet"
        )


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
        rc = _with_isolated_brainstorm_paths(
            lambda: bs.mode_brainstorm(
                "topic",
                ["codex"],
                REPO_ROOT,
                5,
                ["mod"],
                rounds=1,
                max_rounds=1,
            )
        )
    assert rc == 0, rc
    for job in cap.persona_jobs:
        assert job.diff == ""
        assert "ABOUT this change" not in job.prompt


def test_brainstorm_discussion_log_records_task_code():
    """The persisted brainstorm md must carry task=... so task history survives after
    per-call logs age out."""
    old_log = os.environ.get("REVIEW_LOG_DIR")
    old_task = os.environ.get("REVIEW_TASK_CODE")
    with tempfile.TemporaryDirectory() as d:
        os.environ["REVIEW_LOG_DIR"] = d
        os.environ["REVIEW_TASK_CODE"] = "HYP-742"
        cap = _Capture()
        try:
            with cap:
                rc = bs.mode_brainstorm(
                    "topic",
                    ["codex"],
                    REPO_ROOT,
                    5,
                    ["mod"],
                    rounds=1,
                    max_rounds=1,
                )
            assert rc == 0, rc
            logs = list(Path(d).glob("*-brainstorm.md"))
            assert len(logs) == 1, logs
            text = logs[0].read_text(encoding="utf-8")
            assert "task=HYP-742" in text
        finally:
            if old_log is None:
                os.environ.pop("REVIEW_LOG_DIR", None)
            else:
                os.environ["REVIEW_LOG_DIR"] = old_log
            if old_task is None:
                os.environ.pop("REVIEW_TASK_CODE", None)
            else:
                os.environ["REVIEW_TASK_CODE"] = old_task


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
        return ReviewResult(
            model=model, command="fake", returncode=0, stdout="ok", stderr=""
        )

    old = panel.run_single
    panel.run_single = _fake_run_single
    try:
        panel.run_moderator(
            ["mod"], "summarize", REPO_ROOT, 5, diff=SAMPLE_DIFF, round_no=1
        )
    finally:
        panel.run_single = old
    assert seen.get("diff") == SAMPLE_DIFF, repr(seen.get("diff"))


# === CLI-level diff acquisition for --brainstorm ==================================
# These monkeypatch cli._git_diff with a SENTINEL (instead of relying on the dev tree
# being dirty) so the assertions hold regardless of checkout cleanliness (GLM finding 2).
def _capture_cli_brainstorm_diff(
    argv: list[str], *, stdin_text: str | None, git_diff
) -> dict:
    """Run `cli.main(argv)` with mode_brainstorm + cli._git_diff stubbed, returning
    {"diff": <passed to mode_brainstorm>, "git_called": bool}. `stdin_text=None` -> a
    TTY (no pipe, `_read_stdin_if_piped` returns None); a string ("" or non-empty) ->
    a pipe with that content. `git_diff` is a callable used to fake _git_diff."""
    import io
    import os

    from reviewlib import cli
    from reviewlib.modes import brainstorm as brainstorm_mod

    captured: dict = {"git_called": False}

    def _fake_brainstorm(
        topic, models, cwd, timeout, moderators, rounds, max_rounds, diff="", **_k
    ):
        captured["diff"] = diff
        return 0

    def _wrapped_git_diff(cwd, staged):
        captured["git_called"] = True
        return git_diff(cwd, staged)

    # The mode handler calls the module-level mode_brainstorm, so patch it WHERE IT IS
    # DEFINED (the subcommand redesign dispatches through modes/registry, not a
    # cli.mode_brainstorm attribute).
    old_mb = brainstorm_mod.mode_brainstorm
    old_git = cli._git_diff
    old_load_config = cli.load_config
    old_env = os.environ.get("GEMINI_ENV_FILE")
    old_task = os.environ.get("REVIEW_TASK_CODE")
    brainstorm_mod.mode_brainstorm = _fake_brainstorm
    cli._git_diff = _wrapped_git_diff
    cli.load_config = lambda: {}  # no config models, deterministic
    old_stdin = sys.stdin
    try:
        os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
        os.environ["REVIEW_TASK_CODE"] = "TEST-1"
        if stdin_text is None:

            class _Tty(io.StringIO):
                def isatty(self):
                    return True

            sys.stdin = _Tty("")
        else:
            sys.stdin = io.StringIO(stdin_text)  # StringIO.isatty() -> False (a pipe)
        cli.main(argv)
    finally:
        brainstorm_mod.mode_brainstorm = old_mb
        cli._git_diff = old_git
        cli.load_config = old_load_config
        sys.stdin = old_stdin
        # Restore the env var instead of leaking it across tests (GLM finding 15).
        if old_env is None:
            os.environ.pop("GEMINI_ENV_FILE", None)
        else:
            os.environ["GEMINI_ENV_FILE"] = old_env
        if old_task is None:
            os.environ.pop("REVIEW_TASK_CODE", None)
        else:
            os.environ["REVIEW_TASK_CODE"] = old_task
    return captured


def _git_ok(_cwd, _staged):
    return "diff --git a/w b/w\n@@\n+grounded\n"


def _git_raises(_cwd, _staged):
    raise RuntimeError("not a git repository")


def test_cli_brainstorm_picks_up_working_tree_diff():
    """--brainstorm (no pipe) feeds the working-tree diff into mode_brainstorm — the
    happy path (GLM finding 3), proven via a sentinel _git_diff, not tree dirtiness."""
    cap = _capture_cli_brainstorm_diff(
        ["brainstorm", "topic", "-C", str(REPO_ROOT)],
        stdin_text=None,
        git_diff=_git_ok,
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
        ["brainstorm", "topic", "--staged", "-C", str(REPO_ROOT)],
        stdin_text=None,
        git_diff=_git,
    )
    assert seen["staged"] is True, "staged flag not forwarded to git diff"
    assert cap["diff"] == "diff --git a/s b/s\n@@\n+staged\n", repr(cap["diff"])


def test_cli_brainstorm_staged_nonrepo_degrades_to_ideation():
    """--staged --brainstorm against a NON-repo (git diff raises) must degrade to pure
    ideation (diff == ""), NOT raise — the docs promise graceful degradation (codex P2)."""
    cap = _capture_cli_brainstorm_diff(
        ["brainstorm", "topic", "--staged", "-C", str(REPO_ROOT)],
        stdin_text=None,
        git_diff=_git_raises,
    )
    assert cap["git_called"] is True
    assert cap["diff"] == "", repr(cap["diff"])


def test_cli_brainstorm_nonempty_pipe_takes_precedence_over_worktree():
    """A non-empty piped diff is used as-is and git diff is NOT consulted (precedence)."""
    piped = "diff --git a/z b/z\n@@\n+zzz\n"
    cap = _capture_cli_brainstorm_diff(
        ["brainstorm", "topic", "-C", str(REPO_ROOT)],
        stdin_text=piped,
        git_diff=_git_ok,
    )
    assert cap["git_called"] is False, (
        "non-empty pipe must win without probing the tree"
    )
    assert cap["diff"] == piped, repr(cap["diff"])


def _with_default_diff_cap(fn):
    """Clear $REVIEW_DIFF_MAX_BYTES for the duration of `fn` — the two cap tests below
    assume DIFF_MAX_BYTES_DEFAULT, so a host with this exported would otherwise flake
    (the exact ambient-env class test_diff_cap.py's `_with_default_cap` was written to
    fix; kimi review finding: these two tests were missed when that fix landed)."""
    saved = os.environ.get("REVIEW_DIFF_MAX_BYTES")
    try:
        os.environ.pop("REVIEW_DIFF_MAX_BYTES", None)
        return fn()
    finally:
        if saved is None:
            os.environ.pop("REVIEW_DIFF_MAX_BYTES", None)
        else:
            os.environ["REVIEW_DIFF_MAX_BYTES"] = saved


def test_cli_brainstorm_oversized_worktree_diff_is_capped_for_dispatch():
    """codex/kimi P1 finding (2026-08 token-burn investigation): brainstorm auto-probes
    the working-tree diff BY DEFAULT (no --diff needed) and, before this fix, sent it
    UNCAPPED to every persona every round — the worst token-burn multiplier found (an
    order of magnitude worse than the single review-diff panel the cap originally
    covered). Pins that an oversized auto-detected diff reaching `mode_brainstorm` is
    now capped, exactly like `review diff`'s own dispatch."""
    from reviewlib import backends

    big = "diff --git a/x b/x\n" + ("+line\n" * 100_000)
    assert len(big.encode("utf-8")) > backends.DIFF_MAX_BYTES_DEFAULT

    def _git_big(_cwd, _staged):
        return big

    cap = _with_default_diff_cap(
        lambda: _capture_cli_brainstorm_diff(
            ["brainstorm", "topic", "-C", str(REPO_ROOT)],
            stdin_text=None,
            git_diff=_git_big,
        )
    )
    assert cap["git_called"] is True
    assert "[review-cli] diff truncated at" in cap["diff"]
    assert len(cap["diff"].encode("utf-8")) < len(big.encode("utf-8"))


def test_cli_brainstorm_piped_diff_is_not_capped():
    """The stdin exemption applies here too: an explicitly piped diff is the user's own
    already-scoped choice (matching mode_review's identical exemption), so it must reach
    mode_brainstorm byte-identical even when it exceeds the cap."""
    from reviewlib import backends

    big_piped = "diff --git a/x b/x\n" + ("+line\n" * 100_000)
    assert len(big_piped.encode("utf-8")) > backends.DIFF_MAX_BYTES_DEFAULT

    cap = _with_default_diff_cap(
        lambda: _capture_cli_brainstorm_diff(
            ["brainstorm", "topic", "-C", str(REPO_ROOT)],
            stdin_text=big_piped,
            git_diff=_git_ok,
        )
    )
    assert cap["git_called"] is False
    assert cap["diff"] == big_piped, "a piped diff must never be truncated"


def test_cli_brainstorm_empty_pipe_falls_back_to_worktree():
    """An empty / `< /dev/null` stdin reads as None, so brainstorm still probes the
    working-tree diff — matching every other mode and the documented
    `--brainstorm "Q" < /dev/null` convention (an empty redirect must NOT suppress
    grounding). The 2nd-pass board review (codex) flagged the opposite gating as a
    regression for non-interactive runners; this pins the chosen behaviour."""
    cap = _capture_cli_brainstorm_diff(
        ["brainstorm", "topic", "-C", str(REPO_ROOT)],
        stdin_text="",
        git_diff=_git_ok,
    )
    assert cap["git_called"] is True, (
        "empty pipe must fall back to the working-tree diff"
    )
    assert cap["diff"] == _git_ok(None, None), repr(cap["diff"])


def test_cli_default_review_staged_nonrepo_fails_gracefully():
    """The needs_diff formula change ((... ) and brainstorm is None) must NOT relax the
    diff review: `review diff --staged` where `git diff` fails must STILL fail (the review
    handler must NEVER run on an empty/degraded diff — GLM finding 5). What CHANGED with
    the no-git-graceful work: it no longer fails by leaking a raw RuntimeError traceback —
    it fails GRACEFULLY with a stable non-zero exit (EXIT_GIT_DIFF_FAILED) and a structured
    message. So: the run exits non-zero, mode_review is never reached, and no raw
    RuntimeError escapes. (`-C REPO_ROOT` IS a real repo, so `_is_git_repo` passes and the
    stubbed `_git_diff` failure stands in for an in-repo `git diff` blowup.)"""
    import io
    import os

    from reviewlib import cli
    from reviewlib.cli import EXIT_GIT_DIFF_FAILED
    from reviewlib.modes import review as review_mod

    old_git = cli._git_diff
    old_is_git_repo = cli._is_git_repo
    old_load_config = cli.load_config
    old_mr = review_mod.mode_review
    old_env = os.environ.get("GEMINI_ENV_FILE")
    cli._git_diff = _git_raises
    cli._is_git_repo = lambda _cwd: True
    cli.load_config = lambda: {}
    ran = {"review": False}

    def _fake_review(*_a, **_k):
        ran["review"] = (
            True  # must never be reached — the diff is REQUIRED, not degraded
        )
        return 0

    review_mod.mode_review = _fake_review
    old_stdin = sys.stdin
    try:
        os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"

        class _Tty(io.StringIO):
            def isatty(self):
                return True

        sys.stdin = _Tty("")
        raised = False
        rc = None
        try:
            rc = cli.main(
                ["diff", "--task", "TEST-1", "--staged", "-C", str(REPO_ROOT)]
            )
        except RuntimeError:
            raised = True  # the OLD bug: a raw traceback. The graceful path must NOT do this.
        assert not raised, (
            "default --staged review must FAIL GRACEFULLY, not raise a traceback"
        )
        assert rc == EXIT_GIT_DIFF_FAILED, (
            rc
        )  # stable non-zero, distinct from not-a-repo
        assert not ran["review"], (
            "the review handler must NOT run on a failed/empty diff"
        )
    finally:
        cli._git_diff = old_git
        cli._is_git_repo = old_is_git_repo
        cli.load_config = old_load_config
        review_mod.mode_review = old_mr
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
