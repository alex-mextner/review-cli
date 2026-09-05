#!/usr/bin/env python3
"""Unit tests for the streaming backend runner in the reviewlib package.

Proves the two properties the streaming runner must guarantee:
  (a) child stdout reaches the live LOG FILE incrementally, BEFORE the child exits;
  (b) an idle timeout PRESERVES the partial accumulated output (non-empty stdout + a
      clear TIMEOUT marker) and a non-zero returncode, instead of raising the buffer away.

Uses a fake slow command we control (a tiny python one-liner) so the test never
depends on codex/gemini/claude/opencode being installed.

After the Stage 0 decomposition the implementation lives in the `reviewlib`
package (the streaming runner in `reviewlib.process`, the backends in
`reviewlib.backends`); `bin/review` is now a thin shim. These tests import the
package directly — the RUNTIME behaviour is unchanged.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Make the in-repo package importable without an install (mirrors the bin/review shim).
sys.path.insert(0, str(REPO_ROOT))

import reviewlib as review  # noqa: E402  (package façade re-exports the public surface)
from reviewlib import backends as review_backends  # noqa: E402  (backends patch target)
import reviewlib.process as process  # noqa: E402


def _with_env(**env):
    class _Ctx:
        def __enter__(self):
            self._saved = {k: os.environ.get(k) for k in env}
            for k, v in env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            return self

        def __exit__(self, *exc):
            for k, old in self._saved.items():
                if old is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = old
            return False

    return _Ctx()


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
        candidates = sorted(
            log_dir.glob("*-faketest-*.log"), key=lambda p: p.stat().st_mtime
        )
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
    """(b) On idle timeout, return partial stdout + a TIMEOUT marker and rc 124."""
    code = "import time\nprint('line-0', flush=True)\ntime.sleep(60)\n"
    argv = [sys.executable, "-c", code]

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
    assert "TIMEOUT" in result.stdout, (
        "TIMEOUT marker missing from the preserved buffer"
    )


def test_periodic_output_resets_idle_timeout():
    """A backend that keeps writing progress must run longer than one idle window."""
    argv = _slow_argv(lines=5, interval=0.4)  # total runtime ~2s; idle window is 1s

    with _with_env(REVIEW_IDLE_TIMEOUT_SECONDS="1"):
        result = review._run_streamed(
            argv,
            cwd=REPO_ROOT,
            timeout=1,
            backend="fake-progress",
            round_no=9,
        )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "line-4" in result.stdout
    assert "TIMEOUT" not in result.stdout


def test_wall_timeout_ignores_periodic_output():
    """Bounded surfaces can request an exact wall-clock timeout despite chatty output."""
    argv = _slow_argv(lines=20, interval=0.2)  # total runtime ~4s; wall cap is 1s

    result = review._run_streamed(
        argv,
        cwd=REPO_ROOT,
        timeout=1,
        backend="fake-wall",
        round_no=10,
        timeout_mode="wall",
    )

    assert result.returncode == 124, result.stdout + result.stderr
    assert "line-0" in result.stdout
    assert "line-19" not in result.stdout
    assert "total runtime" in result.stdout


def test_idle_timeout_seconds_contract():
    with _with_env(REVIEW_IDLE_TIMEOUT_SECONDS=None):
        assert process.idle_timeout_seconds(30) == 30
        assert process.idle_timeout_seconds(59) == 59
        assert process.idle_timeout_seconds(60) == process._DEFAULT_IDLE_TIMEOUT
        assert process.idle_timeout_seconds(240) == process._DEFAULT_IDLE_TIMEOUT
        assert process.idle_timeout_seconds(60, idle_floor=0) == 60
    with _with_env(REVIEW_IDLE_TIMEOUT_SECONDS="7"):
        assert process.idle_timeout_seconds(240) == 7
        assert process.idle_timeout_seconds(240, idle_floor=0) == 240
    with _with_env(REVIEW_IDLE_TIMEOUT_SECONDS="0"):
        assert process.idle_timeout_seconds(240) is None
        assert process.idle_timeout_seconds(240, idle_floor=0) == 240
    with _with_env(REVIEW_IDLE_TIMEOUT_SECONDS="-5"):
        assert process.idle_timeout_seconds(240) == process._DEFAULT_IDLE_TIMEOUT
    with _with_env(REVIEW_IDLE_TIMEOUT_SECONDS="not-a-number"):
        assert process.idle_timeout_seconds(3) == 3
        assert process.idle_timeout_seconds(90) == process._DEFAULT_IDLE_TIMEOUT
        assert process.idle_timeout_seconds(240) == process._DEFAULT_IDLE_TIMEOUT


def test_idle_timeout_seconds_no_deadline_is_unchanged():
    """Regression guard: with no board deadline armed (the default, and every caller
    that predates review-cli#221), idle_timeout_seconds behaves exactly as before —
    the deadline clamp must be fully opt-in."""
    assert process._active_board_deadline() is None
    with _with_env(REVIEW_IDLE_TIMEOUT_SECONDS=None):
        assert process.idle_timeout_seconds(240) == process._DEFAULT_IDLE_TIMEOUT
        assert process.idle_timeout_seconds(60, idle_floor=0) == 60


def test_idle_timeout_seconds_clamps_to_a_near_deadline():
    """review-cli#221: a reserve promoted late in a wall-clock-bounded board run must
    not be handed the full 20-minute default floor — it gets whatever time genuinely
    remains, so an external wrapper's timeout never SIGKILLs it mid-attempt."""
    try:
        # 150s remaining: comfortably above _MIN_DEADLINE_CLAMPED_IDLE_FLOOR (90) and
        # well below the 20-minute default floor, so this demonstrates the clamp
        # actually engaging rather than either edge case.
        process.set_board_deadline(time.monotonic() + 150)
        clamped = process.idle_timeout_seconds(240)
        assert 145 <= clamped <= 150, clamped
    finally:
        process.set_board_deadline(None)


def test_idle_timeout_seconds_clamp_never_starves_below_the_minimum_floor():
    """A deadline that has already passed (or is seconds away) still gives the
    promoted reserve one real attempt — never an instant, unhelpful ~0s timeout."""
    try:
        process.set_board_deadline(time.monotonic() - 5)
        assert (
            process.idle_timeout_seconds(240)
            == process._MIN_DEADLINE_CLAMPED_IDLE_FLOOR
        )
    finally:
        process.set_board_deadline(None)


def test_idle_timeout_seconds_deadline_never_extends_a_shorter_request():
    """The deadline is a ceiling, not a floor of its own — a caller's own tighter
    request (or the ambient env var) still wins when it is already the smaller value
    AND far enough from the deadline that the clamp doesn't even engage."""
    try:
        process.set_board_deadline(time.monotonic() + 3600)  # generous, far off
        assert process.idle_timeout_seconds(240) == process._DEFAULT_IDLE_TIMEOUT
    finally:
        process.set_board_deadline(None)


def test_idle_timeout_seconds_min_floor_never_extends_computed_above_its_own_request():
    """Regression for a real bug caught in review (3 independent reviewers, review-cli#221
    round 1): _MIN_DEADLINE_CLAMPED_IDLE_FLOOR (90s) exists to protect a promoted reserve
    from being starved by a near/past deadline — it must never OVERRIDE a caller's own
    smaller explicit request by handing back something LARGER than what was asked for.
    An operator's REVIEW_IDLE_TIMEOUT_SECONDS=30 near a deadline must stay <= 30, not get
    silently bumped to the 90s floor (which could itself outlive the external wrapper the
    deadline exists to respect)."""
    try:
        process.set_board_deadline(time.monotonic() + 10)  # near: remaining < computed
        with _with_env(REVIEW_IDLE_TIMEOUT_SECONDS="30"):
            clamped = process.idle_timeout_seconds(240)
            assert clamped <= 30, clamped
        # Same bug, reached via the "tiny timeout, exact-preserve" contract instead of
        # the env var — requested=45 is below _SHORT_TIMEOUT_EXACT_THRESHOLD (60).
        with _with_env(REVIEW_IDLE_TIMEOUT_SECONDS=None):
            clamped = process.idle_timeout_seconds(45)
            assert clamped <= 45, clamped
    finally:
        process.set_board_deadline(None)


def test_idle_timeout_seconds_deadline_skips_explicit_disable_contracts():
    """A caller/operator that explicitly opted OUT of idle reaping (idle_floor=0, or
    REVIEW_IDLE_TIMEOUT_SECONDS=0) keeps that contract even with a live deadline armed —
    the deadline clamp must never silently reintroduce a bound they turned off."""
    try:
        process.set_board_deadline(time.monotonic() + 5)
        assert process.idle_timeout_seconds(240, idle_floor=0) == 240
        with _with_env(REVIEW_IDLE_TIMEOUT_SECONDS="0"):
            assert process.idle_timeout_seconds(240) is None
    finally:
        process.set_board_deadline(None)


def test_silent_child_can_think_until_requested_timeout():
    """No output is not itself a hang signal; only the requested timeout can stop it."""
    code = "import time\ntime.sleep(1.2)\nprint('done', flush=True)\n"
    argv = [sys.executable, "-c", code]

    started = time.monotonic()
    result = review._run_streamed(
        argv,
        cwd=REPO_ROOT,
        timeout=5,
        backend="fakethink",
        round_no=8,
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 0, result.stdout + result.stderr
    assert elapsed >= 1.0, f"silent thinking was cut short after {elapsed:.2f}s"
    assert "done" in result.stdout
    assert "TIMEOUT" not in result.stdout


def test_liveness_timeout_kills_a_totally_silent_process_fast():
    """review-cli#153/#159/#179: a backend that NEVER produces a single byte (opencode's
    zai/glm seat hanging on quota exhaustion) must be reaped at `liveness_timeout`, well
    before the generous default idle window."""
    code = "import time\ntime.sleep(30)\n"
    argv = [sys.executable, "-c", code]

    started = time.monotonic()
    result = review._run_streamed(
        argv,
        cwd=REPO_ROOT,
        timeout=30,
        backend="fake-silent-hang",
        round_no=12,
        liveness_timeout=1,
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 124, result.stdout + result.stderr
    assert elapsed < 10, f"liveness timeout did not fire promptly (took {elapsed:.2f}s)"
    assert "waiting for first output" in result.stdout, result.stdout


def test_liveness_timeout_does_not_fire_once_output_arrives():
    """A backend that DOES emit something must not be killed by the liveness bound --
    only total silence from spawn is a liveness failure; silence AFTER output is still
    governed by the normal (generous) idle timeout."""
    code = "import time\nprint('alive', flush=True)\ntime.sleep(2)\nprint('done', flush=True)\n"
    argv = [sys.executable, "-c", code]

    result = review._run_streamed(
        argv,
        cwd=REPO_ROOT,
        timeout=30,
        backend="fake-emits-then-quiet",
        round_no=13,
        liveness_timeout=1,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "done" in result.stdout
    assert "TIMEOUT" not in result.stdout
    assert result.timeout_kind is None


def test_liveness_timeout_still_fires_when_idle_reap_is_disabled():
    """codex review finding: REVIEW_IDLE_TIMEOUT_SECONDS=0 (disabling generic idle
    reaping) must NOT also disable a caller's explicit `liveness_timeout` -- a
    zero-output backend must still be reaped at the liveness bound, not left to wait
    out the full wall-clock `timeout`."""
    code = "import time\ntime.sleep(30)\n"
    argv = [sys.executable, "-c", code]

    started = time.monotonic()
    with _with_env(REVIEW_IDLE_TIMEOUT_SECONDS="0"):
        result = review._run_streamed(
            argv,
            cwd=REPO_ROOT,
            timeout=30,
            backend="fake-silent-idle-disabled",
            round_no=14,
            liveness_timeout=1,
        )
    elapsed = time.monotonic() - started

    assert result.returncode == 124, result.stdout + result.stderr
    assert elapsed < 10, f"liveness timeout did not fire promptly (took {elapsed:.2f}s)"
    assert "waiting for first output" in result.stdout, result.stdout
    assert result.timeout_kind == "waiting for first output"


def test_disabled_idle_reap_with_liveness_set_still_bounds_intermittent_output():
    """Fable review finding: the disabled-idle-reap FALLBACK (when a caller also sets
    `liveness_timeout`) must measure true wall-clock time, not silence-since-last-byte
    -- a backend that keeps emitting output (so it never trips the liveness check)
    must still be bounded by the requested `timeout`, exactly like the plain
    disabled-idle case without `liveness_timeout` already is
    (`test_disabled_idle_reap_falls_back_to_wall_timeout`). Measuring from
    `activity["last"]` here would let a chatty backend run UNBOUNDED."""
    argv = _slow_argv(lines=100, interval=0.3)  # keeps emitting for ~30s if unbounded

    started = time.monotonic()
    with _with_env(REVIEW_IDLE_TIMEOUT_SECONDS="0"):
        result = review._run_streamed(
            argv,
            cwd=REPO_ROOT,
            timeout=2,
            backend="fake-chatty-idle-disabled",
            round_no=16,
            liveness_timeout=1,
        )
    elapsed = time.monotonic() - started

    assert result.returncode == 124, result.stdout + result.stderr
    assert elapsed < 10, (
        f"wall-clock fallback did not bound a chatty backend (took {elapsed:.2f}s)"
    )
    assert "total runtime" in result.stdout, result.stdout
    assert "waiting for first output" not in result.stdout, result.stdout
    assert result.timeout_kind == "total runtime"


def test_liveness_timeout_is_clamped_to_a_smaller_idle_window():
    """codex review finding: if the effective idle timeout is SMALLER than the
    requested `liveness_timeout`, a totally-silent call must still be classified as a
    stall (not an ordinary idle timeout) -- the idle bound firing first must not mask
    the fact that the call never produced a single byte."""
    code = "import time\ntime.sleep(30)\n"
    argv = [sys.executable, "-c", code]

    with _with_env(REVIEW_IDLE_TIMEOUT_SECONDS="1"):
        result = review._run_streamed(
            argv,
            cwd=REPO_ROOT,
            timeout=30,
            backend="fake-silent-small-idle",
            round_no=15,
            liveness_timeout=300,  # deliberately LARGER than the 1s idle window
        )

    assert result.returncode == 124, result.stdout + result.stderr
    assert "waiting for first output" in result.stdout, result.stdout
    assert "without output" not in result.stdout, result.stdout
    assert result.timeout_kind == "waiting for first output"


def test_disabled_idle_reap_falls_back_to_wall_timeout():
    """REVIEW_IDLE_TIMEOUT_SECONDS=0 must not turn _run_streamed into an unbounded wait."""
    code = "import time\nprint('started', flush=True)\ntime.sleep(60)\n"
    argv = [sys.executable, "-c", code]

    with _with_env(REVIEW_IDLE_TIMEOUT_SECONDS="0"):
        result = review._run_streamed(
            argv,
            cwd=REPO_ROOT,
            timeout=1,
            backend="fake-disabled-idle",
            round_no=11,
        )

    assert result.returncode == 124, result.stdout + result.stderr
    assert "started" in result.stdout
    assert "total runtime" in result.stdout
    assert "without output" not in result.stdout


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
        (
            "import os, sys, time\n"
            "os.environ.setdefault('REVIEW_LOG_DIR', %r)\n"
            "sys.path.insert(0, %r)\n"  # repo root, so the in-repo reviewlib package imports
            "import reviewlib as rv\n"
            "code = (\n"
            '  "import sys, subprocess, time\\n"\n'
            "  \"print('p', flush=True)\\n\"\n"
            "  \"subprocess.Popen([sys.executable,'-c','import sys,time\\\\n\"\n"
            '  "time.sleep(3)\\\\nwhile True:\\\\n "\n'
            '  "sys.stdout.write(chr(120))\\\\n sys.stdout.flush()\\\\n time.sleep(0.2)\'], "\n'
            '  "start_new_session=True)\\n"\n'
            '  "time.sleep(60)\\n"\n'
            ")\n"
            "r = rv._run_streamed([sys.executable,'-c',code], cwd=rv.Path('.'), timeout=2, "
            "backend='postclose', round_no=1)\n"
            "assert r.returncode == 124 and 'p' in r.stdout\n"
            "time.sleep(2)\n"  # let any post-close daemon write surface as a traceback
            "print('DRIVER_OK')\n"
        )
        % (tempfile.mkdtemp(), str(REPO_ROOT))
    )

    proc = _sp.run(
        [sys.executable, "-c", driver],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=40,
    )
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
    review._run_streamed(
        argv, cwd=REPO_ROOT, timeout=10, backend="faketest", round_no=4
    )
    log_dir = review.log_dir()
    logs = sorted(log_dir.glob("*-faketest-*.log"), key=lambda p: p.stat().st_mtime)
    assert logs, "no log file produced"
    mode = stat.S_IMODE(logs[-1].stat().st_mode)
    assert mode & 0o077 == 0, f"log file is group/other-readable (mode {oct(mode)})"


_CLAUDE_READONLY_TOOLS = (
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
)


def _run_claude_cli_capture(which_fn):
    """Drive review_claude_cli with the backend boundary stubbed and the binary resolver
    (`backends._which_optional`) forced by `which_fn`, returning the captured argv/kwargs.
    Restores all globals."""
    captured: dict[str, object] = {}
    old_which = review_backends._which_optional
    old_run_streamed = review_backends._run_streamed
    old_trust = review_backends._ensure_workspace_trusted

    def fake_run_streamed(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return review.subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    try:
        review_backends._which_optional = which_fn
        review_backends._run_streamed = fake_run_streamed
        # Stub auto-trust: it writes ~/.claude.json; this unit test must not touch the
        # real developer/CI config (cwd here is REPO_ROOT).
        review_backends._ensure_workspace_trusted = lambda _cwd: None
        # Target the CLI variant directly: review_claude() is a dispatcher that routes
        # to the API backend when a key is configured, so calling it would be env-dependent.
        result = review_backends.review_claude_cli(
            "claude:opus", "prompt", "diff", REPO_ROOT, 10
        )
    finally:
        review_backends._which_optional = old_which
        review_backends._run_streamed = old_run_streamed
        review_backends._ensure_workspace_trusted = old_trust
    captured["result"] = result
    return captured


def test_claude_backend_disables_tools_and_mcp_to_avoid_headless_approval():
    # Default path: the real `claude` binary in genuine print mode (no PTY/TUI scrape).
    cap = _run_claude_cli_capture(
        lambda name: "/bin/claude" if name == "claude" else None
    )
    assert cap["result"].returncode == 0
    argv = cap["argv"]
    # Genuine headless print mode — no fullscreen TUI to bleed into the captured pipe.
    assert "--print" in argv
    assert argv[argv.index("--output-format") + 1] == "text"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    # Read-only via an EMPTY tool allowlist (all built-in tools off) — stronger and more
    # future-proof than a denylist, and it avoids real `claude` warning on claude-p tool names.
    assert argv[argv.index("--tools") + 1] == ""
    assert (
        "--disallowedTools" not in argv
    )  # the empty allowlist already forbids everything
    assert "--strict-mcp-config" in argv
    assert "--disable-slash-commands" in argv
    assert "--safe-mode" in argv
    assert "--append-system-prompt" in argv
    assert "Do not use tools" in argv[argv.index("--append-system-prompt") + 1]
    assert argv[argv.index("--model") + 1] == "opus"
    # The decoration-hostile env is wired so no renderer emits colour/cursor noise.
    assert cap["kwargs"]["env"]["TERM"] == "dumb"
    # The payload is fed over STDIN, not a `-p <payload>` argv arg: a brainstorm round's
    # growing transcript (or a big diff) as an argument blows past ARG_MAX → execve E2BIG.
    assert cap["kwargs"]["input_text"] == "prompt\n\n```diff\ndiff\n```"


def test_claude_backend_falls_back_to_claude_p_when_claude_absent():
    # A host that ships only the legacy `claude-p` TUI-scraper still gets a working seat,
    # with its wrapper-specific surface (--cwd / --tools '' / --timeout-sec / -p) intact.
    cap = _run_claude_cli_capture(
        lambda name: "/bin/claude-p" if name == "claude-p" else None
    )
    assert cap["result"].returncode == 0
    argv = cap["argv"]
    assert "--print" not in argv  # claude-p has no --print
    assert argv[argv.index("--tools") + 1] == ""
    assert "--timeout-sec" in argv
    assert argv[-1] == "-p"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    for tool in _CLAUDE_READONLY_TOOLS:
        assert tool in argv


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
        proc = review._run_streamed(
            ["cat"], cwd=Path(tmp), input_text=payload, timeout=30
        )
    assert proc.returncode == 0
    assert proc.stdout == payload


def test_run_streamed_forwards_env_to_the_child_process():
    """The REAL _run_streamed (not a stub) must pass `env` through to the child — else the
    claude seat's decoration-hostile env (TERM=dumb/NO_COLOR=1/CI=1) is silently a no-op.
    This pins the contract review_claude_cli relies on (review-cli#76), on the genuine
    function, by reading an env var BACK out of the child's stdout."""
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["REVIEW_LOG_DIR"] = tmp
        child_env = {**os.environ, "TERM": "dumb", "REVIEW_ENV_PROBE": "probe-value-42"}
        proc = review._run_streamed(
            ["sh", "-c", 'printf \'%s|%s\' "$TERM" "$REVIEW_ENV_PROBE"'],
            cwd=Path(tmp),
            env=child_env,
            timeout=30,
        )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "dumb|probe-value-42", proc.stdout


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
            assert (
                review.log_dir()
                == Path(home) / ".local" / "state" / "review-cli" / "logs"
            )
            os.environ["XDG_STATE_HOME"] = str(Path(home) / "xdg")
            assert review.log_dir() == Path(home) / "xdg" / "review-cli" / "logs"
            os.environ["XDG_STATE_HOME"] = "relative-ignored"  # XDG: relative → ignored
            assert (
                review.log_dir()
                == Path(home) / ".local" / "state" / "review-cli" / "logs"
            )
    finally:
        sys.platform = saved_platform
        for key, val in saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


# ── true_silence_timeout (review-cli graduated-timeout / Alex 2026-08-19/20 request) ──


def test_true_silence_kills_a_never_talking_child_before_the_idle_floor():
    """A child that NEVER writes anything must be reaped by true_silence_timeout, well
    before the (much longer) ordinary idle timeout would have caught it."""
    code = "import time\ntime.sleep(60)\n"  # never prints a single byte
    argv = [sys.executable, "-c", code]

    started = time.monotonic()
    result = review._run_streamed(
        argv,
        cwd=REPO_ROOT,
        timeout=30,
        backend="fake-truesilence",
        round_no=1,
        true_silence_timeout=1,
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 125, (
        f"a never-talking child must be reaped with rc 125, got {result.returncode}"
    )
    assert result.true_silenced is True, (
        "the authoritative .true_silenced attribute must be set on a real true-silence "
        "kill, not just the (collision-prone) returncode"
    )
    assert elapsed < 20, (
        f"true-silence kill took {elapsed:.2f}s, far longer than the 1s budget"
    )
    assert "TRUE-SILENCE" in result.stdout
    assert "zero output" in result.stdout


def test_a_genuine_child_exit_125_is_not_mistaken_for_true_silence():
    """round-2 review finding (codex + Fable): 125 is a real exit code some CLIs/
    wrappers use on their own (`timeout(1)`'s "the wrapper itself failed", docker run,
    git-bisect skip). A child that exits 125 BY ITSELF — with real output, well
    before any timeout — must get `.true_silenced is False`; only the poll loop's own
    true-silence kill may ever set it True."""
    code = "print('real output', flush=True)\nraise SystemExit(125)\n"
    argv = [sys.executable, "-c", code]

    result = review._run_streamed(
        argv,
        cwd=REPO_ROOT,
        timeout=10,
        backend="fake-genuine-125",
        round_no=1,
        true_silence_timeout=1,  # armed, but must never fire here — the child exits on its own
    )

    assert result.returncode == 125, result.stdout + result.stderr
    assert result.true_silenced is False, (
        "a genuine child exit(125) was misdiagnosed as a true-silence reap"
    )
    assert "real output" in result.stdout
    assert "TRUE-SILENCE" not in result.stdout


def test_true_silence_still_fires_when_larger_than_the_idle_timeout():
    """Fable review finding (round 4): a true_silence_timeout configured >= the
    ordinary idle_timeout (e.g. a deliberately generous per-model registry entry for
    a known slow starter, or REVIEW_IDLE_TIMEOUT_SECONDS shrunk below it) must still
    fire eventually — an earlier version let the ordinary (smaller) idle check win
    the race and reap with rc 124 first, silently making the generous true-silence
    budget unreachable."""
    code = "import time\ntime.sleep(60)\n"  # never prints a single byte
    argv = [sys.executable, "-c", code]

    result = review._run_streamed(
        argv,
        cwd=REPO_ROOT,
        # timeout=1, below process._SHORT_TIMEOUT_EXACT_THRESHOLD (60), keeps
        # idle_timeout_seconds' "exact" contract — idle_timeout ends up EXACTLY 1s.
        timeout=1,
        backend="fake-truesilence-outlasts-idle",
        round_no=1,
        true_silence_timeout=3,  # LARGER than the ~1s idle timeout above
    )

    assert result.returncode == 125, (
        f"expected a true-silence reap (rc 125), got {result.returncode} — the "
        "smaller idle_timeout won the race instead"
    )
    assert result.true_silenced is True
    assert "TRUE-SILENCE" in result.stdout


def test_true_silence_does_not_fire_once_any_output_arrives():
    """A child that produces even ONE byte before the true-silence deadline must be
    governed by the ordinary idle timeout from then on, not killed as if silent."""
    code = (
        "import time\n"
        "print('hello', flush=True)\n"
        "time.sleep(1.5)\n"
        "print('done', flush=True)\n"
    )
    argv = [sys.executable, "-c", code]

    result = review._run_streamed(
        argv,
        cwd=REPO_ROOT,
        timeout=10,
        backend="fake-truesilence-then-talks",
        round_no=2,
        true_silence_timeout=1,  # would fire at 1s if output were ignored
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.true_silenced is False
    assert "hello" in result.stdout
    assert "done" in result.stdout
    assert "TRUE-SILENCE" not in result.stdout


def test_true_silence_does_not_fire_for_stderr_only_progress():
    """(Opus review finding, review-cli#243 round 6) idle mode's own docstring says it
    "treats stdout/stderr as progress" -- a child that only ever writes to STDERR (a
    common shape: progress lines, spinners) must disarm true-silence the same way a
    stdout-only child does above, not get reaped as if it produced nothing at all.
    Both stream readers share the SAME `_drain` function (confirmed by direct code
    inspection: `stdout_thread`/`stderr_thread` both target `_drain`, which sets
    `activity["got_output"] = True` on any chunk from EITHER stream) -- this pins that
    behavior with a real subprocess rather than relying on inspection alone."""
    code = (
        "import sys, time\n"
        "print('progress', file=sys.stderr, flush=True)\n"
        "time.sleep(1.5)\n"
        "print('more progress', file=sys.stderr, flush=True)\n"
    )
    argv = [sys.executable, "-c", code]

    result = review._run_streamed(
        argv,
        cwd=REPO_ROOT,
        timeout=10,
        backend="fake-truesilence-stderr-only",
        round_no=2,
        true_silence_timeout=1,  # would fire at 1s if stderr didn't count as output
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.true_silenced is False, (
        "stderr-only progress was wrongly reaped as true-silence"
    )
    assert "progress" in result.stderr
    assert "TRUE-SILENCE" not in result.stdout


def test_true_silence_does_not_fire_on_a_stalled_partial_multibyte_sequence():
    """codex review finding (review-cli#243 round 15, P1): activity must be marked on
    RAW byte receipt, not on decoded TEXT. `codecs.getincrementaldecoder('utf-8')`
    buffers an INCOMPLETE multibyte sequence internally and returns "" until it
    completes -- a genuinely-alive child whose first write happens to be only the
    leading byte(s) of a multibyte character (a CJK/Cyrillic character split across a
    read boundary, say) would otherwise look like zero bytes were ever received, even
    though real output DID arrive. Writes just the first byte of a 3-byte UTF-8
    character (U+65E5 "日" = b'\\xe6\\x97\\xa5'), flushes, stalls past
    true_silence_timeout, THEN completes the character and exits -- must NOT be
    reaped as true-silent."""
    code = (
        "import sys, time\n"
        "sys.stdout.buffer.write(b'\\xe6')\n"  # first byte only -- an incomplete UTF-8 sequence
        "sys.stdout.buffer.flush()\n"
        "time.sleep(1.5)\n"
        "sys.stdout.buffer.write(b'\\x97\\xa5 done\\n')\n"  # completes the character
        "sys.stdout.buffer.flush()\n"
    )
    argv = [sys.executable, "-c", code]

    result = review._run_streamed(
        argv,
        cwd=REPO_ROOT,
        timeout=10,
        backend="fake-truesilence-partial-utf8",
        round_no=2,
        true_silence_timeout=1,  # would fire at 1s if the partial byte didn't count
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.true_silenced is False, (
        "a stalled partial multibyte UTF-8 sequence was wrongly reaped as true-silence"
    )
    assert "done" in result.stdout
    assert "TRUE-SILENCE" not in result.stdout


def test_true_silence_timeout_none_disables_the_check():
    """The default (true_silence_timeout=None) must not change ANY existing caller's
    behavior — a silent-but-eventually-talking child still gets its full idle budget."""
    code = "import time\ntime.sleep(1.2)\nprint('done', flush=True)\n"
    argv = [sys.executable, "-c", code]

    result = review._run_streamed(
        argv,
        cwd=REPO_ROOT,
        timeout=5,
        backend="fake-truesilence-disabled",
        round_no=3,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.true_silenced is False
    assert "done" in result.stdout
    assert "TRUE-SILENCE" not in result.stdout


def test_true_silence_wall_mode_is_unaffected():
    """true_silence_timeout is an idle-mode-only concept; wall mode must ignore it.
    Uses a true_silence_timeout SMALLER than the wall timeout (Fable review finding,
    round 1: a prior version of this test used 999 against a 1s wall cap, which
    passes trivially regardless of whether wall mode actually gates the check — this
    tighter budget would expose a real gating bug if one existed, since the child
    would be reaped at 1s by the true-silence path if it were reachable, but must
    instead run the full 3s wall budget it actually requested)."""
    code = "import time\ntime.sleep(60)\n"
    argv = [sys.executable, "-c", code]

    started = time.monotonic()
    result = review._run_streamed(
        argv,
        cwd=REPO_ROOT,
        timeout=3,
        backend="fake-truesilence-wall",
        round_no=4,
        timeout_mode="wall",
        true_silence_timeout=1,  # SMALLER than the 3s wall timeout — must still be ignored
    )
    elapsed = time.monotonic() - started

    assert elapsed >= 2.5, (
        f"wall mode was reaped after {elapsed:.2f}s — the true-silence check leaked "
        "into wall mode instead of being ignored"
    )

    assert result.returncode == 124, result.stdout + result.stderr
    assert result.true_silenced is False
    assert "total runtime" in result.stdout
    assert "TRUE-SILENCE" not in result.stdout


def test_true_silence_timeout_is_clamped_to_a_near_board_deadline():
    """codex review finding (review-cli#243 round 5, P1): true_silence_timeout must
    respect an active board deadline the same way idle_timeout already does via
    idle_timeout_seconds' own _clamp_to_board_deadline call (review-cli#221/#228).
    Without this, a nearly-expired REVIEW_BOARD_DEADLINE_SECONDS clamps idle_timeout
    down close to process._MIN_DEADLINE_CLAMPED_IDLE_FLOOR, but a silent seat
    pre-first-byte would still wait the full, un-clamped true_silence_timeout
    regardless — overrunning the very deadline #228 exists to enforce.

    _clamp_to_board_deadline's own correctness (including its
    _MIN_DEADLINE_CLAMPED_IDLE_FLOOR=90s floor, which makes any REAL end-to-end reap
    of a clamped value take 90+ real seconds) is already covered by
    test_idle_timeout_seconds_clamps_to_a_near_deadline and its siblings above — this
    test proves the WIRING (that _run_streamed actually calls it on true_silence_timeout,
    not just on idle_timeout), by monkeypatching the shared clamp function to a fast,
    deterministic stand-in rather than waiting out the real floor."""
    code = "import time\ntime.sleep(60)\n"  # never prints a single byte
    argv = [sys.executable, "-c", code]

    calls: list[int] = []
    orig_clamp = process._clamp_to_board_deadline

    def _fake_clamp(computed: int) -> int:
        calls.append(computed)
        return 1 if computed == 60 else orig_clamp(computed)

    process._clamp_to_board_deadline = _fake_clamp
    try:
        result = review._run_streamed(
            argv,
            cwd=REPO_ROOT,
            timeout=120,
            backend="fake-truesilence-deadline-clamp",
            round_no=1,
            true_silence_timeout=60,
        )
    finally:
        process._clamp_to_board_deadline = orig_clamp

    assert 60 in calls, (
        "true_silence_timeout was never passed through _clamp_to_board_deadline — "
        "the board-deadline clamp is not wired into the true-silence path"
    )
    assert result.returncode == 125, result.stdout + result.stderr
    assert result.true_silenced is True
    assert "TRUE-SILENCE TIMEOUT after 1s" in result.stdout, (
        "the reap used the raw true_silence_timeout (60s) instead of the clamped "
        "stand-in value (1s) returned by _clamp_to_board_deadline"
    )


def test_true_silence_clamp_reflects_state_after_the_concurrency_wait_not_before():
    """The sibling test above calls `_run_streamed` with an immediately-available
    concurrency slot, so it passes identically whether the clamp is computed at
    function entry (stale for a queued seat) or after the concurrency-slot wait --
    it can't distinguish the two. This test forces real contention: it holds the
    sole slot (`REVIEW_MAX_CONCURRENCY=1`) itself, starts `_run_streamed` in a
    background thread (which must block waiting for the slot), and synchronizes on
    real proof that the worker has reached ITS OWN acquire call -- not a fixed
    `sleep()` -- before flipping the (monkeypatched) board-deadline clamp and
    releasing. A regressed (entry-time) implementation reads the clamp as the very
    first thing in `_run_streamed`, strictly earlier in the worker's program order
    than the acquire call this test waits on, so by the time that signal fires a
    regression has already committed to the PRE-flip value with certainty. Only the
    fixed (post-acquire) implementation can observe the POST-flip value, since it
    cannot compute the clamp until the real semaphore unblocks it -- no earlier
    than our release()."""
    code = "import time\ntime.sleep(60)\n"  # never prints a single byte
    argv = [sys.executable, "-c", code]

    clamp_return = {"value": 999}  # the "pre-flip" (stale) value

    def _fake_clamp(computed: int) -> int:
        return clamp_return["value"]

    # (GLM review finding, round 2 correction) This fake also intercepts the worker's
    # entry-time `idle_timeout_seconds()` call, not just the post-acquire true-silence
    # one -- so `idle_timeout` is ALSO 999 pre-flip. Harmless, not load-bearing: the
    # child never emits a byte, so the round-4 "sole authority" branch is the only one
    # that can ever fire here regardless of idle_timeout's value -- but noted so nobody
    # is surprised the idle budget reads 999 mid-test.

    result_box: dict[str, object] = {}
    t: threading.Thread | None = None

    with _with_env(REVIEW_MAX_CONCURRENCY="1"):
        # Unlike test_concurrency_cap.py's own `_with_env`, this file's version (above)
        # only saves/restores env vars -- it does NOT rebuild the cached concurrency
        # semaphore, so a stale cap from an earlier test would otherwise silently leave
        # extra free slots and this test would never actually contend.
        process._reset_concurrency_sem_for_tests()
        # Patched/restored entirely from the MAIN thread around the whole lifecycle
        # (GLM review finding): the previous version patched inside the worker thread
        # and only restored in the worker's own `finally`, which never ran if the
        # worker hung past `t.join(timeout=...)` -- leaking a flipped global clamp
        # into every later test in this process.
        orig_clamp = process._clamp_to_board_deadline
        process._clamp_to_board_deadline = _fake_clamp
        sem = None
        orig_acquire = None
        held_by_test = False
        acquire_wrapped = False
        try:
            sem = process._get_concurrency_sem()
            assert sem is not None, "cap=1 must build a real semaphore"
            orig_acquire = sem.acquire
            held_by_test = sem.acquire(blocking=False)
            assert held_by_test, (
                "test must hold the sole slot itself before starting the call"
            )
            # Prove cap==1 behaviorally (GLM finding: avoid the CPython-private
            # `_value` attribute) -- a second non-blocking acquire must fail while the
            # first slot is still held.
            contended = sem.acquire(blocking=False)
            assert not contended, (
                "expected a cap=1 semaphore -- a second non-blocking acquire "
                "succeeded while the first slot was still held"
            )

            # Fires the instant the worker thread reaches ITS OWN acquire call --
            # production tries a non-blocking probe first (which will fail, since we
            # hold the only slot) and then falls back to a real blocking acquire();
            # both go through this wrapper, so the event fires no later than the
            # worker reaching the concurrency-slot section, which is well after any
            # entry-time computation a regression would already have done.
            reached_acquire = threading.Event()

            def _acquire_and_signal(*a, **kw):
                reached_acquire.set()
                return orig_acquire(*a, **kw)

            sem.acquire = _acquire_and_signal
            acquire_wrapped = True

            def _run():
                try:
                    result_box["result"] = review._run_streamed(
                        argv,
                        cwd=REPO_ROOT,
                        timeout=120,
                        backend="fake-truesilence-queue-clamp",
                        round_no=1,
                        true_silence_timeout=60,
                    )
                except Exception as exc:  # noqa: BLE001 -- surfaced below, not swallowed
                    result_box["error"] = exc

            t = threading.Thread(target=_run, daemon=True)
            t.start()
            reached = reached_acquire.wait(timeout=10)
            # (GLM + k3 review findings, round 4) Check for a worker exception BEFORE
            # the synchronization asserts below: a worker that raises before ever
            # reaching its own acquire call (e.g. `_open_log` hits an OSError) leaves
            # `reached_acquire` unset, and the asserts below would previously fire
            # first with a misleading "never reached acquire"/"never contended"
            # message -- burying the real exception, unread, in `result_box`.
            if "error" in result_box:
                raise result_box["error"]  # noqa: TRY201 -- the worker's real exception
            assert reached, (
                "worker never reached the concurrency-slot acquire call -- "
                "this test isn't proving anything"
            )
            assert t.is_alive(), (
                "the call returned before the slot was released -- "
                "it never actually contended for the concurrency cap"
            )

            # Flip the clamp's return value only now that the worker is proven to
            # have reached its own acquire call, then release the slot it's blocked
            # on (or about to block on).
            clamp_return["value"] = 1
            sem.release()
            held_by_test = False
            # Generous margin over the ~1s true-silence reap: kill_tree/registration/
            # thread teardown under real concurrent machine load (this repo's own
            # CI/local runs share the machine with other heavy processes) can add
            # real seconds.
            t.join(timeout=30)
        finally:
            # codex review finding: an early assertion failure (e.g. `reached_acquire`
            # never fires) used to skip `sem.release()` entirely, leaving the worker's
            # thread permanently blocked on a slot only the test itself was holding.
            # Release it here so a failing test doesn't also orphan a hung thread.
            if held_by_test and sem is not None:
                sem.release()
            if acquire_wrapped and sem is not None:
                del sem.acquire  # drop the instance wrapper, restore the class method
            process._clamp_to_board_deadline = orig_clamp
            process._reset_concurrency_sem_for_tests()

    # (GLM review finding, round 3) `t is not None` would be dead code here -- every
    # path that reaches this line has already passed through `t = threading.Thread(...)`
    # above; an earlier failure raises inside the `try` and never reaches this assert.
    # The `| None` in `t`'s annotation above exists only for the type checker.
    assert not t.is_alive(), (
        "the call never completed after the slot was released -- if the clamp were "
        "computed before the concurrency wait it would have read the pre-flip 999s "
        "value and nothing would reap within the 30s join"
    )
    if "error" in result_box:
        raise result_box["error"]  # noqa: TRY201 -- re-raised, not swallowed as KeyError
    result = result_box["result"]
    assert result.returncode == 125, result.stdout + result.stderr
    assert result.true_silenced is True
    assert "TRUE-SILENCE TIMEOUT after 1s" in result.stdout, (
        "the reap used the PRE-flip clamp value computed before the concurrency wait, "
        "instead of the POST-flip value the fix requires the clamp to be recomputed "
        f"against after acquiring the slot; stdout={result.stdout!r}"
    )


def test_true_silence_is_also_disabled_when_idle_reap_is_disabled():
    """(Opus review finding, review-cli#243 round 4) The true-silence branch lives in
    the `else` of `if timeout_mode == "wall" or idle_timeout is None:` -- so disabling
    the ordinary idle reap (REVIEW_IDLE_TIMEOUT_SECONDS=0) ALSO disables true-silence
    detection, even with true_silence_timeout explicitly set: a never-talking child
    then runs to the full wall-clock `timeout`, same as any other idle-reap-disabled
    call. Documented in _run_streamed's docstring; this pins the behavior so a future
    refactor of the wait paths can't silently re-arm true-silence under
    idle_timeout=None with no test catching it."""
    code = "import time\ntime.sleep(60)\n"  # never prints a single byte
    argv = [sys.executable, "-c", code]

    with _with_env(REVIEW_IDLE_TIMEOUT_SECONDS="0"):
        result = review._run_streamed(
            argv,
            cwd=REPO_ROOT,
            timeout=2,
            backend="fake-disabled-idle-truesilence",
            round_no=12,
            true_silence_timeout=1,  # SMALLER than the 2s wall timeout — must be ignored
        )

    assert result.returncode == 124, result.stdout + result.stderr
    assert result.true_silenced is False, (
        "true-silence fired despite idle reap being disabled"
    )
    assert "TRUE-SILENCE" not in result.stdout


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
