#!/usr/bin/env python3
"""Unit tests for the EXTERNAL SIGTERM/SIGINT child reaper (reviewlib.process,
review-cli#160).

`reviewlib.backstop` already reaps registered backend children when ITS OWN internal
watchdog fires (see test_backstop.py's
`test_backstop_reaps_registered_backend_children_before_exit`). That covers a run
wedging inside its own logic. It does NOT cover an EXTERNAL signal — an agent's shell
`kill <pid>`, a harness timing out this process, or Ctrl-C — delivered straight to the
`review` process by the OS. Python's default SIGTERM disposition terminates the
process with NO interpreter code run at all (no `finally`, no `atexit`), so
`_run_streamed`'s own cleanup never executes; and because each backend child runs in
its OWN session (`start_new_session=True`, so `_kill_tree` can bound its whole
process-group tree), the external signal never reaches the child either — it
reparents to init and runs unbounded. This is the exact `claude-opus-4-8`/`opencode`
orphan review-cli#160 reports.

`reviewlib.process.install_signal_reaper()` closes this: it registers a SIGTERM/SIGINT
handler that reaps every live registered child (`kill_live_children()`) BEFORE
re-delivering the same signal to the process (restoring the default disposition
first, so the process still dies from the real signal — correct 128+signum exit
status — the reap just runs first).

Same harness style as test_backstop.py: a child process is spawned that registers a
REAL long-lived backend child (`sleep 60`, its own session) and installs the reaper;
the OUTER test sends a REAL external SIGTERM/SIGINT to that child process and asserts
the backend subprocess is dead afterward. No live model call is ever made.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal
    return True


# A child ("the victim") that installs the reaper, registers a REAL long-lived backend
# child (its own session, like the codex/claude/opencode backends), writes both pids
# out, then just sleeps waiting to be signaled by the OUTER test.
_VICTIM_SRC = (
    "import sys; sys.path.insert(0, %r)\n"
    "import os, subprocess, time\n"
    "from reviewlib.process import _register_child, install_signal_reaper\n"
    "install_signal_reaper()\n"
    "p = subprocess.Popen(['sleep', '60'], start_new_session=True)\n"
    "try:\n"
    "    pgid = os.getpgid(p.pid)\n"
    "except OSError:\n"
    "    pgid = None\n"
    "_register_child(p, pgid)\n"
    "with open(sys.argv[1], 'w') as f:\n"
    "    f.write(f'{os.getpid()} {p.pid}')\n"
    "    f.flush()\n"
    "time.sleep(60)\n"
)


def _spawn_victim(pid_file: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", _VICTIM_SRC % str(REPO_ROOT), str(pid_file)],
    )


def _wait_for_pid_file(pid_file: Path, *, timeout: float = 5.0) -> tuple[int, int]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pid_file.exists():
            text = pid_file.read_text().strip()
            if text:
                victim_pid_s, backend_pid_s = text.split()
                return int(victim_pid_s), int(backend_pid_s)
        time.sleep(0.05)
    raise AssertionError(f"victim never wrote its pid file: {pid_file}")


def _assert_dies(pid: int, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        raise AssertionError(f"pid {pid} survived the external signal")


def _run_external_signal_case(sig: int, tmp_path: Path) -> None:
    pid_file = tmp_path / "victim.pid"
    victim = _spawn_victim(pid_file)
    try:
        victim_pid, backend_pid = _wait_for_pid_file(pid_file)
        assert victim_pid == victim.pid
        assert _pid_alive(backend_pid), "backend child did not start"

        # The external signal: exactly what an agent's `kill <pid>` / a harness
        # timeout / Ctrl-C delivers straight to the OS — not something the victim's
        # own code raises internally.
        os.kill(victim.pid, sig)

        victim.wait(timeout=5)
        # The victim must have actually died FROM the signal (the handler restores
        # the default disposition and re-delivers), not swallowed it and kept running.
        assert victim.returncode != 0, victim.returncode

        _assert_dies(backend_pid)
    finally:
        if victim.poll() is None:
            victim.kill()
            victim.wait(timeout=5)


def test_external_sigterm_reaps_registered_backend_child(tmp_path):
    """The regression this closes: review-cli#160 (an orphaned `claude-opus-4-8`
    review-model process, ppid=1, found alive 3.5h+ after its `review` run had
    already been killed/exited). A REAL external SIGTERM to the `review` process must
    not leave its registered backend child behind."""
    _run_external_signal_case(signal.SIGTERM, tmp_path)


def test_external_sigint_reaps_registered_backend_child(tmp_path):
    """Same gap, the Ctrl-C path: SIGINT must also reap before the process dies."""
    _run_external_signal_case(signal.SIGINT, tmp_path)


# A victim that ALSO exercises the graceful-shutdown path a persistent server
# (dashboard/spec-web) relies on: it catches KeyboardInterrupt itself and writes a
# marker before exiting. If `install_signal_reaper`'s SIGINT handler failed to chain to
# Python's own `default_int_handler` (e.g. a regression back to the earlier
# always-SIG_DFL behaviour), this except block would never run — the process would die
# from the raw signal instead, and the marker would never appear.
_GRACEFUL_VICTIM_SRC = (
    "import sys; sys.path.insert(0, %r)\n"
    "import os, subprocess, time\n"
    "from reviewlib.process import _register_child, install_signal_reaper\n"
    "install_signal_reaper()\n"
    "p = subprocess.Popen(['sleep', '60'], start_new_session=True)\n"
    "try:\n"
    "    pgid = os.getpgid(p.pid)\n"
    "except OSError:\n"
    "    pgid = None\n"
    "_register_child(p, pgid)\n"
    "with open(sys.argv[1], 'w') as f:\n"
    "    f.write(f'{os.getpid()} {p.pid}')\n"
    "    f.flush()\n"
    "try:\n"
    "    time.sleep(60)\n"
    "except KeyboardInterrupt:\n"
    "    with open(sys.argv[2], 'w') as f:\n"
    "        f.write('graceful-shutdown-ran')\n"
    "    raise\n"
)


def test_sigint_still_raises_keyboardinterrupt_for_graceful_shutdown(tmp_path):
    """The chaining fix: a SIGINT must both (a) reap the registered backend child and
    (b) still raise `KeyboardInterrupt` in the victim's own code, exactly as Python's
    default SIGINT disposition always has — so a persistent server's own
    `except KeyboardInterrupt` graceful-shutdown path keeps running. Regression target:
    an earlier version of the handler unconditionally reset SIGINT to SIG_DFL before
    re-signaling, which kills the process from the RAW signal and skips this path."""
    pid_file = tmp_path / "victim.pid"
    marker_file = tmp_path / "graceful.marker"
    victim = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _GRACEFUL_VICTIM_SRC % str(REPO_ROOT),
            str(pid_file),
            str(marker_file),
        ],
    )
    try:
        victim_pid, backend_pid = _wait_for_pid_file(pid_file)
        assert victim_pid == victim.pid

        os.kill(victim.pid, signal.SIGINT)
        victim.wait(timeout=5)

        assert marker_file.read_text() == "graceful-shutdown-ran"
        _assert_dies(backend_pid)
    finally:
        if victim.poll() is None:
            victim.kill()
            victim.wait(timeout=5)


def test_install_signal_reaper_is_idempotent():
    """Calling `install_signal_reaper()` more than once (e.g. a test invoking `main()`
    repeatedly in-process) must not raise or double-register — it's a no-op after the
    first call in a given process."""
    from reviewlib.process import install_signal_reaper

    install_signal_reaper()
    install_signal_reaper()  # must not raise


if __name__ == "__main__":
    import tempfile

    failures = []
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            with tempfile.TemporaryDirectory() as d:
                try:
                    if "tmp_path" in fn.__code__.co_varnames[: fn.__code__.co_argcount]:
                        fn(Path(d))
                    else:
                        fn()
                    print(f"PASS {name}")
                except Exception as exc:  # noqa: BLE001
                    print(f"FAIL {name}: {exc}")
                    failures.append(name)
    if failures:
        print(f"\n{len(failures)} failure(s): {failures}")
        raise SystemExit(1)
    print("\nAll tests passed.")
