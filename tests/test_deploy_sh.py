#!/usr/bin/env python3
"""scripts/deploy.sh — the rig-apply deploy hook must be a SAFE fast-forward pull.

WHY this exists: rig-cli 0.8.0+ runs a tool's scripts/deploy.sh on every `rig apply`
to keep the live symlinked checkout fresh (review-cli#105). Because it runs
unattended, its refusal semantics ARE the safety contract:

  - exit 0 both when already up to date AND after a successful fast-forward deploy
    (rig treats either as [deploy.sh: ok]);
  - exit 1 on an environment it must not touch: dirty (tracked) worktree, detached
    HEAD, not a git checkout at all;
  - exit 2 when the checkout DIVERGED from its upstream — never merge/rebase on the
    user's behalf;
  - untracked files must NOT block (a stray egg-info/ would otherwise wedge every
    rig apply forever).

Each case drives the REAL script against a throwaway origin+clone pair built in a
temp dir, via --checkout (no PATH resolution, nothing outside the sandbox is read
or written; the post-deploy install-skill step self-skips because the fake checkout
has no bin/review). Same plain-test_*-with-__main__ harness as the other unit files.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy.sh"

# Neutralize any global review pre-commit gate for the sandbox repos' own commits
# (core.hooksPath on this machine would otherwise block them) — same pattern as
# tests/test_run_stats.py.
_ENV = {**os.environ, "REVIEW_SKIP": "1"}


def _git(cwd: Path, *args: str) -> str:
    p = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, check=True, env=_ENV,
    )
    return p.stdout.strip()


def _neutralize_hooks(repo: Path) -> None:
    _git(repo, "config", "core.hooksPath", "/dev/null")
    _git(repo, "config", "commit.gpgsign", "false")


def _make_origin_and_clone(tmp: Path) -> tuple[Path, Path]:
    """A bare origin with one commit on main, plus a clone of it."""
    origin = tmp / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        capture_output=True, text=True, check=True,
    )
    seed = tmp / "seed"
    subprocess.run(
        ["git", "clone", str(origin), str(seed)],
        capture_output=True, text=True, check=True,
    )
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "t")
    _neutralize_hooks(seed)
    (seed / "file.txt").write_text("v1\n")
    _git(seed, "add", "file.txt")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "push", "origin", "main")

    clone = tmp / "clone"
    subprocess.run(
        ["git", "clone", str(origin), str(clone)],
        capture_output=True, text=True, check=True,
    )
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "t")
    _neutralize_hooks(clone)
    return seed, clone


def _push_new_commit(seed: Path, content: str = "v2\n") -> None:
    (seed / "file.txt").write_text(content)
    _git(seed, "add", "file.txt")
    _git(seed, "commit", "-m", f"update to {content.strip()}")
    _git(seed, "push", "origin", "main")


def _deploy(checkout: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(DEPLOY_SH), "--checkout", str(checkout), *extra],
        capture_output=True, text=True, timeout=120,
    )


def test_up_to_date_exits_0() -> None:
    with tempfile.TemporaryDirectory() as td:
        _, clone = _make_origin_and_clone(Path(td))
        p = _deploy(clone)
        assert p.returncode == 0, (p.stdout, p.stderr)
        assert "already up to date" in p.stdout, p.stdout


def test_behind_fast_forwards_and_exits_0() -> None:
    with tempfile.TemporaryDirectory() as td:
        seed, clone = _make_origin_and_clone(Path(td))
        _push_new_commit(seed)
        p = _deploy(clone)
        assert p.returncode == 0, (p.stdout, p.stderr)
        assert "deployed" in p.stdout, p.stdout
        assert (clone / "file.txt").read_text() == "v2\n"
        assert _git(clone, "rev-parse", "HEAD") == _git(clone, "rev-parse", "origin/main")


def test_dry_run_reports_but_does_not_pull() -> None:
    with tempfile.TemporaryDirectory() as td:
        seed, clone = _make_origin_and_clone(Path(td))
        before = _git(clone, "rev-parse", "HEAD")
        _push_new_commit(seed)
        p = _deploy(clone, "--dry-run")
        assert p.returncode == 0, (p.stdout, p.stderr)
        assert "not pulling" in p.stdout, p.stdout
        assert _git(clone, "rev-parse", "HEAD") == before


def test_dirty_tracked_change_refuses_with_exit_1() -> None:
    with tempfile.TemporaryDirectory() as td:
        seed, clone = _make_origin_and_clone(Path(td))
        _push_new_commit(seed)
        (clone / "file.txt").write_text("local edit\n")
        p = _deploy(clone)
        assert p.returncode == 1, (p.stdout, p.stderr)
        assert "local (tracked) changes" in p.stderr, p.stderr
        # And it must not have pulled over the edit.
        assert (clone / "file.txt").read_text() == "local edit\n"


def test_untracked_file_does_not_block() -> None:
    with tempfile.TemporaryDirectory() as td:
        seed, clone = _make_origin_and_clone(Path(td))
        _push_new_commit(seed)
        (clone / "stray-egg-info").write_text("untracked\n")
        p = _deploy(clone)
        assert p.returncode == 0, (p.stdout, p.stderr)
        assert (clone / "file.txt").read_text() == "v2\n"


def test_untracked_collision_with_incoming_file_is_friendly_exit_1() -> None:
    """An untracked file colliding with a tracked file the upstream ADDS makes
    git refuse the ff-merge; the script must surface that as the documented
    friendly exit 1, not a raw set -e abort (Opus finding)."""
    with tempfile.TemporaryDirectory() as td:
        seed, clone = _make_origin_and_clone(Path(td))
        (seed / "new.txt").write_text("upstream\n")
        _git(seed, "add", "new.txt")
        _git(seed, "commit", "-m", "add new.txt")
        _git(seed, "push", "origin", "main")
        (clone / "new.txt").write_text("local untracked\n")
        p = _deploy(clone)
        assert p.returncode == 1, (p.stdout, p.stderr)
        assert "fast-forward failed" in p.stderr, p.stderr
        # The untracked file survives untouched.
        assert (clone / "new.txt").read_text() == "local untracked\n"


def test_foreign_git_env_vars_do_not_hijack_the_checkout() -> None:
    """GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE in the caller's environment (a git
    hook spawning rig apply) override `git -C` and would pin every command to a
    FOREIGN repo — the review-cli#72 bug class. deploy.sh must scrub them."""
    with tempfile.TemporaryDirectory() as td:
        seed, clone = _make_origin_and_clone(Path(td))
        _push_new_commit(seed)
        foreign = Path(td) / "foreign"
        subprocess.run(
            ["git", "init", "-b", "main", str(foreign)],
            capture_output=True, text=True, check=True,
        )
        env = {
            **_ENV,
            "GIT_DIR": str(foreign / ".git"),
            "GIT_WORK_TREE": str(foreign),
            "GIT_INDEX_FILE": str(foreign / ".git" / "index"),
        }
        p = subprocess.run(
            ["bash", str(DEPLOY_SH), "--checkout", str(clone)],
            capture_output=True, text=True, timeout=120, env=env,
        )
        assert p.returncode == 0, (p.stdout, p.stderr)
        # The CLONE was deployed, not the hijack target.
        assert (clone / "file.txt").read_text() == "v2\n"
        assert not (foreign / "file.txt").exists()


