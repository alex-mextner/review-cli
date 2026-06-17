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

# Registry of LIVE backend subprocesses, so the run backstop (reviewlib.backstop) can
# reap them before its hard `os._exit`. Each backend child is started in its OWN session
# (`start_new_session=True`), so it is NOT in the CLI's process group and survives a plain
# process exit; without this registry a backstop fire would orphan a hung/expensive
# backend and fail to actually bound the work (codex P2). `_run_streamed` registers its
# `(proc, pgid)` while the child is alive and unregisters in its `finally`; the backstop
# drains the registry and `_kill_tree`s each. Holds ONLY backend children's own session
# groups — never the CLI's group or its caller's — so draining it can't take down the
# caller. Lock-guarded because backends run in parallel panel threads.
_LIVE_CHILDREN_LOCK = threading.Lock()
_LIVE_CHILDREN: set[tuple[subprocess.Popen, int | None]] = set()


def _register_child(proc: subprocess.Popen, pgid: int | None) -> tuple[subprocess.Popen, int | None]:
    """Track a live backend child so the backstop can reap it. Returns the handle to
    pass back to `_unregister_child` in a finally."""
    handle = (proc, pgid)
    with _LIVE_CHILDREN_LOCK:
        _LIVE_CHILDREN.add(handle)
    return handle


def _unregister_child(handle: tuple[subprocess.Popen, int | None]) -> None:
    """Drop a child from the live registry once its own cleanup has run (idempotent)."""
    with _LIVE_CHILDREN_LOCK:
        _LIVE_CHILDREN.discard(handle)


def _reregister_child(
    old: tuple[subprocess.Popen, int | None], proc: subprocess.Popen, pgid: int | None
) -> tuple[subprocess.Popen, int | None]:
    """Swap a child handle (e.g. once its pgid is known) WITHOUT ever leaving the child
    unregistered. Add the new handle BEFORE discarding the old, both under one lock hold,
    so a concurrent `kill_live_children` snapshot always sees at least one handle for this
    proc — never a gap in which a backstop fire could orphan it."""
    new = (proc, pgid)
    with _LIVE_CHILDREN_LOCK:
        _LIVE_CHILDREN.add(new)
        _LIVE_CHILDREN.discard(old)
    return new


