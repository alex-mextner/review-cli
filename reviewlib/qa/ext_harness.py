"""The ext Tier-1 DETERMINISTIC harness: launch a VS Code extension in an ISOLATED VS Code
instance and drive it deterministically against a prose ``## Case:`` suite — no flaky model,
no un-caged agent (spec docs/specs/review-qa.md §7.1, Tier 1, ext kind).

WHY A SEPARATE, DETERMINISTIC PATH (not the un-caged executor). The backend tester needs an
un-caged write/exec agent to drive a live system. A Tier-1 ext test does NOT: "run this VS
Code command -> expect this notification / editor text / webview body" is a fully mechanical
assertion the moment the extension is loaded into an isolated VS Code and Playwright can drive
it over CDP. So ext Tier-1 runs as DETERMINISTIC Python — it launches an isolated VS Code with
the extension on ``--extensionDevelopmentPath``, connects over CDP, and classifies each
``## Case:`` block by running its commands and matching the resulting window state. This keeps
the path off the blast radius of an un-caged agent entirely (the agent cage is a real boundary
the spec went to lengths to keep — ext Tier-1 simply doesn't need to remove it), and makes the
run reproducible with zero model spend.

REUSE OF THE PROVEN ``launchVSCode`` PATTERN (not a reinvention). The CTO's e2e harness already
solved isolated-VS-Code launch (``ext-test-projects/e2e/setup/electron-app.ts`` ``launchVSCode``:
isolated ``--user-data-dir`` / fresh ``--extensions-dir``, ``--disable-workspace-trust`` /
``--skip-welcome``, ``--extensionDevelopmentPath``, CDP connect, ``window.screenshot`` over CDP
bypassing macOS Screen-Recording grants). The hard-won facts (run under node/tsx NOT bun — bun
hangs ``_electron.launch`` on macOS; ``EXTENSION_PATH`` env; CDP screenshot) live in the user's
CLAUDE.md. This harness REUSES that pattern via a small TS runner (``ext_runner.mts``) that owns
the Electron/CDP lifecycle and speaks a line-delimited JSON protocol over stdin/stdout; Python
parses the prose suite and drives the runner. The runner is shipped here as the reference impl;
the eventual published ``vscode-playwright`` package (spec §7.1) drops in the same way.

THE TWO-LAYER SPLIT (load-bearing for testability). The VS Code launch is heavy (Electron + a
browser-class runtime, node/tsx-only); requiring it in normal CI would make the harness's CORE
logic untestable without an install. So the driver is decoupled from VS Code behind a tiny
``ExtAutomation`` protocol (``run_command`` / ``open_file`` / ``notifications`` / ``editor_text``
/ ``webview_text`` / ``screenshot``). The deterministic CASE-RUNNER + the prose-grammar PARSER +
the ``## QA RESULTS`` EMITTER (in ``ext_driver.py``) speak only that protocol, so they are
unit-tested against an IN-MEMORY fake with no VS Code. ``ShellRunnerAutomation`` is the real,
CDP-backed implementation talking to the TS runner; it is gated behind ``REVIEW_QA_VSCODE=1`` +
a node-present check, so when the flag is off or node/VS Code is missing, an ext run SKIPs LOUDLY
(a controlled BLOCKED with the enable + install command) rather than crashing.

SAFETY. The TS runner is spawned in its OWN process group so the whole Electron tree can be
reaped, and the session is always closed (try/finally) — an ext run leaks neither a VS Code
window nor a node process. The runner sets a per-action timeout, so a hung command can never
wedge the run.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Protocol

# How much of the runner's stderr tail to retain for a BLOCKED-report proof. A drain thread
# keeps the OS pipe empty (so a chatty runner never blocks on a full pipe); we hold only the
# last few KiB, plenty for a launch crash/traceback tail.
_OUTPUT_TAIL_BYTES = 16384

# Where the reference TS runner lives, shipped alongside this module. The published
# vscode-playwright package (spec §7.1) would override this via REVIEW_QA_EXT_RUNNER.
_DEFAULT_RUNNER = Path(__file__).resolve().parent / "ext_runner" / "ext_runner.mts"


def _env_float(name: str, default: float) -> float:
    """A float from ``name`` in the environment, falling back to ``default`` on absent OR a
    non-numeric value. A bad env value must NOT crash at module import (which would wedge the
    whole qa import chain on a typo); it degrades to the default."""
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


# How long a single VS Code action (run a command, open a file, read window state) may take
# over the JSON protocol before it is abandoned. A hung command (an extension that wedges)
# must not wedge the whole run; the action fails the case with a clear timeout. Overridable so
# a slow extension gets more room.
ACTION_TIMEOUT_S = _env_float("REVIEW_QA_EXT_ACTION_TIMEOUT_S", 30.0)
# How long to wait for the TS runner to finish launching VS Code + signal "ready" on startup.
# An Electron VS Code boot is 30-60s per the user's CLAUDE.md, so this is generous.
LAUNCH_TIMEOUT_S = _env_float("REVIEW_QA_EXT_LAUNCH_TIMEOUT_S", 120.0)


# --- the ExtAutomation protocol (the seam the deterministic driver speaks) -------------
class ExtAutomation(Protocol):
    """The minimal VS Code automation interface the deterministic ext driver drives. Implemented
    by the real ``ShellRunnerAutomation`` (CDP-backed, talking to the TS runner) and by the
    in-memory test fake — so the case-runner logic is unit-testable with no VS Code. Every method
    is synchronous and either succeeds or raises an ``ExtActionError`` the driver turns into a
    case FAIL.

    ``run_command`` runs a VS Code command id (``executeCommand``); ``open_file`` opens a file by
    path (relative to the workspace). ``notifications`` returns the list of notification/message
    toast texts observed so far (each ``showInformationMessage`` etc.); ``editor_text`` returns
    the active editor's full text; ``webview_text`` returns the extension webview frame body's
    text (empty when no webview is open). ``screenshot`` writes a ``window.screenshot`` (over
    CDP) to ``path``, returning whether it succeeded."""

    def run_command(self, command_id: str) -> None: ...
    def open_file(self, rel_path: str) -> None: ...
    def notifications(self) -> list[str]: ...
    def editor_text(self) -> str: ...
    def webview_text(self) -> str: ...
    def screenshot(self, path: Path) -> bool: ...


class ExtActionError(RuntimeError):
    """A VS Code action failed (an unknown command, a missing file, a timed-out read, …). The
    driver turns it into a case FAIL with the message as the proof line — never lets it escape as
    a traceback."""


class ExtHarnessError(RuntimeError):
    """A controlled ext-harness failure (could not launch VS Code / the runtime is unavailable).
    Carries the qa exit class the handler should return so a harness infra failure maps to a
    stable code (NOT a found bug — that is report-only)."""

    def __init__(self, message: str, *, exit_code: int):
        super().__init__(message)
        self.exit_code = exit_code


# --- the heavy-dep gate (mirrors the web harness's playwright_available) --------------
def _flag_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no", "off")


def vscode_available() -> tuple[bool, str]:
    """Whether a real isolated-VS-Code run is GATED ON: the ``REVIEW_QA_VSCODE`` flag is on AND a
    node runtime that can run the ``.mts`` runner is present (``tsx`` or plain ``node``). Returns
    ``(ok, reason)`` — ``reason`` is the actionable skip message when ``ok`` is False, so an ext
    run that cannot launch VS Code BLOCKS loudly with the fix rather than crashing.

    The flag gate is first and deliberate: a real VS Code launch is heavy (Electron) and
    node/tsx-only, so a plain ``review qa --kind ext`` does NOT silently try to launch it — the
    user opts in with ``REVIEW_QA_VSCODE=1`` (mirroring how the bot/web harnesses keep their heavy
    bits behind a clear switch). With the flag off, the harness's pure logic is still fully
    exercised by the unit tests against the fake automation.

    NOTE: this checks the flag + a node runtime, NOT the VS Code BINARY — a missing VS Code is
    caught later at launch and turned into a controlled ``ExtHarnessError`` / BLOCKED (with the
    same guidance), so a half-set-up machine still fails cleanly, not with a traceback."""
    if not _flag_enabled("REVIEW_QA_VSCODE"):
        return False, (
            "the ext Tier-1 harness launches a real isolated VS Code, which is OFF by default. "
            "Set REVIEW_QA_VSCODE=1 to enable it. It needs node/tsx (run the runner under node, "
            "NOT bun — bun hangs Electron launch on macOS) and a VS Code binary (set "
            "VSCODE_PATH or have `code` on PATH)."
        )
    runner = _runner_command()
    if runner is None:
        return False, (
            "REVIEW_QA_VSCODE=1 is set but no node/tsx runtime was found to run the ext runner. "
            "Install one: `npm i -g tsx` (or have `node` on PATH). Do NOT use bun — it hangs "
            "Electron launch on macOS."
        )
    return True, ""


def _runner_command() -> list[str] | None:
    """The argv prefix that runs the ``.mts`` TS runner: ``tsx`` if present (handles ESM ``.mts``
    directly), else ``node`` (which on recent versions runs ``.mts`` via its type-stripping). A
    ``REVIEW_QA_EXT_NODE`` override wins (an explicit runtime path). Returns ``None`` when no
    runtime is found. Deliberately NEVER returns bun — bun hangs ``_electron.launch`` on macOS
    (the user's CLAUDE.md), a silent 180s wedge."""
    override = os.environ.get("REVIEW_QA_EXT_NODE", "").strip()
    if override:
        return [override]
    tsx = shutil.which("tsx")
    if tsx:
        return [tsx]
    node = shutil.which("node")
    if node:
        return [node]
    return None


def _runner_script() -> Path:
    """The TS runner script path: the ``REVIEW_QA_EXT_RUNNER`` override (the published
    vscode-playwright package's runner) else the reference impl shipped here."""
    override = os.environ.get("REVIEW_QA_EXT_RUNNER", "").strip()
    return Path(override) if override else _DEFAULT_RUNNER


# --- the real CDP-backed automation (gated; heavy) ------------------------------------
class ShellRunnerAutomation:
    """A real isolated-VS-Code automation, implemented by talking to the TS runner subprocess
    over a line-delimited JSON protocol on stdin/stdout, implementing ``ExtAutomation``.

    PROTOCOL. The runner launches VS Code (via the proven ``launchVSCode`` pattern), connects
    over CDP, and on stdout emits one JSON object per line. On startup it emits
    ``{"type": "ready"}`` once VS Code is up; thereafter, for each request line we write on its
    stdin (``{"id": N, "op": "run_command"|"open_file"|"notifications"|"editor_text"|
    "webview_text"|"screenshot", ...}``) it replies with a matching ``{"id": N, "ok": true,
    "result": ...}`` or ``{"id": N, "ok": false, "error": "..."}``. A request that exceeds
    ``ACTION_TIMEOUT_S`` (no matching reply) raises ``ExtActionError`` — the case FAILs instead of
    the whole run wedging on a hung extension.

    Lifecycle is owned by ``vscode_session`` (a context manager): it spawns the runner, waits for
    ``ready``, yields this automation, and ALWAYS terminates the runner's process GROUP (reaping
    the Electron tree). A launch failure raises ``ExtHarnessError`` (a controlled BLOCKED)."""

    def __init__(
        self, proc: subprocess.Popen, stderr_tail: _StderrTail, stdout_reader: _StdoutLineReader,
    ):
        self._proc = proc
        self._stderr_tail = stderr_tail
        self._stdout_reader = stdout_reader
        self._next_id = 0
        self._lock = threading.Lock()

    def run_command(self, command_id: str) -> None:
        self._request("run_command", {"command": command_id})

    def open_file(self, rel_path: str) -> None:
        self._request("open_file", {"path": rel_path})

    def notifications(self) -> list[str]:
        result = self._request("notifications", {})
        return [str(x) for x in result] if isinstance(result, list) else []

    def editor_text(self) -> str:
        return str(self._request("editor_text", {}) or "")

    def webview_text(self) -> str:
        return str(self._request("webview_text", {}) or "")

    def screenshot(self, path: Path) -> bool:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            result = self._request("screenshot", {"path": str(path)})
            return bool(result)
        except ExtActionError:
            # A screenshot is best-effort evidence, never fatal — a runner that can't capture
            # must not turn a real finding into a crash.
            return False

    def _request(self, op: str, args: dict) -> object:
        """Write one JSON request to the runner's stdin and block (bounded) for its matching
        reply line. Single-threaded by ``_lock`` (the driver runs cases serially, but the lock
        keeps id/response pairing correct even if a future caller parallelizes). Raises
        ``ExtActionError`` on a protocol/timeout/runner-death failure so the driver classifies the
        case honestly instead of leaking."""
        with self._lock:
            self._next_id += 1
            req_id = self._next_id
            payload = json.dumps({"id": req_id, "op": op, **args})
            if self._proc.stdin is None:
                raise ExtActionError("the VS Code runner has no stdin to send the request to")
            try:
                self._proc.stdin.write(payload + "\n")
                self._proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise ExtActionError(f"could not send {op!r} to the runner: {exc}") from exc
            reply = self._read_reply(req_id, op)
        if not reply.get("ok"):
            raise ExtActionError(f"{op} failed: {reply.get('error', 'unknown runner error')}")
        return reply.get("result")

    def _read_reply(self, req_id: int, op: str) -> dict:
        """Read runner stdout lines until the reply for ``req_id`` arrives (or timeout / runner
        death). Lines that are not our reply (a stray log line, an out-of-order id) are skipped.
        The runner is expected to answer in order, so a generous per-action timeout bounds a hung
        extension without flakiness."""
        deadline = time.monotonic() + ACTION_TIMEOUT_S
        while time.monotonic() < deadline:
            # A runner that exited AND whose stdout is fully drained can never answer — fail now
            # rather than wait out the timeout. We require at_eof too: the runner may have written
            # the reply and exited, with the line still buffered in the reader's queue.
            if self._proc.poll() is not None and self._stdout_reader.at_eof:
                raise ExtActionError(
                    f"the VS Code runner exited before answering {op!r}. "
                    f"stderr tail:\n{self._stderr_tail.text()}"
                )
            # The reader THREAD owns stdout and drains complete lines into a queue; next_line()
            # blocks at most its timeout, so a runner that holds the pipe OPEN but never replies (a
            # hung extension, not a crash) is bounded by the per-action deadline — without the text-
            # mode select/readline buffering footgun this used to have (review-cli#75).
            line = self._stdout_reader.next_line(0.2)
            if line is None:
                continue
            try:
                msg = json.loads(line)
            except (ValueError, TypeError):
                continue  # a non-JSON log line from the runner — ignore
            if isinstance(msg, dict) and msg.get("id") == req_id:
                return msg
        raise ExtActionError(
            f"the VS Code runner did not answer {op!r} within {ACTION_TIMEOUT_S:.0f}s "
            "(a hung command / extension)."
        )


class _StderrTail:
    """Drains the runner's stderr in a daemon thread into a bounded in-memory tail, so the OS
    pipe never fills (a chatty runner that out-logged the pipe buffer before answering would
    otherwise block) and a launch-failure proof has the crash tail. ``text()`` returns the
    buffer."""

    def __init__(self, stream):
        self._tail: deque[str] = deque()
        self._len = 0
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._drain, args=(stream,), daemon=True)
        if stream is not None:
            self._thread.start()

    def _drain(self, stream) -> None:
        try:
            for line in stream:
                with self._lock:
                    self._tail.append(line)
                    self._len += len(line)
                    while self._len > _OUTPUT_TAIL_BYTES and self._tail:
                        self._len -= len(self._tail.popleft())
        except (OSError, ValueError):
            pass

    def text(self, *, limit: int = 2000) -> str:
        with self._lock:
            text = "".join(self._tail)
        return text[-limit:] or "(no runner output captured)"


