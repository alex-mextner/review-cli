#!/usr/bin/env python3
"""`reviewlib.dashboard.call_log_cache` — the persistent, file-identity-keyed cache
that lets a repeat scan of a call-log directory skip re-parsing files that haven't
changed since the cache was last written (the fix for `review stat --days 0` and the
dashboard's default view timing out on a long-lived install's log directory).

Covers the review-cli#317 review findings this SQLite+JSON design answers: a
per-filename indexed lookup (never a whole-cache load, so a bounded 7-day scan
doesn't deserialize the full history), JSON not pickle (no arbitrary code execution
from a planted/foreign cache file), and pruning entries for deleted logs (a deleted
`.log` must not leave its parsed body — potentially a reviewed prompt/diff — sitting
in the cache forever).
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib.dashboard import call_log_cache as clc  # noqa: E402
from reviewlib.dashboard.parser import CallLog  # noqa: E402


def _call(**overrides) -> CallLog:
    defaults = dict(
        path="/logs/a.log",
        filename="a.log",
        started=datetime(2026, 8, 13, 10, 0, 0, tzinfo=timezone.utc),
        backend="z.ai",
        round=0,
        argv0="z.ai API glm-5.2",
        body="Looks fine.\n",
    )
    defaults.update(overrides)
    return CallLog(**defaults)


def _reset(directory: Path) -> None:
    """Drop this test's in-process connection so the next call re-opens (and
    therefore re-loads) whatever is actually persisted on disk — simulating a fresh
    process for the same directory."""
    entry = clc._connections.pop(str(directory), None)
    if entry is not None:
        entry.conn.close()


def test_second_scan_reuses_cached_parse_for_an_unchanged_file():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        f = d / "a.log"
        f.write_text("hello", encoding="utf-8")
        call = _call()

        parses = []

        def fake_parse(path: Path) -> CallLog:
            parses.append(path)
            return call

        first = clc.get_or_parse(d, f, fake_parse)
        clc.save(d)
        assert first == call
        assert len(parses) == 1

        _reset(d)  # simulate a fresh process reopening the same directory's cache
        second = clc.get_or_parse(d, f, fake_parse)
        assert second == call
        assert len(parses) == 1, (
            "unchanged file must not be re-parsed on a fresh cache load"
        )


def test_modified_file_is_reparsed_not_served_stale():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        f = d / "a.log"
        f.write_text("v1", encoding="utf-8")
        call_v1, call_v2 = _call(body="v1"), _call(body="v2-longer")

        parses = []

        def fake_parse(path: Path) -> CallLog:
            parses.append(path)
            return call_v1 if path.read_text(encoding="utf-8") == "v1" else call_v2

        assert clc.get_or_parse(d, f, fake_parse) == call_v1
        clc.save(d)

        time.sleep(
            0.01
        )  # force a distinct mtime -- some filesystems have coarse resolution
        f.write_text("v2-longer", encoding="utf-8")

        _reset(d)
        result = clc.get_or_parse(d, f, fake_parse)
        assert result == call_v2, (
            "a changed file must be re-parsed, not served the stale cached value"
        )
        assert len(parses) == 2


def test_none_result_is_cached_and_not_reparsed():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        f = d / "not-a-call-log.log"
        f.write_text("junk", encoding="utf-8")

        parses = []

        def fake_parse(path: Path) -> None:
            parses.append(path)
            return None

        assert clc.get_or_parse(d, f, fake_parse) is None
        clc.save(d)

        _reset(d)
        assert clc.get_or_parse(d, f, fake_parse) is None
        assert len(parses) == 1, (
            "a cached None must not trigger a reparse on the next scan"
        )


def test_two_different_directories_never_share_cache_entries():
    with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
        d1, d2 = Path(tmp1), Path(tmp2)
        f1, f2 = d1 / "a.log", d2 / "a.log"
        f1.write_text("dir1", encoding="utf-8")
        f2.write_text("dir2", encoding="utf-8")
        call1, call2 = _call(body="dir1"), _call(body="dir2")

        assert clc.get_or_parse(d1, f1, lambda p: call1) == call1
        assert clc.get_or_parse(d2, f2, lambda p: call2) == call2


def test_a_bounded_lookup_never_touches_or_requires_other_entries_to_be_valid():
    """review-cli#317 P1: a bounded (e.g. 7-day) scan must not load/deserialize the
    whole cache. Proven functionally: an unrelated, corrupt row for a DIFFERENT
    filename must not affect (or even be read for) a lookup of a valid one."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        good = d / "good.log"
        good.write_text("g", encoding="utf-8")
        call = _call(filename="good.log", body="g")
        clc.get_or_parse(d, good, lambda p: call)
        clc.save(d)

        # Directly corrupt a DIFFERENT row's JSON payload (simulating an old entry from
        # a since-changed CallLog shape) without touching "good.log"'s row at all.
        entry = clc._conn_for(d)
        entry.conn.execute(
            "INSERT INTO entries (filename, mtime_ns, size, parser_version, data) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "bogus.log",
                1,
                1,
                clc._PARSER_VERSION,
                json.dumps({"not": "a CallLog shape"}),
            ),
        )

        _reset(d)
        result = clc.get_or_parse(
            d, good, lambda p: (_ for _ in ()).throw(AssertionError("must not reparse"))
        )
        assert result == call


