#!/usr/bin/env python3
"""Unit tests for _effective_cwd (cli.py): git-toplevel resolution + non-repo warning.

review runs the diff and the claude/opus workspace in cwd; agents often invoke it
from a scratch/temp dir and forget -C, silently reviewing the wrong place. These
tests pin the resolution contract: inside a repo -> the repo root (also from a
subdir); outside a repo -> the path as-is (with a loud stderr warning).

Same harness style as tests/test_streaming.py: plain test_* functions run by the
__main__ block.
"""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib.cli import _effective_cwd  # noqa: E402


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)


def _real(p) -> str:
    return os.path.realpath(str(p))


def test_returns_repo_root_for_repo_dir():
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d) / "repo"
        repo.mkdir()
        _git_init(repo)
        got = _effective_cwd(str(repo))
        assert _real(got) == _real(repo), (got, repo)


def test_returns_repo_root_from_subdir():
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d) / "repo"
        sub = repo / "pkg" / "deep"
        sub.mkdir(parents=True)
        _git_init(repo)
        # invoked deep inside the repo -> still resolves to the repo root
        got = _effective_cwd(str(sub))
        assert _real(got) == _real(repo), (got, repo)


def test_non_git_dir_returns_resolved_and_warns():
    with tempfile.TemporaryDirectory() as d:
        plain = Path(d) / "scratch"
        plain.mkdir()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            got = _effective_cwd(str(plain))
        assert _real(got) == _real(plain), (got, plain)
        msg = err.getvalue()
        assert "not inside a git repository" in msg, msg
        assert "-C" in msg, msg  # the warning must point the caller at -C


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
