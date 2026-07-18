#!/usr/bin/env python3
"""Tests for `review diff --staged --commit` (the checkpoint-commit feature).

Context: an agent iterating a review-fix-loop (review -> fix findings -> re-review) ran
`git reset --hard` mid-loop to discard a bad attempt and wiped uncommitted work belonging
to a DIFFERENT session sharing the same checkout. `--commit` gives the loop a SAFE
checkpoint instead: a real `git commit` of the reviewed staged diff, undoable with `git
reset --soft HEAD~1` (leaves untracked/foreign files alone) instead of `reset --hard`
(wipes everything).

`--commit` REQUIRES `--staged` (a hard usage error otherwise, not a silent no-op and not
an implicit `--staged`). Once staged, the checkpoint gate mirrors
`_stamp_if_staged_commit_review`'s three conditions exactly: `ok` (the pool produced
usable verdicts — NOT "no findings"; a review with open findings still checkpoints, same
as the existing stamp gate), `staged`, and NOT `diff_from_stdin` (a piped diff is not the
git index). The actual `git commit` subprocess can itself fail (a hook rejection, "nothing
to commit") — that is a DISTINCT failure class (`EXIT_COMMIT_FAILED`) from a normal review
failure, and must never be silently swallowed.

Hermetic: routes every backend to the in-process fake (REVIEW_FAKE_BACKEND) — no network,
no CLI. Real temp git repos via `_init_git_repo` (mirrors tests/test_review_marker.py).
Calls `mode_review(...)` directly (not through the CLI parser). Each `test_*` function is
invoked by the __main__ block below, NO pytest required (runs under the documented CI
runner, `python3 tests/...` via tests/smoke.py).

Run from the repo root::

    python3 tests/test_review_commit_checkpoint.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Make the in-repo package importable without an install (mirrors the bin/review shim).
sys.path.insert(0, str(REPO_ROOT))

from reviewlib.backends import ReviewResult  # noqa: E402
from reviewlib.modes import review as review_mode  # noqa: E402
from reviewlib.modes.review import (  # noqa: E402
    _CHECKPOINT_COMMIT_MESSAGE,
    EXIT_COMMIT_FAILED,
    EXIT_COMMIT_REQUIRES_STAGED,
    mode_review,
)

# Mirrors tests/test_review_marker.py's _MARKER_ENV: REVIEW_MARKER/HOME are sandboxed too,
# because a genuine ok+staged run through mode_review touches the review marker as a side
# effect (_stamp_if_staged_commit_review) -- without this, running this file standalone would
# mutate the developer's real ~/.cache/agent-tools/last-review (codex review finding on this
# feature's own PR).
_ENV_VARS = ("REVIEW_FAKE_BACKEND", "REVIEW_MARKER", "HOME")


class _EnvSandbox:
    """Snapshot + restore the backend-routing env vars so tests don't leak into one
    another (mirrors tests/test_review_marker.py's _EnvSandbox)."""

    def __enter__(self):
        self._saved = {name: os.environ.get(name) for name in _ENV_VARS}
        return self

    def __exit__(self, *exc):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        return False


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)