def test_row_with_wrong_schema_is_a_cache_miss_not_a_crash():
    """review-cli#317 k3 finding #4: a structurally-valid-but-wrong-shape cached value
    (e.g. an old CallLog shape) must fall back to reparsing, not raise."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        f = d / "a.log"
        f.write_text("x", encoding="utf-8")
        st = f.stat()

        entry = clc._conn_for(d)
        entry.conn.execute(
            "INSERT INTO entries (filename, mtime_ns, size, parser_version, data) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "a.log",
                st.st_mtime_ns,
                st.st_size,
                clc._PARSER_VERSION,
                json.dumps({"totally": "wrong"}),
            ),
        )
        _reset(d)

        call = _call()
        assert clc.get_or_parse(d, f, lambda p: call) == call


def test_cache_files_stay_hidden_and_out_of_the_directory_listing_of_log_files():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        f = d / "a.log"
        f.write_text("x", encoding="utf-8")
        clc.get_or_parse(d, f, lambda p: _call())
        clc.save(d)

        log_files = sorted(p.name for p in d.glob("*.log"))
        assert log_files == ["a.log"], (
            "the persisted cache's db/wal/shm files must never match a *.log glob"
        )


def test_db_file_is_created_0600_not_umask_default():
    """review-cli#317 round 4, GLM finding 1: the db holds full parsed call bodies --
    the same private content review-cli's own log files (0600) already protect."""
    import stat as stat_module

    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        f = d / "a.log"
        f.write_text("x", encoding="utf-8")
        clc.get_or_parse(d, f, lambda p: _call())

        mode = stat_module.S_IMODE((d / clc._CACHE_FILENAME).stat().st_mode)
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_missing_cache_file_is_a_cold_cache_not_a_crash():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        f = d / "a.log"
        f.write_text("x", encoding="utf-8")
        # No prior save() ever happened for this directory -- first load must be silent.
        call = _call()
        assert clc.get_or_parse(d, f, lambda p: call) == call


def test_stays_active_even_when_sqlite_threadsafety_reports_unreliably():
    """review-cli#317 round 5, GLM+k3 both: an earlier design refused to cache at all
    unless `sqlite3.threadsafety == 3` -- but that constant was hardcoded to `1` on
    every CPython version through 3.12 regardless of the real SQLite build (dynamic
    only since 3.13), so that design left the cache silently inert on most of this
    repo's own supported-Python/CI matrix, and broke several of this module's own
    tests outright on those interpreters (k3 flagged it CI-breaking). The current
    design doesn't trust that signal at all -- correctness comes from a per-directory
    lock serializing every access to the shared connection, which is safe regardless
    of `sqlite3.threadsafety`'s value. This test proves the cache stays genuinely
    ACTIVE (not just non-crashing) even when that signal reports the OLD, unreliable
    value."""
    import sqlite3

    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        f = d / "a.log"
        f.write_text("x", encoding="utf-8")

        original = sqlite3.threadsafety
        sqlite3.threadsafety = 1  # simulate the pre-3.13 hardcoded report
        try:
            call = _call()
            parses = []

            def fake_parse(path: Path) -> CallLog:
                parses.append(path)
                return call

            assert clc.get_or_parse(d, f, fake_parse) == call
            assert clc.get_or_parse(d, f, fake_parse) == call
            assert len(parses) == 1, (
                "the cache must stay active regardless of sqlite3.threadsafety's value"
            )
            assert (d / clc._CACHE_FILENAME).exists()
        finally:
            sqlite3.threadsafety = original