class _StdoutLineReader:
    """Drains the runner's stdout in a daemon thread into a thread-safe queue, one line per item.

    Why a reader THREAD and not ``select`` + ``readline`` on the stream: the runner's stdout is a
    TEXT-mode pipe (``text=True``). ``select`` watches the OS-level fd, but Python's text wrapper
    has its OWN buffer ABOVE that fd — a full line can already sit decoded in that buffer while
    ``select`` reports "nothing to read" (a false timeout), and a ``readline`` issued anyway can
    block past the deadline waiting on the OS pipe. A dedicated thread doing blocking ``readline``
    sidesteps both: it always drains complete lines, and the consumer polls the queue with its OWN
    deadline (``next_line(timeout)``) — so a silent-but-alive runner is bounded by the timeout, not
    by the buffering (review-cli#75). EOF (runner stdout closed) is recorded so a consumer can stop
    waiting. Mirrors ``_StderrTail``'s drain-in-a-daemon-thread shape."""

    def __init__(self, stream):
        import queue

        self._q: "queue.Queue[str]" = queue.Queue()
        self._eof = threading.Event()
        self._thread = threading.Thread(target=self._drain, args=(stream,), daemon=True)
        if stream is not None:
            self._thread.start()
        else:
            self._eof.set()

    def _drain(self, stream) -> None:
        try:
            for line in stream:  # blocking readline loop — complete lines only
                self._q.put(line)
        except (OSError, ValueError):
            pass
        finally:
            self._eof.set()

    def next_line(self, timeout: float) -> "str | None":
        """The next stdout line, or ``None`` when none arrives within ``timeout`` seconds. Does
        NOT raise on EOF — the caller checks ``proc.poll()`` to distinguish a dead runner from a
        merely-quiet one (the queue may still hold buffered lines after EOF)."""
        import queue

        try:
            return self._q.get(timeout=max(timeout, 0.0))
        except queue.Empty:
            return None

    @property
    def at_eof(self) -> bool:
        """True once the stdout stream has closed AND every drained line has been consumed."""
        return self._eof.is_set() and self._q.empty()


