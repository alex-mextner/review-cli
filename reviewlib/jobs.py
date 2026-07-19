"""Detached background review jobs (`review <mode> --detach` / `review status` / `review jobs`).

A normal `review diff`/`quorum`/`brainstorm`/`just-ask` run blocks the calling process for
the whole review — fine for a human terminal or an unbounded background agent call, but a
hard problem for any caller with its OWN short foreground timeout (a subagent's capped
shell tool, a pre-commit hook budget, a CI step). `--detach` (wired in `cli.main`/
`cli._spawn_detached_job`) spawns a SECOND, full `review` invocation as a session-detached
background process and returns almost immediately with a job-id; this module is the small
bookkeeping layer that makes that job discoverable afterwards — `review status <job-id>`
and `review jobs` read what this module writes.

Nothing about the review itself is reimplemented here: the detached child runs the exact
same `reviewlib.cli.main` code path (backstop, the SIGTERM/SIGINT reaper from
`reviewlib.process`, the `-o` quorum-stamp output) as a synchronous run — this module only
tracks its pid/status/paths so a caller can poll instead of block.

Job records are small JSON files, one per job, under `jobs_dir()`. TWO processes DO write
the same job-id record (the spawning parent's initial + pid-only writes, and the spawned
child's own terminal-status write via `cli.main`'s finally block, or `backstop._fire` on an
internal-backstop kill) — `write_job` serializes them with a per-job `flock` (`_job_lock`)
so a read-modify-write from one process can never clobber a concurrent writer's update with
a stale in-memory copy (codex review: an unlocked read-then-write raced the child's
terminal status back to "running"). File perms are 0600 like `process.log_dir()`: a job's
`argv` can carry a `just-ask`/`quorum`/`brainstorm` prompt verbatim, the same secret-adjacent
content the per-call logs already persist.

Every read/write funnels through `job_path()`, which validates the job-id against the exact
shape `new_job_id()` produces — a job-id can arrive from OUTSIDE this module's control (a
`review status <job-id>` / `review wait <job-id>` CLI positional, or `$REVIEW_JOB_ID`
inherited by a detached child), and without this check a crafted id (an absolute path, a
`../` traversal) could make `job_path()` resolve OUTSIDE `jobs_dir()` entirely — reading or
overwriting an arbitrary file the process can reach (codex review, review-cli#160 follow-up).
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from .process import probe_writable_dir

# The exact shape `new_job_id()` produces: an 8-digit UTC date, "T", a 6-digit time, "-",
# an 8-char lowercase hex suffix. Anything else is rejected by `job_path()` before it ever
# reaches a filesystem call — the single choke point every read/write in this module goes
# through, so validating here covers every caller (CLI positional, $REVIEW_JOB_ID, internal).
_JOB_ID_RE = re.compile(r"^\d{8}T\d{6}-[0-9a-f]{8}$")


class InvalidJobId(ValueError):
    """Raised by `job_path()` for a job-id that doesn't match `new_job_id()`'s shape —
    e.g. a path-traversal attempt (`../../etc/passwd`) or an absolute path smuggled in via
    a CLI argument or `$REVIEW_JOB_ID`."""


# Default terminal status set (assigned by `cli.main`'s finally block) vs. the transient
# "running" a job is created with. "unknown-terminated" is a THIRD, reconciled-only status
# (see `job_status`): the recorded file still says "running" but the pid is gone — the
# child crashed, was SIGKILLed, or the machine rebooted, none of which get a chance to
# write a terminal status themselves.
TERMINAL_STATUSES = frozenset({"done", "failed", "unknown-terminated"})


# Memoizes the LAST-tier (`tempfile.mkdtemp()`) branch of `jobs_dir()`, mirroring
# `process._fallback_log_dir_cache` — `mkdtemp()` mints a NEW unique directory on every
# call, and `_spawn_detached_job` alone calls `jobs_dir()` several times for one job
# (result path, log path, stdin path); without this cache those calls would scatter
# across different directories within the SAME invocation (codex review, review-cli#162
# follow-up).
_jobs_dir_fallback_cache: Path | None = None


def _fallback_pointer_path() -> Path:
    """A FIXED, well-known location recording where the last-resort `mkdtemp()`
    directory actually landed — the one thing about that directory that ISN'T random.
    Lives directly under the temp root (NOT inside the unwritable fixed uid-keyed
    subdir this tier only exists because we couldn't use), keyed by uid so a shared
    multi-user box doesn't cross-report between users."""
    import tempfile

    return Path(tempfile.gettempdir()) / f".review-cli-jobs-{os.getuid()}.pointer"


def _fallback_pointer_lock_path() -> Path:
    """The lock file guarding the pointer's read-or-mint-and-record sequence — a
    SEPARATE fixed path from the pointer itself (never the pointer + a suffix swap,
    same reasoning as `_job_lock_path`: the lock must exist even before the pointer
    file itself does, for the very first process to hit this tier)."""
    import tempfile

    return Path(tempfile.gettempdir()) / f".review-cli-jobs-{os.getuid()}.pointer.lock"


def _open_pointer_lock() -> int:
    """Open (creating if needed) and take an exclusive `flock` on the pointer's own
    lock file, returning the fd for the caller to release via
    `fcntl.flock(fd, fcntl.LOCK_UN)` + `os.close(fd)`. Serializes the WHOLE
    read-pointer-or-mint-a-new-directory-and-record-it sequence across processes
    (codex review, review-cli#162 follow-up): without this, two `--detach` calls
    entering this tier at the same moment could each mint a DIFFERENT `mkdtemp()`
    directory and race the pointer write — the loser's job bookkeeping would then live
    somewhere a later `review status`/`review jobs` (which finds the pointer's WINNING
    value) can never look. `O_NOFOLLOW` for the same symlink-attack reason
    `_read_fallback_pointer`/`_write_fallback_pointer` use on the pointer file itself —
    the lock file lives at an equally predictable path.

    Verifies OWNERSHIP before ever attempting the (blocking) `flock` (codex review,
    review-cli#162 follow-up round 3): a foreign-owned but world-writable regular
    file pre-planted at this predictable path would pass the `os.open` above (perm
    bits allow it), but `flock(LOCK_EX)` could then block INDEFINITELY on whatever
    that OTHER user's process is doing with its own lock on the same file — turning
    a best-effort coordination mechanism into a hang. Raises `OSError` on a
    foreign-owned file so the caller's existing except-clause falls back to an
    uncoordinated `mkdtemp()` instead of ever calling `flock` on it."""
    path = _fallback_pointer_lock_path()
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        st = os.fstat(fd)
        if st.st_uid != os.getuid():
            raise OSError(
                f"{path} is owned by uid {st.st_uid}, not us — refusing to lock it"
            )
        fcntl.flock(fd, fcntl.LOCK_EX)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_fallback_pointer() -> Path | None:
    """The directory a PRIOR process (in this same fallback tier) recorded via
    `_write_fallback_pointer`, if it still exists, is actually writable, and — the
    hardening this function exists for (codex review, review-cli#162 follow-up) — is
    OWNED BY US and not reached through a symlink. Both the pointer FILE and the
    directory it names are checked this way: a shared multi-user temp root means
    another local user could pre-plant a symlink or a foreign-owned directory at
    these predictable paths, hoping a naive `read_text`/`is_dir` trusts it and
    redirects this process's job bookkeeping (secret-bearing argv, possibly) into
    somewhere they control. `O_NOFOLLOW` makes the open itself fail on a symlinked
    pointer file; the `st_uid` checks cover a foreign-owned regular file/directory a
    symlink isn't even needed for. Returns None (never raises) on ANY of these —
    treated identically to "no prior process recorded anything yet", so the caller
    falls through to minting its own directory rather than trusting anything
    suspicious."""
    pointer = _fallback_pointer_path()
    try:
        # O_NONBLOCK: another local user could pre-plant a FIFO (not a symlink, so
        # O_NOFOLLOW alone doesn't stop it) at this predictable path — a blocking
        # O_RDONLY open on a FIFO with no writer hangs indefinitely, turning a
        # best-effort lookup into a hang for every future `review status`/`review
        # jobs` call (codex review, review-cli#162 follow-up round 4). O_NONBLOCK
        # makes that open return immediately instead.
        fd = os.open(str(pointer), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        # Reject anything that isn't a plain regular file (a FIFO/socket/device
        # planted at this path, even one O_NONBLOCK let us open) OR isn't ours.
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            return None
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            recorded = fh.read().strip()
    except OSError:
        return None
    finally:
        # `os.fdopen` above takes ownership of `fd` and closes it on context exit —
        # but every EARLIER return path (bad fstat, wrong type/owner, or the mode
        # checks failing) left it open. Closing an already-closed fd is a silent
        # no-op in CPython (fdopen sets an internal "closed" flag), so this is safe
        # to run unconditionally rather than tracking whether fdopen ever ran (codex
        # review, review-cli#162 follow-up round 4: every early return here leaked
        # the descriptor — repeated lookups could exhaust the process's fd limit).
        try:
            os.close(fd)
        except OSError:
            pass
    if not recorded:
        return None
    candidate = Path(recorded)
    try:
        if candidate.is_symlink():
            return None
        cst = candidate.stat()
    except OSError:
        return None
    if cst.st_uid != os.getuid():
        return None
    if candidate.is_dir() and probe_writable_dir(candidate):
        return candidate
    return None


def _write_fallback_pointer(path: Path) -> None:
    """Best-effort: record the last-resort directory at the fixed pointer location so
    a LATER, independent process can find it via `_read_fallback_pointer`. A failure
    here must never break the caller — the fallback directory itself is already
    usable even if no other process ever learns where it is.

    Written via a same-directory temp file + `os.replace` (mirroring `_atomic_write`)
    rather than truncate-then-write in place, so a concurrent `_read_fallback_pointer`
    can never observe a transient EMPTY file mid-write (codex review, review-cli#162
    follow-up round 4: the previous truncate-in-place version had exactly that
    window). `O_NOFOLLOW` on the temp file's own creation refuses to write through a
    pre-existing symlink at ITS path (an unlikely but equally predictable name);
    ownership is checked on the FINAL pointer path before the atomic rename — if a
    foreign-owned regular file already sits there, this refuses to overwrite it
    rather than silently truncating another user's file (codex review, review-cli#162
    follow-up round 4: the previous version's `O_TRUNC` had no ownership check at
    all, so it would have clobbered a foreign file the permission bits merely
    allowed writing to)."""
    pointer = _fallback_pointer_path()
    try:
        existing_st = pointer.lstat()
    except OSError:
        existing_st = None
    if existing_st is not None and (
        stat.S_ISLNK(existing_st.st_mode) or existing_st.st_uid != os.getuid()
    ):
        return
    tmp = pointer.with_name(pointer.name + f".tmp{os.getpid()}")
    try:
        fd = os.open(
            str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
        )
    except OSError:
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(str(path))
        os.replace(tmp, pointer)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def jobs_dir() -> Path:
    """Predictable per-user dir for job records + their default log/result files.

    Honors $REVIEW_JOBS_DIR (tests / an isolated run), else the OS-standard per-user
    CACHE location (job records are disposable — unlike `process.log_dir()`'s per-call
    transcripts, which are durable review history, a job record is a transient sentinel
    an operator can safely delete once its result has been consumed):
      macOS -> ~/Library/Caches/review-cli/jobs
      Linux/other -> $XDG_CACHE_HOME/review-cli/jobs (default ~/.cache/...)
    Created private (0700) — see module docstring on why job records can carry secrets.

    Falls back to a writable temp dir (mirroring `process.log_dir()`'s review-cli#162
    fallback) when the standard CACHE location can't be created — the SAME class of
    sandboxed-caller denial that affects the review's own log dir applies here, and a
    `--detach` job whose OWN bookkeeping directory can't be created is a much louder,
    more confusing failure than a review that just can't persist its transcript log.
    The last-resort `mkdtemp()` branch is memoized per-process (see
    `_jobs_dir_fallback_cache`) so repeat calls converge on ONE directory; a caller that
    spawns a CHILD process across this fallback (`cli._spawn_detached_job`) also
    propagates the resolved path via `$REVIEW_JOBS_DIR` explicitly for that child. For
    any OTHER, independent process (no inherited env, e.g. a later `review status
    <job-id>`/`review jobs` invocation) this tier also checks a fixed pointer file
    (`_read_fallback_pointer`) recording where a PRIOR process in this same fallback
    tier landed, before minting its own brand-new `mkdtemp()` directory (codex review,
    review-cli#162 follow-up: without this, every cold process independently
    recomputed a different random directory and could never find a job recorded by a
    different process)."""
    global _jobs_dir_fallback_cache
    if _jobs_dir_fallback_cache is not None and _jobs_dir_fallback_cache.is_dir():
        return _jobs_dir_fallback_cache

    override = os.environ.get("REVIEW_JOBS_DIR")
    if override:
        base = Path(override)
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches" / "review-cli" / "jobs"
    else:
        cache = os.environ.get("XDG_CACHE_HOME", "").strip()
        root = (
            Path(cache) if cache and os.path.isabs(cache) else (Path.home() / ".cache")
        )
        base = root / "review-cli" / "jobs"
    reason: OSError | None = None
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        reason = exc
    # `mkdir(..., exist_ok=True)` only checks EXISTENCE, not permission — a directory
    # that already exists (e.g. from an earlier unsandboxed run) but is no longer
    # writable by this caller passes the mkdir silently, and the FIRST real failure
    # only surfaces later inside `_job_lock`'s own `os.open`, well after `jobs_dir()`
    # already reported success (codex review, review-cli#162 follow-up). Probe with an
    # actual create+delete (shared with `process.py`'s identical standard-location
    # check — see `probe_writable_dir`), the only reliable writability test.
    if reason is None and not probe_writable_dir(base):
        reason = OSError(f"{base} exists but is not writable")
    if reason is not None:
        import tempfile

        print(
            f"[review-cli] cannot create/write jobs dir {base} ({reason}) — falling "
            "back to a temp dir. This is usually a SANDBOXED caller denying writes "
            "outside its allowed roots; disable the sandbox for the review call, or "
            "set $REVIEW_JOBS_DIR to a path the sandbox allows, to use the real "
            "location.",
            file=sys.stderr,
            flush=True,
        )
        base = Path(tempfile.gettempdir()) / f"review-cli-jobs-{os.getuid()}"
        try:
            base.mkdir(parents=True, exist_ok=True)
            if not probe_writable_dir(base):
                raise OSError(f"{base} exists but is not writable")
        except OSError:
            # The WHOLE read-pointer-or-mint-and-record sequence runs under the
            # pointer's own lock (codex review, review-cli#162 follow-up): two
            # concurrent first-time fallbacks racing this unlocked would each mint a
            # DIFFERENT `mkdtemp()` directory and the last pointer write would win,
            # stranding the loser's job bookkeeping somewhere `review status`/`review
            # jobs` (which only ever looks at the pointer's final value) can never
            # find.
            try:
                lock_fd = _open_pointer_lock()
            except OSError:
                # The lock file itself lives at an equally predictable path — another
                # local user could have pre-created a symlink, a foreign-owned file,
                # or a directory there. Never let that turn into a hard failure here
                # (codex review, review-cli#162 follow-up): fall back to minting an
                # UNCOORDINATED `mkdtemp()` directory. Losing cross-process
                # coordination in this already-extreme corner case is strictly better
                # than the seat/job dying outright.
                base = Path(tempfile.mkdtemp(prefix="review-cli-jobs-"))
                _jobs_dir_fallback_cache = base
            else:
                try:
                    pointed = _read_fallback_pointer()
                    if pointed is not None:
                        base = pointed
                    else:
                        base = Path(tempfile.mkdtemp(prefix="review-cli-jobs-"))
                        _write_fallback_pointer(base)
                finally:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    finally:
                        os.close(lock_fd)
                _jobs_dir_fallback_cache = base
    try:
        base.chmod(0o700)
    except OSError:
        pass
    return base


def new_job_id() -> str:
    """A sortable, collision-safe job id: a UTC timestamp prefix (so `review jobs`'s
    default filename-sort is also a start-time sort) plus a short random suffix (so two
    jobs started in the same second never collide)."""
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def _validate_job_id(job_id: str) -> None:
    """Raise `InvalidJobId` for anything that isn't `new_job_id()`'s exact shape — a
    path-traversal attempt (`../../etc/passwd`) or an absolute path (`/tmp/x`)
    smuggled in via a CLI positional or `$REVIEW_JOB_ID`. Called FIRST, before either
    `job_path()` OR `_job_lock_path()` touches the filesystem (codex review,
    review-cli#162 follow-up): the lock path used to be built and OPENED before this
    check ran, so a malformed id could create/lock an arbitrary `<id>.lock` file
    outside `jobs_dir()` before `InvalidJobId` was ever raised. Both path-builders
    below call this before constructing anything, so there is exactly one place a
    malformed id can slip through, not two independently-guarded ones that could
    drift out of sync."""
    if not _JOB_ID_RE.match(job_id):
        raise InvalidJobId(job_id)


def job_path(job_id: str) -> Path:
    """`jobs_dir() / f"{job_id}.json"` — the SOLE choke point every RECORD read/write
    in this module funnels through, which is why job-id validation (`_validate_job_id`)
    runs here: any caller (`review status`/`wait`'s CLI positional, `$REVIEW_JOB_ID`)
    is covered by this one check rather than needing to be re-validated at each call
    site."""
    _validate_job_id(job_id)
    return jobs_dir() / f"{job_id}.json"


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    """Write via a same-directory temp file + `os.replace` so a reader never observes a
    half-written record (a plain truncate-then-write can race `read_job`/`list_jobs`)."""
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _job_lock_path(job_id: str) -> Path:
    # Deliberately NOT `job_path()` + a suffix swap — the lock must exist even for a
    # job-id that has no `.json` record YET (the parent's very first write), so it is
    # keyed the same way but built directly from `jobs_dir()`. Still runs the SAME
    # `_validate_job_id` check `job_path()` does, and runs it FIRST, before anything
    # below touches the filesystem (codex review, review-cli#162 follow-up: this used
    # to be unvalidated, so `_job_lock()` could `os.open()` a lock file built from a
    # malformed id — e.g. an absolute path — OUTSIDE `jobs_dir()` entirely, before
    # `write_job()`'s later `job_path()` call ever got a chance to raise).
    _validate_job_id(job_id)
    return jobs_dir() / f"{job_id}.lock"


def _job_lock(job_id: str) -> int:
    """Open (creating if needed) and take an exclusive `flock` on this job's lock file,
    returning the fd for the caller to release via `fcntl.flock(fd, fcntl.LOCK_UN)` +
    `os.close(fd)`. Serializes `write_job` across PROCESSES for the same job-id — the
    spawning parent's initial + pid-only writes, and the spawned child's own terminal-
    status write (or `backstop._fire`'s, on an internal-backstop kill), are genuinely
    concurrent writers, and an unlocked read-modify-write can lose one's update to a
    stale in-memory copy of the other's (codex review). `flock` (not a `fcntl.lockf`
    byte-range lock) because the whole tiny file is always the one critical section —
    no need for byte-range granularity here."""
    path = _job_lock_path(job_id)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def write_job(job_id: str, **fields: Any) -> Path:
    """Create-or-update a job record, merging `fields` over whatever is already on disk
    (so the terminal-status writer only needs to pass the fields it knows: status,
    exit_code, finished_at — not repeat the spawner's argv/pid/log_path). The whole
    read-modify-write is done under `_job_lock` so a concurrent writer for the SAME
    job-id (see `_job_lock`'s docstring) can never race this one."""
    lock_fd = _job_lock(job_id)
    try:
        path = job_path(job_id)
        data = read_job(job_id) or {}
        data["job_id"] = job_id
        data.update(fields)
        data["updated_at"] = time.time()
        _atomic_write(path, data)
        return path
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def _secondary_jobs_dir() -> Path | None:
    """The pointer-recorded fallback jobs dir, if one is on record and it differs from
    what `jobs_dir()` resolves to for THIS call — or None. Read-only: unlike
    `jobs_dir()`'s own fallback branch, this never mints or writes anything, it only
    checks whether a POSSIBLY DIFFERENT process left something behind at the pointer.

    This exists because a job's bookkeeping and a LATER read of it can legitimately
    resolve `jobs_dir()` to two different directories even in the shipped call graph
    (codex review, review-cli#162 follow-up): if the standard CACHE location becomes
    writable again BETWEEN a `--detach` spawn (which hit the mkdtemp fallback tier and
    recorded a pointer) and a later `review status`/`review jobs` call (which no
    longer hits that tier at all, since the standard location now succeeds) — e.g. a
    sandbox grant that widened, or a permissions fix applied mid-session — the later
    call's own `jobs_dir()` would return the STANDARD location and never even look at
    the pointer, silently reporting the job as `unknown`/absent even though its record
    still exists, untouched, in the fallback dir from before. Checking this secondary
    location on every miss closes that gap without requiring the two calls' failure
    modes to match.

    Skips secondary discovery ENTIRELY when `$REVIEW_JOBS_DIR` is set (codex review,
    review-cli#162 follow-up round 2): an explicit override is the caller declaring
    "use exactly this directory, and no other" — tests rely on this for isolation
    (every test in this suite sets it to a throwaway tmp dir), and a real caller that
    sets it is doing so ON PURPOSE (a sandboxed CI runner pinning a known-writable
    path). Silently also consulting the machine-wide pointer would leak unrelated
    records (including another run's `argv`, which can carry prompts) across that
    boundary. This narrows the scope of this mitigation to the DEFAULT (no override)
    case — the same one the original bug report actually described; a fuller fix
    (tracking every historical fallback directory, not just the single most-recent
    pointer, and distinguishing "readable" from "writable" when validating a
    candidate) is a genuine architecture question tracked in review-cli#163, not
    patched in here."""
    if os.environ.get("REVIEW_JOBS_DIR"):
        return None
    primary = jobs_dir()
    pointed = _read_fallback_pointer()
    if pointed is None or pointed == primary:
        return None
    return pointed


def read_job(job_id: str) -> dict[str, Any] | None:
    """The raw recorded fields for `job_id`, or None if no such job exists, the job-id
    is malformed (`InvalidJobId` — treated the same as "doesn't exist" for a READ; see
    `job_path`), or the file is corrupt (a torn read of a job mid-write by another
    process — `_atomic_write`'s rename makes this rare, but a reader must not crash on
    it). Falls through to `_secondary_jobs_dir()` on a miss in the primary location —
    see its docstring for why the SAME job-id lookup can legitimately need to check
    two different directories."""
    try:
        path = job_path(job_id)
    except InvalidJobId:
        return None
    if not path.exists():
        secondary = _secondary_jobs_dir()
        if secondary is None:
            return None
        path = secondary / f"{job_id}.json"
        if not path.exists():
            return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_jobs() -> list[dict[str, Any]]:
    """Every recorded job, oldest first (job ids sort chronologically — see `new_job_id`).
    Skips any record that fails to parse rather than raising, so one corrupt file can't
    hide every other job from `review jobs`. Merges in `_secondary_jobs_dir()`'s
    records too (deduped by job_id, primary wins a collision) — see its docstring."""
    seen: dict[str, dict[str, Any]] = {}
    dirs = [jobs_dir()]
    secondary = _secondary_jobs_dir()
    if secondary is not None:
        dirs.append(secondary)
    for d in dirs:
        for p in sorted(d.glob("*.json")):
            job_id = p.stem
            if job_id in seen:
                continue
            try:
                seen[job_id] = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
    return [seen[k] for k in sorted(seen)]


def is_pid_alive(pid: int) -> bool:
    """True iff `pid` names a live process THIS user can at least signal-probe.
    `kill(pid, 0)` sends no signal — it only checks existence/permission."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def job_status(job_id: str) -> dict[str, Any] | None:
    """The job's record, RECONCILED against reality: a record still marked "running"
    whose pid is no longer alive is relabeled "unknown-terminated" (crash / SIGKILL /
    reboot — none of which give the child a chance to write its own terminal status via
    `cli.main`'s finally block). Every other status is returned as recorded; None if the
    job doesn't exist."""
    data = read_job(job_id)
    if data is None:
        return None
    if data.get("status") == "running":
        pid = data.get("pid")
        alive = isinstance(pid, int) and is_pid_alive(pid)
        if not alive:
            data = dict(data)
            data["status"] = "unknown-terminated"
    return data


def tail_lines(path: Path, n: int) -> list[str]:
    """The last `n` lines of a log file, best-effort (missing/unreadable file -> [])."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    return lines[-n:] if n > 0 else []
