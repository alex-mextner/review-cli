#!/usr/bin/env python3
"""`--staged -C <repo>` must diff the `-C` repo, NEVER an unrelated repo leaked via env.

Bug (review-cli#71, reported by the web-harness agent): `review diff --task TEST-1 --staged -C <repoB>`
(and `review just-ask "Q" --task TEST-1 --staged -C <repoB>`) mis-resolved the diff to an UNRELATED
worktree's `git diff` instead of repoB's staged diff. This is the EXACT command every
review-gate uses (`review diff --task TEST-1 --staged -m claude:... --pool 1 -C <worktree>`), so a
mis-resolved diff means the gate reviewed the WRONG (or empty) diff all session.

Root cause: every git invocation (`_git_diff`, `_effective_cwd`'s `git rev-parse
--show-toplevel`, `_is_git_repo`) inherited the parent environment. When a parent harness
or a git hook had `GIT_DIR` / `GIT_WORK_TREE` (and friends) exported — git sets these in
hook contexts, and a stale shell export carries them into a spawned `review` — those env
vars OVERRIDE both `-C` and the subprocess `cwd`. `git -C /repoB diff --cached` then reads
`GIT_DIR`'s repo (the unrelated worktree), not repoB.

Contract pinned here:
  * `review diff --task TEST-1 --staged -C repoB`, run from inside repoA with `GIT_DIR`/`GIT_WORK_TREE`
    pointing at repoA, reviews repoB's staged diff — NOT repoA's.
  * Same for `review just-ask "Q" --task TEST-1 --staged -C repoB` (the diff is optional grounding, but it
    must still come from repoB when present).

Driven in-process: two real temp repos with DISTINCT staged content, the mode handler
stubbed to capture the diff it receives (no backend), the repo-pointing git env vars set
to repoA, and `-C repoB`. The RED state (pre-fix) captured repoA's diff.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import cli  # noqa: E402
from reviewlib.modes import just_ask as _ja_mod  # noqa: E402
from reviewlib.modes import review as _review_mod  # noqa: E402

# The git env vars that pin git to a specific repo regardless of cwd / -C. A leak of any of
# these from a parent process (a git hook, a stale export) is the footgun under test.
_LEAK_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")


def _init_repo(path: Path, filename: str, content: str) -> None:
    """A real git repo with one committed base and a DISTINCT staged change."""
    path.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "t"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "t@t"

    def run(*a: str) -> None:
        subprocess.run(["git", *a], cwd=str(path), env=env, check=True,
                       capture_output=True, text=True)

    run("init", "-q")
    run("commit", "-q", "--allow-empty", "-m", "init")
    (path / filename).write_text(content, encoding="utf-8")
    run("add", filename)


def _run_capturing(handler_owner, handler_name, dispatch_argv, *, cwd_repo, leak_repo):
    """Run `cli._dispatch(dispatch_argv)` with the mode handler stubbed to capture the diff.

    `cwd_repo` is the process cwd (repoA). `leak_repo` is the repo the leaked GIT_DIR /
    GIT_WORK_TREE point at (also repoA here) — the unrelated repo that pre-fix won the diff.
    Returns the captured diff string.
    """
    captured: dict = {}

    def _fake(*args, **kwargs):
        # `diff` is the 3rd positional for both mode_review(models, prompt, diff, cwd, ...)
        # and mode_just_ask(question, models, diff, cwd, ...).
        captured["diff"] = args[2] if len(args) > 2 else kwargs.get("diff", "")
        captured["ran"] = True
        return 0

    saved_handler = getattr(handler_owner, handler_name)
    saved_stdin = cli._read_stdin_if_piped
    saved_cfg = cli.load_config
    saved_cwd = os.getcwd()
    saved_env = {k: os.environ.get(k) for k in _LEAK_VARS}

    setattr(handler_owner, handler_name, _fake)
    cli._read_stdin_if_piped = lambda: None
    cli.load_config = lambda: {"models": ["codex"]}  # explicit models -> no real board
    os.chdir(str(cwd_repo))
    # The leak: point git at repoA via the env, exactly as a git hook / stale export would.
    os.environ["GIT_DIR"] = str(Path(leak_repo) / ".git")
    os.environ["GIT_WORK_TREE"] = str(leak_repo)
    err = io.StringIO()
    try:
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            cli._dispatch(dispatch_argv)
    finally:
        setattr(handler_owner, handler_name, saved_handler)
        cli._read_stdin_if_piped = saved_stdin
        cli.load_config = saved_cfg
        os.chdir(saved_cwd)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    assert captured.get("ran"), f"{handler_name} never ran"
    return captured.get("diff") or ""


def test_diff_staged_honors_c_repo_despite_git_env_leak():
    """`review diff --task TEST-1 --staged -C repoB` from cwd repoA with GIT_DIR/GIT_WORK_TREE leaked to
    repoA must review repoB's staged diff, not repoA's."""
    with tempfile.TemporaryDirectory() as d:
        repo_a = Path(d) / "A"
        repo_b = Path(d) / "B"
        _init_repo(repo_a, "fileA.txt", "AAAAA repoA unique content\n")
        _init_repo(repo_b, "fileB.txt", "BBBBB repoB unique content\n")
        diff = _run_capturing(
            _review_mod, "mode_review",
            ["diff", "--task", "TEST-1", "--staged", "-C", str(repo_b)],
            cwd_repo=repo_a, leak_repo=repo_a,
        )
        assert "BBBBB repoB" in diff, f"expected repoB's staged diff, got: {diff!r}"
        assert "AAAAA repoA" not in diff, f"leaked repoA's diff: {diff!r}"


def test_just_ask_staged_honors_c_repo_despite_git_env_leak():
    """`review just-ask "Q" --task TEST-1 --staged -C repoB` from cwd repoA with the git env leaked to repoA
    must attach repoB's staged diff as grounding, not repoA's."""
    with tempfile.TemporaryDirectory() as d:
        repo_a = Path(d) / "A"
        repo_b = Path(d) / "B"
        _init_repo(repo_a, "fileA.txt", "AAAAA repoA unique content\n")
        _init_repo(repo_b, "fileB.txt", "BBBBB repoB unique content\n")
        diff = _run_capturing(
            _ja_mod, "mode_just_ask",
            ["just-ask", "review this", "--task", "TEST-1", "--staged", "-C", str(repo_b)],
            cwd_repo=repo_a, leak_repo=repo_a,
        )
        assert "BBBBB repoB" in diff, f"expected repoB's staged diff, got: {diff!r}"
        assert "AAAAA repoA" not in diff, f"leaked repoA's diff: {diff!r}"


def _git(path: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "t"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "t@t"
    return subprocess.run(["git", "-C", str(path), *args], env=env, check=True,
                          capture_output=True, text=True)


def test_git_diff_preserves_target_repos_own_index_partial_commit():
    """A LEGITIMATE pre-commit hook of the TARGET repo sets GIT_INDEX_FILE to a temporary
    `next-index` (a `git commit <pathspec>` partial commit) that scopes `git diff --cached` to
    ONLY the files being committed. The #71 env-strip must NOT drop that (it belongs to the
    target repo) — else `_git_diff` reads the full default index and reviews files NOT in the
    commit, and the stamp hash won't match the hook's own index (codex P2 on PR #72).

    Build a real repo with TWO staged files, write a partial-index containing only one of them,
    point GIT_INDEX_FILE at it (as git's partial-commit hook would), and assert `_git_diff`
    honors that scoped index — only `keep.txt`, not `extra.txt`."""
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d) / "repo"
        repo.mkdir()
        _git(repo, "init", "-q")
        _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
        (repo / "keep.txt").write_text("KEEP committed file\n", encoding="utf-8")
        (repo / "extra.txt").write_text("EXTRA not-in-this-commit\n", encoding="utf-8")
        _git(repo, "add", "keep.txt", "extra.txt")  # both in the DEFAULT index
        # Build a partial index containing only keep.txt, like git's `next-index` for a
        # `git commit keep.txt`. GIT_INDEX_FILE must be RELATIVE-resolvable and belong to .git.
        partial = repo / ".git" / "next-index-test"
        saved_idx = os.environ.get("GIT_INDEX_FILE")
        saved_dir = os.environ.get("GIT_DIR")
        try:
            # Stage ONLY keep.txt into the partial index (the `next-index` git builds for a
            # `git commit keep.txt`), basing it on HEAD so it doesn't carry extra.txt.
            _git_partial = dict(os.environ)
            _git_partial["GIT_INDEX_FILE"] = str(partial)
            subprocess.run(["git", "-C", str(repo), "read-tree", "HEAD"],
                           env=_git_partial, check=True, capture_output=True, text=True)
            subprocess.run(["git", "-C", str(repo), "add", "keep.txt"],
                           env=_git_partial, check=True, capture_output=True, text=True)
            # Now invoke _git_diff WITH the target repo's own GIT_INDEX_FILE set (the hook env).
            os.environ["GIT_INDEX_FILE"] = str(partial)
            os.environ["GIT_DIR"] = str((repo / ".git").resolve())
            diff = cli._git_diff(repo, staged=True)
        finally:
            for k, v in (("GIT_INDEX_FILE", saved_idx), ("GIT_DIR", saved_dir)):
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        assert "keep.txt" in diff, f"target repo's own partial index was dropped: {diff!r}"
        assert "extra.txt" not in diff, (
            f"the partial-commit index scope was lost — reviewed a file not in the commit: {diff!r}"
        )


if __name__ == "__main__":
    failures = 0
    for _name, _fn in list(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"PASS {_name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {_name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {_name}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failures else 0)
