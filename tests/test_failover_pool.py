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

import os  # noqa: E402
import tempfile  # noqa: E402

# This file tests BOARD-LEVEL reserve-replace failover in ISOLATION. Provider-failover
# (seat-level, same-model-across-providers — reviewlib.provider_failover) is a DISTINCT
# layer tested in tests/test_provider_failover.py, and it would otherwise change what "a
# seat fails" means here (a chained seat like GLM-cc would fall over to z.ai instead of
# failing, so no reserve backfill fires). Neutralise it to an identity chain so these tests
# exercise reserve-replace alone. Also redirect the last-working cache to a throwaway file
# so a test never reads/writes the real ~/.cache.
os.environ.setdefault(
    "REVIEW_PROVIDER_CACHE", str(Path(tempfile.mkdtemp()) / "last-provider.json")
)
# Neutralise seat-level provider-failover to an identity chain, but SCOPED per-test (an
# autouse fixture under pytest, and around each fn() in __main__) so it never leaks to other
# suites — a module-level patch permanently poisoned test_provider_failover.py's integration
# tests at collection time. See tests/_failover_neutralise.py.
from _failover_neutralise import identity_provider_chain  # noqa: E402

import reviewlib.panel as panel  # noqa: E402
from reviewlib.backends import ReviewResult  # noqa: E402
from reviewlib.config import (  # noqa: E402
    DEFAULT_BOARD,
    DEFAULT_POOL_SIZE,
    GLM_COMMANDCODE_SEAT,
    BoardReviewer,
    split_pool_reserve,
)
from reviewlib.panel import (  # noqa: E402
    result_is_usable,
    run_board_with_failover,
)

PROMPT = "Review this diff."

try:
    import pytest  # noqa: E402

    @pytest.fixture(autouse=True)
    def _neutralise_provider_failover():
        """Every test in this file runs with seat-level provider-failover neutralised to an
        identity chain, RESTORED afterwards (never leaks to other suites)."""
        with identity_provider_chain():
            yield
except ImportError:  # plain-script harness (no pytest) applies it in __main__ instead
    pass


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
            def _backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
                self.calls.append((model, prompt))
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
    body = (
        "Claude Fable 5 is currently unavailable. "
        "Learn more: https://www.anthropic.com/news/fable-mythos-access"
    )
    assert not result_is_usable(ReviewResult("claude:claude-fable-5", "c", 0, body, ""))


def test_usable_long_review_mentioning_availability_is_not_misclassified():
    """A genuine LONG review that happens to mention availability must NOT be flagged —
    the sentinel check only fires on a SHORT body."""
    body = (
        "x" * 500
        + " the service is currently unavailable in some regions, consider a retry"
    )
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
    """Fable (priority #1) unavailable -> the top-4 pool starts at Sol and pulls one
    more in from the reserve, so it is still 4 WORKING seats."""
    board = list(DEFAULT_BOARD)
    available = {r.model for r in board if r.model != "claude:claude-fable-5"}
    pool, reserve = split_pool_reserve(board, 4, _avail(available))
    assert [r.model for r in pool] == [
        "codex:gpt-5.6-sol",
        "claude:claude-opus-4-8",
        "commandcode:zai-org/GLM-5.2",
        "oc:commandcode/moonshotai/Kimi-K2.7-Code",
    ], [r.model for r in pool]
    # Fable is NOT in the pool nor the reserve — it's unavailable.
    assert "claude:claude-fable-5" not in {r.model for r in pool + reserve}


def test_fable_backfill_keeps_four_distinct_lenses_with_sol():
    """With Sol inserted after Fable, losing Fable now backfills with Kimi and keeps four
    distinct lenses: consistency/correctness/performance/quality."""
    board = list(DEFAULT_BOARD)
    available = {r.model for r in board if r.model != "claude:claude-fable-5"}
    pool, _ = split_pool_reserve(board, DEFAULT_POOL_SIZE, _avail(available))
    roles = [r.role for r in pool]
    assert len(set(roles)) == 4, roles
    assert set(roles) == {"consistency", "correctness", "performance", "quality"}, roles
    assert "architect" not in roles, roles  # Fable's lens is the one lost, as expected.