def test_detached_head_refuses_with_exit_1() -> None:
    with tempfile.TemporaryDirectory() as td:
        _, clone = _make_origin_and_clone(Path(td))
        _git(clone, "checkout", "--detach", "HEAD")
        p = _deploy(clone)
        assert p.returncode == 1, (p.stdout, p.stderr)
        assert "detached-HEAD" in p.stderr, p.stderr


def test_diverged_refuses_with_exit_2() -> None:
    with tempfile.TemporaryDirectory() as td:
        seed, clone = _make_origin_and_clone(Path(td))
        # A local commit the remote lacks, plus a remote commit the clone lacks.
        (clone / "local.txt").write_text("local\n")
        _git(clone, "add", "local.txt")
        _git(clone, "commit", "-m", "local-only")
        _push_new_commit(seed)
        p = _deploy(clone)
        assert p.returncode == 2, (p.stdout, p.stderr)
        assert "cannot fast-forward" in p.stderr, p.stderr


def test_ahead_only_is_not_divergence_and_exits_0() -> None:
    """Unpushed local commits with a still upstream: nothing to deploy, exit 0 —
    NOT the misleading exit-2 'diverged' (which would turn every unattended
    rig apply red until the commits are pushed)."""
    with tempfile.TemporaryDirectory() as td:
        _, clone = _make_origin_and_clone(Path(td))
        (clone / "local.txt").write_text("local\n")
        _git(clone, "add", "local.txt")
        _git(clone, "commit", "-m", "local-only")
        p = _deploy(clone)
        assert p.returncode == 0, (p.stdout, p.stderr)
        assert "AHEAD" in p.stdout, p.stdout
        assert "nothing to deploy" in p.stdout, p.stdout


def test_honors_configured_upstream_with_non_origin_remote_name() -> None:
    """A checkout whose branch tracks a remote NOT named origin (fork layout:
    main tracks upstream/main) must deploy against the CONFIGURED upstream,
    not a hardcoded origin/$branch (codex P1)."""
    with tempfile.TemporaryDirectory() as td:
        seed, clone = _make_origin_and_clone(Path(td))
        _git(clone, "remote", "rename", "origin", "upstream")
        # remote rename preserves branch.main.remote=upstream; assert the premise.
        assert _git(clone, "rev-parse", "--abbrev-ref", "main@{upstream}") == "upstream/main"
        _push_new_commit(seed)
        p = _deploy(clone)
        assert p.returncode == 0, (p.stdout, p.stderr)
        assert (clone / "file.txt").read_text() == "v2\n"


def test_fetch_failure_is_friendly_exit_1() -> None:
    """A dead remote (network down / repo moved) must be the documented exit 1
    with a clear message, not a raw set -e abort."""
    with tempfile.TemporaryDirectory() as td:
        _, clone = _make_origin_and_clone(Path(td))
        _git(clone, "remote", "set-url", "origin", str(Path(td) / "no-such-origin.git"))
        p = _deploy(clone)
        assert p.returncode == 1, (p.stdout, p.stderr)
        assert "git fetch" in p.stderr and "failed" in p.stderr, p.stderr


