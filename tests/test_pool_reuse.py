#!/usr/bin/env python3
"""Unit tests for model reuse + usage-limit exclusion in board/panel composition
(select_pool_with_reuse / expand_flat_models_with_reuse, reviewlib.config).

Covers, all offline (no model call, no network — `usage_percent` is a plain
in-memory dict lookup, never the real tg-ctl file):
  (a) enough distinct models -> IDENTICAL output to select_pool/the plain list
      (both when usage_percent is None and when nothing is excluded);
  (b) fewer distinct models than roles -> reuse fills every role, cycling
      through the available models, each repeat taking a DIFFERENT board role;
  (c) some models excluded by the 70%-threshold -> the pool shrinks BEFORE
      role-filling, then reuse pads it back up from what remains;
  (d) every model excluded -> falls back to the single least-depleted model,
      repeated for every role/slot, rather than an empty board;
  (e) a model with unknown/unavailable usage data is NOT excluded (fails open).

Plain-script harness (mirrors tests/test_failover_pool.py): each test_* is run
by __main__, and also pytest-discoverable.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib.config import (  # noqa: E402
    DEFAULT_BOARD,
    BoardReviewer,
    expand_flat_models_with_reuse,
    select_pool,
    select_pool_and_reserve_with_reuse,
    select_pool_with_reuse,
)

BOARD = [
    BoardReviewer("fable", "architect", "Fable"),
    BoardReviewer("sol", "consistency", "Sol"),
    BoardReviewer("opus", "correctness", "Opus"),
    BoardReviewer("glm-cc", "performance", "GLM-cc"),
    BoardReviewer("kimi", "quality", "Kimi"),
    BoardReviewer("codex", "consistency", "Codex"),
]


def _usage(pcts: dict[str, float]):
    return lambda model: pcts.get(model)


def test_enough_models_no_usage_percent_matches_select_pool():
    reuse = select_pool_with_reuse(BOARD, 4)
    plain = select_pool(BOARD, 4)
    assert reuse == plain
    assert [r.model for r in reuse] == ["fable", "sol", "opus", "glm-cc"]


def test_enough_models_with_usage_percent_nothing_excluded_matches_select_pool():
    usage_percent = _usage({})  # every model unknown -> nothing excluded
    reuse = select_pool_with_reuse(BOARD, 4, usage_percent=usage_percent)
    plain = select_pool(BOARD, 4)
    assert reuse == plain


def test_raw_scarcity_without_usage_percent_still_clamps_like_select_pool():
    # Reuse is a USAGE-EXCLUSION recovery mechanism, not a general "not enough
    # distinct models" one: with no `usage_percent` given at all, only fable
    # and glm-cc are AVAILABLE (2 of 6), and the pool still clamps to 2 —
    # identical to plain `select_pool` (no reuse happens).
    available = {"fable", "glm-cc"}
    result = select_pool_with_reuse(BOARD, 4, available=lambda r: r.model in available)
    assert len(result) == 2
    assert [r.model for r in result] == ["fable", "glm-cc"]


def test_raw_scarcity_with_usage_percent_but_nothing_excluded_still_clamps():
    # Same raw scarcity, but usage_percent IS supplied this time with nothing
    # actually near its limit — still no reuse, because `under_limit` here
    # equals `reachable` (2 seats), which is < the requested pool of 4 but
    # reuse only pads the CANDIDATE pool back to `n`, and `n` itself is
    # clamped to `len(reachable)` = 2 (see `_effective_pool_size`). Reuse
    # fills a SHRUNK candidate pool up to `n`, it does not grow `n` itself.
    available = {"fable", "glm-cc"}
    result = select_pool_with_reuse(
        BOARD, 4, available=lambda r: r.model in available, usage_percent=_usage({})
    )
    assert len(result) == 2
    assert [r.model for r in result] == ["fable", "glm-cc"]


def test_limit_exclusion_shrinks_pool_before_reuse():
    # All 6 board seats are reachable, but sol/opus/kimi/codex are all >=70% used —
    # only fable and glm-cc remain under the threshold. Pool wants 4 seats: reuse
    # must fill the remaining 2 slots by cycling fable/glm-cc again, each under a
    # DIFFERENT (unused) role.
    usage_percent = _usage(
        {"sol": 71, "opus": 95, "kimi": 70, "codex": 88, "fable": 10, "glm-cc": 20}
    )
    result = select_pool_with_reuse(BOARD, 4, usage_percent=usage_percent)
    assert len(result) == 4
    models = [r.model for r in result]
    roles = [r.role for r in result]
    assert models == ["fable", "glm-cc", "fable", "glm-cc"]
    assert len(set(roles)) == 4  # every slot has a DISTINCT role/lens
    assert roles[0] == "architect"  # fable keeps its own role first
    assert roles[1] == "performance"  # glm-cc keeps its own role first


def test_real_default_board_duplicate_roles_never_dispatch_byte_identical_seats():
    # DEFAULT_BOARD genuinely repeats two roles ("consistency": Sol + Codex;
    # "quality": Kimi + GLM). Excluding everything except the two seats that
    # share ONE of those roles must never pad a second slot onto that SAME
    # role — that would dispatch the same model under the same role/lens
    # twice, a pure-cost duplicate the "N distinct lenses" contract forbids
    # (k3 review finding, review-cli#205 round 3).
    sol, opus, glm_cc, kimi, codex, qwen, deepseek, gemini, glm, fable = DEFAULT_BOARD
    assert sol.role == "consistency" and codex.role == "consistency"
    usage_percent = _usage(
        {m.model: 99 for m in DEFAULT_BOARD if m.model not in (sol.model, codex.model)}
    )
    result = select_pool_with_reuse(list(DEFAULT_BOARD), 4, usage_percent=usage_percent)
    seen = set()
    for r in result:
        key = (r.model, r.role)
        assert key not in seen, f"byte-identical duplicate seat dispatched: {key}"
        seen.add(key)
    # Both under-limit seats (sol, codex) share role "consistency" -- padding
    # must borrow roles from ELSEWHERE on the board (architect/correctness/...)
    # for the extra slots, never repeat "consistency" onto a second slot for
    # the same model. Plenty of other distinct roles exist on the 10-seat
    # board, so all 4 requested slots fill (this fixture doesn't exercise the
    # separate "roles genuinely exhausted -> stop short" branch).
    assert len(result) == 4


def test_total_exhaustion_falls_back_to_least_depleted_single_model():
    usage_percent = _usage(
        {"fable": 99, "sol": 95, "opus": 91, "glm-cc": 100, "kimi": 88, "codex": 71}
    )
    result = select_pool_with_reuse(BOARD, 4, usage_percent=usage_percent)
    assert len(result) == 4
    models = {r.model for r in result}
    assert models == {"codex"}  # codex (71) is the least-depleted of the six
    roles = [r.role for r in result]
    assert len(set(roles)) == 4  # still 4 distinct lenses, just one model


def test_unknown_usage_is_not_excluded():
    # opus has NO usage sample (None) — must be treated as "not near limit", never
    # excluded, even though every OTHER seat is deep in its limit.
    usage_percent = _usage({"fable": 99, "sol": 95, "opus": None, "glm-cc": 100})
    result = select_pool_with_reuse(BOARD, 2, usage_percent=usage_percent)
    models = [r.model for r in result]
    assert "opus" in models


def test_explicit_threshold_is_honored():
    usage_percent = _usage({"fable": 50, "sol": 50})
    strict = select_pool_with_reuse(
        BOARD[:2], 2, usage_percent=usage_percent, limit_threshold=40
    )
    # both models are >=40 -> both excluded -> fallback to the single least-depleted
    assert {r.model for r in strict} == {"fable"}  # tie -> first in priority order

    lenient = select_pool_with_reuse(
        BOARD[:2], 2, usage_percent=usage_percent, limit_threshold=60
    )
    assert {r.model for r in lenient} == {"fable", "sol"}


def test_role_less_board_never_reuses():
    # A config `models:` roster with no lens metadata carries role="" for every
    # seat. Reuse would add pure cost (an extra dispatch) with zero added
    # review diversity there, so it must stay a plain shrink instead.
    role_less = [
        BoardReviewer("fable", "", "Fable"),
        BoardReviewer("glm-cc", "", "GLM-cc"),
    ]
    usage_percent = _usage({"fable": 90, "glm-cc": 10})
    result = select_pool_with_reuse(role_less, 4, usage_percent=usage_percent)
    assert len(result) == 1  # excluded fable, kept glm-cc, did NOT pad back to 4
    assert result[0].model == "glm-cc"


# --- expand_flat_models_with_reuse (quorum/just-ask/brainstorm's flat panel) -------


def test_flat_no_usage_percent_is_identity():
    models = ["fable", "sol", "opus"]
    assert expand_flat_models_with_reuse(models, 3) == models


def test_flat_nothing_excluded_is_identity():
    models = ["fable", "sol", "opus"]
    assert expand_flat_models_with_reuse(models, 3, usage_percent=_usage({})) == models


def test_flat_reuse_pads_back_to_target():
    result = expand_flat_models_with_reuse(
        ["fable", "glm-cc"], 4, usage_percent=_usage({})
    )
    assert result == ["fable", "glm-cc", "fable", "glm-cc"]


def test_flat_excludes_near_limit_then_reuses_remainder():
    models = ["fable", "sol", "opus", "glm-cc"]
    usage_percent = _usage({"sol": 90, "opus": 85, "fable": 5, "glm-cc": 15})
    result = expand_flat_models_with_reuse(models, 4, usage_percent=usage_percent)
    assert result == ["fable", "glm-cc", "fable", "glm-cc"]


def test_flat_total_exhaustion_falls_back_to_single_least_depleted():
    models = ["fable", "sol"]
    usage_percent = _usage({"fable": 99, "sol": 95})
    result = expand_flat_models_with_reuse(models, 3, usage_percent=usage_percent)
    assert result == ["sol", "sol", "sol"]


# --- select_pool_and_reserve_with_reuse (the board-mode dispatch wiring) -----------


def test_reserve_is_seat_identity_not_model_string():
    # A config board that legitimately lists the SAME model under two DIFFERENT
    # roles is a supported configuration (board_from_models / role metadata) —
    # filtering reserve by `.model` string would wrongly drop the second seat's
    # reserve slot just because the FIRST seat (same model) landed in the pool.
    dup_model_board = [
        BoardReviewer("fable", "architect", "Fable-A"),
        BoardReviewer("sol", "consistency", "Sol"),
        BoardReviewer("fable", "security", "Fable-B"),  # same model, distinct seat
    ]
    pool, reserve = select_pool_and_reserve_with_reuse(dup_model_board, 2)
    assert [r.display for r in pool] == ["Fable-A", "Sol"]
    # Fable-B is a DISTINCT seat object from Fable-A (same model, different
    # role/display) — it must still be reserve-eligible.
    assert [r.display for r in reserve] == ["Fable-B"]


def test_reserve_excludes_near_limit_seat_only_when_it_is_actually_used():
    usage_percent = _usage({"opus": 90})  # opus is near-limit
    pool, reserve = select_pool_and_reserve_with_reuse(
        BOARD, 2, usage_percent=usage_percent
    )
    # pool wants 2: fable (fine) + sol (fine) — opus is skipped for being
    # near-limit, but with 5 OTHER under-limit candidates available no reuse
    # is needed to fill 2 seats.
    assert [r.model for r in pool] == ["fable", "sol"]
    # opus must still be VISIBLE in reserve (coherent policy: a near-limit
    # seat that isn't actually in the pool is a valid last-resort backfill).
    reserve_models = [r.model for r in reserve]
    assert "opus" in reserve_models
    assert set(reserve_models) == {"opus", "glm-cc", "kimi", "codex"}
    # But its ORDER is demoted to the END: opus sits at board position 2
    # (ahead of glm-cc/kimi/codex), yet mid-run failover must try the
    # perfectly healthy overflow seats FIRST, not promote the very
    # near-limit account this feature exists to protect (Fable review
    # finding, review-cli#205 round 5). Board-priority order is preserved
    # WITHIN each group (under-limit seats keep their relative order; here
    # there's only one near-limit seat, so its own relative order is moot).
    assert reserve_models == ["glm-cc", "kimi", "codex", "opus"]


def test_reserve_ordering_preserves_relative_priority_within_each_group():
    # BOARD board-priority order: fable, sol, opus, glm-cc, kimi, codex.
    # pool=1 -> only fable is primary. Reserve holds the other 5: opus and
    # kimi are near-limit; sol, glm-cc, codex are under-limit. Each group
    # must keep its OWN relative board-priority order after the demotion.
    usage_percent = _usage({"opus": 80, "kimi": 90})
    _, reserve = select_pool_and_reserve_with_reuse(
        BOARD, 1, usage_percent=usage_percent
    )
    assert [r.model for r in reserve] == ["sol", "glm-cc", "codex", "opus", "kimi"]


def test_pool_and_reserve_probe_availability_exactly_once():
    calls = []

    def _available(r: BoardReviewer) -> bool:
        calls.append(r.model)
        return True

    select_pool_and_reserve_with_reuse(BOARD, 3, _available)
    # One probe per board seat, not two (pool split + reserve split each
    # re-probing independently would double this).
    assert len(calls) == len(BOARD)


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