def _make_staged_repo(tmp: Path, *, filename: str = "a.txt") -> tuple[Path, str]:
    """A real repo with one staged change. Returns (repo, staged_diff). Also redirects
    REVIEW_MARKER under `tmp` so a genuine ok+staged review in the caller never touches the
    developer's real ~/.cache/agent-tools/last-review."""
    os.environ["REVIEW_MARKER"] = str(tmp / "last-review")
    repo = tmp / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    (repo / filename).write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", filename], cwd=repo, check=True)
    diff = subprocess.run(
        ["git", "diff", "--cached"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return repo, diff


def _head_count(repo: Path) -> int:
    proc = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"], cwd=repo, capture_output=True, text=True
    )
    return int(proc.stdout.strip()) if proc.returncode == 0 else 0


def _failing_backend(model, prompt, diff, cwd, timeout, round_no=0):
    return ReviewResult(
        model=model, command="fake-fail", returncode=1, stdout="", stderr="boom"
    )


def test_commit_without_staged_is_a_usage_error():
    """--commit without --staged errors with the dedicated exit code, no commit attempted,
    and no backend is even dispatched (the check runs before the panel)."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo, diff = _make_staged_repo(tmp)
        os.environ["REVIEW_FAKE_BACKEND"] = "1"
        before = _head_count(repo)
        rc = mode_review(
            ["codex"],
            prompt="p",
            diff=diff,
            cwd=repo,
            timeout=30,
            staged=False,
            commit=True,
        )
        assert rc == EXIT_COMMIT_REQUIRES_STAGED, rc
        assert _head_count(repo) == before, "no commit should be attempted"


def test_ok_staged_commit_creates_a_real_commit():
    """ok=True, staged=True, --commit -> a real git commit is created; HEAD advances and
    the message matches this repo's own conventional-commit commit-msg hook pattern."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo, diff = _make_staged_repo(tmp)
        os.environ["REVIEW_FAKE_BACKEND"] = "1"
        before = _head_count(repo)
        rc = mode_review(
            ["codex"],
            prompt="p",
            diff=diff,
            cwd=repo,
            timeout=30,
            staged=True,
            commit=True,
        )
        assert rc == 0, rc
        assert _head_count(repo) == before + 1, (
            "HEAD should advance by exactly one commit"
        )
        subject = subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        import re

        pattern = r"^(feat|fix|refactor|chore|test|docs|style|perf|build|ci|revert)(\([a-z0-9._/-]+\))?(!)?: .+"
        assert re.match(pattern, subject), (
            f"commit subject {subject!r} must satisfy the conventional-commit hook pattern"
        )


def test_ok_staged_commit_checkpoints_even_with_open_findings():
    """The checkpoint gates on `ok` (every seat produced a usable verdict), NOT on "no
    findings" — a review that reports issues but every backend ran cleanly must still be
    checkpointed. The fake backend's stdout carries a finding-shaped suggestion and the
    returncode is still 0, so this exercises exactly that distinction."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo, diff = _make_staged_repo(tmp)
        os.environ["REVIEW_FAKE_BACKEND"] = "1"
        before = _head_count(repo)
        rc = mode_review(
            ["codex"],
            prompt="p",
            diff=diff,
            cwd=repo,
            timeout=30,
            staged=True,
            commit=True,
        )
        assert rc == 0, rc
        assert _head_count(repo) == before + 1, (
            "a reviewed-with-findings diff must still checkpoint"
        )


def test_failing_review_does_not_checkpoint():
    """ok=False (a failing backend, mirroring test_review_marker's _failing_backend
    pattern) -> NO commit is created even with --commit --staged, and the exit code is
    the ORDINARY review-failure 1, not EXIT_COMMIT_FAILED (the checkpoint never ran)."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo, diff = _make_staged_repo(tmp)
        before = _head_count(repo)
        original = review_mode.resolve_backend
        review_mode.resolve_backend = lambda model: _failing_backend
        try:
            rc = mode_review(
                ["codex"],
                prompt="p",
                diff=diff,
                cwd=repo,
                timeout=30,
                staged=True,
                commit=True,
            )
        finally:
            review_mode.resolve_backend = original
        assert rc == 1, rc
        assert _head_count(repo) == before, "a FAILED review must NOT be checkpointed"


def test_piped_staged_diff_does_not_checkpoint():
    """diff_from_stdin=True with --staged --commit -> NO commit (piped stdin is not the
    git index, same reasoning as test_review_marker's
    test_piped_staged_review_does_not_touch_marker). The review itself still succeeds."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo, diff = _make_staged_repo(tmp)
        os.environ["REVIEW_FAKE_BACKEND"] = "1"
        before = _head_count(repo)
        rc = mode_review(
            ["codex"],
            prompt="p",
            diff=diff,
            cwd=repo,
            timeout=30,
            staged=True,
            commit=True,
            diff_from_stdin=True,
        )
        assert rc == 0, rc
        assert _head_count(repo) == before, (
            "a piped --staged diff must NOT be checkpointed"
        )


def test_commit_hook_failure_is_a_distinct_exit_code():
    """The review succeeds and the gate is satisfied, but the `git commit` subprocess
    itself fails (a rejecting pre-commit hook here) -> the process exits
    EXIT_COMMIT_FAILED (distinct from both 0 and the ordinary review-failure 1), no
    commit is created, and a clear stderr message is printed explaining why."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo, diff = _make_staged_repo(tmp)
        os.environ["REVIEW_FAKE_BACKEND"] = "1"
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.write_text(
            "#!/bin/sh\necho 'blocked by hook' >&2\nexit 1\n", encoding="utf-8"
        )
        hook.chmod(0o755)
        before = _head_count(repo)

        import io
        import contextlib

        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            rc = mode_review(
                ["codex"],
                prompt="p",
                diff=diff,
                cwd=repo,
                timeout=30,
                staged=True,
                commit=True,
            )
        assert rc == EXIT_COMMIT_FAILED, rc
        assert rc not in (0, 1), (
            "must be distinct from both success and a normal review failure"
        )
        assert _head_count(repo) == before, (
            "the hook rejection must leave HEAD untouched"
        )
        stderr_text = stderr_capture.getvalue()
        assert "checkpoint" in stderr_text.lower()
        assert "blocked by hook" in stderr_text


def test_index_drift_since_review_refuses_to_checkpoint():
    """TOCTOU guard (codex review finding on this feature's own PR): a review is
    multi-minute / multi-model, leaving a window in which another process/session sharing
    the same checkout could stage additional changes. If the staged index no longer
    matches the diff that was actually reviewed by the time --commit runs, the checkpoint
    must REFUSE (EXIT_COMMIT_FAILED) rather than silently commit unreviewed/unrelated
    staged work -- exactly the "sweep in someone else's changes" accident this feature
    exists to prevent."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo, diff = _make_staged_repo(tmp)
        os.environ["REVIEW_FAKE_BACKEND"] = "1"
        # Simulate drift: something stages an EXTRA file AFTER `diff` was captured (the
        # snapshot the review actually ran against) but BEFORE the checkpoint commit fires.
        (repo / "b.txt").write_text("sneaky\n", encoding="utf-8")
        subprocess.run(["git", "add", "b.txt"], cwd=repo, check=True)
        before = _head_count(repo)

        import contextlib
        import io

        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            rc = mode_review(
                ["codex"],
                prompt="p",
                diff=diff,
                cwd=repo,
                timeout=30,
                staged=True,
                commit=True,
            )
        assert rc == EXIT_COMMIT_FAILED, rc
        assert _head_count(repo) == before, "drifted index must NOT be checkpointed"
        assert "changed since the review ran" in stderr_capture.getvalue()


def test_hook_mutating_index_during_commit_is_undone_not_checkpointed():
    """P1 codex finding on this feature's own PR (review-cli#120): a pre-commit hook that
    auto-formats/lint-`--fix`es and RE-STAGES a file, then exits 0, lets `git commit`
    succeed -- but the tree it commits is read from the index AS IT STANDS once the hook
    finishes, which can now hold more than the reviewed diff. The TOCTOU guard exercised in
    test_index_drift_since_review_refuses_to_checkpoint only catches drift BEFORE `git
    commit` runs; it cannot catch a hook that mutates the index as a side effect of that
    SAME call, since the tree snapshot happens after the hook exits, not before. This must
    be caught AFTER the fact by re-diffing the produced commit against the reviewed diff,
    and the bad commit undone (index/working tree left alone) rather than kept."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo, diff = _make_staged_repo(tmp)
        os.environ["REVIEW_FAKE_BACKEND"] = "1"
        hook = repo / ".git" / "hooks" / "pre-commit"
        # Mirrors an auto-format/lint-staged hook: stages an EXTRA file, then exits 0 --
        # git commits using the index AS IT NOW STANDS, not the one that was reviewed.
        hook.write_text(
            "#!/bin/sh\necho sneaky > b.txt\ngit add b.txt\nexit 0\n", encoding="utf-8"
        )
        hook.chmod(0o755)
        before = _head_count(repo)

        import contextlib
        import io

        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            rc = mode_review(
                ["codex"],
                prompt="p",
                diff=diff,
                cwd=repo,
                timeout=30,
                staged=True,
                commit=True,
            )
        assert rc == EXIT_COMMIT_FAILED, rc
        assert _head_count(repo) == before, (
            "a hook that smuggled in an extra file must be undone, not checkpointed"
        )
        # b.txt must never have landed in history — the checkpoint's whole point is to
        # commit ONLY the reviewed diff, and the hook's file was never reviewed.
        history = subprocess.run(
            ["git", "-C", str(repo), "log", "--all", "--name-only", "--format="],
            cwd=repo,
            capture_output=True,
            text=True,
        ).stdout
        assert "b.txt" not in history, (
            "the hook's extra file must never land in history"
        )
        # The hook's own staged change is left in the index for the user to inspect --
        # neither lost nor silently swept into the checkpoint.
        status = subprocess.run(
            ["git", "-C", str(repo), "status", "--short"],
            cwd=repo,
            capture_output=True,
            text=True,
        ).stdout
        assert "b.txt" in status, (
            "the hook's staged file must survive the undo, untouched"
        )
        stderr_text = stderr_capture.getvalue()
        assert "hook" in stderr_text.lower()


def test_verify_checkpoint_uses_the_passed_sha_not_a_stale_head():
    """P1 codex finding on this feature's own PR (review-cli#120): verification/undo MUST
    identify the checkpoint commit by the exact SHA passed in (in the real flow, parsed
    from `git commit`'s own stdout summary line via `_parse_new_commit_sha`), never by
    re-reading `HEAD` afterward -- HEAD can have moved past our commit by the time
    verification runs (a post-commit hook, or a concurrent session sharing the checkout,
    making its own commit). A naive `rev-parse HEAD` would then verify/undo THAT later
    commit instead of ours -- in the worst case resetting away a commit we have no
    business touching.

    Exercises `_verify_checkpoint_matches_review` directly against a checkpoint sha while
    HEAD has since moved on to an unrelated commit -- this can't accidentally pass by
    coincidentally reading HEAD, since HEAD deliberately points somewhere else.

    Both raw commits below pass REVIEW_SKIP=1: this test is exercising pure git-plumbing
    behaviour of the new verification function, not the review-marker gate (covered by
    the other tests in this file), and the ambient machine's own ~/.config/git/hooks
    pre-commit gate (this repo's sibling `review-gate` hook, installed globally) would
    otherwise refuse a commit with no recorded review, unrelated to what this test checks.
    """
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        _init_git_repo(repo)
        env = {**os.environ, "REVIEW_SKIP": "1"}
        (repo / "a.txt").write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
        reviewed_diff = subprocess.run(
            ["git", "diff", "--cached"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        subprocess.run(
            ["git", "commit", "-m", _CHECKPOINT_COMMIT_MESSAGE],
            cwd=repo,
            env=env,
            check=True,
        )
        checkpoint_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        # Simulate something advancing HEAD after our checkpoint (a post-commit hook, or a
        # concurrent session sharing the checkout) -- an unrelated commit lands on top.
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "unrelated later commit"],
            cwd=repo,
            env=env,
            check=True,
        )
        later_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert later_sha != checkpoint_sha

        detail = review_mode._verify_checkpoint_matches_review(
            repo, checkpoint_sha, reviewed_diff
        )
        assert detail == "", (
            f"verification against the correct sha must succeed: {detail!r}"
        )
        # Nothing must have been touched: HEAD is still the later commit, and the
        # checkpoint commit is untouched underneath it.
        head_after = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert head_after == later_sha, "the later, unrelated commit must be left alone"


def test_undo_refuses_when_head_moved_past_the_mismatched_commit():
    """P1 codex finding on this feature's own PR (review-cli#120): the undo must use a
    compare-and-swap (`git update-ref <ref> <new> <old>`), not a blind `git reset --soft`
    -- a blind reset acts on whatever HEAD CURRENTLY is, so if HEAD has already moved past
    the mismatched commit (a post-commit hook, or a concurrent session, made its own
    commit on top) before the undo runs, a blind reset would silently drag that later,
    unrelated commit backwards too -- discarding it as collateral damage.

    Here `sha`'s diff will NOT match `reviewed_diff` (a genuine mismatch, same shape as
    the hook-mutation case), but by the time verification runs, HEAD has already advanced
    past `sha`. The undo must REFUSE (return a non-empty detail, nothing touched) rather
    than reset the later commit away."""
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        _init_git_repo(repo)
        env = {**os.environ, "REVIEW_SKIP": "1"}
        (repo / "a.txt").write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
        reviewed_diff = "this reviewed diff will never match the real commit below\n"
        subprocess.run(
            ["git", "commit", "-m", _CHECKPOINT_COMMIT_MESSAGE],
            cwd=repo,
            env=env,
            check=True,
        )
        checkpoint_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "unrelated later commit"],
            cwd=repo,
            env=env,
            check=True,
        )
        later_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        detail = review_mode._verify_checkpoint_matches_review(
            repo, checkpoint_sha, reviewed_diff
        )
        assert detail != "", (
            "a genuine content mismatch must be reported, not swallowed"
        )
        assert (
            "could not" in detail.lower() or "not be safely undone" in detail.lower()
        ), f"must explain the undo was refused, not silently attempted: {detail!r}"
        head_after = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert head_after == later_sha, (
            "the later, unrelated commit must survive -- the CAS must refuse, not reset it away"
        )


