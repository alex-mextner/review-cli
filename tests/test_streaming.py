#!/usr/bin/env python3
"""Unit tests for the streaming backend runner in the reviewlib package.

Proves the two properties the streaming runner must guarantee:
  (a) child stdout reaches the live LOG FILE incrementally, BEFORE the child exits;
  (b) a timeout PRESERVES the partial accumulated output (non-empty stdout + a clear
      TIMEOUT marker) and a non-zero returncode, instead of raising the buffer away.

Uses a fake slow command we control (a tiny python one-liner) so the test never
depends on codex/gemini/claude/opencode being installed.

After the Stage 0 decomposition the implementation lives in the `reviewlib`
package (the streaming runner in `reviewlib.process`, the backends in
`reviewlib.backends`); `bin/review` is now a thin shim. These tests import the
package directly — the RUNTIME behaviour is unchanged.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Make the in-repo package importable without an install (mirrors the bin/review shim).
sys.path.insert(0, str(REPO_ROOT))

import reviewlib as review  # noqa: E402  (package façade re-exports the public surface)
from reviewlib import backends as review_backends  # noqa: E402  (backends patch target)


# A fake backend that prints one line every 0.4s for N lines, flushing each line,
# then exits 0. Slow enough that we can observe incremental log growth and force a
# timeout, fast enough that the happy-path test stays quick.
def _slow_argv(lines: int, interval: float = 0.4) -> list[str]:
    code = (
        "import time,sys\n"
        f"for i in range({lines}):\n"
        "    print('line-%d' % i, flush=True)\n"
        f"    time.sleep({interval})\n"
    )
    return [sys.executable, "-c", code]


def test_log_file_grows_incrementally_before_exit():
    """(a) The live log file must contain early lines while the child is STILL running."""
    argv = _slow_argv(lines=12, interval=0.4)  # ~4.8s total runtime

    result_holder: dict[str, object] = {}

    def _run_it():
        result_holder["result"] = review._run_streamed(
            argv,
            cwd=REPO_ROOT,
            timeout=30,
            backend="faketest",
            round_no=1,
        )

    t = threading.Thread(target=_run_it, daemon=True)
    t.start()

    # Poll the log dir for a fresh log file and wait until it has a few lines while
    # the runner thread is STILL alive (proves no wait-for-exit buffering).
    log_dir = review.log_dir()
    deadline = time.time() + 8.0
    grew_while_running = False
    seen_path: Path | None = None
    while time.time() < deadline and t.is_alive():
        candidates = sorted(log_dir.glob("*-faketest-*.log"), key=lambda p: p.stat().st_mtime)
        if candidates:
            seen_path = candidates[-1]
            text = seen_path.read_text(encoding="utf-8", errors="replace")
            if "line-0" in text and t.is_alive():
                grew_while_running = True
                break
        time.sleep(0.1)

    assert seen_path is not None, "no live log file was created"
    assert grew_while_running, (
        "log file did not receive output while the child was still running "
        "(runner is buffering until exit instead of streaming)"
    )

    t.join(timeout=30)
    result = result_holder["result"]
    assert result is not None
    assert result.returncode == 0
    assert "line-0" in result.stdout
    assert "line-11" in result.stdout
    # The full accumulator should also be persisted in the log.
    assert "line-11" in seen_path.read_text(encoding="utf-8", errors="replace")


def test_timeout_preserves_partial_output():
    """(b) On timeout, return partial stdout + a TIMEOUT marker and rc 124."""
    argv = _slow_argv(lines=40, interval=0.4)  # ~16s; we cut it off at 2s

    result = review._run_streamed(
        argv,
        cwd=REPO_ROOT,
        timeout=2,
        backend="faketest",
        round_no=2,
    )

    assert result.returncode == 124, (
        f"timed-out call must report rc 124, got {result.returncode}"
    )
    assert result.stdout.strip(), "partial output was lost on timeout (stdout empty)"
    assert "line-0" in result.stdout, "early lines missing from the preserved buffer"
    assert "TIMEOUT" in result.stdout, "TIMEOUT marker missing from the preserved buffer"


def test_flushed_partial_line_survives_timeout():
    """Output that is FLUSHED without a trailing newline must still be captured.

    Line-buffered iteration would hold a newline-less chunk until a newline or EOF;
    if an escaped descendant keeps the pipe open, EOF never comes in the grace window
    and that already-written text would be lost. Chunk-based reads must capture it.
    """
    code = (
        "import sys, subprocess, time\n"
        "sys.stdout.write('partial-no-newline')\n"  # flushed, but NO newline
        "sys.stdout.flush()\n"
        # escaped descendant keeps stdout open so the parent's pipe never EOFs
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
        "start_new_session=True)\n"
        "time.sleep(60)\n"
    )
    argv = [sys.executable, "-c", code]

    result = review._run_streamed(
        argv, cwd=REPO_ROOT, timeout=2, backend="faketest-partial", round_no=7
    )

    assert result.returncode == 124
    assert "partial-no-newline" in result.stdout, (
        "flushed newline-less output was lost on timeout"
    )
    assert "TIMEOUT" in result.stdout


def test_timeout_kills_process_tree_without_hanging():
    """A backend that spawns a grandchild inheriting stdout must NOT hang the runner.

    If we only signalled the direct child, the grandchild would keep the stdout pipe
    open and the read loop would block past the deadline forever. start_new_session +
    process-group kill must reap the whole tree, so the call returns near the deadline.
    """
    # Parent prints one line, spawns a long-sleeping grandchild that INHERITS stdout,
    # then sleeps long itself — leaving the pipe open via both.
    code = (
        "import os, sys, subprocess, time\n"
        "print('parent-line', flush=True)\n"
        # grandchild keeps stdout (fd 1) open while sleeping 60s
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "time.sleep(60)\n"
    )
    argv = [sys.executable, "-c", code]

    started = time.monotonic()
    result = review._run_streamed(
        argv,
        cwd=REPO_ROOT,
        timeout=3,
        backend="faketree",
        round_no=3,
    )
    elapsed = time.monotonic() - started

    # Must return shortly after the deadline, not hang on the grandchild's open pipe.
    assert elapsed < 20, f"runner hung on the process tree (took {elapsed:.1f}s)"
    assert result.returncode == 124
    assert "parent-line" in result.stdout
    assert "TIMEOUT" in result.stdout


def test_clean_exit_with_grandchild_holding_pipe_does_not_hang_or_leak():
    """The parent backend EXITS CLEANLY but leaves a grandchild in the same process
    group still holding stdout open.

    An EOF-dependent reader would block forever on that open pipe. The runner must NOT
    hang, must still return the parent's captured output, and must reap the group so
    no grandchild is left running. Because the process itself exited 0, this is a
    clean success (rc 0) — not a timeout — so no TIMEOUT marker is appended.
    """
    code = (
        "import sys, subprocess, time\n"
        "print('parent-line', flush=True)\n"
        # grandchild inherits stdout (same group) and sleeps; parent exits right away.
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "sys.exit(0)\n"
    )
    argv = [sys.executable, "-c", code]

    started = time.monotonic()
    result = review._run_streamed(
        argv,
        cwd=REPO_ROOT,
        timeout=30,  # generous: the parent exits instantly, so we never hit it
        backend="faketree-clean",
        round_no=5,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 20, (
        f"runner hung on a grandchild after the parent exited (took {elapsed:.1f}s)"
    )
    assert "parent-line" in result.stdout
    assert result.returncode == 0
    # The group must be reaped: no 60s sleeper should survive this call.
    import subprocess as _sp

    survivors = _sp.run(
        ["pgrep", "-f", "import time; time.sleep(60)"], capture_output=True, text=True
    ).stdout.strip()
    assert not survivors, f"grandchild leaked (surviving pids: {survivors})"


def test_timeout_when_grandchild_escapes_the_process_group():
    """The HARDEST case: a grandchild puts itself in its OWN session (new process
    group) while inheriting stdout, then sleeps long. killpg(parent_group) cannot
    reach it, so the inherited stdout fd stays open and an EOF-dependent reader would
    hang forever. The timeout must be independent of pipe EOF: kill what we can, then
    return partial output at the deadline regardless of the escaped fd.
    """
    code = (
        "import sys, subprocess, time\n"
        "print('parent-line', flush=True)\n"
        # grandchild starts a NEW session -> escapes the parent's process group, but
        # still inherits stdout (fd 1) and holds it open while sleeping.
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
        "start_new_session=True)\n"
        "time.sleep(60)\n"
    )
    argv = [sys.executable, "-c", code]

    started = time.monotonic()
    result = review._run_streamed(
        argv,
        cwd=REPO_ROOT,
        timeout=3,
        backend="faketree-escape",
        round_no=6,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 20, (
        f"runner hung on a grandchild that escaped the process group (took {elapsed:.1f}s)"
    )
    assert "parent-line" in result.stdout
    assert "TIMEOUT" in result.stdout
    assert result.returncode == 124


def test_no_daemon_traceback_when_escaped_writer_outlives_close():
    """An escaped descendant that KEEPS WRITING after the runner returns and closes the
    log must not crash the still-alive daemon drain thread (write-after-close).

    Run in a child process so we can assert its stderr carries no traceback.
    """
    import subprocess as _sp
    import tempfile

    driver = (
        "import os, sys, time\n"
        "os.environ.setdefault('REVIEW_LOG_DIR', %r)\n"
        "sys.path.insert(0, %r)\n"  # repo root, so the in-repo reviewlib package imports
        "import reviewlib as rv\n"
        "code = (\n"
        "  \"import sys, subprocess, time\\n\"\n"
        "  \"print('p', flush=True)\\n\"\n"
        "  \"subprocess.Popen([sys.executable,'-c','import sys,time\\\\nwhile True:\\\\n \"\n"
        "  \"sys.stdout.write(chr(120))\\\\n sys.stdout.flush()\\\\n time.sleep(0.2)'], \"\n"
        "  \"start_new_session=True)\\n\"\n"
        "  \"time.sleep(60)\\n\"\n"
        ")\n"
        "r = rv._run_streamed([sys.executable,'-c',code], cwd=rv.Path('.'), timeout=2, "
        "backend='postclose', round_no=1)\n"
        "assert r.returncode == 124 and 'p' in r.stdout\n"
        "time.sleep(2)\n"  # let any post-close daemon write surface as a traceback
        "print('DRIVER_OK')\n"
    ) % (tempfile.mkdtemp(), str(REPO_ROOT))

    proc = _sp.run([sys.executable, "-c", driver], cwd=str(REPO_ROOT),
                   capture_output=True, text=True, timeout=40)
    # cleanup any survivors the driver spawned
    _sp.run(["pkill", "-f", "while True"], capture_output=True)
    assert "DRIVER_OK" in proc.stdout, f"driver failed: {proc.stdout}\n{proc.stderr}"
    lowered = proc.stderr.lower()
    assert "traceback" not in lowered and "exception in thread" not in lowered, (
        f"daemon thread wrote after close:\n{proc.stderr}"
    )


def test_log_file_is_private():
    """Logs may contain reviewed prompts/diffs, so files must be 0600 (owner-only)."""
    import stat

    argv = [sys.executable, "-c", "print('x', flush=True)"]
    review._run_streamed(argv, cwd=REPO_ROOT, timeout=10, backend="faketest", round_no=4)
    log_dir = review.log_dir()
    logs = sorted(log_dir.glob("*-faketest-*.log"), key=lambda p: p.stat().st_mtime)
    assert logs, "no log file produced"
    mode = stat.S_IMODE(logs[-1].stat().st_mode)
    assert mode & 0o077 == 0, f"log file is group/other-readable (mode {oct(mode)})"


def test_claude_backend_disables_tools_and_mcp_to_avoid_headless_approval():
    captured: dict[str, list[str]] = {}
    # review_claude resolves `_which` / `_run_streamed` from the reviewlib.backends
    # module namespace, so patch THAT module (not the façade) for the override to bite.
    old_which = review_backends._which
    old_run_streamed = review_backends._run_streamed
    old_trust = review_backends._ensure_workspace_trusted

    def fake_which(name: str) -> str:
        assert name == "claude-p"
        return "/bin/claude-p"

    def fake_run_streamed(argv: list[str], **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return review.subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    try:
        review_backends._which = fake_which
        review_backends._run_streamed = fake_run_streamed
        # Stub the auto-trust helper: it writes ~/.claude.json, and this unit
        # test must not touch the real developer/CI config (cwd here is REPO_ROOT).
        review_backends._ensure_workspace_trusted = lambda _cwd: None
        result = review_backends.review_claude("claude:opus", "prompt", "diff", REPO_ROOT, 10)
    finally:
        review_backends._which = old_which
        review_backends._run_streamed = old_run_streamed
        review_backends._ensure_workspace_trusted = old_trust

    assert result.returncode == 0
    argv = captured["argv"]
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert "--tools" in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in argv
    assert "--disable-slash-commands" in argv
    assert "--safe-mode" in argv
    assert "--append-system-prompt" in argv
    assert "Do not use tools" in argv[argv.index("--append-system-prompt") + 1]
    blocked = argv[argv.index("--disallowedTools") + 1 : argv.index("--timeout-sec")]
    for tool in (
        "Edit",
        "MultiEdit",
        "Write",
        "Bash",
        "Read",
        "Grep",
        "Glob",
        "NotebookEdit",
        "SlashCommand",
        "Task",
        "TodoWrite",
        "ExitPlanMode",
        "WebFetch",
        "WebSearch",
    ):
        assert tool in blocked
    assert argv[argv.index("--model") + 1] == "opus"
    # The payload is fed over STDIN, not a `-p <payload>` argv arg: a brainstorm
    # round's growing transcript (or a big diff) as a command-line argument blows
    # past ARG_MAX → execve E2BIG → the call dies with no output. `-p` stays the
    # trailing print flag; the prompt arrives via input_text (stdin).
    assert argv[-1] == "-p"
    assert captured["kwargs"]["input_text"] == "prompt\n\n```diff\ndiff\n```"


def test_run_streamed_feeds_large_input_over_stdin_without_deadlock():
    """A payload bigger than the OS pipe buffer must round-trip via stdin — proving
    stdin is written, CLOSED (EOF so the child exits, not hangs to timeout), and
    drained CONCURRENTLY with stdout (else a full pipe deadlocks). The real contract
    the claude backend now relies on instead of a `-p <payload>` arg."""
    import os
    import tempfile

    payload = "x" * 200_000 + "\nEND\n"  # > 64 KiB pipe buffer
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["REVIEW_LOG_DIR"] = tmp
        proc = review._run_streamed(["cat"], cwd=Path(tmp), input_text=payload, timeout=30)
    assert proc.returncode == 0
    assert proc.stdout == payload


def test_log_dir_uses_os_standard_locations():
    import os
    import tempfile

    saved_platform = sys.platform
    saved = {k: os.environ.get(k) for k in ("REVIEW_LOG_DIR", "XDG_STATE_HOME", "HOME")}
    try:
        with tempfile.TemporaryDirectory() as home:
            os.environ["HOME"] = home
            os.environ.pop("REVIEW_LOG_DIR", None)
            os.environ.pop("XDG_STATE_HOME", None)
            sys.platform = "darwin"
            assert review.log_dir() == Path(home) / "Library" / "Logs" / "review-cli"
            sys.platform = "linux"
            assert review.log_dir() == Path(home) / ".local" / "state" / "review-cli" / "logs"
            os.environ["XDG_STATE_HOME"] = str(Path(home) / "xdg")
            assert review.log_dir() == Path(home) / "xdg" / "review-cli" / "logs"
            os.environ["XDG_STATE_HOME"] = "relative-ignored"  # XDG: relative → ignored
            assert review.log_dir() == Path(home) / ".local" / "state" / "review-cli" / "logs"
    finally:
        sys.platform = saved_platform
        for key, val in saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


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
