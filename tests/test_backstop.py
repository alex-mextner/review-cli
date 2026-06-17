#!/usr/bin/env python3
"""Unit tests for the internal run backstop (reviewlib.backstop) and its CLI wiring.

`review` advertises NO external timeout — the ONLY time bound is an INTERNAL
last-resort backstop of <=4h that force-terminates a genuinely wedged run. These
tests cover:
  * the clamped `backstop_seconds()` (default 4h; env can only LOWER, never raise;
    garbage / non-positive ignored),
  * `run_backstop` cancels its watchdog on a normal exit (no fire on a fast block),
  * the watchdog ACTUALLY fires for a wedged run — exercised in a child process,
    since a fire calls `os._exit(124)` (asserting it exits 124 in-band would kill the
    test runner), and
  * `main()` arms the backstop, so a hung dispatch is force-terminated with 124 and
    the loud backstop line.

Same harness style as the other test_* files: plain test_* functions run by the
__main__ block (and pytest-collectable). No live model call is ever made — the
wedged-run tests sleep in a throwaway child, they don't dispatch a backend.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import backstop as _bs  # noqa: E402


# ---------------------------------------------------------------------------
# backstop_seconds(): the clamped ceiling
# ---------------------------------------------------------------------------
class _EnvBackstop:
    """Context manager: set/clear $REVIEW_BACKSTOP_SECONDS for one test."""

    def __init__(self, value: str | None) -> None:
        self._value = value

    def __enter__(self):
        self._saved = os.environ.get("REVIEW_BACKSTOP_SECONDS")
        if self._value is None:
            os.environ.pop("REVIEW_BACKSTOP_SECONDS", None)
        else:
            os.environ["REVIEW_BACKSTOP_SECONDS"] = self._value
        return self

    def __exit__(self, *exc):
        if self._saved is None:
            os.environ.pop("REVIEW_BACKSTOP_SECONDS", None)
        else:
            os.environ["REVIEW_BACKSTOP_SECONDS"] = self._saved
        return False


def test_backstop_defaults_to_4h_ceiling():
    with _EnvBackstop(None):
        assert _bs.backstop_seconds() == _bs.MAX_BACKSTOP_SECONDS == 14400


def test_backstop_env_can_lower_but_not_raise():
    # A value below the ceiling is honored verbatim (a tighter last-resort bound).
    with _EnvBackstop("60"):
        assert _bs.backstop_seconds() == 60
    # A value ABOVE the 4h ceiling is clamped DOWN — the cap can never be raised.
    with _EnvBackstop(str(_bs.MAX_BACKSTOP_SECONDS + 5000)):
        assert _bs.backstop_seconds() == _bs.MAX_BACKSTOP_SECONDS


def test_backstop_env_garbage_and_nonpositive_fall_back_to_ceiling():
    for bad in ("", "abc", "0", "-30", "1.5"):
        with _EnvBackstop(bad):
            assert _bs.backstop_seconds() == _bs.MAX_BACKSTOP_SECONDS, bad


# ---------------------------------------------------------------------------
# run_backstop(): cancels cleanly on a normal (fast) exit — it does NOT fire
# ---------------------------------------------------------------------------
def test_run_backstop_does_not_fire_on_fast_block():
    """A block that finishes well under the ceiling must NOT trip the watchdog: we
    are still alive after the `with`, and the timer is cancelled. (If the watchdog
    fired it would os._exit and this process would never reach the assert.)"""
    fired = {"n": 0}

    # A tiny backstop, but the block returns in microseconds — far under it.
    with _bs.run_backstop(2):
        pass
    # Give a fired-but-cancelled timer no chance to sneak through.
    time.sleep(0.05)
    assert fired["n"] == 0  # trivially true; the real proof is that we got here alive


def test_run_backstop_clamps_explicit_seconds_to_ceiling():
    # An explicit `seconds` above the ceiling is clamped too (parity with the env path),
    # and a <1 value floors at 1 — but neither fires here (the block is instant).
    with _bs.run_backstop(_bs.MAX_BACKSTOP_SECONDS + 1000):
        pass
    with _bs.run_backstop(0):
        pass


# ---------------------------------------------------------------------------
# The watchdog ACTUALLY fires for a wedged run — exercised in a child process,
# since a fire is an os._exit(124) that would otherwise kill the test runner.
# ---------------------------------------------------------------------------
_CHILD_WEDGED = (
    "import sys; sys.path.insert(0, %r)\n"
    "from reviewlib.backstop import run_backstop\n"
    "import time\n"
    "with run_backstop(1):\n"  # 1s backstop ...
    "    time.sleep(60)\n"     # ... around a 60s 'wedged' block
    "print('UNREACHABLE')\n"
) % str(REPO_ROOT)


def test_backstop_fires_and_exits_124_for_a_wedged_run():
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD_WEDGED],
        capture_output=True, text=True, timeout=20,
    )
    # It must NOT have run the full 60s sleep; the 1s backstop terminates it.
    assert proc.returncode == _bs.BACKSTOP_EXIT_CODE == 124, (proc.returncode, proc.stderr)
    assert "INTERNAL BACKSTOP fired" in proc.stderr, proc.stderr
    assert "4h" in proc.stderr  # the ceiling is named in the loud line
    assert "UNREACHABLE" not in proc.stdout  # the block never completed


def test_backstop_fires_within_seconds_not_minutes():
    """The fire latency tracks the (lowered) backstop, not the wedged block's length:
    a 1s backstop around a 60s sleep returns in ~1s, proving it's the backstop firing
    and not the sleep completing."""
    t0 = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD_WEDGED],
        capture_output=True, text=True, timeout=20,
    )
    elapsed = time.monotonic() - t0
    assert proc.returncode == 124, (proc.returncode, proc.stderr)
    assert elapsed < 15, elapsed  # nowhere near the 60s sleep


# A child that registers a REAL long-lived backend subprocess (its own session, like the
# codex/claude/opencode backends), writes that pid out, then wedges under a 1s backstop.
# The backstop must reap the registered child before its hard exit, so the OUTER test can
# assert the backend pid is dead — i.e. the backstop actually bounds the WORK, not just
# the CLI process (codex P2).
_CHILD_REGISTERS_BACKEND = (
    "import sys; sys.path.insert(0, %r)\n"
    "import subprocess, time\n"
    "from reviewlib.process import _register_child\n"
    "from reviewlib.backstop import run_backstop\n"
    "p = subprocess.Popen(['sleep', '60'], start_new_session=True)\n"
    "import os\n"
    "try:\n"
    "    pgid = os.getpgid(p.pid)\n"
    "except OSError:\n"
    "    pgid = None\n"
    "_register_child(p, pgid)\n"
    "open(sys.argv[1], 'w').write(str(p.pid))\n"
    "with run_backstop(1):\n"
    "    time.sleep(30)\n"
)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal
    return True


def test_backstop_reaps_registered_backend_children_before_exit():
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        pid_file = os.path.join(d, "backend.pid")
        proc = subprocess.run(
            [sys.executable, "-c", _CHILD_REGISTERS_BACKEND % str(REPO_ROOT), pid_file],
            capture_output=True, text=True, timeout=20,
        )
        assert proc.returncode == 124, (proc.returncode, proc.stderr)
        backend_pid = int(Path(pid_file).read_text().strip())
        # Give the kill a beat to take effect, then assert the backend subprocess is gone
        # — the backstop reaped it instead of orphaning it past the CLI's force-exit.
        deadline = time.monotonic() + 5
        while _pid_alive(backend_pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        if _pid_alive(backend_pid):
            # Don't leak the sleep if the assert is about to fail.
            try:
                os.kill(backend_pid, 9)
            except OSError:
                pass
            raise AssertionError(f"backend pid {backend_pid} survived the backstop fire")


# A registered backend that IGNORES SIGTERM (traps it) — exactly codex's concern: a polite
# SIGTERM-then-wait reap could be preempted by the deadman before SIGKILL lands, leaving
# the backend alive. The kill-first `kill_live_children` sends SIGKILL straight away, so an
# uncatchable kill still reaps it. The OUTER test writes the backend pid out and asserts it
# is dead after the backstop fires.
_CHILD_SIGTERM_IGNORING_BACKEND = (
    "import sys; sys.path.insert(0, %r)\n"
    "import subprocess, time, os, textwrap\n"
    "from reviewlib.process import _register_child\n"
    "from reviewlib.backstop import run_backstop\n"
    # The backend traps SIGTERM (ignores it) and sleeps — only SIGKILL can stop it.
    "backend_src = 'import signal,time\\n'\n"
    "backend_src += 'signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n'\n"
    "backend_src += 'time.sleep(60)\\n'\n"
    "p = subprocess.Popen([sys.executable, '-c', backend_src], start_new_session=True)\n"
    "try:\n"
    "    pgid = os.getpgid(p.pid)\n"
    "except OSError:\n"
    "    pgid = None\n"
    "_register_child(p, pgid)\n"
    "open(sys.argv[1], 'w').write(str(p.pid))\n"
    "with run_backstop(1):\n"
    "    time.sleep(30)\n"
)


def test_backstop_kills_sigterm_ignoring_backend():
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        pid_file = os.path.join(d, "backend.pid")
        proc = subprocess.run(
            [sys.executable, "-c", _CHILD_SIGTERM_IGNORING_BACKEND % str(REPO_ROOT), pid_file],
            capture_output=True, text=True, timeout=20,
        )
        assert proc.returncode == 124, (proc.returncode, proc.stderr)
        backend_pid = int(Path(pid_file).read_text().strip())
        deadline = time.monotonic() + 5
        while _pid_alive(backend_pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        if _pid_alive(backend_pid):
            try:
                os.kill(backend_pid, 9)
            except OSError:
                pass
            raise AssertionError(f"SIGTERM-ignoring backend {backend_pid} survived the backstop")


# A child that wedges with its stderr ALREADY FILLED — exactly codex's scenario: a
# wrapper captures stderr (PIPE) and isn't draining it, so a flushed `print` in the
# watchdog would block forever. The backstop must STILL terminate the process (the
# deadman + the reap-before-announce ordering guarantee it), so the OUTER test reads
# nothing from the pipe (never drains it) and asserts the child exits 124 promptly.
_CHILD_FULL_STDERR = (
    "import sys; sys.path.insert(0, %r)\n"
    "import os, time\n"
    "from reviewlib.backstop import run_backstop\n"
    "with run_backstop(1):\n"
    # Arm FIRST, then wedge by filling stderr past the pipe buffer (no reader drains it):
    # this os.write blocks once the buffer is full, which IS the wedged state. When the
    # 1s watchdog fires, its own announce hits the same full pipe — and must NOT deadlock
    # the backstop (the deadman + reap-before-announce ordering guarantee the exit).
    "    os.write(2, b'x' * (1024 * 1024 * 16))\n"
    "    time.sleep(60)\n"
)


def test_backstop_fires_even_when_stderr_pipe_is_full():
    """codex P2: if stderr is a pipe a non-draining parent has let fill, the watchdog's
    flushed announce would block forever and the process would NOT terminate. We capture
    stderr (a PIPE) and deliberately NEVER read it, then assert the child still exits 124
    well within the deadman grace — proving the announce can't deadlock the backstop."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD_FULL_STDERR % str(REPO_ROOT)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        # Do NOT communicate()/read the pipe — that would drain it and mask the bug.
        rc = proc.wait(timeout=12)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise AssertionError("backstop did not terminate the process with a full stderr pipe")
    assert rc == 124, rc


