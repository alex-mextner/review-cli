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
# (light) pool of 2 are unaffected (2 <= the cap). Overridable via $REVIEW_MAX_CONCURRENCY; <= 0
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

# A reserve seat promoted late in a board run must still get a fair shot, even when
# almost no wall-clock budget remains — otherwise a tight deadline would starve every
# backfill attempt to zero and the board would degrade for no reason. 90s is enough
# for a normal backend to at least attempt a call and fail fast if it's the one that's
# actually stuck, without letting the deadline clamp become a no-op.
_MIN_DEADLINE_CLAMPED_IDLE_FLOOR = 90

# Process-wide wall-clock deadline (a time.monotonic() timestamp) that
# idle_timeout_seconds() clamps its returned idle-silence budget against, so a board
# run bounded by an external wrapper (e.g. `timeout 900 review diff ...`) degrades
# gracefully instead of being SIGKILLed mid-reserve-promotion. Set/cleared once around
# a single run_board_with_failover call (panel.py) — mirrors that function's existing
# _suppress_autotally set/restore-in-finally shape: one board run owns this for the
# process at a time, matching how the CLI actually invokes it (one `review` command =
# one process = one board run). None (the default) preserves the pre-existing
# unclamped behaviour exactly.
_DEADLINE_LOCK = threading.Lock()
_board_deadline: float | None = None


def set_board_deadline(deadline: float | None) -> None:
    """Set (or clear, with None) the active board-run wall-clock deadline that
    idle_timeout_seconds() clamps against. See the module-level comment above
    _board_deadline for the ownership contract."""
    global _board_deadline
    with _DEADLINE_LOCK:
        _board_deadline = deadline


def _active_board_deadline() -> float | None:
    with _DEADLINE_LOCK:
        return _board_deadline


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

    When a board-run wall-clock deadline is active (set_board_deadline — normally by
    run_board_with_failover for a run wrapped in an external timeout), the returned floor
    is additionally clamped to whatever time genuinely remains before that deadline, down
    to a minimum of _MIN_DEADLINE_CLAMPED_IDLE_FLOOR seconds so a late reserve promotion
    still gets a fair shot instead of being starved to ~0. This clamp is skipped for the
    two EXPLICIT-DISABLE contracts above (idle_floor<=0, and $REVIEW_IDLE_TIMEOUT_SECONDS=0)
    — those are a caller/operator opting OUT of idle reaping entirely, and a deadline clamp
    silently reintroducing a bound would violate that contract.
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
                return _clamp_to_board_deadline(value)
    if idle_floor is None:
        computed = requested
    # Tiny timeouts are test/debug contracts. Preserve them exactly so unit tests and
    # one-off probes can still finish quickly; normal human review timeouts get the floor.
    elif requested < _SHORT_TIMEOUT_EXACT_THRESHOLD:
        computed = requested
    else:
        computed = max(requested, idle_floor)
    return _clamp_to_board_deadline(computed)


def _clamp_to_board_deadline(computed: int) -> int:
    """Clamp an already-computed idle-timeout value down to whatever wall-clock time
    remains before the active board deadline (if any), never below
    _MIN_DEADLINE_CLAMPED_IDLE_FLOOR — but NEVER above `computed` either: the floor
    protects a late reserve promotion from being starved by the DEADLINE, it must never
    override a caller's own smaller request (an explicit REVIEW_IDLE_TIMEOUT_SECONDS, or
    the exact-preserve contract for tiny timeouts) by handing back something LARGER than
    what was asked for. A elapsed/past deadline still returns the minimum floor (never
    zero/negative, so the caller gets one last real attempt), but capped at `computed`."""
    deadline = _active_board_deadline()
    if deadline is None:
        return computed
    remaining = int(deadline - time.monotonic())
    if remaining >= computed:
        return computed
    return min(computed, max(remaining, _MIN_DEADLINE_CLAMPED_IDLE_FLOOR))


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
        root = (
            Path(state)
            if state and os.path.isabs(state)
            else (Path.home() / ".local" / "state")
        )
        base = root / "review-cli" / "logs"
    base.mkdir(parents=True, exist_ok=True)
    try:
        base.chmod(0o700)
    except OSError:
        pass
    return base


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


