"""Subprocess plumbing: blocking + live-streaming backend runners.

Extracted verbatim from the original single-file `bin/review` (Stage 0
decomposition — zero behaviour change). `_run_streamed` is the workhorse for
long backend calls: it streams stdout/stderr to a per-call log file in real time
and preserves partial output on timeout. See the module docstrings below.
"""

from __future__ import annotations

import codecs
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .stats import normalize_task_code

_LOG_HEADER_CONTROL_TRANSLATION = {
    **{codepoint: "?" for codepoint in range(0x20)},
    **{codepoint: "?" for codepoint in range(0x80, 0xA0)},
    0x7F: "?",
    0x2028: "?",
    0x2029: "?",
}


def _safe_log_header(value: object) -> str:
    return str(value or "?").translate(_LOG_HEADER_CONTROL_TRANSLATION)


# Single source of truth for stripping terminal control noise out of captured
# backend output. A backend can leak ANSI/VT100 escapes into its stdout — colour
# codes, cursor moves, OSC hyperlinks — and an interactive-TUI-scraper backend (the
# `claude` seat historically ran through `claude-p`, which drives the fullscreen
# `claude` TUI under a PTY and screen-scrapes it) can additionally bleed spinner
# redraws and bare C0 control bytes into the pipe. Either corrupts the parsed
# `## <model> [ok]/[needs-changes]` verdict, so the captured text is sanitised before
# it reaches the verdict pipeline AND the `-o` output file. cli._ANSI_ESCAPE_RE
# (output-file path) and the claude backend both delegate here so the rules never drift.
_CSI_OSC_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"  # CSI: ESC [ … final-byte (colours / cursor moves)
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC: ESC ] … (BEL | ST) (hyperlinks / titles)
    r"|\x1b[ -/]*[@-~]"  # other 2+-byte ESC seqs incl. ESC c (RIS), ESC M
)
# C0 control chars to drop after CSI/OSC removal — everything below 0x20 (plus DEL)
# EXCEPT newline (\n) and tab (\t), the whitespace that carries real verdict structure.
# Carriage return (\r) IS dropped: in a TUI-scraper transcript it is the line-OVERWRITE
# byte, so a stray CR could splice an old redraw fragment into a verdict line — we never
# want carriage-return overwrite semantics in parsed text, only the resulting characters.
_C0_CONTROL_RE = re.compile(
    r"[\x00-\x08\x0b-\x1f\x7f]"
)  # 0x00–0x1F minus \t (0x09) & \n (0x0A), plus 0x7F


def strip_control_sequences(text: str) -> str:
    """Remove ANSI/OSC escape sequences and stray C0 control bytes from `text`.

    Belt-and-suspenders against terminal noise (colours, cursor moves, TUI spinner
    redraws) corrupting a parsed verdict. Keeps newlines and tabs so line structure
    survives; drops carriage returns (the TUI line-overwrite byte). Idempotent and safe
    on text that has no control bytes."""
    return _C0_CONTROL_RE.sub("", _CSI_OSC_RE.sub("", text))


# ── memory-aware concurrency cap (board resilience under swarm load, review-cli#65) ───────
# Each heavy backend (codex / claude / opencode) spawns a model-runner subprocess, and a
# `review` invocation runs the whole pool in PARALLEL (panel.run_panel uses
# ThreadPoolExecutor(max_workers=len(jobs)) — one thread per seat, NO upper bound). Under
# swarm load (a high `--pool`, or many seats backfilled by the failover) that fans out into
# as many concurrent model subprocesses as there are seats. Each child is a fat agent CLI
# (hundreds of MB resident); enough of them at once and the box OOM-kills a seat mid-review
# — the live failure that motivated this (an Opus seat "died mid-review, exit 1").
#
# This semaphore bounds the number of heavy backend subprocesses THIS PROCESS runs at once,
# regardless of how many seats the pool/failover wants in flight. A seat blocked on the
# semaphore simply WAITS for a slot (it does not fail or drop) — the per-call timeout clock
# only starts once the child is actually spawned (the wait is BEFORE Popen), so a queued
# seat is never falsely timed out. The default cap is small enough that a single high-`--pool`
# run can't exhaust memory, while the common single-seat gate (`--pool 1`) and the default
# pool of 4 are unaffected (4 <= the cap). Overridable via $REVIEW_MAX_CONCURRENCY; <= 0
# disables the cap (unbounded, the legacy behaviour) for a box that can sustain it.
#
# NOTE: this is a PER-PROCESS cap. A swarm of N separate `review` processes is N independent
# caps — the cross-process lever is the per-seat timeout (a stalled seat frees its slot fast)
# plus deprioritizing the known-slow reserve seat, both also part of #65. A per-process cap
# still bounds the worst single-invocation fan-out (`--pool 0` on a large board, or a cascade of
# reserve backfills) that one process can create.
_DEFAULT_MAX_CONCURRENCY = 4
_MAX_CONCURRENCY_CEILING = 64  # a typo'd env can't pin an absurd number of children
_DEFAULT_IDLE_TIMEOUT = 20 * 60
_SHORT_TIMEOUT_EXACT_THRESHOLD = 60
_IDLE_TIMEOUT_ENV = "REVIEW_IDLE_TIMEOUT_SECONDS"

