#!/usr/bin/env python3
"""Unit tests for the PRIORITY-ORDERED FAILOVER reviewer pool.

The board is a priority-ordered list (strongest model first). A plain `review` runs the
top-N AVAILABLE seats; a higher-priority seat that is unavailable at startup is skipped
and the next-priority one pulled up (startup failover), and a seat that FAILS during the
run (backend error, timeout, empty/unusable output, "unavailable" body) is replaced by
the next-priority reserve (mid-run failover) so the run still yields N working verdicts.

These tests, all OFFLINE (no model call, no network — `resolve_backend` is stubbed to a
fake keyed on model id), prove:
  (a) startup failover skips an unavailable higher-priority seat and pulls the next up;
  (b) mid-run failover backfills a FAILED pool seat from the reserve to keep the count;
  (c) the "unavailable" sentinel body (rc=0, e.g. paywalled Fable) counts as a FAILURE
      and triggers a backfill — the cheap probe can't see it, the run-time body can;
  (d) `--pool N` sizes the pool with the same failover;
  (e) priority order is respected (pool fills from the top, reserve in priority order);
  (f) graceful degradation when the reserve is exhausted (degraded=True, fewer verdicts);
  (g) run-stats: only the FINAL outcome of each logical seat is tallied (a failed-then-
      replaced seat is one fail + the replacement's outcome, not double-counted), and the
      recorded pool = the models that actually produced verdicts.

Plain-script harness (mirrors tests/test_moderator.py): each test_* is run by __main__.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import reviewlib.panel as panel  # noqa: E402
from reviewlib.backends import ReviewResult  # noqa: E402
from reviewlib.config import (  # noqa: E402
    DEFAULT_BOARD,
    BoardReviewer,
    split_pool_reserve,
)
from reviewlib.panel import (  # noqa: E402
    result_is_usable,
    run_board_with_failover,
)

PROMPT = "Review this diff."


# === fake-backend harness =======================================================
class _FakeBackends:
    """Stub panel.resolve_backend with a fake keyed on per-model behaviour.

    `behaviour[model]` -> (returncode, stdout). Default = (0, "ok <model>"). A model can
    thus be made to fail (rc!=0), return empty (""), or return an "unavailable" sentinel
    body. Records every (model, prompt) actually dispatched in `calls`."""

    def __init__(self, behaviour: dict[str, tuple[int, str]] | None = None):
        self.behaviour = behaviour or {}
        self.calls: list[tuple[str, str]] = []

    def __enter__(self):
        self._old = panel.resolve_backend

        def _resolve(_model: str):
            def _backend(model, prompt, diff, cwd, timeout, round_no=0):
                self.calls.append((model, prompt))
                rc, out = self.behaviour.get(model, (0, f"ok {model}"))
                return ReviewResult(model=model, command="fake", returncode=rc, stdout=out, stderr="")
            return _backend

        panel.resolve_backend = _resolve
        return self

    def __exit__(self, *exc):
        panel.resolve_backend = self._old
        return False

    @property
    def dispatched(self) -> list[str]:
        return [m for m, _ in self.calls]


def _avail(models: set[str]):
    return lambda r: r.model in models


# === result_is_usable: the failure classifier ===================================
def test_usable_true_for_real_verdict():
    assert result_is_usable(ReviewResult("m", "c", 0, "Here is a real finding.", ""))


def test_unusable_on_nonzero_exit():
    assert not result_is_usable(ReviewResult("m", "c", 1, "anything", ""))


def test_unusable_on_empty_output():
    assert not result_is_usable(ReviewResult("m", "c", 0, "   \n  ", ""))


def test_unusable_on_unavailable_sentinel_body():
    """The paywalled-Fable shape: rc=0, short body that is an unavailability notice."""
    body = ("Claude Fable 5 is currently unavailable. "
            "Learn more: https://www.anthropic.com/news/fable-mythos-access")
    assert not result_is_usable(ReviewResult("claude:claude-fable-5", "c", 0, body, ""))


def test_usable_long_review_mentioning_availability_is_not_misclassified():
    """A genuine LONG review that happens to mention availability must NOT be flagged —
    the sentinel check only fires on a SHORT body."""
    body = "x" * 500 + " the service is currently unavailable in some regions, consider a retry"
    assert result_is_usable(ReviewResult("m", "c", 0, body, ""))


def test_usable_short_review_with_generic_not_available_phrase():
    """The markers must be TIGHT: a short, real review using a generic 'is not available'
    phrase (e.g. a py-version note) must NOT be misclassified as an unavailable-model
    sentinel — that would brand a real verdict failed and could block a clean commit."""
    for body in (
        "`asyncio.timeout` is not available before Python 3.11; guard it.",
        "The `match` statement is not available in py<3.10 — this breaks older runtimes.",
        "Looks good. The new flag is not available in the legacy API, document that.",
    ):
        assert result_is_usable(ReviewResult("m", "c", 0, body, "")), body


def test_unusable_short_temporarily_unavailable_sentinel():
    """A short 'temporarily unavailable' provider notice still counts as a failed seat."""
    body = "This model is temporarily unavailable. Please try again later."
    assert not result_is_usable(ReviewResult("m", "c", 0, body, ""))


# === startup failover (split_pool_reserve) ======================================
def test_startup_failover_skips_unavailable_top_seat():
    """Fable (priority #1) unavailable -> the top-4 pool starts at Opus and pulls one
    more in from the reserve, so it is still 4 WORKING seats."""
    board = list(DEFAULT_BOARD)
    available = {r.model for r in board if r.model != "claude:claude-fable-5"}
    pool, reserve = split_pool_reserve(board, 4, _avail(available))
    assert [r.model for r in pool] == [
        "claude:claude-opus-4-8",
        "codex",
        "oc:commandcode/moonshotai/Kimi-K2.7-Code",
        "oc:zai/glm-5.2",
    ], [r.model for r in pool]
    # Fable is NOT in the pool nor the reserve — it's unavailable.
    assert "claude:claude-fable-5" not in {r.model for r in pool + reserve}


def test_startup_failover_respects_priority_order():
    """Pool fills strictly from the top of the priority list; reserve is the next
    available seats, also in priority order."""
    board = list(DEFAULT_BOARD)
    pool, reserve = split_pool_reserve(board, 4, _avail({r.model for r in board}))
    assert [r.model for r in pool] == [r.model for r in DEFAULT_BOARD[:4]]
    assert [r.model for r in reserve] == [r.model for r in DEFAULT_BOARD[4:]]


def test_startup_pool_n_honored():
    board = list(DEFAULT_BOARD)
    all_avail = _avail({r.model for r in board})
    for n in (1, 2, 3, 5):
        pool, reserve = split_pool_reserve(board, n, all_avail)
        assert len(pool) == n, n
        assert len(reserve) == len(board) - n, n
    # --pool 0 = all available in the pool, no reserve.
    pool0, reserve0 = split_pool_reserve(board, 0, all_avail)
    assert len(pool0) == len(board) and reserve0 == []


def test_startup_only_two_available_pool_shrinks_no_phantom():
    """Fewer available seats than the pool size -> the pool is just the available ones
    (no phantom seat), reserve empty."""
    board = list(DEFAULT_BOARD)
    pool, reserve = split_pool_reserve(board, 4, _avail({"oc:zai/glm-5.2", "gemini"}))
    assert {r.model for r in pool} == {"oc:zai/glm-5.2", "gemini"}
    assert reserve == []


# === mid-run failover (run_board_with_failover) =================================
def test_midrun_all_pool_succeeds_no_backfill():
    board = list(DEFAULT_BOARD)
    pool, reserve = split_pool_reserve(board, 4, _avail({r.model for r in board}))
    with _FakeBackends() as fb:
        outcome = run_board_with_failover(pool, reserve, PROMPT, "+x", REPO_ROOT, 5)
    assert len(outcome.usable) == 4
    assert not outcome.degraded
    # Only the 4 pool seats ran; no reserve was touched.
    assert set(fb.dispatched) == {r.model for r in pool}


def test_midrun_failed_seat_is_backfilled_keeps_count():
    """A pool seat that FAILS mid-run is replaced by the next-priority reserve, so the
    run still ends with `pool_size` (4) usable verdicts — it does NOT degrade to 3."""
    board = list(DEFAULT_BOARD)
    pool, reserve = split_pool_reserve(board, 4, _avail({r.model for r in board}))
    # The 4th pool seat (Kimi) errors out mid-run; the next reserve (GLM) backfills it.
    failing = pool[3].model
    with _FakeBackends({failing: (1, "boom")}) as fb:
        outcome = run_board_with_failover(pool, reserve, PROMPT, "+x", REPO_ROOT, 5)
    assert len(outcome.usable) == 4, outcome.usable_models
    assert not outcome.degraded
    # The failed seat ran (and is in results but not usable); the first reserve ran too.
    assert failing in fb.dispatched
    assert reserve[0].model in fb.dispatched
    assert failing not in outcome.usable_models
    assert reserve[0].model in outcome.usable_models


def test_midrun_unavailable_body_triggers_backfill_fable_case():
    """The exact CTO scenario: Fable is in the startup pool (the cheap probe says
    available — its paywall is invisible) but returns an 'unavailable' body at run time.
    Mid-run failover treats that as a failure and backfills, so the working pool-4 ends as
    Opus / Codex / Kimi / GLM-5.2 — Fable replaced."""
    board = list(DEFAULT_BOARD)
    # Cheap probe: ALL available (Fable's paywall is invisible to it).
    pool, reserve = split_pool_reserve(board, 4, _avail({r.model for r in board}))
    assert pool[0].model == "claude:claude-fable-5"  # Fable IS selected at startup
    fable_body = ("Claude Fable 5 is currently unavailable. "
                  "Learn more: https://www.anthropic.com/news/fable-mythos-access")
    with _FakeBackends({"claude:claude-fable-5": (0, fable_body)}):
        outcome = run_board_with_failover(pool, reserve, PROMPT, "+x", REPO_ROOT, 5)
    assert len(outcome.usable) == 4
    assert not outcome.degraded
    assert "claude:claude-fable-5" not in outcome.usable_models
    assert set(outcome.usable_models) == {
        "claude:claude-opus-4-8",
        "codex",
        "oc:commandcode/moonshotai/Kimi-K2.7-Code",
        "oc:zai/glm-5.2",
    }, outcome.usable_models


def test_midrun_cascading_failures_walk_the_reserve():
    """Multiple pool seats fail AND the first promoted reserve also fails -> the failover
    keeps walking the reserve until the count is met."""
    board = list(DEFAULT_BOARD)
    pool, reserve = split_pool_reserve(board, 4, _avail({r.model for r in board}))
    # pool[0] and pool[1] fail; reserve[0] (the first backfill for pool[0]) ALSO fails;
    # reserve[1], reserve[2] succeed -> still 4 usable.
    behaviour = {
        pool[0].model: (1, "boom0"),
        pool[1].model: (0, ""),          # empty output = failure
        reserve[0].model: (500, "err"),  # first backfill also fails
    }
    with _FakeBackends(behaviour):
        outcome = run_board_with_failover(pool, reserve, PROMPT, "+x", REPO_ROOT, 5)
    assert len(outcome.usable) == 4, [r.model for r in outcome.usable]
    assert not outcome.degraded


def test_midrun_reserve_exhausted_degrades_gracefully():
    """When the reserve can't refill the pool, the run degrades: fewer usable verdicts,
    degraded=True — it does NOT crash or hang."""
    board = list(DEFAULT_BOARD)
    # Only the 4 pool seats are available; NO reserve. Two of the pool fail.
    pool, reserve = split_pool_reserve(board, 4, _avail({r.model for r in DEFAULT_BOARD[:4]}))
    assert reserve == []
    behaviour = {pool[0].model: (1, "x"), pool[1].model: (1, "y")}
    with _FakeBackends(behaviour):
        outcome = run_board_with_failover(pool, reserve, PROMPT, "+x", REPO_ROOT, 5)
    assert len(outcome.usable) == 2, [r.model for r in outcome.usable]
    assert outcome.degraded
    assert outcome.target == 4


def test_midrun_pool_n_honored_in_failover():
    """--pool N sizes the failover pool: a pool of 2 with one failure backfills to 2."""
    board = list(DEFAULT_BOARD)
    pool, reserve = split_pool_reserve(board, 2, _avail({r.model for r in board}))
    assert len(pool) == 2
    with _FakeBackends({pool[1].model: (1, "boom")}):
        outcome = run_board_with_failover(pool, reserve, PROMPT, "+x", REPO_ROOT, 5)
    assert len(outcome.usable) == 2
    assert not outcome.degraded


# === run-stats tally correctness ================================================
def test_tally_counts_final_outcomes_not_failed_attempts():
    """The run-stats tally records exactly one outcome PER LOGICAL SEAT (its final
    verdict): a failed-then-replaced seat = 1 fail (the failed attempt) + 1 ok (the
    replacement), NOT a double-count. A clean 4-seat run = 4 ok, 0 fail."""
    board = list(DEFAULT_BOARD)
    pool, reserve = split_pool_reserve(board, 4, _avail({r.model for r in board}))

    # Clean run: 4 ok.
    panel.begin_call_tally()
    with _FakeBackends():
        run_board_with_failover(pool, reserve, PROMPT, "+x", REPO_ROOT, 5)
    tally = panel.end_call_tally()
    assert tally == {"ok": 4, "fail": 0}, tally

    # One seat fails then backfills: 1 fail (the failed seat) + 4 ok (3 healthy pool + the
    # backfill) = 5 calls tallied, exactly one per attempt's final verdict.
    panel.begin_call_tally()
    with _FakeBackends({pool[3].model: (1, "boom")}):
        run_board_with_failover(pool, reserve, PROMPT, "+x", REPO_ROOT, 5)
    tally = panel.end_call_tally()
    assert tally == {"ok": 4, "fail": 1}, tally


def test_results_include_failed_and_replacement_seats():
    """`results` is the WHOLE story (every seat that ran, failed + healthy + backfill);
    `usable` is just the working verdicts."""
    board = list(DEFAULT_BOARD)
    pool, reserve = split_pool_reserve(board, 4, _avail({r.model for r in board}))
    with _FakeBackends({pool[0].model: (1, "boom")}):
        outcome = run_board_with_failover(pool, reserve, PROMPT, "+x", REPO_ROOT, 5)
    # run_panel sets ReviewResult.model to the board LABEL ("<display> [<role>]"), so
    # `results` is keyed by label; `usable_models` carries the bare model ids.
    ran_labels = {r.model for r in outcome.results}
    failed_label = f"{pool[0].display} [{pool[0].role}]"
    backfill_label = f"{reserve[0].display} [{reserve[0].role}]"
    assert failed_label in ran_labels, ran_labels        # the failed seat is in results
    assert backfill_label in ran_labels, ran_labels      # so is its replacement
    assert len(outcome.results) == 5                      # 4 pool + 1 backfill
    assert len(outcome.usable) == 4
    assert reserve[0].model in outcome.usable_models      # bare id of the backfill


# === lens travels with the promoted reserve =====================================
def test_promoted_reserve_brings_its_own_role_lens():
    """A backfilled reserve reviews with ITS OWN role lens (priority decides who sits;
    the role decides the lens). The promoted seat's prompt carries its role lens."""
    from reviewlib.config import REVIEW_ROLES

    board = [
        BoardReviewer("m-pool", "architect", "P"),
        BoardReviewer("m-reserve", "security", "R"),
    ]
    pool, reserve = split_pool_reserve(board, 1, _avail({"m-pool", "m-reserve"}))
    with _FakeBackends({"m-pool": (1, "boom")}) as fb:
        run_board_with_failover(pool, reserve, PROMPT, "+x", REPO_ROOT, 5)
    prompts = {m: p for m, p in fb.calls}
    assert REVIEW_ROLES["security"] in prompts["m-reserve"]  # reserve's own lens


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