# The `timeout_marker_kind` value stamped (via the `timeout_kind` result attribute,
# see `_run_streamed`'s return) when a call times out via the LIVENESS/stall path
# specifically — review-cli#153/#159/#179. A named constant, not a literal re-typed at
# each call site (Fable review finding): a caller like `backends._opencode_call_stalled`
# compares against THIS, so rewording the human-readable marker text can never silently
# desync detection from display.
TIMEOUT_KIND_STALL = "waiting for first output"


def _timeout_marker_text(secs: int, kind: str) -> str:
    """The exact `[review-cli] TIMEOUT ...]` line `_run_streamed` appends to `stdout`
    on a timeout, WITHOUT the leading/trailing newlines. Internal to this module —
    callers that need to tell timeout KINDS apart should read the `timeout_kind`
    attribute `_run_streamed` stamps on its result (see `TIMEOUT_KIND_STALL` above),
    never text-scrape `stdout` for this marker (a backend's own untrusted output could
    coincidentally contain the same words, and after idle/liveness clamping the exact
    embedded seconds value here may not equal what a caller originally requested)."""
    return f"[review-cli] TIMEOUT after {secs}s {kind} — partial output above]"


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
    liveness_timeout: int | None = None,
    true_silence_timeout: int | None = None,
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
      * (idle mode only) optionally enforces a SEPARATE, tighter TRUE-SILENCE timeout via
        ``true_silence_timeout``: if the child has produced literally ZERO bytes of output
        by that many seconds after spawn, it is reaped early with a distinct
        "true-silence" marker and the returned result's ``.true_silenced`` attribute set
        to True (returncode is ALSO set to 125 purely for on-screen/log visibility,
        mirroring the existing 124 idle-timeout convention — but ``.true_silenced`` is
        the only signal a caller should branch control flow on; 125 is a real exit code
        some CLIs/wrappers legitimately use on their own, so a caller must never infer
        true-silence from the bare returncode alone, round-2 review finding). This lets
        a caller distinguish "never said anything at all" (a much stronger stuck signal,
        see reviewlib.model_behavior) from "produced some output, then went idle" (the
        existing 124/idle path). Once ANY output arrives this check stops applying for
        the rest of the call; the normal idle timeout governs from then on, unchanged.
        None (the default) disables this check entirely — existing callers that don't
        pass it see no behavior change, and always get ``.true_silenced is False``.
        This check only runs in IDLE mode with idle reap enabled (``idle_timeout is
        not None``) — it is not a substitute for the idle floor, so disabling idle reap
        (``REVIEW_IDLE_TIMEOUT_SECONDS=0``) also disables true-silence detection, even
        with ``true_silence_timeout`` set; a never-talking child then runs to the full
        wall-clock ``timeout`` like any other idle-reap-disabled call (Opus review
        finding: worth knowing before relying on true-silence as an independent guard).

    Returns a CompletedProcess-compatible object (.returncode/.stdout/.stderr) so
    callers like review_codex need no structural change.
    """
    path = _open_log(backend, round_no)
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
    true_silenced = False
    liveness_clamped = False
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
    activity = {"last": time.monotonic(), "got_output": False}
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

    # Private file perms: logs persist prompts/diffs that may contain secrets.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
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

        # Deadline-dependent: computed HERE (post concurrency-slot wait, pre-spawn),
        # not at entry — the true-silence clock starts at Popen, so the board-deadline
        # clamp (review-cli#243/#228) must see time remaining AT spawn. idle_timeout is
        # still entry-computed and stale for a queued seat (review-cli#272, separately
        # tracked). Floor-bounded by _MIN_DEADLINE_CLAMPED_IDLE_FLOOR — never a
        # near-zero instant reap. Orthogonal to the round-4 "sole authority before
        # idle_timeout" branch in the poll loop below (that governs true_silence_timeout
        # vs idle_timeout; this governs true_silence_timeout vs the board's own
        # wall-clock budget).
        #
        # Open fairness gap (review-cli#256): when this clamp shrinks
        # true_silence_timeout below what a seat would've needed unclamped,
        # _record_true_silence_if_needed can't distinguish "genuinely silent" from
        # "silent only because this run was deadline-pressured" — both record the same
        # escalating cooldown. Product decision, not a mechanical fix.
        effective_true_silence_timeout = (
            _clamp_to_board_deadline(true_silence_timeout)
            if true_silence_timeout is not None
            else None
        )

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

        # Queueing on the concurrency cap is not backend runtime. Start the idle clock (and
        # the true-silence clock, if enabled) only once the child exists, so a queued seat
        # is never falsely timed out before spawn.
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
                    # codex review finding, review-cli#243 round 15 (P1): activity must
                    # be marked on RAW byte receipt, not on decoded TEXT — the
                    # incremental UTF-8 decoder buffers an incomplete multibyte
                    # sequence internally and returns "" until it completes, so a
                    # genuinely-alive child whose next chunk happens to end mid-
                    # character (rare, but not impossible: e.g. a CJK/Cyrillic
                    # character split across a 64KB read boundary) would otherwise
                    # look like zero bytes were ever received — wrongly reaped as
                    # true-silent and cooldown-benched despite having produced real
                    # output. `chunk` (raw bytes) is the correct liveness signal;
                    # `text` (decoded) is only ever used for what gets buffered/logged.
                    if chunk:
                        with log_lock:
                            if not stopping.is_set():
                                activity["last"] = time.monotonic()
                                activity["got_output"] = True
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
        #
        # A caller-supplied `liveness_timeout` must be honoured whenever it is set,
        # REGARDLESS of whether idle reaping itself is enabled (codex review finding:
        # the original version fell straight into the plain `proc.wait(timeout_secs)`
        # branch whenever idle reaping was disabled — e.g. $REVIEW_IDLE_TIMEOUT_
        # SECONDS=0 — silently ignoring `liveness_timeout` and waiting the FULL
        # per-call timeout on a zero-output stall). So the polling loop (where
        # `liveness_timeout` is actually checked) runs whenever EITHER idle reaping is
        # on OR a liveness bound was requested; only `timeout_mode == "wall"` (bounded
        # surfaces like QA/vision, which never pass `liveness_timeout`) or "neither is
        # configured" falls back to the plain wall-clock wait.
        use_polling = timeout_mode == "idle" and (
            idle_timeout is not None or liveness_timeout is not None
        )
        if not use_polling:
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
            # If idle reaping itself is disabled but a liveness bound was still
            # requested, the idle DIMENSION falls back to the wall-clock timeout
            # (matching the plain-disabled-idle behaviour above) rather than polling
            # forever on that axis. Fable review finding: this fallback must measure
            # elapsed time from a FIXED start (true wall-clock), never from
            # `activity["last"]` (silence since the last byte) — a backend that emits
            # output periodically keeps resetting `activity["last"]`, so measuring the
            # fallback that way would let it run UNBOUNDED, contradicting the very
            # "REVIEW_IDLE_TIMEOUT_SECONDS=0 must not be an unbounded wait" contract
            # `test_disabled_idle_reap_falls_back_to_wall_timeout` already pins for the
            # no-liveness case.
            idle_uses_wall_clock = idle_timeout is None
            loop_started = time.monotonic()
            effective_idle_timeout = (
                idle_timeout if idle_timeout is not None else timeout_secs
            )
            effective_idle_kind = (
                timeout_marker_kind if idle_timeout is not None else "total runtime"
            )
            # codex review finding: a smaller EFFECTIVE idle bound than
            # `liveness_timeout` would otherwise fire the ordinary idle branch first
            # (misclassified as "without output"/"total runtime", not a stall) even
            # though the call genuinely never produced a single byte — clamping here
            # guarantees a true zero-output call is ALWAYS classified as a stall
            # whenever it times out at all, regardless of which numeric bound happens
            # to be smaller.
            effective_liveness_timeout = (
                min(liveness_timeout, effective_idle_timeout)
                if liveness_timeout is not None
                else None
            )
            # Remember whether the clamp actually SHORTENED the caller's bound: a stall
            # that fired at a board-deadline-clamped window (round-2 review finding,
            # Fable) says "no output before the deadline ran out", not "no output for
            # the configured stall window" — the opencode retry loop must not read the
            # former as the quota-exhaustion signature and bench a healthy seat.
            liveness_clamped = (
                liveness_timeout is not None
                and effective_liveness_timeout < liveness_timeout
            )
            while True:
                try:
                    proc.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    # CPython's GIL makes these single-value reads safe enough; stale
                    # reads are harmless because the next 0.5s poll sees any newer state.
                    elapsed_idle = time.monotonic() - activity["last"]
                    # A backend that has NEVER emitted a byte gets a separate, much
                    # shorter bound when the caller opts in via `liveness_timeout`
                    # (review-cli#153/#159/#179: opencode's zai/glm seat hangs at 0%
                    # CPU with ZERO output on provider quota exhaustion). This does
                    # NOT change default behaviour: a backend that has produced SOME
                    # output still gets the full, generous idle window below — "no
                    # output yet" and "went quiet after producing output" are
                    # different signals, and only the former is fast-failed here.
                    # Keys off the SAME `activity["got_output"]` latch the true-silence
                    # branch below uses (round-1 review finding, Opus + Fable: buffer
                    # emptiness was a second, independent definition of "has produced
                    # output" that could drift from the latch), and inherits the same
                    # `activity["last"]`-first write ordering in `_drain` that closes
                    # the first-byte race for that branch.
                    no_output_yet = not activity["got_output"]
                    if (
                        effective_liveness_timeout is not None
                        and no_output_yet
                        and elapsed_idle >= effective_liveness_timeout
                    ):
                        timed_out = True
                        timeout_marker_secs = effective_liveness_timeout
                        timeout_marker_kind = TIMEOUT_KIND_STALL
                        _kill_tree(proc, pgid)
                        try:
                            proc.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            pass
                        break
                    # `timeout_mode == "idle"` is ALREADY structurally guaranteed here
                    # (this whole poll loop is the `else` of `if timeout_mode == "wall"
                    # or idle_timeout is None:` above, which wall mode always takes) —
                    # but two independent review rounds (codex, Fable) both misread
                    # that distant control flow as NOT enforcing it, so the condition
                    # is repeated explicitly below: cheap, always true today, and it
                    # makes the "(idle mode only)" contract locally self-evident
                    # instead of relying on a reader tracing 20+ lines upward — the
                    # exact class of thing a future refactor of the wait paths could
                    # silently break (Fable review finding, round 3).
                    if (
                        timeout_mode == "idle"
                        and effective_true_silence_timeout is not None
                        and not activity["got_output"]
                    ):
                        # Pre-first-byte with true-silence armed: true_silence_timeout
                        # is the SOLE authority until output arrives, regardless of how
                        # it compares to idle_timeout (Fable review finding, round 4:
                        # a true_silence_timeout configured >= idle_timeout — e.g. a
                        # deliberately generous per-model registry entry for a known
                        # slow starter — was silently unreachable, because the ordinary
                        # idle check below fired first once ITS smaller threshold
                        # elapsed, and this branch's own elapsed check hadn't yet
                        # crossed its larger one). Once output arrives, `got_output`
                        # flips True and this whole branch is skipped from then on —
                        # idle_timeout alone governs the rest of the call, unchanged.
                        # `liveness_timeout`'s check above already ran first and would
                        # have broken out of the loop if it tripped, so reaching here
                        # means either liveness_timeout is unset for this call or it
                        # hasn't tripped yet — the two mechanisms are independent and
                        # both get a chance to fire each iteration, whichever is
                        # SHORTER (or configured at all) wins.
                        # Uses the board-deadline-CLAMPED value (round 5), not the raw
                        # true_silence_timeout, so a nearly-expired board deadline still
                        # bounds this branch the same way it already bounds idle_timeout.
                        # codex review finding, review-cli#243 round 8: compares against
                        # activity["last"], NOT the separately-tracked spawn_time —
                        # `_drain` writes activity["last"] BEFORE activity["got_output"],
                        # so a first byte landing in this poll's final ~0.5s window moves
                        # `last` to "now" even if this thread still observes a stale
                        # got_output=False, closing the race that would otherwise reap a
                        # genuinely-alive child (and wrongly cooldown-bench the seat).
                        # Pre-first-byte, activity["last"] == the spawn timestamp exactly
                        # (nothing else updates it while got_output is False), so the
                        # genuinely-silent case is byte-for-byte unchanged.
                        if elapsed_idle >= effective_true_silence_timeout:
                            true_silenced = True
                            timeout_marker_secs = effective_true_silence_timeout
                            _kill_tree(proc, pgid)
                            try:
                                proc.wait(timeout=3)
                            except subprocess.TimeoutExpired:
                                pass
                            break
                        continue  # still within the true-silence budget
                    idle_elapsed = (
                        (time.monotonic() - loop_started)
                        if idle_uses_wall_clock
                        else elapsed_idle
                    )
                    if idle_elapsed < effective_idle_timeout:
                        continue
                    timed_out = True
                    timeout_marker_secs = effective_idle_timeout
                    timeout_marker_kind = effective_idle_kind
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
        if true_silenced:
            # A distinct code from the ordinary idle-timeout 124: true-silence means the
            # backend never produced a single byte, a much stronger "this looks dead"
            # signal than "went idle after producing something" — callers (backends.py)
            # branch on this to record a cooldown via reviewlib.model_behavior instead of
            # just treating it as one more ordinary seat failure.
            returncode = 125
        elif timed_out:
            returncode = (
                124  # conventional timeout exit code (overrides the kill signal)
            )

        with log_lock:
            stopping.set()  # freeze the buffers + stop late log writes
            stdout = "".join(out_buf)
            stderr = "".join(err_buf)
            if true_silenced:
                marker = (
                    f"\n[review-cli] TRUE-SILENCE TIMEOUT after {timeout_marker_secs}s "
                    "with zero output — treated as stuck, not thinking]\n"
                )
                stdout += marker
                try:
                    log_fh.write(marker)
                    log_fh.flush()
                    log_tail["nl"] = True
                except (ValueError, OSError):
                    pass
            elif timed_out:
                marker = f"\n{_timeout_marker_text(timeout_marker_secs, timeout_marker_kind)}\n"
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
        result = subprocess.CompletedProcess(
            args=argv, returncode=returncode, stdout=stdout, stderr=stderr
        )
        # `subprocess.CompletedProcess` has no `__slots__`, so a dynamic attribute is a
        # normal, safe way to carry WHICH timeout kind fired (if any) without a caller
        # having to text-scrape `stdout` for a marker string (codex review finding:
        # scraping for the "waiting for first output" substring is both forgeable by a
        # backend's own output text AND, after the idle/liveness clamping above, may not
        # even carry the exact seconds value a caller expected). `None` when the call
        # did not time out at all.
        result.timeout_kind = timeout_marker_kind if timed_out else None
        # True only for a STALL that fired at a bound SHORTER than the caller asked
        # for (board-deadline / idle clamp) — see `liveness_clamped` above. Consumers
        # (`backends._opencode_call_stalled`) treat such a stall as an honest bounded
        # failure, never as retry/cooldown-worthy.
        result.stall_bound_clamped = bool(
            timed_out and timeout_marker_kind == TIMEOUT_KIND_STALL and liveness_clamped
        )
        # Explicit, OUT-OF-BAND signal (round-2 review finding, codex + Fable): 125 is
        # a real exit code some CLIs/wrappers use on their own (`timeout(1)`'s "the
        # wrapper itself failed", docker run, git-bisect skip) — a child that happens
        # to genuinely exit 125 on its own, even after producing full real output,
        # must NEVER be mistaken for a true-silence reap by a caller pattern-matching
        # on `returncode == 125`. `result.true_silenced` is the only authoritative
        # signal; the returncode is kept at 125 purely for on-screen/log visibility
        # (mirroring the existing 124 idle-timeout convention), never for a caller's
        # control-flow decision.
        result.true_silenced = true_silenced
        return result
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
