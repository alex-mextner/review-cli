#!/usr/bin/env python3
"""Tests for the agent-tools session review marker (`~/.cache/agent-tools/last-review`).

A successful `review --staged` run must touch the marker file that the separate
agent-tools `require-review-before-commit` agent-hook checks (mtime-windowed), so a
GENUINE review run satisfies that gate without the agent forging the marker. An
unstaged/piped review must NOT touch it (it does not satisfy the staged commit gate),
and a FAILED staged review must NOT touch it either.

Hermetic: routes every backend to the in-process fake (REVIEW_FAKE_BACKEND) — no
network, no CLI — and redirects the marker to a temp path via REVIEW_MARKER. Runs as a
plain script (mirroring tests/test_provider_keys.py / tests/test_streaming.py): each
`test_*` function is invoked by the __main__ block below, NO pytest required (so it runs
under the documented CI runner, `python3 tests/...` via tests/smoke.sh).

Run from the repo root::

    python3 tests/test_review_marker.py
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

from reviewlib import install  # noqa: E402
from reviewlib.backends import ReviewResult  # noqa: E402
from reviewlib.config import BoardReviewer  # noqa: E402
from reviewlib.modes import review as review_mode  # noqa: E402
from reviewlib.modes.review import mode_review  # noqa: E402

_MARKER_ENV = ("REVIEW_MARKER", "REVIEW_FAKE_BACKEND", "HOME")


class _EnvSandbox:
    """Snapshot + restore the marker-related env vars so tests don't leak into one
    another (or touch the real ~/.cache/agent-tools/last-review on the dev machine)."""

    def __enter__(self):
        self._saved = {name: os.environ.get(name) for name in _MARKER_ENV}
        return self

    def __exit__(self, *exc):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        return False


def _init_git_repo(path: Path) -> None:
    """A real git repo so `git rev-parse --git-path review-stamp` resolves (the stamp
    write shares mode_review's success path with the marker touch)."""
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)


def _make_repo(tmp: Path) -> Path:
    d = tmp / "repo"
    d.mkdir()
    _init_git_repo(d)
    return d


_DIFF = "diff --git a b\n+x\n"


def test_touch_helper_creates_marker_and_parents():
    """The helper makes the parent dir and an empty marker file (idempotent)."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        marker = Path(d) / "deep" / "nested" / "last-review"
        os.environ["REVIEW_MARKER"] = str(marker)
        assert not marker.exists()
        install._touch_review_marker()
        assert marker.exists()
        # Re-touch must not raise and keeps the file present (idempotent).
        install._touch_review_marker()
        assert marker.exists()


def test_touch_helper_advances_mtime():
    """The hook is mtime-WINDOWED, so a re-touch must refresh mtime (not just keep the
    file). A regression to write-once-only would pass an exists() check but break the
    gate — pin the mtime advance explicitly."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        marker = Path(d) / "last-review"
        os.environ["REVIEW_MARKER"] = str(marker)
        install._touch_review_marker()
        # Backdate so the second touch must move mtime forward on any fs granularity.
        old = marker.stat().st_mtime
        os.utime(marker, (old - 100, old - 100))
        backdated = marker.stat().st_mtime
        install._touch_review_marker()
        assert marker.stat().st_mtime > backdated, (
            "re-touch must refresh the marker mtime"
        )


def test_touch_helper_expands_default_home_path_when_env_unset():
    """With REVIEW_MARKER unset, the helper must expand the `~/...` DEFAULT_REVIEW_MARKER
    via os.path.expanduser — NOT write a literal `~`-prefixed path. Pin this so a refactor
    that drops expanduser (or a non-`~` default) is caught. HOME is redirected to a temp
    dir so the real ~/.cache is never touched."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        os.environ.pop("REVIEW_MARKER", None)
        os.environ["HOME"] = d  # expanduser resolves ~ against this
        install._touch_review_marker()
        expected = Path(d) / ".cache" / "agent-tools" / "last-review"
        assert expected.exists(), f"default marker should expand to {expected}"
        # And NOT a literal '~' path under the cwd.
        assert not (Path("~") / ".cache").exists()


def test_touch_helper_uses_default_when_review_marker_is_empty_string():
    """REVIEW_MARKER="" (explicitly set to empty, not unset) must resolve to the SAME
    default path as unset — not Path(""), which is the review's own cwd. This is the
    producer-side half of a contract with agent-tools' require-review-before-commit hook,
    whose marker_path() normalizes empty-string the same way (`os.environ.get(...) or
    DEFAULT`, not `.get(..., DEFAULT)` — the latter's default only applies when the key is
    ABSENT, and an explicitly-empty value IS present). If the two sides disagree here, a
    successful staged review touches the wrong path and the commit gate keeps blocking
    despite review-cli reporting success — this test pins that they agree."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        os.environ["REVIEW_MARKER"] = ""
        os.environ["HOME"] = d
        install._touch_review_marker()
        expected = Path(d) / ".cache" / "agent-tools" / "last-review"
        assert expected.exists(), f"empty REVIEW_MARKER should fall back to {expected}"