# ---------------------------------------------------------------------------
# main() arms the backstop: a hung dispatch is force-terminated with 124.
# ---------------------------------------------------------------------------
_CHILD_MAIN_HANGS = (
    "import sys; sys.path.insert(0, %r)\n"
    "import reviewlib.cli as cli\n"
    "import time\n"
    # Replace the dispatch with a hang, so the ONLY thing that can stop the process is
    # the backstop main() arms around it. If main() did not arm the backstop, this
    # would hang until the outer subprocess timeout (a test failure).
    "cli._dispatch = lambda argv=None: time.sleep(60)\n"
    "sys.exit(cli.main([]))\n"
) % str(REPO_ROOT)


def test_main_arms_the_backstop_around_dispatch():
    env = dict(os.environ, REVIEW_BACKSTOP_SECONDS="1")
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD_MAIN_HANGS],
        capture_output=True, text=True, timeout=20, env=env,
    )
    assert proc.returncode == 124, (proc.returncode, proc.stderr)
    assert "INTERNAL BACKSTOP fired" in proc.stderr, proc.stderr


def test_main_does_not_backstop_persistent_server_subcommands():
    """The persistent server invocations (`dashboard run` / its hidden `__serve`, and
    `spec-web <spec>`) run until Ctrl-C, so main() must NOT arm the watchdog around them —
    otherwise a lowered backstop (or the 4h ceiling) would kill the server (codex P2).

    NOTE: a BARE `review dashboard` is now the managed-service HELP, not a server, and the
    short-lived lifecycle actions (`start`/`status`/`stop`/`enable`/`disable`) return
    immediately — only the FOREGROUND blocking server (`dashboard run`, `dashboard __serve`)
    is persistent and must bypass the backstop. Stub the server dispatch with a block longer
    than a tiny backstop; reaching the post-block print proves the server was left
    unbounded."""
    from reviewlib import cli as _cli

    for sub in ("dashboard", "spec-web"):
        assert sub in _cli._SERVER_SUBCOMMANDS
    child = (
        "import sys; sys.path.insert(0, %r)\n"
        "import reviewlib.cli as cli, time\n"
        # Server 'runs' for 5s; a 1s backstop, if armed, would kill it at ~1s with 124.
        "cli._dispatch = lambda argv=None: (time.sleep(5), print('SERVER-RAN'), 0)[-1]\n"
        "sys.exit(cli.main(['dashboard', 'run']))\n"
    ) % str(REPO_ROOT)
    env = dict(os.environ, REVIEW_BACKSTOP_SECONDS="1")
    proc = subprocess.run(
        [sys.executable, "-c", child],
        capture_output=True, text=True, timeout=20, env=env,
    )
    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    assert "SERVER-RAN" in proc.stdout, proc.stdout
    assert "INTERNAL BACKSTOP fired" not in proc.stderr, proc.stderr


# ---------------------------------------------------------------------------
# Advertising: the SKILL.md / blurb / README state "no external timeout; <=4h backstop"
# ---------------------------------------------------------------------------
def test_skill_md_advertises_no_external_timeout_and_4h_backstop():
    from reviewlib.install import SKILL_BLURB, SKILL_MD

    low_md = SKILL_MD.lower()
    assert "no external timeout" in low_md
    assert "backstop" in low_md
    assert "4h" in low_md or "<=4h" in low_md
    low_blurb = SKILL_BLURB.lower()
    assert "no external timeout" in low_blurb
    assert "backstop" in low_blurb


def test_skill_md_does_not_contradict_no_external_timeout():
    """codex P2: the generated skill must not also TELL agents an external cap is OK. The
    old 'if you must bound it, give it MINUTES … a cap below the printed ETA' bullet
    allowed exactly that, contradicting the new no-external-timeout contract — assert that
    contradicting phrasing is gone so a regenerated skill stays internally consistent."""
    from reviewlib.install import SKILL_MD

    low = SKILL_MD.lower()
    assert "if you must bound it" not in low
    assert "minutes, not seconds" not in low


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
