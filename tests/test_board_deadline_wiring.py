"""review-cli#221: run_board_with_failover arms a process-wide wall-clock deadline
(process.set_board_deadline) from $REVIEW_BOARD_DEADLINE_SECONDS for its duration, so a
reserve promoted late in the run gets a clamped idle timeout instead of the default
20-minute floor outliving an external `timeout N` wrapper. See reviewlib/process.py's
idle_timeout_seconds / _clamp_to_board_deadline for the actual clamp logic (covered in
tests/test_streaming.py); this file covers only the wiring — does the board run
actually arm and clear the deadline at the right moments.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import reviewlib.panel as panel  # noqa: E402
import reviewlib.process as process  # noqa: E402
from reviewlib.backends import ReviewResult  # noqa: E402
from reviewlib.config import BoardReviewer  # noqa: E402
from reviewlib.panel import run_board_with_failover  # noqa: E402

PROMPT = "Review this diff."


def _with_env(**env):
    class _Ctx:
        def __enter__(self):
            self._saved = {k: os.environ.get(k) for k in env}
            for k, v in env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            return self

        def __exit__(self, *exc):
            for k, old in self._saved.items():
                if old is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = old
            return False

    return _Ctx()


class _DeadlineSpyBackends:
    """Stub panel.resolve_backend like test_failover_pool.py's _FakeBackends, but each
    dispatched call also snapshots process._active_board_deadline() at call time, so a
    test can assert the deadline was genuinely armed DURING the run (not just that the
    setter was called, which a mock could get right for the wrong reason)."""

    def __init__(self, behaviour: dict[str, tuple[int, str]] | None = None):
        self.behaviour = behaviour or {}
        self.deadline_seen: list[float | None] = []

    def __enter__(self):
        self._old = panel.resolve_backend

        def _resolve(_model: str):
            def _backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
                self.deadline_seen.append(process._active_board_deadline())
                rc, out = self.behaviour.get(model, (0, f"ok {model}"))
                return ReviewResult(
                    model=model, command="fake", returncode=rc, stdout=out, stderr=""
                )

            return _backend

        panel.resolve_backend = _resolve
        return self

    def __exit__(self, *exc):
        panel.resolve_backend = self._old
        return False


@pytest.fixture(autouse=True)
def _clean_deadline():
    """Every test starts and ends with no deadline armed — a leaked deadline from one
    test (or a bug in the finally-clearing under test) must never bleed into another."""
    process.set_board_deadline(None)
    yield
    process.set_board_deadline(None)


def test_no_env_var_means_no_deadline_armed():
    """Backward compatibility: a caller that never sets $REVIEW_BOARD_DEADLINE_SECONDS
    (every caller before review-cli#221, and most callers after it) sees the deadline
    stay None for the whole run — current unclamped behaviour, unchanged."""
    pool = [BoardReviewer(model="m:a", role="", display="A")]
    with _with_env(REVIEW_BOARD_DEADLINE_SECONDS=None):
        with _DeadlineSpyBackends() as backends:
            run_board_with_failover(pool, [], PROMPT, "diff", Path("."), 900)
    assert backends.deadline_seen == [None]
    assert process._active_board_deadline() is None


def test_env_var_arms_a_deadline_for_the_run_and_clears_it_after():
    """With the env var set, the deadline is armed (a real future monotonic timestamp,
    not just 'truthy') during dispatch, and cleared back to None once the board run
    returns — it must never leak into whatever runs next in the same process."""
    pool = [BoardReviewer(model="m:a", role="", display="A")]
    with _with_env(REVIEW_BOARD_DEADLINE_SECONDS="600"):
        before = time.monotonic()
        with _DeadlineSpyBackends() as backends:
            run_board_with_failover(pool, [], PROMPT, "diff", Path("."), 900)
        after = time.monotonic()
    assert len(backends.deadline_seen) == 1
    seen = backends.deadline_seen[0]
    assert seen is not None
    # Armed roughly `before + 600` .. `after + 600` — a real deadline, not a sentinel.
    assert before + 590 <= seen <= after + 600
    assert process._active_board_deadline() is None


def test_deadline_is_cleared_even_when_every_seat_fails():
    """The finally-clear must run on every exit path, not just the happy one — a board
    that fully degrades (every pool seat AND every reserve fails) must not leave a
    stale deadline armed for whatever the process does next."""
    pool = [BoardReviewer(model="m:a", role="", display="A")]
    with _with_env(REVIEW_BOARD_DEADLINE_SECONDS="60"):
        with _DeadlineSpyBackends(behaviour={"m:a": (1, "")}):
            outcome = run_board_with_failover(pool, [], PROMPT, "diff", Path("."), 900)
    assert outcome.degraded is True
    assert process._active_board_deadline() is None


def test_zero_or_negative_budget_is_treated_as_unset():
    """A misconfigured $REVIEW_BOARD_DEADLINE_SECONDS (0, negative, non-numeric) must
    fail OPEN to the pre-existing unclamped behaviour, never fail closed to an
    instantly-expired deadline that would starve every seat to the minimum floor."""
    pool = [BoardReviewer(model="m:a", role="", display="A")]
    for bad in ("0", "-5", "not-a-number", ""):
        with _with_env(REVIEW_BOARD_DEADLINE_SECONDS=bad):
            with _DeadlineSpyBackends() as backends:
                run_board_with_failover(pool, [], PROMPT, "diff", Path("."), 900)
        assert backends.deadline_seen == [None], bad