def test_touch_helper_never_raises_on_bad_path():
    """A marker path whose parent can't be created must be swallowed (best-effort)."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        # Point the marker UNDER a regular file so mkdir(parents=True) fails.
        blocker = Path(d) / "iamafile"
        blocker.write_text("x", encoding="utf-8")
        os.environ["REVIEW_MARKER"] = str(blocker / "child" / "last-review")
        install._touch_review_marker()  # must not raise


def test_staged_review_touches_marker():
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        marker = tmp / "cache" / "agent-tools" / "last-review"
        os.environ["REVIEW_MARKER"] = str(marker)
        os.environ["REVIEW_FAKE_BACKEND"] = "1"  # deterministic in-process backend
        rc = mode_review(
            ["codex"], prompt="p", diff=_DIFF, cwd=repo, timeout=30, staged=True
        )
        assert rc == 0, rc
        assert marker.exists(), (
            "a successful staged review must touch the session marker"
        )


def test_unstaged_review_does_not_touch_marker():
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        marker = tmp / "cache" / "agent-tools" / "last-review"
        os.environ["REVIEW_MARKER"] = str(marker)
        os.environ["REVIEW_FAKE_BACKEND"] = "1"
        rc = mode_review(
            ["codex"], prompt="p", diff=_DIFF, cwd=repo, timeout=30, staged=False
        )
        assert rc == 0, rc
        assert not marker.exists(), (
            "an unstaged/piped review must NOT touch the gate marker"
        )


def test_failed_staged_review_does_not_touch_marker():
    """A staged review whose backend FAILS (rc != 0) must NOT satisfy the gate. The
    marker touch is gated on `ok and staged`, so a failed review leaves it absent."""

    def _failing_backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
        return ReviewResult(
            model=model, command="fake-fail", returncode=1, stdout="", stderr="boom"
        )

    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        marker = tmp / "cache" / "agent-tools" / "last-review"
        os.environ["REVIEW_MARKER"] = str(marker)
        # Route the resolved backend to a failing stand-in (the single chokepoint
        # mode_review funnels through). Restore it after, regardless of outcome.
        original = review_mode.resolve_backend
        review_mode.resolve_backend = lambda model: _failing_backend
        try:
            rc = mode_review(
                ["codex"], prompt="p", diff=_DIFF, cwd=repo, timeout=30, staged=True
            )
        finally:
            review_mode.resolve_backend = original
        assert rc == 1, rc
        assert not marker.exists(), (
            "a FAILED staged review must NOT touch the gate marker"
        )


def test_piped_staged_review_does_not_touch_marker():
    """A diff piped on stdin (`printf ... | review --staged`) is NOT the git index, so it
    must NOT satisfy the commit gate even under --staged — otherwise the mtime-only marker
    would be forgeable for a commit whose staged changes were never reviewed (codex P2).
    The handler passes diff_from_stdin=True, which suppresses the stamp/marker."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        marker = tmp / "cache" / "agent-tools" / "last-review"
        os.environ["REVIEW_MARKER"] = str(marker)
        os.environ["REVIEW_FAKE_BACKEND"] = "1"
        rc = mode_review(
            ["codex"],
            prompt="p",
            diff=_DIFF,
            cwd=repo,
            timeout=30,
            staged=True,
            diff_from_stdin=True,
        )
        assert rc == 0, rc
        assert not marker.exists(), (
            "a piped --staged review must NOT touch the gate marker"
        )


def test_staged_review_returns_zero_even_if_marker_unwritable():
    """Best-effort contract at the INTEGRATION level: if the marker can't be written
    (its parent is a regular file), a successful staged review still returns 0 — the
    marker is a discipline reminder, never a correctness gate that can fail the review."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        blocker = tmp / "iamafile"
        blocker.write_text("x", encoding="utf-8")
        os.environ["REVIEW_MARKER"] = str(blocker / "child" / "last-review")
        os.environ["REVIEW_FAKE_BACKEND"] = "1"
        rc = mode_review(
            ["codex"], prompt="p", diff=_DIFF, cwd=repo, timeout=30, staged=True
        )
        assert rc == 0, (
            "an unwritable marker must never fail an otherwise-successful review"
        )


def test_staged_board_review_touches_marker():
    """The board path (run_board_with_failover) shares the same `ok and staged` gate, so
    a successful, non-degraded staged board review must also touch the marker. With
    REVIEW_FAKE_BACKEND every seat is usable, so the pool is not degraded."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        marker = tmp / "cache" / "agent-tools" / "last-review"
        os.environ["REVIEW_MARKER"] = str(marker)
        os.environ["REVIEW_FAKE_BACKEND"] = "1"
        board = [
            BoardReviewer(model="codex", role="correctness", display="codex"),
            BoardReviewer(model="claude", role="security", display="claude"),
        ]
        rc = mode_review(
            ["codex"],
            prompt="p",
            diff=_DIFF,
            cwd=repo,
            timeout=30,
            staged=True,
            board=board,
            pool_size=2,
        )
        assert rc == 0, rc
        assert marker.exists(), (
            "a successful staged board review must touch the session marker"
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
