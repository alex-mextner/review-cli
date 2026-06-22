#!/usr/bin/env python3
"""Unit tests for the memory-aware concurrency cap on heavy backend subprocesses (review-cli#65).

A `review` invocation runs its whole pool in parallel (one thread per seat, no upper bound),
and each heavy seat (codex/claude/opencode) spawns a fat model-runner subprocess. Under swarm
load that fans out into enough concurrent agent subprocesses to OOM-kill a seat mid-review.
`reviewlib.process` now caps how many heavy backend subprocesses THIS PROCESS spawns at once,
via a process-wide semaphore acquired before `Popen` and released after the child is reaped.

These tests prove:
  (a) `max_concurrency()` reads/clamps $REVIEW_MAX_CONCURRENCY (default / disabled / ceiling);
  (b) the semaphore is built once and cached, and a disabled cap is a None semaphore;
  (c) the cap actually HOLDS — with the cap set to K, no more than K real backend subprocesses
      are ever alive at the same time, even when many seats are dispatched concurrently;
  (d) a disabled cap (<=0) runs all seats concurrently (no serialization);
  (e) the single-seat gate path is unaffected (a lone seat never blocks on the cap).

Uses a tiny python one-liner as a fake slow backend (like test_streaming.py) so nothing
depends on a real model CLI being installed. The fake records its own start/exit timestamps
into a shared dir so the test can compute the true peak concurrency from on-disk evidence
(not a mock's say-so).
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import reviewlib.process as process  # noqa: E402


def _with_env(**env):
    """Set env vars for the body, restore exactly afterward (no monkeypatch fixture so the
    standalone __main__ runner works). Returns a context manager."""

    class _Ctx:
        def __enter__(self):
            self._saved = {k: os.environ.get(k) for k in env}
            for k, v in env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            process._reset_concurrency_sem_for_tests()
            return self

        def __exit__(self, *exc):
            for k, old in self._saved.items():
                if old is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = old
            process._reset_concurrency_sem_for_tests()
            return False

    return _Ctx()


# === (a) max_concurrency() reader ================================================
def test_max_concurrency_default_when_unset():
    with _with_env(REVIEW_MAX_CONCURRENCY=None):
        assert process.max_concurrency() == process._DEFAULT_MAX_CONCURRENCY


def test_max_concurrency_reads_env():
    with _with_env(REVIEW_MAX_CONCURRENCY="2"):
        assert process.max_concurrency() == 2


def test_max_concurrency_blank_and_garbage_fall_back_to_default():
    for raw in ("", "   ", "not-a-number", "3.5"):
        with _with_env(REVIEW_MAX_CONCURRENCY=raw):
            assert process.max_concurrency() == process._DEFAULT_MAX_CONCURRENCY, raw


def test_max_concurrency_zero_or_negative_disables():
    for raw in ("0", "-1", "-99"):
        with _with_env(REVIEW_MAX_CONCURRENCY=raw):
            assert process.max_concurrency() == 0, raw  # 0 == disabled


def test_max_concurrency_clamps_to_ceiling():
    with _with_env(REVIEW_MAX_CONCURRENCY="100000"):
        assert process.max_concurrency() == process._MAX_CONCURRENCY_CEILING


# === (b) the semaphore is built once + cached, disabled cap is None ==============
def test_sem_built_once_and_cached():
    with _with_env(REVIEW_MAX_CONCURRENCY="3"):
        sem1 = process._get_concurrency_sem()
        sem2 = process._get_concurrency_sem()
        assert sem1 is sem2  # same object — built once
        assert sem1 is not None


def test_disabled_cap_is_none_semaphore():
    with _with_env(REVIEW_MAX_CONCURRENCY="0"):
        assert process._get_concurrency_sem() is None


# === fake slow backend that records its own liveness window =====================
def _liveness_argv(record_dir: str, hold: float = 0.6) -> list[str]:
    """A fake backend: on start, append a unique 'start' marker file; sleep `hold`; on exit,
    write an 'end' marker. The test reconstructs the concurrency timeline from the marker
    mtimes to compute the true PEAK of simultaneously-live children — real on-disk evidence,
    not a mock counter."""
    code = (
        "import os,sys,time\n"
        f"d=os.path.join({record_dir!r}, 'events')\n"
        "os.makedirs(d, exist_ok=True)\n"
        "pid=str(os.getpid())\n"
        "open(os.path.join(d, pid+'.start'),'w').write(str(time.time()))\n"
        f"time.sleep({hold})\n"
        "open(os.path.join(d, pid+'.end'),'w').write(str(time.time()))\n"
        "print('done', flush=True)\n"
    )
    return [sys.executable, "-c", code]


def _peak_concurrency(events_dir: Path) -> int:
    """Compute the maximum number of children alive at the same instant from the per-pid
    start/end marker timestamps (a classic interval-overlap sweep)."""
    intervals: list[tuple[float, float]] = []
    for start_file in events_dir.glob("*.start"):
        pid = start_file.stem
        end_file = events_dir / f"{pid}.end"
        start_t = float(start_file.read_text())
        # A child the cap never let finish before the test read would still have an .end
        # (we join all threads first); a missing .end means it crashed — treat its window as
        # open-ended (conservatively counts toward peak), which would only INCREASE the peak,
        # so it can never hide a cap breach.
        end_t = float(end_file.read_text()) if end_file.exists() else start_t + 1e6
        intervals.append((start_t, end_t))
    points: list[tuple[float, int]] = []
    for s, e in intervals:
        points.append((s, +1))
        points.append((e, -1))
    points.sort()
    peak = cur = 0
    for _, delta in points:
        cur += delta
        peak = max(peak, cur)
    return peak


def _run_n_seats(n: int, record_dir: str, hold: float = 0.6) -> None:
    """Dispatch `n` _run_streamed calls concurrently (mirrors run_panel's per-seat threads)."""
    argv = _liveness_argv(record_dir, hold=hold)

    def _seat(i: int) -> None:
        process._run_streamed(
            argv, cwd=REPO_ROOT, timeout=30, backend=f"capfake{i}", round_no=0,
        )

    threads = [threading.Thread(target=_seat, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)


# === (c) the cap HOLDS under concurrent dispatch ================================
def test_cap_bounds_concurrent_children():
    """With the cap at 2 and 6 seats dispatched at once, never more than 2 backend
    subprocesses are alive simultaneously — the OOM guard actually bounds the fan-out."""
    tmp = tempfile.mkdtemp()
    with _with_env(REVIEW_MAX_CONCURRENCY="2", REVIEW_LOG_DIR=tempfile.mkdtemp()):
        _run_n_seats(6, tmp, hold=0.6)
    events = Path(tmp) / "events"
    # All 6 seats ran (6 start markers) ...
    assert len(list(events.glob("*.start"))) == 6, sorted(events.glob("*.start"))
    # ... but never more than 2 at once.
    assert _peak_concurrency(events) <= 2, _peak_concurrency(events)


def test_disabled_cap_runs_all_concurrently():
    """A disabled cap (<=0) imposes NO serialization: dispatching 5 seats at once lets all 5
    be alive together (the legacy unbounded behaviour). Proves the cap is the ONLY thing that
    serializes — when off, nothing does."""
    tmp = tempfile.mkdtemp()
    with _with_env(REVIEW_MAX_CONCURRENCY="0", REVIEW_LOG_DIR=tempfile.mkdtemp()):
        _run_n_seats(5, tmp, hold=0.8)
    events = Path(tmp) / "events"
    assert len(list(events.glob("*.start"))) == 5
    # With no cap, the peak should reach the full fan-out (allow a tiny scheduling slack: at
    # least 4 of 5 overlap — a strict ==5 could flake if one thread is slow to spawn).
    assert _peak_concurrency(events) >= 4, _peak_concurrency(events)


# === (f) a queued seat is NOT falsely timed out (the core correctness claim) =====
def test_queued_seat_is_not_falsely_timed_out():
    """The property the README + the _run_streamed comment promise: the per-call timeout
    starts only AFTER the seat spawns, so a seat that sat in the cap queue LONGER than its
    own timeout is NOT falsely killed. Cap=1, per-call timeout=2s, three seats each holding
    1s: the third waits ~2s behind the first two, then runs its 1s and finishes cleanly. If
    queue time counted toward the timeout, the third would be SIGKILLed (rc 124, no `.end`
    marker). We assert all three produced an `.end` marker — i.e. none was timed out for
    waiting."""
    tmp = tempfile.mkdtemp()
    argv = _liveness_argv(tmp, hold=1.0)
    results: dict[int, object] = {}

    def _seat(i: int) -> None:
        # A SHORT per-call timeout (2s) — shorter than the total queue+run wall time for the
        # last seat (~3s), so a queue that wrongly counted toward the timeout would kill it.
        results[i] = process._run_streamed(
            argv, cwd=REPO_ROOT, timeout=2, backend=f"queuefake{i}", round_no=0,
        )

    with _with_env(REVIEW_MAX_CONCURRENCY="1", REVIEW_LOG_DIR=tempfile.mkdtemp()):
        threads = [threading.Thread(target=_seat, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

    events = Path(tmp) / "events"
    # All three completed normally (an `.end` marker each) — none was timed out while queued.
    assert len(list(events.glob("*.start"))) == 3, sorted(p.name for p in events.glob("*.start"))
    assert len(list(events.glob("*.end"))) == 3, sorted(p.name for p in events.glob("*.end"))
    # And none returned the timeout exit code (124) — a queued-then-timed-out seat would.
    assert all(r.returncode == 0 for r in results.values()), {i: r.returncode for i, r in results.items()}


# === (g) a spawn FAILURE releases the slot (the sem_acquired guard's whole point) =
def test_spawn_failure_releases_the_slot():
    """The documented real scenario the `sem_acquired` guard exists for: a missing backend
    binary (no `claude-p` on PATH) makes Popen raise FileNotFoundError. The slot acquired
    just before Popen MUST be released in the finally, or it leaks forever and permanently
    shrinks the cap — eventually wedging the whole pool. Cap=1: run a seat whose binary does
    not exist (it raises), then dispatch a REAL seat and assert it actually ran (the slot was
    freed; a leaked slot would block the second seat forever and the join would time out with
    no markers)."""
    tmp = tempfile.mkdtemp()
    with _with_env(REVIEW_MAX_CONCURRENCY="1", REVIEW_LOG_DIR=tempfile.mkdtemp()):
        # First seat: a non-existent binary -> Popen raises. The slot acquired before Popen
        # must be released by the finally despite the raise.
        try:
            process._run_streamed(
                ["/nonexistent/review-cap-test-binary"], cwd=REPO_ROOT, timeout=5,
                backend="missingbin", round_no=0,
            )
        except FileNotFoundError:
            pass  # expected — the binary is not on PATH
        # Second seat: a REAL seat. If the slot leaked, cap=1 would block this forever.
        argv = _liveness_argv(tmp, hold=0.3)
        result = process._run_streamed(
            argv, cwd=REPO_ROOT, timeout=10, backend="afterfail", round_no=0,
        )
    events = Path(tmp) / "events"
    assert result.returncode == 0, result.returncode
    assert len(list(events.glob("*.end"))) == 1, "the real seat ran — the leaked slot was freed"


# === (e) the single-seat gate path is unaffected ================================
def test_single_seat_never_blocks_on_cap():
    """The everyone-uses-it gate (`--pool 1`): a lone seat must run immediately, never
    waiting on the cap. Even with the cap at 1, one seat completes (and its window exists)."""
    tmp = tempfile.mkdtemp()
    with _with_env(REVIEW_MAX_CONCURRENCY="1", REVIEW_LOG_DIR=tempfile.mkdtemp()):
        t0 = time.monotonic()
        _run_n_seats(1, tmp, hold=0.3)
        elapsed = time.monotonic() - t0
    events = Path(tmp) / "events"
    assert len(list(events.glob("*.start"))) == 1
    assert len(list(events.glob("*.end"))) == 1  # it actually finished
    # A lone seat with cap=1 pays no queueing tax — it finishes in ~its own hold time, not a
    # multiple of it. Generous bound (the spawn + python startup adds overhead) but well under
    # what a serialized second seat would have added.
    assert elapsed < 10.0, elapsed


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