def kill_live_children() -> None:
    """Reap every still-registered backend child's process group. Best-effort, never
    raises — called from the backstop's `_fire` right before its hard exit so a wedged
    run's backend subprocesses don't outlive the force-terminated CLI. Each child is in
    its own session, so this kills the backends WITHOUT touching the CLI's/caller's
    group.

    KILL-FIRST, never blocking. Unlike `_kill_tree` (the per-call path, which politely
    SIGTERMs then waits up to 3s before SIGKILL — the right etiquette for a normal
    timeout), this last-resort path sends SIGKILL straight away to EVERY child group with
    NO wait in between. That is deliberate: the backstop's deadman force-exits the process
    after a short grace, so a per-child `proc.wait(timeout=3)` here could be preempted
    before later children are reached or before SIGKILL even lands, leaving a wedged
    backend alive (codex P2). SIGKILL is uncatchable, so no grace is needed anyway — the
    whole reap is a handful of non-blocking signals that complete in microseconds and
    cannot be preempted."""
    with _LIVE_CHILDREN_LOCK:
        snapshot = list(_LIVE_CHILDREN)
    for proc, pgid in snapshot:
        try:
            if pgid is not None:
                os.killpg(pgid, signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass
        except Exception:  # noqa: BLE001 — best-effort; reaping must never block the exit
            pass


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
    """Predictable dir for per-call live logs (and the brainstorm discussion log)
    that an external `tail -f` can watch.

    Honors $REVIEW_LOG_DIR (tests), else the OS-standard per-user log location:
      macOS → ~/Library/Logs/review-cli
      Linux/other → $XDG_STATE_HOME/review-cli/logs  (default ~/.local/state/...)
    Logs are state, not throwaway cache, so on Linux they live under XDG_STATE_HOME
    rather than the cache dir. Created private (0700) because logs persist reviewed
    prompts/diffs (possibly secrets).
    """
    override = os.environ.get("REVIEW_LOG_DIR")
    if override:
        base = Path(override)
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Logs" / "review-cli"
    else:
        state = os.environ.get("XDG_STATE_HOME", "").strip()
        # XDG spec: a relative $XDG_STATE_HOME must be ignored.
        root = Path(state) if state and os.path.isabs(state) else (Path.home() / ".local" / "state")
        base = root / "review-cli" / "logs"
    base.mkdir(parents=True, exist_ok=True)
    try:
        base.chmod(0o700)
    except OSError:
        pass
    return base


def _safe_backend(backend: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in backend) or "backend"


def _open_log(backend: str, round_no: int) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    return log_dir() / f"{stamp}-{_safe_backend(backend)}-r{round_no}.log"


# Explicit status footer the dashboard parser reads to decide success/failure. The
# parser must NOT infer failure from substrings like `error:` in the body — a review's
# OUTPUT legitimately contains those (it is literally describing errors in the code).
# The return code is the only authoritative signal, so EVERY log writer (the streamed
# subprocess runner below AND the non-subprocess sidecar writer) stamps it as a final
# `[review-cli] EXIT {code}` line. (HYP-742: explicit status, no body grep.)
_EXIT_PREFIX = "[review-cli] EXIT "


def _exit_line(returncode: int, *, preceding_text: str = "") -> str:
    """The `[review-cli] EXIT {code}` footer, guaranteed to start on its OWN line.

    The parser only recognises the footer when it is the first thing on a line, so if
    the preceding logged output did not end with a newline (a subprocess that flushed
    stdout WITHOUT a trailing `\\n`) we prepend one — otherwise `...lastlineEXIT 1` is
    unparsable and the call is misclassified (codex P2)."""
    lead = "" if (not preceding_text or preceding_text.endswith("\n")) else "\n"
    return f"{lead}{_EXIT_PREFIX}{returncode}\n"


def write_sidecar_log(
    backend: str,
    *,
    round_no: int,
    argv0: str,
    returncode: int,
    stdout: str,
    stderr: str = "",
    started: datetime | None = None,
    timed_out: bool = False,
    timeout_secs: int | None = None,
) -> Path:
    """Emit a per-call ``{stamp}-{backend}-r{n}.log`` for a NON-subprocess backend.

    The streamed subprocess runner (`_run_streamed`) writes a live log for every
    subprocess-based backend (codex/claude/opencode), but REST backends (gemini, and
    any future HTTP backend) return without ever touching the log dir. The dashboard
    parser reads ONLY those `.log` files, so a Gemini-only run was invisible — it
    undercounted models and made Gemini-only sessions vanish (HYP-742 finding 2).

    This writes the SAME on-disk format the parser already understands: the redacted
    header line, the body, stderr lines tagged ``[stderr] ``, and the explicit
    ``[review-cli] EXIT {code}`` status footer (finding 4 — success/failure comes from
    the return code, never a body substring). File perms are 0600: logs persist
    reviewed prompts/diffs that may carry secrets.

    ``started`` is the call's START time and becomes the filename stamp. The parser
    treats the filename stamp as the call start and the file mtime as the end, so the
    caller MUST pass the time the call BEGAN — not "now" at write time — or a slow REST
    call would show a near-zero duration and could fall into the wrong session cluster
    (codex P2). Defaults to now() only when the caller has no better anchor.

    ``timed_out`` writes the same ``[review-cli] TIMEOUT after {N}s`` marker the
    subprocess runner uses, so the dashboard counts a REST timeout as a TIMEOUT (not a
    generic error) — keeping the timeout metric consistent across backends (codex P2).
    """
    stamp = (started or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%S_%fZ")
    path = log_dir() / f"{stamp}-{_safe_backend(backend)}-r{round_no}.log"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(f"[review-cli] {backend}: {argv0 or '?'} (args redacted)\n")
        if stdout:
            fh.write(stdout if stdout.endswith("\n") else stdout + "\n")
        for line in stderr.splitlines():
            fh.write("[stderr] " + line + "\n")
        if timed_out:
            secs = timeout_secs if timeout_secs is not None else 0
            fh.write(f"[review-cli] TIMEOUT after {secs}s — partial output above]\n")
        fh.write(_exit_line(returncode))
    return path


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
    header_argv0: str | None = None,
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
    child_handle: tuple[subprocess.Popen, int | None] | None = None
    # Last char written to the log, so the trailing `EXIT {code}` footer can be put on
    # its own line even when the subprocess flushed stdout without a trailing newline
    # (codex P2: an unanchored footer is unparsable). The header ends with "\n".
    log_tail = {"nl": True}

    # Private file perms: logs persist prompts/diffs that may contain secrets.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    log_fh = os.fdopen(fd, "w", encoding="utf-8", buffering=1)  # line-buffered
    try:
        # Header records the backend + argv[0] only — NOT the full argv, which carries
        # the prompt/diff for claude/opencode and could leak secrets into the log. A
        # backend may pass `header_argv0` to record a model SELECTOR instead of the bare
        # binary path (e.g. opencode's `opencode -m <provider/model>`), so the dashboard
        # can attribute the call to its board seat — it must still contain NO prompt/diff.
        header = header_argv0 or (argv[0] if argv else "?")
        log_fh.write(f"[review-cli] {backend}: {header} (args redacted)\n")
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
        # Register IMMEDIATELY — before resolving the pgid — so a backstop fire can never
        # orphan a just-Popen'd backend in the window before `os.getpgid` returns. At this
        # instant the child has no descendants yet, so the pgid=None handle (which reaps
        # via `proc.kill()` on its own pid) is sufficient cover for that window.
        child_handle = _register_child(proc, None)
        # Capture the group id NOW, while the child is alive. We need it later even
        # after the direct child is reaped, because a grandchild in the same group
        # may still be holding the stdout pipe open.
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, OSError):
            pgid = None
        # Once the pgid is known, swap to a group-bearing handle so a later reap kills the
        # WHOLE tree (grandchildren included), not just the direct child. The swap is
        # gap-free (`_reregister_child` adds before it discards), so the child is reapable
        # throughout. Unregistered in the finally, after the normal `_kill_tree` cleanup.
        if pgid is not None:
            child_handle = _reregister_child(child_handle, proc, pgid)

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
                if text:
                    log_tail["nl"] = text.endswith("\n")
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
                    log_tail["nl"] = True  # marker ends with "\n"
                except (ValueError, OSError):
                    pass
            # Explicit status footer so the dashboard parser decides success/failure
            # from the RETURN CODE, not a body substring (HYP-742 finding 4). Written
            # last, under the lock, after `stopping` is set so no drain thread races it.
            # Anchored on its own line (codex P2) even if the child's last stdout flush
            # had no trailing newline.
            try:
                lead = "" if log_tail["nl"] else "\n"
                log_fh.write(lead + _exit_line(returncode))
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
        # Drop from the backstop's live-children registry: this child's own cleanup just
        # ran, so the backstop must not try to re-reap a (possibly recycled) pid.
        if child_handle is not None:
            _unregister_child(child_handle)
        with log_lock:
            log_fh.close()
