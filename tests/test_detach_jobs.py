#!/usr/bin/env python3
"""REAL end-to-end tests for `review <mode> --detach` and its companion commands
(`review jobs` / `review status` / `review wait`) — review-cli#160 companion feature.

Same harness style as test_e2e_resume.py: spawns the ACTUAL `bin/review` CLI as a
subprocess with `REVIEW_FAKE_BACKEND=1` (a deterministic, network-free, in-process
stand-in for every real backend — see `reviewlib.backends.review_fake`). Nothing about
`--detach` itself is faked: argument pre-scanning (`_extract_detach_flag`), the actual
second-process spawn (`_spawn_detached_job`, `python -m reviewlib`), job bookkeeping
(`reviewlib.jobs`), and the job-status finalization in `cli.main` all run for real. Only
the LEAF model call is deterministic/network-free, exactly like the resume e2e suite.

Covers:
  * `--detach` returns almost immediately (well under the fake backend's own delay),
    printing a job-id.
  * `review jobs` / `review status <job-id>` observe it transition running -> done.
  * `review wait <job-id>` blocks until done and returns the job's own exit code.
  * The detached run's `-o` RESULT matches what a SYNCHRONOUS run of the identical
    review produces (the detach path must not alter the review's actual output).
  * Killing the detached job's OS process while it is mid-review (SIGTERM) is
    reconciled by `review status`/`jobs` as terminated rather than staying "running"
    forever (see tests/test_signal_reaper.py for the underlying child-reap mechanism
    this build on: the fake backend spawns no real subprocess, so THIS suite proves
    the job-bookkeeping side of a kill; the process-tree reap itself is proven with a
    real spawned child in test_signal_reaper.py).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_BIN = REPO_ROOT / "bin" / "review"
sys.path.insert(0, str(REPO_ROOT))


def _make_repo(tmp: Path) -> Path:
    """A throwaway git repo with one staged file, for `review diff --staged`."""
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=repo, check=True
    )
    (repo / "a.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
    return repo


def _env(tmp: Path, *, fake_delay: float | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["REVIEW_FAKE_BACKEND"] = "1"
    env["REVIEW_LOG_DIR"] = str(tmp / "logs")
    env["REVIEW_STATS_FILE"] = str(tmp / "logs" / "stats.jsonl")
    env["REVIEW_JOBS_DIR"] = str(tmp / "jobs")
    env["REVIEW_TASK_CODE"] = "TEST-1"
    if fake_delay is not None:
        env["REVIEW_FAKE_DELAY"] = str(fake_delay)
    return env


def _run_cli(
    args: list[str], env: dict[str, str], timeout: int = 30
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REVIEW_BIN), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _run_cli_with_stdin(
    args: list[str], env: dict[str, str], stdin_text: str, timeout: int = 30
) -> subprocess.CompletedProcess[str]:
    """Like `_run_cli`, but with a real PIPE feeding `stdin_text` — the shape a caller
    doing `git diff | review diff --detach` actually produces (`_run_cli`'s inherited
    stdin is a tty/EOF, never a real pipe with content)."""
    return subprocess.run(
        [sys.executable, str(REVIEW_BIN), *args],
        input=stdin_text,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _review_diff_args(repo: Path) -> list[str]:
    return ["diff", "--staged", "-C", str(repo), "-m", "claude:claude-opus-4-8"]


def _extract_job_id(stdout: str) -> str:
    for line in stdout.splitlines():
        if "detached job" in line:
            return line.split("detached job", 1)[1].strip().split()[0]
    raise AssertionError(f"no job-id in --detach output:\n{stdout}")


def _poll_status_json(
    job_id: str, env: dict[str, str], *, timeout: float = 15.0
) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        proc = _run_cli(["status", job_id, "--json"], env)
        if proc.stdout.strip():
            last = json.loads(proc.stdout)
            if last.get("status") in ("done", "failed", "unknown-terminated"):
                return last
        time.sleep(0.2)
    return last


def test_detach_returns_immediately_with_job_id():
    """`--detach` must return in well under the fake backend's own artificial delay —
    proving the CALLER never blocks for the review itself."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        env = _env(tmp, fake_delay=3.0)

        t0 = time.monotonic()
        proc = _run_cli([*_review_diff_args(repo), "--detach"], env, timeout=10)
        elapsed = time.monotonic() - t0

        assert proc.returncode == 0, proc.stderr
        assert elapsed < 3.0, (
            elapsed
        )  # the review itself sleeps 3s; the caller must not wait
        job_id = _extract_job_id(proc.stdout)
        assert job_id