def test_corrupt_database_file_is_treated_as_cold_not_a_crash():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / clc._CACHE_FILENAME).write_bytes(b"not a valid sqlite database at all")
        f = d / "a.log"
        f.write_text("x", encoding="utf-8")
        call = _call()
        assert clc.get_or_parse(d, f, lambda p: call) == call


def test_save_prunes_entries_for_files_no_longer_present():
    """review-cli#317 k3 P1: a deleted `.log` must not leave its parsed body (which
    can hold reviewed prompts/diffs) in the cache forever."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        keep, gone = d / "keep.log", d / "gone.log"
        keep.write_text("k", encoding="utf-8")
        gone.write_text("g", encoding="utf-8")
        clc.get_or_parse(d, keep, lambda p: _call(filename="keep.log"))
        clc.get_or_parse(d, gone, lambda p: _call(filename="gone.log"))
        clc.save(d)

        gone.unlink()
        clc.save(d)  # must reconcile against the CURRENT directory listing

        entry = clc._conn_for(d)
        names = {row[0] for row in entry.conn.execute("SELECT filename FROM entries")}
        assert names == {"keep.log"}


def test_save_does_not_wipe_the_cache_when_the_directory_listing_fails():
    """review-cli#317 round 4, k3 finding 2: if listing the directory raises (a
    permission flip, a network-FS hiccup) mid-`save()`, that must NOT be read as
    "every file is gone" -- which would prune (delete) every row even though nothing
    was actually removed, silently wiping the whole per-directory cache on a
    transient error. Pruning should just be skipped for that cycle."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        f = d / "a.log"
        f.write_text("x", encoding="utf-8")
        clc.get_or_parse(d, f, lambda p: _call())
        clc.save(d)

        real_glob = Path.glob

        def _raising_glob(self, pattern):
            if self == d and pattern == "*.log":
                raise OSError("simulated directory-listing failure")
            return real_glob(self, pattern)

        Path.glob = _raising_glob
        try:
            clc.save(d)  # must not raise, and must not prune anything
        finally:
            Path.glob = real_glob

        entry = clc._conn_for(d)
        names = {row[0] for row in entry.conn.execute("SELECT filename FROM entries")}
        assert names == {"a.log"}, (
            "a failed listing must not be treated as an empty directory"
        )


def test_save_is_a_noop_when_nothing_was_ever_parsed_for_that_directory():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        clc.save(d)  # nothing was ever get_or_parse'd for this dir
        assert not (d / clc._CACHE_FILENAME).exists()


def test_two_separate_connections_to_the_same_db_both_persist_their_writes():
    """review-cli#317 round 1 P2: two SEPARATE connections to the same db file
    (simulating two processes -- e.g. the dashboard and a concurrent `review stat`)
    must both be able to write without one clobbering or corrupting the other's row.
    NOTE: this uses two connections sequentially, not two threads -- it does NOT
    exercise thread-affinity (see test_a_second_thread_is_served_from_cache_not_a_
    ProgrammingError below for that, review-cli#317 round 2 finding 1)."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        f1, f2 = d / "one.log", d / "two.log"
        f1.write_text("1", encoding="utf-8")
        f2.write_text("2", encoding="utf-8")

        # First "process": writes one.log via the module's own connection.
        clc.get_or_parse(d, f1, lambda p: _call(filename="one.log"))
        clc.save(d)

        # Second "process": an entirely separate connection to the same db file.
        conn2 = clc._connect(d)
        st2 = f2.stat()
        conn2.execute(
            "INSERT INTO entries (filename, mtime_ns, size, parser_version, data) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "two.log",
                st2.st_mtime_ns,
                st2.st_size,
                clc._PARSER_VERSION,
                clc._serialize(_call(filename="two.log")),
            ),
        )
        conn2.close()

        _reset(d)
        # Both entries must have survived -- neither writer's transaction was lost.
        entry = clc._conn_for(d)
        names = {row[0] for row in entry.conn.execute("SELECT filename FROM entries")}
        assert names == {"one.log", "two.log"}


def test_a_second_thread_is_served_from_cache_not_a_programming_error():
    """review-cli#317 round 2, GLM+k3 finding 1 (high): the dashboard is a
    ThreadingHTTPServer -- a new thread per request, plus a background-refresh daemon
    thread, plus a prewarm thread, all reaching this cache. `sqlite3.connect` defaults
    to `check_same_thread=True`, so a connection created on one thread raises
    `sqlite3.ProgrammingError` (a subclass of `sqlite3.Error`) when used from another
    -- which every handler in this module was built to swallow as "just a cache
    miss", making the cache silently 100% inert on every thread but the first. This
    test fails on that regression: a second thread must be SERVED from the first
    thread's cached entry (parse must NOT be called again), not silently re-parse."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        f = d / "a.log"
        f.write_text("x", encoding="utf-8")
        call = _call()
        parses = []

        def fake_parse(path: Path) -> CallLog:
            parses.append(path)
            return call

        # Thread 1 primes the cache and creates the (thread-affine, if broken)
        # connection.
        clc.get_or_parse(d, f, fake_parse)
        assert len(parses) == 1

        # Thread 2 must reuse it, not raise/swallow-and-reparse.
        result: dict[str, object] = {}

        def _from_thread_2() -> None:
            try:
                result["value"] = clc.get_or_parse(d, f, fake_parse)
            except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
                result["error"] = exc

        t = threading.Thread(target=_from_thread_2)
        t.start()
        t.join(timeout=10)

        assert "error" not in result, f"cross-thread use raised: {result.get('error')}"
        assert result.get("value") == call
        assert len(parses) == 1, (
            f"thread 2 re-parsed ({len(parses)} calls) instead of being served from "
            "thread 1's cached entry -- the connection is not actually shared safely"
        )


