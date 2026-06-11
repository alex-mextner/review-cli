#!/usr/bin/env python3
"""Unit tests for the streaming backend runner in bin/review.

Proves the two properties the streaming runner must guarantee:
  (a) child stdout reaches the live LOG FILE incrementally, BEFORE the child exits;
  (b) a timeout PRESERVES the partial accumulated output (non-empty stdout + a clear
      TIMEOUT marker) and a non-zero returncode, instead of raising the buffer away.

Uses a fake slow command we control (a tiny python one-liner) so the test never
depends on codex/gemini/claude/opencode being installed.
"""
from __future__ import annotations

import importlib.util
import sys
import threading
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_REVIEW = REPO_ROOT / "bin" / "review"


def _load_module():
    # bin/review has no .py extension, so pin the source loader explicitly.
    loader = SourceFileLoader("review_cli_bin", str(BIN_REVIEW))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Register before exec so @dataclass can resolve cls.__module__ during import.
    sys.modules[loader.name] = module
    spec.loader.exec_module(module)
    return module


review = _load_module()


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


def test_timeout_when_parent_exits_but_grandchild_holds_pipe():
    """The HARD case: the parent backend EXITS immediately, leaving a grandchild in
    the same process group still holding stdout open.

    Killing only the direct child (or giving up once proc.poll() != None) leaves the
    grandchild's open pipe, so the stdout read loop never EOFs and the call hangs
    forever. The watchdog must enforce the deadline by signalling the whole process
    GROUP even after the direct child has already exited.
    """
    code = (
        "import sys, subprocess, time\n"
        "print('parent-line', flush=True)\n"
        # grandchild inherits stdout and sleeps; parent exits right away.
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "sys.exit(0)\n"
    )
    argv = [sys.executable, "-c", code]

    started = time.monotonic()
    result = review._run_streamed(
        argv,
        cwd=REPO_ROOT,
        timeout=3,
        backend="faketree-orphan",
        round_no=5,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 20, (
        f"runner hung on a grandchild after the parent exited (took {elapsed:.1f}s)"
    )
    assert "parent-line" in result.stdout
    assert "TIMEOUT" in result.stdout
    assert result.returncode == 124


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