def test_detach_job_transitions_to_done_and_matches_sync_result():
    """The detached job eventually reports "done", and its `-o` RESULT is byte-identical
    to a SYNCHRONOUS run of the exact same review — `--detach` must not change what the
    review actually produces, only when the caller finds out."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        env = _env(tmp, fake_delay=0.2)

        sync_out = tmp / "sync-result.txt"
        sync = _run_cli([*_review_diff_args(repo), "-o", str(sync_out)], env)
        assert sync.returncode == 0, sync.stderr

        detach = _run_cli([*_review_diff_args(repo), "--detach"], env)
        assert detach.returncode == 0, detach.stderr
        job_id = _extract_job_id(detach.stdout)

        rec = _poll_status_json(job_id, env)
        assert rec.get("status") == "done", rec
        assert rec.get("exit_code") == 0, rec

        detached_result = Path(rec["result_path"]).read_text(encoding="utf-8")
        assert detached_result == sync_out.read_text(encoding="utf-8")

        listed = _run_cli(["jobs", "--json"], env)
        ids = [r["job_id"] for r in json.loads(listed.stdout)]
        assert job_id in ids


def test_wait_blocks_until_done_and_returns_job_exit_code():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        env = _env(tmp, fake_delay=0.5)

        detach = _run_cli([*_review_diff_args(repo), "--detach"], env)
        job_id = _extract_job_id(detach.stdout)

        waited = _run_cli(["wait", job_id, "--poll", "0.1"], env, timeout=15)
        assert waited.returncode == 0, waited.stderr
        assert "done" in waited.stdout, waited.stdout


def test_wait_and_status_pass_through_the_job_own_distinctive_exit_code():
    """`review wait`/`review status` must report the JOB's own recorded exit code, not
    collapse every failure to a bare 1 — a caller scripting against a specific code
    (2 for a usage error, 124 for a timeout, …) needs the real value. A missing
    `--task` is a deterministic, fake-backend-independent way to force exit code 2."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        env = _env(tmp)
        env.pop("REVIEW_TASK_CODE", None)

        args = [
            "diff",
            "--staged",
            "-C",
            str(repo),
            "-m",
            "claude:claude-opus-4-8",
            "--detach",
        ]
        detach = _run_cli(args, env)
        assert detach.returncode == 0, detach.stderr
        job_id = _extract_job_id(detach.stdout)

        waited = _run_cli(["wait", job_id, "--poll", "0.1"], env, timeout=15)
        assert waited.returncode == 2, (waited.returncode, waited.stdout, waited.stderr)

        status = _run_cli(["status", job_id, "--json"], env)
        assert json.loads(status.stdout)["exit_code"] == 2
        assert status.returncode == 2, status.returncode