# Built lazily + cached: read $REVIEW_MAX_CONCURRENCY once on first spawn so a test can set
# the env before importing/using the module, and so every seat in a run shares ONE semaphore
# (a per-call build would never actually limit anything). Guarded by its own lock; None until
# first built. A value <= 0 means "no cap", represented by a None semaphore (the acquire path
# is then a no-op).
_CONCURRENCY_LOCK = threading.Lock()
_concurrency_sem: threading.BoundedSemaphore | None = None
_concurrency_built = False


def max_concurrency() -> int:
    """The configured cap on concurrent heavy backend subprocesses, read from
    $REVIEW_MAX_CONCURRENCY (a missing/blank/non-integer value falls back to the default; a
    value above the ceiling clamps down; <= 0 disables the cap). Read at build time so a run
    can pin it via the env before the first spawn."""
    raw = os.environ.get("REVIEW_MAX_CONCURRENCY")
    if raw is None or not raw.strip():
        return _DEFAULT_MAX_CONCURRENCY
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_MAX_CONCURRENCY
    if value <= 0:
        return 0  # disabled
    return min(value, _MAX_CONCURRENCY_CEILING)


def idle_timeout_seconds(
    timeout: int, *, idle_floor: int | None = _DEFAULT_IDLE_TIMEOUT
) -> int | None:
    """Seconds of backend silence allowed before reaping a subprocess.

    The historical `timeout` was a hard wall clock cap. Agent CLIs can legitimately run
    for a long time (Fable can take ~15 minutes) while still being alive, so subprocess
    calls are now bounded by *silence*: if a backend writes stdout/stderr before the idle
    window expires, the clock resets. Normal review-seat subprocesses get at least 20
    minutes of quiet thinking time; callers with a tighter idle contract can pass
    ``idle_floor=0`` to keep the requested value exact, and bounded surfaces such as
    QA/vision use `_run_streamed(..., timeout_mode="wall")` instead.
    Set REVIEW_IDLE_TIMEOUT_SECONDS=0 to disable idle reap and use the requested wall-clock
    timeout instead. Callers that pass ``idle_floor=0`` have a tight user-facing contract,
    so their requested value stays exact even when the ambient env var is present.
    """
    requested = max(int(timeout), 1)
    if idle_floor is not None and idle_floor <= 0:
        return requested
    raw = os.environ.get(_IDLE_TIMEOUT_ENV)
    if raw is not None and raw.strip():
        try:
            value = int(raw)
        except ValueError:
            value = None
        if value is not None:
            if value == 0:
                return None
            if value > 0:
                return value
    if idle_floor is None:
        return requested
    # Tiny timeouts are test/debug contracts. Preserve them exactly so unit tests and
    # one-off probes can still finish quickly; normal human review timeouts get the floor.
    if requested < _SHORT_TIMEOUT_EXACT_THRESHOLD:
        return requested
    return max(requested, idle_floor)


def _get_concurrency_sem() -> threading.BoundedSemaphore | None:
    """The process-wide spawn semaphore, built once and cached. None means the cap is
    disabled ($REVIEW_MAX_CONCURRENCY <= 0) — the acquire/release path then no-ops."""
    global _concurrency_sem, _concurrency_built
    with _CONCURRENCY_LOCK:
        if not _concurrency_built:
            cap = max_concurrency()
            _concurrency_sem = threading.BoundedSemaphore(cap) if cap > 0 else None
            _concurrency_built = True
        return _concurrency_sem


def _reset_concurrency_sem_for_tests() -> None:
    """Drop the cached semaphore so a test can change $REVIEW_MAX_CONCURRENCY and rebuild.
    Test-only; production builds the semaphore once per process and never resets it."""
    global _concurrency_sem, _concurrency_built
    with _CONCURRENCY_LOCK:
        _concurrency_sem = None
        _concurrency_built = False


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


