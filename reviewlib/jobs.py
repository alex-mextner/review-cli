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
    spawns a CHILD process across this fallback (`cli._spawn_detached_job`) must still
    propagate the resolved path via `$REVIEW_JOBS_DIR` explicitly — this cache only
    covers calls within the SAME process."""
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
            base = Path(tempfile.mkdtemp(prefix="review-cli-jobs-"))
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


def job_path(job_id: str) -> Path:
    """`jobs_dir() / f"{job_id}.json"` — the SOLE choke point every read/write in this
    module funnels through, which is why job-id validation lives here: any caller
    (`review status`/`wait`'s CLI positional, `$REVIEW_JOB_ID`) is covered by this one
    check rather than needing to be re-validated at each call site."""
    if not _JOB_ID_RE.match(job_id):
        raise InvalidJobId(job_id)
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
    # keyed the same way but built directly from `jobs_dir()`.
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


def read_job(job_id: str) -> dict[str, Any] | None:
    """The raw recorded fields for `job_id`, or None if no such job exists, the job-id
    is malformed (`InvalidJobId` — treated the same as "doesn't exist" for a READ; see
    `job_path`), or the file is corrupt (a torn read of a job mid-write by another
    process — `_atomic_write`'s rename makes this rare, but a reader must not crash on
    it)."""
    try:
        path = job_path(job_id)
    except InvalidJobId:
        return None
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_jobs() -> list[dict[str, Any]]:
    """Every recorded job, oldest first (job ids sort chronologically — see `new_job_id`).
    Skips any record that fails to parse rather than raising, so one corrupt file can't
    hide every other job from `review jobs`."""
    out: list[dict[str, Any]] = []
    for p in sorted(jobs_dir().glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


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
