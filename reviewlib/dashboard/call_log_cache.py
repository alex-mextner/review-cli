"""Persistent, file-identity-keyed cache for parsed ``CallLog`` objects.

WHY: ``parse_call_log`` reads and fully parses each call-log file's content
(including the full ``body`` text every downstream stats/paywall/skill-md check
needs). On a long-lived install the log directory grows to tens of thousands of
files (2026-08 investigation: ~94k recorded calls), and every ``review stat``
invocation or dashboard cold start re-parses ALL of them from scratch — the
documented cause of ``review stat --days 0`` timing out and the dashboard's
default view rendering blank before the parse finishes.

Call-log FILENAMES are stable once written (the timestamp/backend/round in the name
never changes), though a streaming call's file content still grows while it runs —
the cache validates on (mtime, size), so an in-progress file simply keeps missing
the cache until it stops changing, never serves a stale mid-stream read.

DESIGN, and why it isn't "just pickle a dict":
  - Storage is SQLite, one row per file, keyed by filename — a bounded (e.g. 7-day)
    scan does one indexed SELECT per file it actually needs, never loads entries for
    the other tens of thousands of files it doesn't care about. A single "pickle the
    whole cache" blob was tried first and rejected: it made every lookup pay to
    deserialize the ENTIRE history (review-cli#317 review, round 1, P1).
  - Values are JSON, not pickle: a `.log` directory can be pointed at by
    `$REVIEW_LOG_DIR`, and `pickle.load` on a file living in a directory an attacker
    can influence is arbitrary code execution. `CallLog` is a plain dataclass of
    str/int/bool/list[str]/datetime — no reducers, no code execution surface.
  - One connection PER DIRECTORY, SHARED across every thread that reaches it
    (`check_same_thread=False`), with every use of that connection serialized behind
    ONE `threading.Lock` PER DIRECTORY -- the dashboard is a `ThreadingHTTPServer` (a
    new thread per request) plus a background-refresh daemon thread plus a prewarm
    thread, all reaching this module. The FIRST design created the connection with
    `check_same_thread=True` (sqlite3's own default) -- every thread OTHER than the one
    that created it then raised `sqlite3.ProgrammingError` on use, which is a subclass
    of `sqlite3.Error` and was silently swallowed by this module's own best-effort
    handlers, making the cache 100% inert on every thread but the first (review-cli#317
    review, round 2, GLM+k3, both independently). The SECOND design tried to detect
    whether sharing was safe via `sqlite3.threadsafety == 3`, then refused to cache at
    all when it couldn't confirm SERIALIZED -- but that constant was hardcoded to `1`
    on EVERY CPython version through 3.12 regardless of the actual SQLite build (it
    only became build-derived in 3.13), so on this repo's CI matrix (3.10-3.13) the
    cache was silently inert on three of the four tested interpreters, and several of
    this module's own tests (written assuming an active cache) failed outright on them
    (review-cli#317 review, round 5, GLM+k3, both independently -- k3 flagged it
    CI-breaking). Locking sidesteps the whole question: this module NEVER lets two
    threads touch the connection concurrently regardless of what threading mode the
    linked SQLite was actually built with -- correctness doesn't depend on trusting an
    unreliable runtime signal, and the cache stays genuinely active on every supported
    Python version. WAL mode additionally handles cross-*process* concurrency (a
    separate `review stat` CLI run at the same time as the dashboard) via SQLite's own
    file locking, which the per-directory Python lock has no bearing on.
  - Autocommit (one upsert = one transaction), not one open transaction for an entire
    multi-second scan: holding a write transaction open across a whole directory walk
    means a concurrent writer (the dashboard mid-scan + a `review stat` process, or
    vice versa) blocks for the full connect `timeout` and then silently loses its
    writes, and a mid-scan crash rolls back everything parsed so far instead of
    keeping whatever was already committed (review-cli#317 review, round 2, both
    reviewers).
  - The db file is created 0600, matching every other piece of log-derived data this
    repo persists (`process.py`'s log files themselves, `stats.py`'s run-stats store,
    `dashboard/store.py`'s overseer annotations) — it holds full parsed call bodies,
    the same content those already protect.
  - `save()` prunes rows for files no longer present in the directory — a deleted
    `.log` can hold reviewed prompts/diffs (review-cli's own logs are private,
    0700/0600, for exactly this reason); the cache must not keep a permanent copy of
    a call the user believed they deleted.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import fields as _dataclass_fields
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .parser import CallLog

_CACHE_FILENAME = ".call-log-cache.sqlite3"
# `sqlite3.Error` is not the only way binding/using a filename as a SQL TEXT parameter
# can fail: a name containing lone surrogates (raw non-UTF-8 bytes decoded via
# surrogateescape -- reachable from restored backups/tar/rsync content in an
# attacker-influenceable `$REVIEW_LOG_DIR`) raises `UnicodeEncodeError`, a `ValueError`
# (review-cli#317 review, round 6, k3 finding 1). Module-scoped (not a per-call local)
# so every guarded block -- `get_or_parse`'s SELECT/INSERT and `save()`'s DELETE --
# catches the identical tuple; those two drifting apart once already let a foreign
# cache db's surrogate-named row crash `save()` uncaught (review-cli#317 review,
# round 8, GLM finding 1).
_CACHE_ERRORS = (sqlite3.Error, ValueError)
# Bump whenever `parse_call_log`'s EXTRACTION LOGIC changes in a way that would
# produce a different CallLog for an already-parsed (unchanged) file -- e.g. a fix to
# the exit-code/timeout/task-code detection. `(mtime_ns, size)` alone can't catch
# this: a finished log's file never changes again, so a pre-fix cached row would
# otherwise be served forever (review-cli#317 review, round 3, k3 finding 1).
# review-cli#326: bumped 1 -> 2 because `parse_call_log` now caps retained body/stderr
# text via `_cap_body`/`_cap_stderr_lines`, and computes `completed`/`has_error`/
# `error_summary`/`is_paywall`/`is_cf_blocked`/`is_bad_key` once from the FULL
# untruncated text (see `_classify_from_full_text` in parser.py). Without this bump a
# pre-existing cache row (keyed only on filename/mtime/size) would satisfy the
# cache-hit check and deserialize its OLD, uncapped body -- or a row missing the six
# newer fields entirely -- straight back into memory on exactly the long-lived install
# this fix targets, since finished `.log` files never change again and that row would
# otherwise never be re-parsed through the new code path (codex review finding, round
# 1; behavior proven end-to-end by
# `test_bumping_parser_version_invalidates_an_otherwise_unchanged_row` below, Fable
# review round 4 finding 4).
#
# THIS BUMP DISCIPLINE COVERS MORE THAN THE FOUR MARKER CONSTANTS documented next to
# `_PAYWALL_SENTINEL` in parser.py -- `CALL_BODY_STORE_CAP`/`_CAP_CHARS`/
# `CALL_STDERR_LINE_CAP` (also in parser.py) are baked into every persisted row's
# `body`/`stderr_lines` too. Bump `_PARSER_VERSION` alongside ANY edit to: the four
# marker constants, the two cap constants, or `_classify_from_full_text`'s own logic
# (Fable review round 4 finding 3: lowering a cap without bumping this would leave
# every cached row's retained-memory size unchanged on a warm 132k-log install, so the
# fix would silently appear not to work).
_PARSER_VERSION = 2
_CALLLOG_FIELDS: set[str] | None = None  # lazily computed -- see `_calllog_fields()`
_CALLLOG_DATETIME_FIELDS: frozenset[str] | None = (
    None  # see `_calllog_datetime_fields()`
)


def _calllog_fields() -> set[str]:
    # Local import: `parser.py` imports THIS module (to cache `load_sessions`'s own
    # `parse_call_log` calls), so a module-level `from .parser import CallLog` here
    # would be a hard circular import at load time.
    global _CALLLOG_FIELDS
    if _CALLLOG_FIELDS is None:
        from .parser import CallLog

        _CALLLOG_FIELDS = {f.name for f in _dataclass_fields(CallLog)}
    return _CALLLOG_FIELDS


def _calllog_datetime_fields() -> frozenset[str]:
    """Every `CallLog` field typed `datetime` or `datetime | None` -- derived from the
    dataclass's OWN (resolved) type hints, not a hand-kept list, so `_deserialize`
    converts a newly-added datetime field automatically instead of passing its ISO
    string straight through to the constructor (review-cli#317 review, round 7, GLM
    finding 1: `_serialize` already derives field NAMES this way; the codec side
    didn't, so a future datetime field would round-trip as a `str` on every cache hit
    and crash far away, only for files parsed before the field existed)."""
    global _CALLLOG_DATETIME_FIELDS
    if _CALLLOG_DATETIME_FIELDS is None:
        import typing

        from .parser import CallLog

        hints = typing.get_type_hints(CallLog)
        _CALLLOG_DATETIME_FIELDS = frozenset(
            name
            for name, hint in hints.items()
            if hint is datetime or datetime in typing.get_args(hint)
        )
    return _CALLLOG_DATETIME_FIELDS


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"object of type {type(value).__name__} is not JSON serializable")


def _serialize(call: "CallLog | None") -> str:
    """Raises (TypeError) on a genuinely non-serializable field -- deliberately NOT
    swallowed here, so a schema/serialization bug surfaces as a real error instead of
    silently degrading into an eternally-missing cache (review-cli#317 round 2, GLM
    finding 2). Derived from the dataclass's OWN fields (not a hand-kept override
    list) so a newly-added field is covered automatically."""
    if call is None:
        return "null"
    payload = {name: getattr(call, name) for name in _calllog_fields()}
    return json.dumps(payload, default=_json_default)


def _deserialize(raw: str) -> "CallLog | None":
    from .parser import CallLog

    payload = json.loads(raw)
    if payload is None:
        return None
    # EXACT key-set equality, not a superset check: a row that's merely MISSING a
    # newly-added field would otherwise deserialize cleanly (the dataclass fills its
    # default), match on (mtime, size) since the source file never changes again, and
    # serve that stale default forever instead of ever being re-parsed. A row with
    # extra keys is caught the same way (review-cli#317 review, round 3, GLM+k3 both).
    if not isinstance(payload, dict) or set(payload) != _calllog_fields():
        raise ValueError("cached row does not match the current CallLog shape")
    for name in _calllog_datetime_fields():
        if payload.get(name) is not None:
            payload[name] = datetime.fromisoformat(payload[name])
    return CallLog(**payload)


class _ConnEntry:
    """A connection plus the ONE lock every access to it must be taken under -- see
    the module docstring's concurrency bullet for why this replaces trusting
    `sqlite3.threadsafety` (unreliable on this repo's supported Python floor)."""

    __slots__ = ("conn", "lock")

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.lock = threading.Lock()


def _connect(directory: Path) -> sqlite3.Connection:
    db_path = directory / _CACHE_FILENAME
    # check_same_thread=False: safe because every caller in this module takes the
    # matching `_ConnEntry.lock` before touching the connection -- see module
    # docstring. autocommit (isolation_level=None): every execute() is its own
    # transaction, so a long directory scan never holds one open write lock for its
    # whole duration.
    conn = sqlite3.connect(
        str(db_path), timeout=30.0, isolation_level=None, check_same_thread=False
    )
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS entries ("
        "filename TEXT PRIMARY KEY, mtime_ns INTEGER NOT NULL, "
        "size INTEGER NOT NULL, parser_version INTEGER NOT NULL, data TEXT NOT NULL)"
    )
    # Unconditional, every connect (not just on first creation): WAL mode's `-wal`/
    # `-shm` sidecars hold the newest parsed call bodies -- the exact private content
    # this chmod exists to protect -- and are recreated at umask-default permissions
    # on every fresh open, including by a short-lived `review stat` process that opens
    # after the dashboard has checkpointed them away (review-cli#317 review, round 4,
    # GLM finding 1). Doing this every connect, not gated on `not existed`, also
    # tightens perms on a db created by an older build under a looser umask.
    for suffix in ("", "-wal", "-shm"):
        try:
            os.chmod(f"{db_path}{suffix}", 0o600)
        except OSError:
            pass  # best-effort, matches the other swallowed-chmod sites in this repo
    return conn


_registry_lock = (
    threading.Lock()
)  # guards `_connections` itself, distinct from each entry's OWN lock
_connections: dict[str, _ConnEntry | None] = {}  # None = a prior connect failed


def _conn_for(directory: Path) -> _ConnEntry | None:
    key = str(directory)
    with _registry_lock:
        if key in _connections:
            return _connections[key]  # may legitimately be None -- see below
        try:
            entry = _ConnEntry(_connect(directory))
        except sqlite3.Error:
            # Memoize the failure too (review-cli#317 round 2, GLM finding 3): without
            # this, a corrupt db or an unwritable dir costs one failed connect + a
            # swallowed exception PER FILE on the very scan this module exists to
            # speed up -- turning "cache is broken" into "slower than no cache at all".
            entry = None
        _connections[key] = entry
        return entry


def get_or_parse(
    directory: Path, path: Path, parse: Callable[[Path], "CallLog | None"]
) -> "CallLog | None":
    """Return ``parse(path)``, reusing a prior cached result for ``directory`` when
    ``path``'s (mtime, size) still match what was cached last time. Best-effort for
    genuine cache/storage failures (unusable db, corrupt row) -- those fall back to
    calling ``parse``. NOT best-effort for a bug in serializing ``parse``'s own
    result (a non-JSON-safe field): that's a real defect and is allowed to raise."""
    try:
        st = path.stat()
    except OSError:
        return parse(path)
    entry = _conn_for(directory)
    if entry is None:
        return parse(path)
    with entry.lock:
        try:
            row = entry.conn.execute(
                "SELECT mtime_ns, size, parser_version, data FROM entries WHERE filename = ?",
                (path.name,),
            ).fetchone()
        except _CACHE_ERRORS:
            row = None
    if (
        row is not None
        and row[0] == st.st_mtime_ns
        and row[1] == st.st_size
        and row[2] == _PARSER_VERSION
    ):
        try:
            # NOTE: the returned CallLog's `.path` is whatever was stored when
            # this row was written, not necessarily `str(path)` -- it goes stale
            # if the log directory is ever moved/renamed (review-cli#317 review,
            # round 5, k3 finding 2, filed as a follow-up: display-only field,
            # nothing re-opens a file by this path, so left as-is for now).
            return _deserialize(row[3])
        except Exception:  # noqa: BLE001 -- a corrupt/foreign-shape row is just a cache miss
            pass
    # `parse` (a full file read) and `_serialize` (JSON-encoding the body) run
    # OUTSIDE the lock -- review-cli#317 review round 6, GLM finding 2: holding it
    # across that work would serialize every OTHER thread wanting this directory's
    # cache (other requests, the SSE watcher, `save()`) behind one potentially
    # multi-MB file's I/O, and risks a self-deadlock if `parse` ever re-entered this
    # cache for the same directory (`threading.Lock` is non-reentrant). Two threads
    # racing the same cache-miss file just parse it twice and upsert identical rows
    # -- redundant work, not a correctness problem.
    value = parse(path)
    serialized = _serialize(
        value
    )  # may raise -- deliberately not caught, see docstring
    with entry.lock:
        try:
            entry.conn.execute(
                "INSERT INTO entries (filename, mtime_ns, size, parser_version, data) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(filename) DO UPDATE SET "
                "mtime_ns = excluded.mtime_ns, size = excluded.size, "
                "parser_version = excluded.parser_version, data = excluded.data",
                (path.name, st.st_mtime_ns, st.st_size, _PARSER_VERSION, serialized),
            )
        except _CACHE_ERRORS:
            pass  # persistence is a pure perf optimization, never fatal to the caller
    return value


def save(directory: Path) -> None:
    """Prune rows for files no longer present in ``directory`` -- a deleted `.log`
    must not leave its parsed body (which can hold reviewed prompts/diffs) sitting in
    the cache forever. Writes are already committed as they happen (autocommit), so
    this is pruning only, not a required flush. May open (never create) a
    pre-existing db even if THIS process never cached anything for ``directory`` --
    see the comment below."""
    with _registry_lock:
        entry = _connections.get(str(directory))
    if entry is None:
        # `_connections.get()` returns None in TWO distinct cases -- distinguish them:
        # (a) this directory was never touched this process, or (b) a prior connect
        # for it already failed and was memoized as None (`_conn_for`'s documented
        # failure-memoization). Case (b) is handled below by simply calling
        # `_conn_for` again: it returns the SAME memoized None without retrying the
        # doomed connect, so this degrades to the pre-fix no-op for that case.
        #
        # Case (a) is why this branch exists at all: an existing db from a PRIOR
        # process may still be sitting on disk holding now-stale rows. Concrete
        # case: every `.log` in `directory` gets deleted, so a fresh scan calls
        # `get_or_parse` zero times (nothing to look up) and `_conn_for` never
        # runs -- returning early here would leave every deleted call's full
        # prompt/diff body cached forever, exactly the privacy leak `save()`
        # exists to close (GitHub PR #324 review, chatgpt-codex-connector P1).
        # The `exists()` check is a fast-path optimization, not a hard CREATE
        # guarantee: an unlikely delete-in-between-the-two-calls race can still let
        # `_conn_for` create an empty db (harmless -- no rows, nothing to prune,
        # nothing to leak) rather than genuinely guaranteeing no file materializes.
        if not (directory / _CACHE_FILENAME).exists():
            return
        entry = _conn_for(directory)
        if entry is None:
            return
    try:
        # A directory listing that fails or lies (a permission flip, a network-FS
        # hiccup, an OSError mid-scandir) must NOT be read as "every file is gone" --
        # that would prune (delete) every cache row even though nothing was actually
        # removed, wiping the whole per-directory cache on a transient FS error
        # (review-cli#317 review, round 4, k3 finding 2). Skip pruning this cycle
        # instead; the next successful `save()` reconciles normally.
        current = {p.name for p in directory.glob("*.log")}
    except OSError:
        return
    with entry.lock:
        try:
            cached = {
                row[0] for row in entry.conn.execute("SELECT filename FROM entries")
            }
            stale = cached - current
            if stale:
                entry.conn.executemany(
                    "DELETE FROM entries WHERE filename = ?",
                    [(name,) for name in stale],
                )
        except _CACHE_ERRORS:
            pass