def test_call_log_round_trips_every_field_through_serialize_deserialize():
    call = CallLog(
        path="/logs/x.log",
        filename="x.log",
        started=datetime(2026, 8, 1, 12, 30, 0, tzinfo=timezone.utc),
        backend="codex",
        round=2,
        argv0="codex exec",
        body="some output\nwith lines",
        stderr_lines=["warn: something"],
        timed_out=True,
        timeout_secs=1200,
        true_silenced=False,
        true_silence_secs=None,
        size_bytes=42,
        mtime=datetime(2026, 8, 1, 12, 31, 0, tzinfo=timezone.utc),
        exit_code=124,
        task_code="HYP-1",
    )
    restored = clc._deserialize(clc._serialize(call))
    assert restored == call


def test_call_log_with_none_mtime_round_trips():
    call = _call(mtime=None)
    assert clc._deserialize(clc._serialize(call)) == call


def test_a_row_missing_a_newly_added_field_is_a_cache_miss_not_served_with_a_stale_default():
    """review-cli#317 round 3, GLM+k3 both: a row that's a strict SUBSET of the
    current CallLog fields (simulating a row cached before a new defaulted field was
    added) must be treated as a miss and reparsed -- not accepted with the dataclass
    silently filling in the default, which would serve a wrong/stale value forever
    for any file parsed before the field existed (mtime/size never change again for a
    finished log, so nothing would ever invalidate it)."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        f = d / "a.log"
        f.write_text("x", encoding="utf-8")
        st = f.stat()

        # A real, fully-formed CallLog payload with one field REMOVED -- simulating a
        # row written before that field existed, not a garbage/corrupt payload.
        payload = json.loads(clc._serialize(_call()))
        del payload["task_code"]

        entry = clc._conn_for(d)
        entry.conn.execute(
            "INSERT INTO entries (filename, mtime_ns, size, parser_version, data) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                f.name,
                st.st_mtime_ns,
                st.st_size,
                clc._PARSER_VERSION,
                json.dumps(payload),
            ),
        )
        _reset(d)

        fresh = _call(task_code="a-real-value-only-a-reparse-would-produce")
        assert clc.get_or_parse(d, f, lambda p: fresh) == fresh


def test_bumping_parser_version_invalidates_an_otherwise_unchanged_row():
    """review-cli#317 round 3, k3 finding 1: a `parse_call_log` LOGIC fix (not a
    CallLog shape change) must still invalidate already-cached rows for files whose
    (mtime, size) never change again -- a stat-only cache key can't see this on its
    own, hence the separate `parser_version` column."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        f = d / "a.log"
        f.write_text("x", encoding="utf-8")

        old_call = _call(body="pre-fix parse")
        assert clc.get_or_parse(d, f, lambda p: old_call) == old_call

        # Simulate a parser logic fix by bumping the version the module checks against.
        original_version = clc._PARSER_VERSION
        clc._PARSER_VERSION = original_version + 1
        try:
            new_call = _call(body="post-fix parse")
            assert clc.get_or_parse(d, f, lambda p: new_call) == new_call
        finally:
            clc._PARSER_VERSION = original_version


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
