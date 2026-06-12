"""Subprocess plumbing: blocking + live-streaming backend runners.

Extracted verbatim from the original single-file `bin/review` (Stage 0
decomposition — zero behaviour change). `_run_streamed` is the workhorse for
long backend calls: it streams stdout/stderr to a per-call log file in real time
and preserves partial output on timeout. See the module docstrings below.
"""
from __future__ import annotations

import codecs
import os
import signal
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path


def _run(
    argv: list[str],
    *,
    cwd: Path,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 1200,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=str(cwd),
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=timeout,
    )


def log_dir() -> Path:
    """Predictable dir for per-call live logs that an external `tail -f` can watch.

    Honors $REVIEW_LOG_DIR (used by tests), else ~/.cache/review-cli/logs. Created
    private (0700) because logs can contain reviewed prompts/diffs (possibly secrets).
    """
    override = os.environ.get("REVIEW_LOG_DIR")
    base = Path(override) if override else (Path.home() / ".cache" / "review-cli" / "logs")
    base.mkdir(parents=True, exist_ok=True)
    try:
        base.chmod(0o700)
    except OSError:
        pass
    return base


def _open_log(backend: str, round_no: int) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    safe_backend = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in backend) or "backend"
    return log_dir() / f"{stamp}-{safe_backend}-r{round_no}.log"


def _kill_tree(proc: subprocess.Popen, pgid: int | None) -> None:
    """Best-effort terminate→kill of the child's whole process group.

    Backends (codex/claude/opencode wrappers) spawn grandchildren; killing only the
    direct child can leave a grandchild holding the stdout pipe open, so the read
    loop never sees EOF and the call hangs past its deadline. We start each child in
    its own session (start_new_session=True) and signal the captured GROUP — which
    we must do even after the direct child has exited, because a grandchild in the
    same group can still be alive and holding the pipe. `pgid` is captured at Popen
    time precisely so it stays valid after the parent is reaped.
    """
    def _signal_group(sig: int) -> None:
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            elif sig == signal.SIGKILL:
                proc.kill()
            else:
                proc.terminate()
        except (ProcessLookupError, PermissionError, OSError):
            pass

    _signal_group(signal.SIGTERM)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    # SIGKILL the group unconditionally: even if the parent is reaped, a grandchild
    # may still hold the pipe open. Killing an already-dead group is a harmless no-op.
    _signal_group(signal.SIGKILL)