def test_parse_new_commit_sha_handles_detached_head():
    """P2 codex finding on this feature's own PR (review-cli#120): `git commit`'s summary
    line reads `[detached HEAD abc1234] message` in detached HEAD state -- "detached HEAD"
    is TWO words where every other ref-description is a single branch-name token. Without
    a dedicated alternative in the regex, parsing silently fails and the checkpoint is
    reported as failed even though the commit itself succeeded and is perfectly fine."""
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        _init_git_repo(repo)
        env = {**os.environ, "REVIEW_SKIP": "1"}
        (repo / "a.txt").write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "add", "a.txt"], cwd=repo, env=env, check=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"], cwd=repo, env=env, check=True
        )
        first_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "checkout", "--detach", first_sha], cwd=repo, env=env, check=True
        )
        (repo / "b.txt").write_text("world\n", encoding="utf-8")
        subprocess.run(["git", "add", "b.txt"], cwd=repo, env=env, check=True)
        commit_proc = subprocess.run(
            ["git", "commit", "-m", _CHECKPOINT_COMMIT_MESSAGE],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "detached HEAD" in commit_proc.stdout, (
            f"test setup assumption broken -- expected detached-HEAD summary line, got: "
            f"{commit_proc.stdout!r}"
        )
        parsed = review_mode._parse_new_commit_sha(repo, commit_proc.stdout)
        expected_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert parsed == expected_sha, (
            f"must parse the detached-HEAD commit summary correctly: got {parsed!r}, "
            f"expected {expected_sha!r}"
        )


def test_board_path_commit_checkpoint():
    """The board (failover) path shares the same checkpoint gate as the flat path — a
    successful, non-degraded staged board review with --commit must also checkpoint."""
    from reviewlib.config import BoardReviewer

    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo, diff = _make_staged_repo(tmp)
        os.environ["REVIEW_FAKE_BACKEND"] = "1"
        board = [
            BoardReviewer(model="codex", role="correctness", display="codex"),
            BoardReviewer(model="claude", role="security", display="claude"),
        ]
        before = _head_count(repo)
        rc = mode_review(
            ["codex"],
            prompt="p",
            diff=diff,
            cwd=repo,
            timeout=30,
            staged=True,
            board=board,
            pool_size=2,
            commit=True,
        )
        assert rc == 0, rc
        assert _head_count(repo) == before + 1, (
            "a successful staged board review must checkpoint"
        )


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