def test_reviewlib_change_prints_daemon_restart_note() -> None:
    with tempfile.TemporaryDirectory() as td:
        seed, clone = _make_origin_and_clone(Path(td))
        (seed / "reviewlib").mkdir()
        (seed / "reviewlib" / "mod.py").write_text("x = 1\n")
        _git(seed, "add", "reviewlib")
        _git(seed, "commit", "-m", "touch reviewlib")
        _git(seed, "push", "origin", "main")
        p = _deploy(clone)
        assert p.returncode == 0, (p.stdout, p.stderr)
        assert "review dashboard stop" in p.stdout, p.stdout
        assert "review spec-web stop" in p.stdout, p.stdout


def test_dry_run_reports_daemon_restart_note() -> None:
    with tempfile.TemporaryDirectory() as td:
        seed, clone = _make_origin_and_clone(Path(td))
        (seed / "reviewlib").mkdir()
        (seed / "reviewlib" / "mod.py").write_text("x = 1\n")
        _git(seed, "add", "reviewlib")
        _git(seed, "commit", "-m", "touch reviewlib")
        _git(seed, "push", "origin", "main")
        before = _git(clone, "rev-parse", "HEAD")
        p = _deploy(clone, "--dry-run")
        assert p.returncode == 0, (p.stdout, p.stderr)
        assert "would need a restart" in p.stdout, p.stdout
        assert _git(clone, "rev-parse", "HEAD") == before


def test_no_arg_deploys_the_scripts_own_checkout_rig_style() -> None:
    """rig's freshness pass runs `bash <repo>/scripts/deploy.sh` with NO args
    (riglib/actions/runner.py _run_tool_deploy) and rig treats the tool as
    installed even when its bin dir is not on PATH — so the no-arg default must
    target the checkout the SCRIPT lives in, and must work with no `review` on
    PATH at all (codex P1)."""
    with tempfile.TemporaryDirectory() as td:
        seed, clone = _make_origin_and_clone(Path(td))
        _push_new_commit(seed)
        script = clone / "scripts" / "deploy.sh"
        script.parent.mkdir()
        script.write_text(DEPLOY_SH.read_text())
        script.chmod(0o755)
        # A bare system PATH: no fake `review` symlink anywhere — the script-dir
        # default must not need one. (The untracked scripts/ copy also proves
        # untracked files don't block the pull.)
        env = {**_ENV, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin"}
        p = subprocess.run(
            ["bash", str(script)],
            capture_output=True, text=True, timeout=120, env=env, cwd=str(clone),
        )
        assert p.returncode == 0, (p.stdout, p.stderr)
        assert "script's own checkout" in p.stdout, p.stdout
        assert "deployed" in p.stdout, p.stdout
        assert (clone / "file.txt").read_text() == "v2\n"
        assert _git(clone, "rev-parse", "HEAD") == _git(clone, "rev-parse", "origin/main")


def test_path_fallback_follows_review_symlink_when_script_outside_checkout() -> None:
    """A copy of the script OUTSIDE any checkout falls back to resolving the
    `review` on PATH (a symlink to <checkout>/bin/review, install.sh's
    contract) back to its containing repo and deploys THERE."""
    with tempfile.TemporaryDirectory() as td:
        seed, clone = _make_origin_and_clone(Path(td))
        _push_new_commit(seed)
        standalone = Path(td) / "standalone" / "deploy.sh"
        standalone.parent.mkdir()
        standalone.write_text(DEPLOY_SH.read_text())
        standalone.chmod(0o755)
        shim = clone / "bin" / "review"
        shim.parent.mkdir()
        shim.write_text("#!/bin/sh\nexit 0\n")
        shim.chmod(0o755)
        fake_bin = Path(td) / "fake-bin"
        fake_bin.mkdir()
        (fake_bin / "review").symlink_to(shim)
        env = {**_ENV, "PATH": f"{fake_bin}:{_ENV['PATH']}"}
        p = subprocess.run(
            ["bash", str(standalone)],
            capture_output=True, text=True, timeout=120, env=env,
            cwd=str(Path(td)),
        )
        assert p.returncode == 0, (p.stdout, p.stderr)
        # Don't assert the literal path: on macOS git prints the /private/var
        # physical path for a /var/... temp dir. The deploy landing v2 in THIS
        # clone is the proof the symlink resolved to the right checkout.
        assert "deployed" in p.stdout, p.stdout
        assert (clone / "file.txt").read_text() == "v2\n"
        assert _git(clone, "rev-parse", "HEAD") == _git(clone, "rev-parse", "origin/main")


def test_not_a_git_checkout_refuses_with_exit_1() -> None:
    with tempfile.TemporaryDirectory() as td:
        plain = Path(td) / "plain"
        plain.mkdir()
        p = _deploy(plain)
        assert p.returncode == 1, (p.stdout, p.stderr)
        assert "not a git checkout" in p.stderr, p.stderr


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