def _register_child(
    proc: subprocess.Popen, pgid: int | None
) -> tuple[subprocess.Popen, int | None]:
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
    raises — called from the backstop's `_fire` right before its hard exit, AND from
    `install_signal_reaper`'s SIGTERM/SIGINT handler, so a wedged/killed run's backend
    subprocesses don't outlive the terminated CLI. Each child is in its own session, so
    this kills the backends WITHOUT touching the CLI's/caller's group.

    KILL-FIRST, never blocking. Unlike `_kill_tree` (the per-call path, which politely
    SIGTERMs then waits up to 3s before SIGKILL — the right etiquette for a normal
    timeout), this last-resort path sends SIGKILL straight away to EVERY child group with
    NO wait in between. That is deliberate: the backstop's deadman force-exits the process
    after a short grace, so a per-child `proc.wait(timeout=3)` here could be preempted
    before later children are reached or before SIGKILL even lands, leaving a wedged
    backend alive (codex P2). SIGKILL is uncatchable, so no grace is needed anyway — the
    whole reap is a handful of non-blocking signals that complete in microseconds and
    cannot be preempted.

    The snapshot read is lock-guarded with a SHORT TIMEOUT, not an unbounded `with
    _LIVE_CHILDREN_LOCK:` (codex review, review-cli#160 follow-up). Reason: since
    `install_signal_reaper`'s handler runs this on the MAIN thread — signal handlers in
    CPython always execute there regardless of which thread the signal targets — a
    signal that lands while the MAIN thread itself is inside `_register_child`/
    `_reregister_child`/`_unregister_child`'s critical section (any call path where
    `_run_streamed` runs synchronously on the main thread, e.g. a single-seat `--pool 1`
    or non-panel call, not routed through the panel's ThreadPoolExecutor workers) would
    otherwise try to re-acquire that SAME non-reentrant lock from within itself — an
    unconditional `with _LIVE_CHILDREN_LOCK:` deadlocks FOREVER in exactly that window,
    which defeats the entire point of this reaper (the process can no longer die at
    all). A bounded `acquire(timeout=...)` caps the wait; if it can't get the lock in
    time we fall back to an unlocked, best-effort read of `_LIVE_CHILDREN` (wrapped
    against the rare `RuntimeError: set changed size during iteration` a concurrent
    mutation can raise) rather than hang — a stale/partial snapshot here is far better
    than never reaping and never exiting."""
    got_lock = _LIVE_CHILDREN_LOCK.acquire(timeout=0.5)
    try:
        try:
            snapshot = list(_LIVE_CHILDREN)
        except RuntimeError:
            snapshot = []
    finally:
        if got_lock:
            _LIVE_CHILDREN_LOCK.release()
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


_SIGNAL_REAPER_LOCK = threading.Lock()
_signal_reaper_installed = False


def install_signal_reaper() -> None:
    """Make an external SIGTERM/SIGINT to THIS process reap every live backend child
    before the process dies (review-cli#160).

    Gap this closes: `run_backstop` (reviewlib.backstop) only bounds a run that wedges
    INSIDE its own logic — its watchdog fires from a live, running interpreter and can
    call `kill_live_children()` on the way to its `os._exit`. But an EXTERNAL signal
    (an agent's shell `kill <pid>`, a harness that SIGTERMs a Bash-tool timeout, or a
    plain Ctrl-C) is delivered straight to the OS. Python's default SIGTERM disposition
    is immediate process termination with NO interpreter code run at all — no
    `finally`, no `atexit` — so `_run_streamed`'s own `finally: _kill_tree(...)` never
    executes. Each backend child was started via `start_new_session=True` specifically
    so `_kill_tree` could bound its whole process-group tree; that same isolation means
    the child is in a DIFFERENT session from `review`, so the external signal never
    reaches it either. The result: the child (and any grandchildren) reparent to init
    (ppid=1) and run unbounded — exactly the `claude-opus-4-8`/`opencode` orphans this
    issue reports (observed alive 3.5h+ after their `review` run had already exited).

    Fix: install a handler for SIGTERM and SIGINT that calls `kill_live_children()`
    (the same best-effort, non-blocking, whole-process-group reap the internal
    backstop uses) and then CHAINS to whatever handler was ALREADY installed for that
    signal before this call — never a blind `SIG_DFL` + re-kill. This matters because
    SIGINT's pre-existing disposition is virtually never `SIG_DFL`: Python installs
    `signal.default_int_handler` (which raises `KeyboardInterrupt`) as its own default
    long before any of our code runs. The persistent SERVER subcommands (`dashboard`,
    `spec-web`) rely on exactly that — Ctrl-C raising `KeyboardInterrupt` so their own
    `try/except` can shut down gracefully (flush state, close sockets). An earlier
    version of this handler unconditionally reset to `SIG_DFL` and re-signaled itself,
    which for SIGINT means the process dies from the RAW signal instead of raising
    `KeyboardInterrupt` — silently skipping every server's graceful-shutdown path
    (codex review). Chaining to whatever was there before preserves that: for SIGINT
    the previous handler is `default_int_handler` (or a REPL's own handler under
    pytest/IPython), which we simply call after reaping — the exact same
    `KeyboardInterrupt` still gets raised, servers still shut down gracefully, and the
    reap now *additionally* happens first. Only when the previous disposition is NOT a
    Python callable (`SIG_DFL`/`SIG_IGN` — SIGTERM's normal starting point, since Python
    does not install its own SIGTERM handler) do we fall back to restore-default +
    re-kill, which is the only way to reproduce "the process actually dies from this
    signal" when there is no callable to chain to. Idempotent and safe to call more
    than once (e.g. a test process invoking `main()` repeatedly in-process) — each call
    simply re-registers the same handler for that signal (the previous-handler capture
    only happens on the FIRST call, so re-installing never chains to itself). Must run
    on the main thread (`signal.signal` requirement); `main()` in `cli.py` is always
    the process's main thread.
    """
    global _signal_reaper_installed
    with _SIGNAL_REAPER_LOCK:
        if _signal_reaper_installed:
            return
        _signal_reaper_installed = True

    prev_handlers: dict[int, object] = {
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
        signal.SIGINT: signal.getsignal(signal.SIGINT),
    }

    def _handler(signum: int, frame: object) -> None:
        try:
            kill_live_children()
        finally:
            prev = prev_handlers.get(signum)
            if callable(prev):
                # Preserve whatever the previous disposition actually DID (e.g. Python's
                # own `default_int_handler` raising KeyboardInterrupt for SIGINT) — the
                # reap above just runs first.
                prev(signum, frame)
            else:
                # No callable to chain to (SIGTERM's normal SIG_DFL starting point, or an
                # explicit SIG_IGN): the only way to make the process actually die FROM
                # this signal (correct 128+signum exit status) is to restore the default
                # disposition and re-deliver it to ourselves.
                signal.signal(signum, signal.SIG_DFL)
                os.kill(os.getpid(), signum)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


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


# Git env vars that PIN git to a specific repository regardless of the `cwd` / `-C` it is
# invoked with. Git itself exports these into hook/alias contexts (a pre-commit hook spawning
# `review` inherits a GIT_DIR/GIT_INDEX_FILE pointing at the COMMITTING repo), and a stale
# shell export carries them into any later `review`. With one set, `git -C /repoB diff
# --cached` silently reads the env's repo (an UNRELATED worktree), not repoB — so a git call
# anchored to the review's `-C` repo must drop these when they belong to a DIFFERENT repo
# (review-cli#71).
_GIT_REPO_ENV_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)


def _resolve_git_dir(cwd: Path) -> Path | None:
    """The absolute `.git` dir of the repo at `cwd`, resolved with EVERY git-repo env var
    stripped so a leaked GIT_DIR can't answer for `cwd`. None when `cwd` is not a git repo
    (or git is missing / wedged) — the caller then treats any set env var as foreign."""
    stripped = {k: v for k, v in os.environ.items() if k not in _GIT_REPO_ENV_VARS}
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--absolute-git-dir"],
            cwd=str(cwd),
            env=stripped,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return Path(proc.stdout.strip()).resolve()
    except OSError:
        return None


def git_repo_env(cwd: Path) -> dict[str, str]:
    """The current environment with the repo-pinning git vars dropped IFF they point at a repo
    OTHER than the one at `cwd` — so a git call anchored to `cwd`/`-C` targets that repo, not a
    foreign repo leaked through the environment (review-cli#71).

    The subtlety (codex P2 on PR #72): a LEGITIMATE pre-commit hook of the TARGET repo sets
    GIT_DIR/GIT_INDEX_FILE pointing at THAT repo — and for a PARTIAL commit (`git commit
    <pathspec>`) GIT_INDEX_FILE is a temporary `next-index` that scopes `git diff --cached` to
    only the files being committed. Stripping that unconditionally would (a) widen the review to
    files not in the commit and (b) break the stamp-hash match against the hook's own index. So
    only FOREIGN env vars (resolving outside `cwd`'s git dir) are dropped; the target repo's own
    hook env is preserved. When `cwd` is not a repo, every set var is treated as foreign and
    dropped (a leak must not divert a non-repo target). Returns a fresh dict; never mutates
    os.environ."""
    target_git_dir = _resolve_git_dir(cwd)
    env = dict(os.environ)
    for var in _GIT_REPO_ENV_VARS:
        raw = env.get(var)
        if raw is None:
            continue
        if target_git_dir is None or not _path_belongs_to(raw, target_git_dir):
            del env[var]
    return env


def _path_belongs_to(raw: str, git_dir: Path) -> bool:
    """True iff the env-var path `raw` is inside (or equal to) the target repo's `.git` dir or
    its parent work tree — i.e. it belongs to the target repo, not a foreign one. The work-tree
    parent is included so GIT_WORK_TREE (the checkout, the .git's parent) also counts as
    belonging. A path that does not resolve (e.g. a temp lock already gone) is treated as
    foreign (dropped) — conservatively favouring correctness of the anchored target."""
    try:
        p = Path(raw).resolve()
    except OSError:
        return False
    work_tree = git_dir.parent
    for anchor in (git_dir, work_tree):
        if p == anchor:
            return True
        if anchor in p.parents:
            return True
    return False


def probe_writable_dir(path: Path) -> bool:
    """True iff a real file can be created and removed under `path` right now — the
    only reliable writability test. Existence, or `mkdir(..., exist_ok=True)`
    succeeding, says NOTHING about permission on a directory that already existed: a
    leftover from an earlier unsandboxed run (or a stale root-owned directory) passes
    both silently while every actual write inside it still fails (codex review,
    review-cli#162 follow-up — this is the one shared choke point every "is this
    standard location usable" decision in this module and `jobs.py` funnels through,
    so the check is never duplicated ad hoc at each call site)."""
    probe = path / f".write-probe-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        fd = os.open(str(probe), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    except OSError:
        return False
    try:
        probe.unlink()
    except OSError:
        pass
    return True


# Memoizes the LAST-tier (`tempfile.mkdtemp()`) branch of `_fallback_log_dir` for the
# life of this process. `mkdtemp()` mints a brand-new, uniquely-named directory on
# EVERY call by design — without caching, each of the many `log_dir()` calls a single
# `review` invocation makes (one per backend round) would land in a DIFFERENT
# directory, scattering one run's logs across several temp dirs with no way to find
# them all (codex review, review-cli#162 follow-up). The FIXED uid-keyed first tier
# needs no such cache — it already returns the same path every call by construction.
_fallback_log_dir_cache: Path | None = None


def _fallback_log_dir() -> Path:
    """A guaranteed-writable fallback used when the OS-standard log location can't be
    created (review-cli#162): a SANDBOXED caller (an agent harness's restricted Bash
    tool, a locked-down CI runner) commonly grants writes only under the system temp
    dir, not under `~/Library/Logs` or `$XDG_STATE_HOME` — those live outside most
    sandbox profiles' default allow-list. Under `tempfile.gettempdir()`, keyed by uid so
    a shared multi-user box doesn't collide different users into one directory.

    Two-tier: the FIXED uid-keyed path is preferred (stable across calls in the same
    run — an external `tail -f` can follow it), but if it can't be created OR — same
    "exists but not actually writable" trap `probe_writable_dir` exists for — already
    exists as a stale, unwritable leftover, `tempfile.mkdtemp()` gets a brand-new,
    guaranteed-unique directory under the same temp root instead (codex review,
    review-cli#162 follow-up: the mkdir-only version silently kept returning the stale
    unwritable path, and a separate version that only caught mkdir's own OSError still
    raised uncaught when `_open_log_with_fallback`'s open of a file inside it failed
    for a REASON OTHER than the standard PermissionError). The mkdtemp branch is
    memoized in `_fallback_log_dir_cache` (see its docstring) so repeat calls within
    the same process converge on ONE directory instead of scattering across many."""
    global _fallback_log_dir_cache
    if _fallback_log_dir_cache is not None and _fallback_log_dir_cache.is_dir():
        return _fallback_log_dir_cache

    import tempfile

    base = Path(tempfile.gettempdir()) / f"review-cli-logs-{os.getuid()}"
    try:
        base.mkdir(parents=True, exist_ok=True)
        if not probe_writable_dir(base):
            raise OSError(f"{base} exists but is not writable")
    except OSError:
        base = Path(tempfile.mkdtemp(prefix="review-cli-logs-"))
        _fallback_log_dir_cache = base
    try:
        base.chmod(0o700)
    except OSError:
        pass
    return base


def log_dir() -> Path:
    """Predictable dir for per-call live logs (and the brainstorm discussion log)
    that an external `tail -f` can watch.

    Honors $REVIEW_LOG_DIR (tests), else the OS-standard per-user log location:
      macOS → ~/Library/Logs/review-cli
      Linux/other → $XDG_STATE_HOME/review-cli/logs  (default ~/.local/state/...)
    Logs are state, not throwaway cache, so on Linux they live under XDG_STATE_HOME
    rather than the cache dir. Created private (0700) because logs persist reviewed
    prompts/diffs (possibly secrets).

    If the standard location can't be CREATED (review-cli#162: a sandboxed caller
    denies writes outside its allowed roots — observed live as a Fable/claude-p seat
    dying with a raw `PermissionError`/"Operation not permitted" from deep inside
    `_run_streamed`, since `_open_log` calls this before the backend subprocess is even
    spawned), fall back to a writable temp dir (`_fallback_log_dir`) with a loud
    stderr line, rather than letting the whole seat crash over an inability to persist
    its OWN transcript log — a nice-to-have artifact, not the review itself. Every
    seat hits the identical code path here regardless of backend (opus/codex/fable all
    call `_open_log` -> `log_dir`), so this is not actually fable-specific; fable is
    simply the slowest seat (commonly ~15 minutes) and so the most likely to still be
    running when a time-scoped sandbox grant (if that is the trigger) has narrowed.
    """
    override = os.environ.get("REVIEW_LOG_DIR")
    if override:
        base = Path(override)
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Logs" / "review-cli"
    else:
        state = os.environ.get("XDG_STATE_HOME", "").strip()
        # XDG spec: a relative $XDG_STATE_HOME must be ignored.
        root = (
            Path(state)
            if state and os.path.isabs(state)
            else (Path.home() / ".local" / "state")
        )
        base = root / "review-cli" / "logs"
    reason: OSError | None = None
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        reason = exc
    # Mirrors `jobs.jobs_dir()`'s identical fix (codex review, review-cli#162
    # follow-up): `mkdir(..., exist_ok=True)` succeeding only means the directory
    # EXISTS, not that THIS caller can write to it. Without this probe, `log_dir()`
    # reported the standard location as usable while `_open_log_with_fallback()`'s own
    # per-file open then (correctly) fell back elsewhere — the two disagreed on where
    # a log actually lands, so a consumer that lists logs via `log_dir()` (the
    # dashboard, `review sessions`) never finds the ones a fallback-affected run
    # actually wrote.
    if reason is None and not probe_writable_dir(base):
        reason = OSError(f"{base} exists but is not writable")
    if reason is not None:
        print(
            f"[review-cli] cannot create/write log dir {base} ({reason}) — falling "
            "back to a temp dir. This is usually a SANDBOXED caller denying writes "
            "outside its allowed roots; disable the sandbox for the review call, or "
            "set $REVIEW_LOG_DIR to a path the sandbox allows, to use the real "
            "location.",
            file=sys.stderr,
            flush=True,
        )
        try:
            return _fallback_log_dir()
        except OSError as fallback_exc:
            # The genuinely-extreme case (codex review, review-cli#162 follow-up:
            # observed live during this very review run): the system temp root
            # itself is unusable, so even `_fallback_log_dir()`'s own two-tier
            # fallback raises. `log_dir()` must still return SOME path rather than
            # propagate — callers upstream of `_open_log_with_fallback` (which has
            # its own devnull last resort for the actual FILE open) resolve a path
            # from this function directly and have no other safety net. The raw
            # temp root, unadorned, is the last thing left to try.
            print(
                f"[review-cli] the temp-dir fallback also failed ({fallback_exc}) — "
                "using the raw system temp root as an absolute last resort.",
                file=sys.stderr,
                flush=True,
            )
            import tempfile

            return Path(tempfile.gettempdir())
    try:
        base.chmod(0o700)
    except OSError:
        pass
    return base


# Default open flags for a per-call transcript log: write/create/truncate. Callers that
# need the fd to also be READABLE (e.g. `cli._spawn_detached_job`'s spooled-stdin file,
# which is written once then handed to the child as its stdin) pass O_RDWR instead —
# see `_open_log_with_fallback`'s `flags` parameter.
_LOG_OPEN_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_TRUNC


def _open_log_with_fallback(
    path: Path, flags: int = _LOG_OPEN_FLAGS
) -> tuple[int, Path]:
    """Open `path` (0600, private — logs persist prompts/diffs that may carry secrets),
    retrying under `_fallback_log_dir()` with the SAME filename if the open itself
    fails, and as a last resort opening `os.devnull` if even the fallback directory
    can't be written to (review-cli#162).

    `log_dir()` already falls back when the DIRECTORY can't be created, but a sandboxed
    caller can just as easily deny the FILE open even when the directory already exists
    (e.g. `~/Library/Logs/review-cli` persists from earlier unsandboxed runs — no mkdir
    is ever attempted, so `log_dir()`'s own fallback never triggers — yet a per-file
    open still hits the sandbox's write deny-list). Every raw `os.open(...)` call in
    this module for a PER-CALL log file goes through this helper so the fallback is
    consistent regardless of which one fails; `write_retry_log`'s open is exempt
    because it is already wrapped in its own best-effort `try/except OSError` at the
    call site (a lost retry-event log must never break the review either way).

    The `os.devnull` tier exists because `_fallback_log_dir()` itself can — in a
    genuinely broken environment (the whole system temp root revoked) — still be
    unwritable; the ONE hard invariant this helper exists to guarantee is that a
    seat's log-open can NEVER raise and kill it before the backend subprocess is even
    spawned (codex review, review-cli#162 follow-up: the earlier version let that
    second-level OSError escape uncaught). `os.devnull` always exists and is opened
    for write by any user on every platform this ships to.

    Returns the open fd AND the path actually used, so a caller that reports the log
    path to the user (or returns it, like `write_sidecar_log`) reports the real one."""
    try:
        fd = os.open(str(path), flags, 0o600)
        return fd, path
    except OSError as exc:
        print(
            f"[review-cli] cannot open log file {path} ({exc}) — falling back to a "
            "temp dir. This is usually a SANDBOXED caller denying writes outside its "
            "allowed roots; disable the sandbox for the review call, or set "
            "$REVIEW_LOG_DIR to a path the sandbox allows, to use the real location.",
            file=sys.stderr,
            flush=True,
        )
    try:
        fallback_path = _fallback_log_dir() / path.name
        fd = os.open(str(fallback_path), flags, 0o600)
        return fd, fallback_path
    except OSError as exc:
        print(
            f"[review-cli] cannot open a fallback log file ({exc}) — giving up on "
            "persisting this log; using os.devnull so the run still proceeds.",
            file=sys.stderr,
            flush=True,
        )
        devnull_path = Path(os.devnull)
        fd = os.open(str(devnull_path), flags)
        return fd, devnull_path


def _safe_backend(backend: str) -> str:
    return (
        "".join(c if (c.isalnum() or c in "-_.") else "_" for c in backend) or "backend"
    )


def current_task_code() -> str | None:
    """Task code currently attached to backend logs, if one is active."""
    try:
        return normalize_task_code(os.environ.get("REVIEW_TASK_CODE"))
    except ValueError:
        return None


def _task_header_suffix() -> str:
    code = current_task_code()
    return f" task={code}" if code else ""


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
    fd, path = _open_log_with_fallback(path)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(
            f"[review-cli] {_safe_log_header(backend)}: {_safe_log_header(argv0)} "
            f"(args redacted){_task_header_suffix()}\n"
        )
        if stdout:
            fh.write(stdout if stdout.endswith("\n") else stdout + "\n")
        for line in stderr.splitlines():
            fh.write("[stderr] " + line + "\n")
        if timed_out:
            secs = timeout_secs if timeout_secs is not None else 0
            fh.write(f"[review-cli] TIMEOUT after {secs}s — partial output above]\n")
        fh.write(_exit_line(returncode))
    return path


# How much of the failing error channel a retry/promotion log records. Enough to see the
# provider's throttle string ("429 Too Many Requests", "529 overloaded") without persisting a
# whole partial body (which may carry prompt/diff fragments). Logs are 0600 regardless.
_RETRY_LOG_DETAIL_MAX = 2000

# A process-wide monotonic counter that disambiguates retry-log filenames. The microsecond
# stamp alone can collide when two events for the SAME model land in the same microsecond —
# possible under a zero/near-zero backoff (tests, or a future 0-delay config) where one
# O_TRUNC open would overwrite the other. Appending this counter (lock-guarded) guarantees a
# unique filename per event regardless of clock resolution.
_RETRY_LOG_SEQ_LOCK = threading.Lock()
_retry_log_seq = 0


def _next_retry_log_seq() -> int:
    global _retry_log_seq
    with _RETRY_LOG_SEQ_LOCK:
        _retry_log_seq += 1
        return _retry_log_seq


def write_retry_log(
    model: str,
    *,
    kind: str,
    attempt: int,
    max_attempts: int,
    delay: float,
    result,
) -> Path:
    """Durably record ONE in-seat retry / seat-fatal / reserve-promotion event.

    The board's in-seat retry (`reviewlib.retry`) and reserve-replace failover must leave a
    DURABLE trail — not stderr-only, which is lost the moment the terminal scrolls or a CI
    step discards it. This writes a small ``{stamp}-{backend}-retry.log`` next to the per-call
    logs under ``log_dir()`` so a post-mortem (or the dashboard) can reconstruct exactly how
    many times a seat was retried, why, and whether a reserve was promoted.

    ``kind`` is one of ``retry`` (a transient failure is about to be retried), ``seat-fatal``
    (a non-retryable failure went straight to the reserve), or ``promote`` (a reserve seat was
    pulled up to backfill a failed pool seat). ``attempt``/``max_attempts`` are the 1-based
    retry index and the total attempt budget; ``delay`` is the backoff slept (0 for a
    non-retry event). The failing ``result`` contributes its exit code + a TRIMMED error
    channel. File perms 0600 (the channel may echo a fragment of a reviewed prompt/diff).

    Returns the log path. Best-effort: any OS error is swallowed (a log we couldn't write must
    never break the review) — durability is a goal, not a gate."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    # The seq discriminator makes the filename unique even when two events for the same model
    # share a microsecond stamp (zero-backoff path) — without it the second O_TRUNC open would
    # clobber the first event's log.
    seq = _next_retry_log_seq()
    path = log_dir() / f"{stamp}-{_safe_backend(model)}-retry-{seq:04d}.log"
    rc = getattr(result, "returncode", "?")
    detail = (
        getattr(result, "stderr", "") or getattr(result, "stdout", "") or ""
    ).strip()
    detail = detail[:_RETRY_LOG_DETAIL_MAX]
    # The attempt fraction is only meaningful for a `retry` event (the Nth of `budget`
    # retries). A `promote` / `seat-fatal` event has no retry index, so it omits the fraction
    # rather than printing a meaningless `0/0`.
    attempt_field = f" attempt={attempt}/{max_attempts - 1}" if kind == "retry" else ""
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(
                f"[review-cli] RETRY-EVENT kind={kind} model={model}"
                f"{attempt_field} delay={delay:.2f}s exit={rc}\n"
            )
            for line in detail.splitlines():
                fh.write("[detail] " + line + "\n")
    except OSError as exc:  # noqa: BLE001 - a log we can't write must not break the review
        print(
            f"[review-cli] could not write retry log ({exc})",
            file=sys.stderr,
            flush=True,
        )
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
    idle_floor: int | None = _DEFAULT_IDLE_TIMEOUT,
    timeout_mode: str = "idle",
) -> subprocess.CompletedProcess[str]:
    """Run a long backend call, streaming its output in real time.

    Unlike `_run` (subprocess.run, which blocks until exit and DROPS the buffer on
    TimeoutExpired), this:
      * drains stdout AND stderr on daemon threads, TEEing each line to an in-memory
        accumulator AND a per-call log file under log_dir(), flushed per line so
        `tail -f <path>` shows live progress as output arrives (no wait-for-exit);
      * enforces either an IDLE timeout on the PROCESS (normal review seats) or an exact
        WALL timeout for bounded surfaces such as QA/vision. On timeout it SIGTERM→SIGKILLs
        the child's whole process group, gives the readers a brief grace flush, then RETURNS
        the partial buffer plus a clear TIMEOUT marker and returncode 124 — it never raises
        the buffer away. Idle mode treats stdout/stderr as progress, including output from
        descendants that inherited the pipe; if idle reap is disabled, the runner falls back
        to the requested wall-clock timeout rather than waiting forever.

    Returns a CompletedProcess-compatible object (.returncode/.stdout/.stderr) so
    callers like review_codex need no structural change.
    """
    path = _open_log(backend, round_no)
    # Open (with the review-cli#162 fallback) BEFORE the announce line below, so a
    # fallen-back path is what gets announced/logged everywhere else in this call —
    # never the pre-fallback path the caller can't actually `tail -f`.
    fd, path = _open_log_with_fallback(path)
    if announce:
        print(
            f"[review-cli] {backend} live log: {path} (tail -f to follow)",
            file=sys.stderr,
            flush=True,
        )

    out_buf: list[str] = []
    err_buf: list[str] = []
    log_lock = threading.Lock()
    stopping = threading.Event()
    timed_out = False
    if timeout_mode not in {"idle", "wall"}:
        raise ValueError(f"unknown timeout_mode: {timeout_mode}")
    idle_timeout = (
        idle_timeout_seconds(timeout, idle_floor=idle_floor)
        if timeout_mode == "idle"
        else None
    )
    timeout_secs = max(int(timeout), 1)
    timeout_marker_secs = timeout_secs
    timeout_marker_kind = (
        "without output" if timeout_mode == "idle" else "total runtime"
    )
    activity = {"last": time.monotonic()}
    proc: subprocess.Popen | None = None
    pgid: int | None = None
    child_handle: tuple[subprocess.Popen, int | None] | None = None
    # The process-wide spawn semaphore (review-cli#65): acquired BEFORE Popen so the count of
    # concurrent heavy backend children is bounded, released in the finally once this child is
    # reaped. None when the cap is disabled. `sem_acquired` guards a double-release on a path
    # that raised between acquire and Popen.
    concurrency_sem = _get_concurrency_sem()
    sem_acquired = False
    # Last char written to the log, so the trailing `EXIT {code}` footer can be put on
    # its own line even when the subprocess flushed stdout without a trailing newline
    # (codex P2: an unanchored footer is unparsable). The header ends with "\n".
    log_tail = {"nl": True}

    # `fd` (and possibly-fallen-back `path`) were already opened above, before the
    # announce line — private file perms: logs persist prompts/diffs that may contain
    # secrets.
    log_fh = os.fdopen(fd, "w", encoding="utf-8", buffering=1)  # line-buffered
    try:
        # Header records the backend + argv[0] only — NOT the full argv, which carries
        # the prompt/diff for claude/opencode and could leak secrets into the log. A
        # backend may pass `header_argv0` to record a model SELECTOR instead of the bare
        # binary path (e.g. opencode's `opencode -m <provider/model>`), so the dashboard
        # can attribute the call to its board seat — it must still contain NO prompt/diff.
        header = _safe_log_header(header_argv0 or (argv[0] if argv else "?"))

        # Acquire a concurrency slot BEFORE spawning, so the number of heavy backend
        # subprocesses in flight is capped (review-cli#65). This BLOCKS until a slot frees —
        # but only the SPAWN waits; the per-call `timeout` (proc.wait below) starts after
        # Popen, so a queued seat is never falsely timed out for waiting on the cap. A
        # disabled cap (None) makes this a no-op.
        #
        # The header is written AFTER the slot is acquired so a seat parked in the cap queue
        # is NOT logged as "started" (header but no EXIT) — which a post-mortem / the
        # dashboard would otherwise misread as a hung/running call. When the cap actually has
        # to block (a non-blocking try fails), we log a distinct WAITING marker first so the
        # queued state is visible-but-distinguishable from a live spawn (codex observability
        # finding). The common path (a slot is free) writes only the header, unchanged.
        if concurrency_sem is not None:
            if concurrency_sem.acquire(blocking=False):
                sem_acquired = True
            else:
                log_fh.write(
                    f"[review-cli] {backend}: waiting for a concurrency slot "
                    f"(cap {max_concurrency()})\n"
                )
                log_fh.flush()
                concurrency_sem.acquire()
                sem_acquired = True

        log_fh.write(
            f"[review-cli] {_safe_log_header(backend)}: {header} (args redacted){_task_header_suffix()}\n"
        )
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

        # Queueing on the concurrency cap is not backend runtime. Start the idle clock only
        # once the child exists, so a queued seat is never falsely timed out before spawn.
        activity["last"] = time.monotonic()

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
                            activity["last"] = time.monotonic()
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
        stdout_thread = threading.Thread(
            target=_drain, args=(proc.stdout, out_buf, ""), daemon=True
        )
        stderr_thread = threading.Thread(
            target=_drain, args=(proc.stderr, err_buf, "[stderr] "), daemon=True
        )
        stdin_thread.start()
        stdout_thread.start()
        stderr_thread.start()

        # Enforce the configured timeout. Review seats use silence-from-backend so long
        # agent calls such as Fable can think for ~15 minutes; QA/vision use wall time
        # because their public `--timeout` flags are cost/latency caps.
        if timeout_mode == "wall" or idle_timeout is None:
            timeout_marker_kind = "total runtime"
            try:
                proc.wait(timeout=timeout_secs)
            except subprocess.TimeoutExpired:
                timed_out = True
                timeout_marker_secs = timeout_secs
                _kill_tree(proc, pgid)
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
        else:
            while True:
                try:
                    proc.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    # CPython's GIL makes this single-float read safe enough; stale reads are
                    # harmless because the next 0.5s poll sees any newer activity.
                    if time.monotonic() - activity["last"] < idle_timeout:
                        continue
                    timed_out = True
                    timeout_marker_secs = idle_timeout
                    _kill_tree(proc, pgid)
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass
                    break

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
            returncode = (
                124  # conventional timeout exit code (overrides the kill signal)
            )

        with log_lock:
            stopping.set()  # freeze the buffers + stop late log writes
            stdout = "".join(out_buf)
            stderr = "".join(err_buf)
            if timed_out:
                marker = (
                    f"\n[review-cli] TIMEOUT after {timeout_marker_secs}s {timeout_marker_kind} "
                    "— partial output above]\n"
                )
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
        return subprocess.CompletedProcess(
            args=argv, returncode=returncode, stdout=stdout, stderr=stderr
        )
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
        # Release the concurrency slot LAST — after the child is reaped (`_kill_tree` above)
        # — so a freed slot really means a freed model subprocess, not one still resident.
        # Guarded by `sem_acquired` so an early raise (between acquire and Popen) still
        # releases exactly once, and a disabled cap never touches it (review-cli#65).
        if sem_acquired and concurrency_sem is not None:
            sem_acquired = False
            concurrency_sem.release()
        with log_lock:
            log_fh.close()