def test_detach_preserves_a_piped_diff():
    """`git diff | review diff --detach` must review the PIPED diff, not silently fall
    back to the live working tree (or fail outside a repo) because the detached
    child's own stdin started out empty. The detached result must match a synchronous
    run fed the exact same piped diff."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        env = _env(tmp, fake_delay=0.2)
        piped_diff = (
            "diff --git a/piped.txt b/piped.txt\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/piped.txt\n"
            "@@ -0,0 +1 @@\n"
            "+piped content, not the working tree\n"
        )
        args = [
            "diff",
            "-C",
            str(repo),
            "-m",
            "claude:claude-opus-4-8",
            "--task",
            "TEST-1",
        ]

        sync_out = tmp / "sync-piped-result.txt"
        sync = _run_cli_with_stdin([*args, "-o", str(sync_out)], env, piped_diff)
        assert sync.returncode == 0, sync.stderr

        detach = _run_cli_with_stdin([*args, "--detach"], env, piped_diff)
        assert detach.returncode == 0, detach.stderr
        job_id = _extract_job_id(detach.stdout)

        rec = _poll_status_json(job_id, env)
        assert rec.get("status") == "done", rec
        detached_result = Path(rec["result_path"]).read_text(encoding="utf-8")
        assert detached_result == sync_out.read_text(encoding="utf-8")


def test_detach_rejected_for_dashboard_and_spec_web():
    """`--detach` is rejected for ANY dashboard/spec-web subaction — not just the
    blocking foreground server — since they already have their own start/stop
    lifecycle and detaching one would double-daemonize it."""
    with tempfile.TemporaryDirectory() as d:
        env = _env(Path(d))

        for args in (
            ["dashboard", "status", "--detach"],
            ["dashboard", "run", "--detach"],
        ):
            proc = _run_cli(args, env)
            assert proc.returncode == 2, (
                args,
                proc.returncode,
                proc.stdout,
                proc.stderr,
            )
            assert "not supported for dashboard/spec-web" in proc.stderr, proc.stderr


def test_killed_detached_job_is_reconciled_not_stuck_running():
    """SIGTERM the detached job's OWN process mid-review: `review status`/`jobs` must
    reconcile it (job-bookkeeping side of review-cli#160) rather than reporting
    "running" forever once the pid is actually gone."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        env = _env(tmp, fake_delay=5.0)

        detach = _run_cli([*_review_diff_args(repo), "--detach"], env)
        job_id = _extract_job_id(detach.stdout)
        rec = json.loads(_run_cli(["status", job_id, "--json"], env).stdout)
        pid = rec["pid"]

        # give the child a moment to actually be mid-review before killing it
        time.sleep(0.3)
        os.kill(pid, signal.SIGTERM)

        deadline = time.monotonic() + 10
        final: dict = {}
        while time.monotonic() < deadline:
            final = json.loads(_run_cli(["status", job_id, "--json"], env).stdout)
            if final.get("status") != "running":
                break
            time.sleep(0.2)
        assert final.get("status") == "unknown-terminated", final


def test_detached_job_pid_update_persists_the_actual_fallback_log_path():
    """codex review (review-cli#162 follow-up, PR #164): the job record's `log_path`
    is written BEFORE `_open_log_with_fallback` opens the log file, so if that open
    falls back to a DIFFERENT path than the one already recorded, the record used to
    keep pointing at the ORIGINAL, inaccessible path forever — `review status` (and
    its JSON output / log-tail feature) reported a path the process never actually
    wrote to. The pid-update write after `Popen` now re-sends the RESOLVED
    `log_path`.

    In-process, white-box test rather than the e2e `_run_cli` style the rest of this
    file uses: a prior version of this test pointed `$REVIEW_JOBS_DIR` at an
    unwritable directory, but `jobs.jobs_dir()` detects that BEFORE `_spawn_detached_job`
    ever builds `log_path` from it and falls back FIRST — so `log_path` was built from
    the already-resolved fallback directory from the start, `_open_log_with_fallback`
    never had to replace anything, and the test could not tell "the fix threads the
    resolved path through" apart from "jobs_dir() already fell back" (codex review: the
    test passed even with the fix reverted). This version forces ONLY the log-file
    open (never the stdin spool) to land at a different, obviously-distinct path
    than the one already recorded — isolating the exact substitution the fix is
    responsible for.

    Manual save/restore (no pytest `monkeypatch` fixture): `tests/smoke.py` runs
    this file's `__main__` block directly (no pytest), which cannot inject fixture
    arguments — a `monkeypatch`-taking test here crashes that runner with "missing
    1 required positional argument" (CI: this exact failure, review-cli#162
    follow-up round 5)."""
    from reviewlib import cli, jobs as jobs_mod, process as process_mod

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        env = _env(tmp, fake_delay=0.2)
        saved_env = {k: os.environ.get(k) for k in env}
        real_open_log_with_fallback = process_mod._open_log_with_fallback
        real_isatty = sys.stdin.isatty
        try:
            os.environ.update(env)

            forced_log_path = tmp / "forced-fallback" / "the.log"
            forced_log_path.parent.mkdir(parents=True)

            def _fake_open_log_with_fallback(path, flags=process_mod._LOG_OPEN_FLAGS):
                if path.name.endswith(".log"):
                    fd = os.open(
                        str(forced_log_path),
                        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                        0o600,
                    )
                    return fd, forced_log_path
                return real_open_log_with_fallback(path, flags)

            cli._open_log_with_fallback = _fake_open_log_with_fallback
            # Simulate a tty (no piped stdin) — under pytest's captured stdin (or
            # this file's own standalone `__main__` runner, whose stdin is also not
            # a real tty), `sys.stdin.isatty()` is False and a real `.read()`
            # raises, since this test calls `_spawn_detached_job` directly rather
            # than through a real subprocess with real inherited stdin.
            sys.stdin.isatty = lambda: True

            raw = [*_review_diff_args(repo), "--task", "TEST-1"]
            rc = cli._spawn_detached_job(raw)
            assert rc == 0

            recorded = None
            for candidate in jobs_mod.jobs_dir().glob("*.json"):
                data = json.loads(candidate.read_text())
                if Path(data.get("log_path", "")) != forced_log_path:
                    continue
                recorded = data
                break
            assert recorded is not None, (
                "no job record's log_path matches the forced fallback path — the "
                "pid-update write did not re-send the resolved log_path"
            )
            assert recorded["pid"] > 0
            # Let the short-lived detached child actually finish before the tempdir
            # this test created is torn down — it's writing into `tmp` via the
            # forced log fd.
            for _ in range(50):
                try:
                    os.kill(recorded["pid"], 0)
                except OSError:
                    break
                time.sleep(0.1)
        finally:
            cli._open_log_with_fallback = real_open_log_with_fallback
            sys.stdin.isatty = real_isatty
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    failures = []
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                print(f"FAIL {name}: {exc}")
                failures.append(name)
    if failures:
        print(f"\n{len(failures)} failure(s): {failures}")
        raise SystemExit(1)
    print("\nAll tests passed.")