def _run_streamed(
    argv: list[str],
    *,
    cwd: Path,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 1200,
    backend: str = "backend",
    round_no: int = 0,
    announce: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a long backend call, streaming its output in real time.

    Unlike `_run` (subprocess.run, which blocks until exit and DROPS the buffer on
    TimeoutExpired), this:
      * drains stdout AND stderr on daemon threads, TEEing each line to an in-memory
        accumulator AND a per-call log file under log_dir(), flushed per line so
        `tail -f <path>` shows live progress as output arrives (no wait-for-exit);
      * enforces `timeout` on the PROCESS via proc.wait(timeout) — NOT on pipe EOF —
        so a leaked stdout fd held by an escaped/daemonized descendant cannot make
        the call hang past its deadline; on timeout it SIGTERM→SIGKILLs the child's
        whole process group, gives the readers a brief grace flush, then RETURNS the
        partial buffer plus a clear TIMEOUT marker and returncode 124 — it never
        raises the buffer away.

    Returns a CompletedProcess-compatible object (.returncode/.stdout/.stderr) so
    callers like review_codex need no structural change.
    """
    path = _open_log(backend, round_no)
    if announce:
        print(f"[review-cli] {backend} live log: {path} (tail -f to follow)", file=sys.stderr, flush=True)

    out_buf: list[str] = []
    err_buf: list[str] = []
    log_lock = threading.Lock()
    stopping = threading.Event()
    timed_out = False
    proc: subprocess.Popen | None = None
    pgid: int | None = None

    # Private file perms: logs persist prompts/diffs that may contain secrets.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    log_fh = os.fdopen(fd, "w", encoding="utf-8", buffering=1)  # line-buffered
    try:
        # Header records the backend + argv[0] only — NOT the full argv, which carries
        # the prompt/diff for claude/opencode and could leak secrets into the log.
        log_fh.write(f"[review-cli] {backend}: {argv[0] if argv else '?'} (args redacted)\n")
        log_fh.flush()

        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            # BINARY pipes: we read fixed CHUNKS off the raw fd (os.read), not lines, so
            # flushed-but-newline-less output is captured immediately and is never lost
            # if a descendant keeps the pipe open past the deadline.
            start_new_session=True,  # own process group, so we can kill the whole tree
        )
        running = proc  # non-None alias for the nested closures
        # Capture the group id NOW, while the child is alive. We need it later even
        # after the direct child is reaped, because a grandchild in the same group
        # may still be holding the stdout pipe open.
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, OSError):
            pgid = None

        def _feed_stdin() -> None:
            if input_text is None or running.stdin is None:
                return
            try:
                running.stdin.write(input_text.encode("utf-8"))
                running.stdin.close()
            except (BrokenPipeError, ValueError, OSError):
                pass

        # `stopping` (defined above) lets the finalizer tell still-alive drain threads
        # (an escaped descendant can keep a pipe open past the bounded joins) to stop
        # touching the shared buffers and the log handle, so nothing writes after the
        # result is built or after log_fh is closed.
        def _log_write(text: str) -> None:
            # Guarded so a drain thread that wakes after close()/stop is a silent no-op
            # rather than a daemon-thread traceback.
            if stopping.is_set():
                return
            try:
                log_fh.write(text)
                log_fh.flush()
            except (ValueError, OSError):  # closed handle / write error
                pass

        # Both pipes are drained on DAEMON threads so the main flow never blocks on a
        # pipe that some escaped/daemonized descendant keeps open past the deadline.
        # We os.read() chunks (returns as soon as ANY bytes are available, b'' on EOF),
        # decode incrementally (multibyte-safe), and TEE to buffer + live log. Buffers
        # are plain lists (atomic append in CPython); snapshotted under log_lock. The
        # log tags stderr per line via a small remainder splitter.
        def _drain(stream, buf: list[str], tag: str) -> None:
            if stream is None:
                return
            fd = stream.fileno()
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            line_rem = ""  # only for tagging stderr lines in the log
            try:
                while not stopping.is_set():
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:
                        break
                    if not chunk:  # EOF
                        text = decoder.decode(b"", final=True)
                    else:
                        text = decoder.decode(chunk)
                    if text:
                        with log_lock:
                            if stopping.is_set():
                                break
                            buf.append(text)
                            if tag:
                                line_rem += text
                                while "\n" in line_rem:
                                    one, line_rem = line_rem.split("\n", 1)
                                    _log_write(tag + one + "\n")
                            else:
                                _log_write(text)
                    if not chunk:
                        if tag and line_rem:  # flush a trailing tagged remainder
                            with log_lock:
                                _log_write(tag + line_rem + "\n")
                        break
            finally:
                try:
                    stream.close()
                except OSError:
                    pass

        stdin_thread = threading.Thread(target=_feed_stdin, daemon=True)
        stdout_thread = threading.Thread(target=_drain, args=(proc.stdout, out_buf, ""), daemon=True)
        stderr_thread = threading.Thread(target=_drain, args=(proc.stderr, err_buf, "[stderr] "), daemon=True)
        stdin_thread.start()
        stdout_thread.start()
        stderr_thread.start()

        # Enforce the timeout on the PROCESS, not on pipe EOF. proc.wait(timeout) is
        # immune to a leaked fd held by an escaped descendant.
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc, pgid)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass

        # The process has exited (or been killed). Give the readers a short grace window
        # to flush. If EITHER is STILL blocked, a child is holding a pipe open — reap
        # the whole group to free our fds so we never hang.
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
        if stdout_thread.is_alive() or stderr_thread.is_alive():
            _kill_tree(proc, pgid)
        # Final bounded joins: if a descendant escaped the group entirely and keeps an
        # fd open, these time out and we return the partial buffer anyway. We then SET
        # `stopping` so any drain thread still alive stops touching the buffers/log
        # before we snapshot and the finally closes log_fh.
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
        stdin_thread.join(timeout=1)

        returncode = proc.returncode if proc.returncode is not None else -1
        if timed_out:
            returncode = 124  # conventional timeout exit code (overrides the kill signal)

        with log_lock:
            stopping.set()  # freeze the buffers + stop late log writes
            stdout = "".join(out_buf)
            stderr = "".join(err_buf)
            if timed_out:
                marker = f"\n[review-cli] TIMEOUT after {timeout}s — partial output above]\n"
                stdout += marker
                try:
                    log_fh.write(marker)
                    log_fh.flush()
                except (ValueError, OSError):
                    pass
        return subprocess.CompletedProcess(args=argv, returncode=returncode, stdout=stdout, stderr=stderr)
    finally:
        # The log handle ALWAYS closes, even if Popen or a write raised before the
        # normal return path (the docstring's partial-output promise depends on it).
        # Set `stopping` first so a still-alive drain thread won't write to a closing
        # handle (no-op if already set on the normal path).
        stopping.set()
        if proc is not None:
            _kill_tree(proc, pgid)
        with log_lock:
            log_fh.close()