class _VSCodeSession:
    """Owns the TS runner subprocess (and through it the Electron VS Code) for one ext run.
    Created via ``vscode_session``; ``__exit__`` terminates the runner's process GROUP so the
    whole VS Code tree is reaped — an ext run leaks no Electron process. A launch failure (the
    runner never signals ``ready``) raises ``ExtHarnessError`` (a controlled BLOCKED)."""

    def __init__(
        self, *, extension_path: str, workspace: Path, exit_blocked: int,
        extra_env: dict[str, str] | None = None,
    ):
        self._extension_path = extension_path
        self._workspace = workspace
        self._exit_blocked = exit_blocked
        self._extra_env = extra_env or {}
        self._proc: subprocess.Popen | None = None
        self._stderr_tail: _StderrTail | None = None
        self._stdout_reader: _StdoutLineReader | None = None

    def __enter__(self) -> ShellRunnerAutomation:
        runner_cmd = _runner_command()
        if runner_cmd is None:
            raise ExtHarnessError(
                "no node/tsx runtime to run the ext runner (do NOT use bun).",
                exit_code=self._exit_blocked,
            )
        script = _runner_script()
        if not script.exists():
            raise ExtHarnessError(
                f"the ext TS runner is missing at {script} (set REVIEW_QA_EXT_RUNNER).",
                exit_code=self._exit_blocked,
            )
        env = dict(os.environ)
        # The SUT's declared NON-SECRET sut.ext.env (a feature flag, a config toggle) must reach
        # the extension under test — without it a configured SUT's activation / command assertions
        # would run with the wrong environment (codex PR review P2: env was parsed but discarded,
        # unlike the web path which passes web_config.env). Secrets stay in host env (inherited via
        # os.environ above), so the SUT-declared map only ADDS non-secret config.
        env.update(self._extra_env)
        # Reuse the proven EXTENSION_PATH convention the e2e harness keys on. Set AFTER the SUT env
        # so a SUT cannot accidentally override the extension-under-test path.
        env["EXTENSION_PATH"] = self._extension_path
        try:
            self._proc = subprocess.Popen(  # noqa: S603 — runtime + script resolved above
                [*runner_cmd, str(script)],
                cwd=str(self._workspace), env=env, start_new_session=True,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
        except (OSError, ValueError) as exc:
            raise ExtHarnessError(
                f"could not launch the ext runner {runner_cmd!r}: {exc}",
                exit_code=self._exit_blocked,
            ) from exc
        self._stderr_tail = _StderrTail(self._proc.stderr)
        # One stdout reader for the whole session — used by _await_ready AND handed to the
        # automation, so a single thread owns the stream (two readers would race on readline).
        self._stdout_reader = _StdoutLineReader(self._proc.stdout)
        self._await_ready()
        return ShellRunnerAutomation(self._proc, self._stderr_tail, self._stdout_reader)

    def _await_ready(self) -> None:
        """Block (bounded by ``LAUNCH_TIMEOUT_S``) until the runner emits ``{"type":"ready"}`` on
        stdout — VS Code is up and the extension is loaded. A runner that exits or never signals
        ready raises ``ExtHarnessError`` with its stderr tail, so a launch crash is a diagnosable
        BLOCKED, not a hang."""
        assert self._proc is not None and self._stderr_tail is not None
        assert self._stdout_reader is not None
        deadline = time.monotonic() + LAUNCH_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._proc.poll() is not None and self._stdout_reader.at_eof:
                raise ExtHarnessError(
                    "the VS Code runner exited before signalling ready (launch failed). "
                    f"stderr tail:\n{self._stderr_tail.text()}",
                    exit_code=self._exit_blocked,
                )
            # A reader THREAD drains stdout into a queue, so a runner that stays ALIVE but emits no
            # stdout before `ready` (a wedged VS Code launch, a custom REVIEW_QA_EXT_RUNNER that
            # hangs) cannot block past the deadline — next_line() returns None on its own timeout and
            # the LAUNCH_TIMEOUT_S check fires, giving a controlled BLOCKED + process-group teardown
            # instead of a hang (codex PR review P2 + the buffered-readline fix, review-cli#75).
            line = self._stdout_reader.next_line(0.05)
            if line is None:
                continue
            try:
                msg = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(msg, dict) and msg.get("type") == "ready":
                return
            if isinstance(msg, dict) and msg.get("type") == "error":
                self._terminate()
                raise ExtHarnessError(
                    f"the VS Code runner reported a launch error: {msg.get('error')}",
                    exit_code=self._exit_blocked,
                )
        self._terminate()
        raise ExtHarnessError(
            f"VS Code did not become ready within {LAUNCH_TIMEOUT_S:.0f}s. "
            f"stderr tail:\n{self._stderr_tail.text()}",
            exit_code=self._exit_blocked,
        )

    def __exit__(self, *_exc) -> None:
        self._terminate()

    def _terminate(self) -> None:
        """SIGTERM then SIGKILL the runner's whole process GROUP (the runner + Electron + any
        child renderer), so a VS Code window can't outlive the run. Best-effort; never raises."""
        if self._proc is None:
            return
        proc = self._proc
        self._proc = None
        # Close stdin first so a runner blocked on a read sees EOF and can exit cleanly.
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except (OSError, ValueError):
            pass
        _terminate_group(proc)


def vscode_session(
    *, extension_path: str, workspace: Path, exit_blocked: int,
    extra_env: dict[str, str] | None = None,
) -> _VSCodeSession:
    """A context manager yielding a real ``ShellRunnerAutomation`` and guaranteeing VS Code
    teardown. ``extra_env`` is the SUT's declared NON-SECRET ``sut.ext.env`` map, merged into the
    runner's environment so the extension under test sees its configured variables. Call
    ``vscode_available()`` FIRST — this assumes the runtime is present (it is only reached on the
    real path)."""
    return _VSCodeSession(
        extension_path=extension_path, workspace=workspace, exit_blocked=exit_blocked,
        extra_env=extra_env)


# --- process-group reaping (mirrors web_harness; shared shape) -------------------------
def _terminate_group(proc: subprocess.Popen) -> None:
    """SIGTERM then (after a grace) SIGKILL the runner's whole process GROUP, so a forked Electron
    child can't outlive the run. Captures the pgid up front so a reaped leader doesn't make
    ``getpgid`` fail; signals the GROUP even after the leader exits. Best-effort; never raises."""
    pgid = _pgid_of(proc)
    leader_alive = proc.poll() is None
    if pgid is None and not leader_alive:
        return
    _signal_group_or_proc(proc, pgid, signal.SIGTERM)
    if leader_alive:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    else:
        time.sleep(0.2)
    _signal_group_or_proc(proc, pgid, signal.SIGKILL)


def _pgid_of(proc: subprocess.Popen) -> int | None:
    try:
        return os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        return proc.pid if proc.pid else None


def _signal_group_or_proc(proc: subprocess.Popen, pgid: int | None, sig: int) -> None:
    if pgid is not None:
        try:
            os.killpg(pgid, sig)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        if sig == signal.SIGKILL:
            proc.kill()
        else:
            proc.terminate()
    except (OSError, ValueError):
        pass
