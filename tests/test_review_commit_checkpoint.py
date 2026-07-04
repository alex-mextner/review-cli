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
    EXIT_COMMIT_FAILED,
    EXIT_COMMIT_REQUIRES_STAGED,
    mode_review,
)

_ENV_VARS = ("REVIEW_FAKE_BACKEND",)


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
    """A real repo with one staged change. Returns (repo, staged_diff)."""
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