def test_glm_cc_unavailable_keeps_four_distinct_lenses():
    """When GLM-cc itself is unavailable, Kimi backfills and the pool still keeps four
    distinct lenses (architect/consistency/correctness/quality)."""
    board = list(DEFAULT_BOARD)
    available = {r.model for r in board if r.model != GLM_COMMANDCODE_SEAT}
    pool, _ = split_pool_reserve(board, DEFAULT_POOL_SIZE, _avail(available))
    models = [r.model for r in pool]
    assert GLM_COMMANDCODE_SEAT not in models, models
    roles = [r.role for r in pool]
    assert len(set(roles)) == 4, roles  # four DISTINCT lenses, none lost
    assert set(roles) == {"architect", "consistency", "correctness", "quality"}, roles


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
    Sol / Opus / GLM-cc / Kimi — Fable replaced by the first reserve (Kimi, #5)."""
    board = list(DEFAULT_BOARD)
    # Cheap probe: ALL available (Fable's paywall is invisible to it).
    pool, reserve = split_pool_reserve(board, 4, _avail({r.model for r in board}))
    assert pool[0].model == "claude:claude-fable-5"  # Fable IS selected at startup
    fable_body = (
        "Claude Fable 5 is currently unavailable. "
        "Learn more: https://www.anthropic.com/news/fable-mythos-access"
    )
    with _FakeBackends({"claude:claude-fable-5": (0, fable_body)}):
        outcome = run_board_with_failover(pool, reserve, PROMPT, "+x", REPO_ROOT, 5)
    assert len(outcome.usable) == 4
    assert not outcome.degraded
    assert "claude:claude-fable-5" not in outcome.usable_models
    assert set(outcome.usable_models) == {
        "codex:gpt-5.6-sol",
        "claude:claude-opus-4-8",
        "commandcode:zai-org/GLM-5.2",
        "oc:commandcode/moonshotai/Kimi-K2.7-Code",
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
        pool[1].model: (0, ""),  # empty output = failure
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
    pool, reserve = split_pool_reserve(
        board, 4, _avail({r.model for r in DEFAULT_BOARD[:4]})
    )
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
    assert failed_label in ran_labels, ran_labels  # the failed seat is in results
    assert backfill_label in ran_labels, ran_labels  # so is its replacement
    assert len(outcome.results) == 5  # 4 pool + 1 backfill
    assert len(outcome.usable) == 4
    assert reserve[0].model in outcome.usable_models  # bare id of the backfill


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


# === usable_roles (review-cli#221 --min-roles) ==================================
def test_usable_roles_index_aligned_with_usable_models_review_cli_221():
    """FailoverOutcome.usable_roles is index-aligned with usable_models, one board
    ROLE per USABLE seat -- including a shortage-resilience duplicate-model role-
    fill seat (PR #207: `config.select_pool_with_reuse` reuses an already-picked
    model onto an otherwise-empty role slot), which repeats a `.model` string but
    still carries its OWN distinct `.role`."""
    pool = [
        BoardReviewer("fable", "architect", "Fable"),
        BoardReviewer("opus", "correctness", "Opus"),
        # Duplicated-model role-fill: same model as pool[0] ("fable"), a DIFFERENT
        # role -- exactly what select_pool_with_reuse produces under scarcity.
        BoardReviewer("fable", "security", "Fable"),
    ]
    with _FakeBackends():
        outcome = run_board_with_failover(pool, [], PROMPT, "+x", REPO_ROOT, 5)
    assert not outcome.degraded
    assert outcome.usable_models == ["fable", "opus", "fable"]
    assert outcome.usable_roles == ["architect", "correctness", "security"]


def test_usable_roles_only_covers_seats_that_produced_a_verdict():
    """A FAILED seat contributes nothing to `usable_roles`, matching `usable_models`
    -- only the seat that actually backfills it (with its own role) counts."""
    board = [
        BoardReviewer("m-pool", "architect", "P"),
        BoardReviewer("m-reserve", "security", "R"),
    ]
    pool, reserve = split_pool_reserve(board, 1, _avail({"m-pool", "m-reserve"}))
    with _FakeBackends({"m-pool": (1, "boom")}):
        outcome = run_board_with_failover(pool, reserve, PROMPT, "+x", REPO_ROOT, 5)
    assert outcome.usable_models == ["m-reserve"]
    assert outcome.usable_roles == ["security"]


def test_usable_roles_stay_aligned_with_usable_models_when_a_middle_seat_backfills_review_cli_221():
    """Round-2/6/7 review finding (Opus, raised repeatedly): confirm — with a
    DISCRIMINATING multi-seat case, not the single-element pool the earlier test
    used — that a backfilled seat can never desync `usable_models`/`usable_roles`
    from each other. A single-usable-seat list is trivially 'aligned' under any
    ordering; this needs >=2 REAL successes plus one failure+backfill, with the
    failure in the MIDDLE of the pool (not first or last), so a mispaired append
    would produce a visibly wrong pairing, not just a wrong length."""
    pool = [
        BoardReviewer("m-a", "architect", "A"),
        BoardReviewer("m-b", "correctness", "B"),  # this one fails and backfills
        BoardReviewer("m-c", "consistency", "C"),
    ]
    reserve = [BoardReviewer("m-r", "security", "R")]
    with _FakeBackends({"m-b": (1, "boom")}):
        outcome = run_board_with_failover(pool, reserve, PROMPT, "+x", REPO_ROOT, 5)
    assert not outcome.degraded
    assert outcome.usable_models == ["m-a", "m-c", "m-r"]
    assert outcome.usable_roles == ["architect", "consistency", "security"]
    # Pairwise, not just as two independently-correct lists: each model landed
    # with the role it ACTUALLY reviewed under, not a neighbor's.
    assert list(zip(outcome.usable_models, outcome.usable_roles)) == [
        ("m-a", "architect"),
        ("m-c", "consistency"),
        ("m-r", "security"),
    ]


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                with identity_provider_chain():
                    fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failures else 0)
