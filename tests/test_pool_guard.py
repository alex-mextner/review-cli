#!/usr/bin/env python3
"""Unit tests for the pool/model-selection FOOLPROOFING (защита от дурака).

`reviewlib.pool_guard.evaluate_selection` is the pre-dispatch guard that turns a
non-convergent selection into an ACTIONABLE outcome instead of a silent, degenerate
review. Three branches are proven here, all OFFLINE (availability + reason are injected
closures — no backend, no network):

  (1) explicit `-m` narrows the set, some picks are DOWN, and the live subset can't fill
      the requested size  -> a PROPOSAL listing only the fallbacks that CAN converge, each
      with its FULL model list annotated live/down + per-seat reason;
  (2) `--pool N` larger than the live count                    -> propose not overriding
      the pool, showing the default option + live/down annotations;
  (3) even the default board can't muster the minimum panel    -> a TARGETED per-provider
      error enumerating WHY each dead seat is down, plus the current live-model list.

Also proven: the happy path returns PROCEED untouched; codex and Sol collapse to ONE
distinct engine (they share an underlying model); and EVERY printed model list carries
the per-seat live/down+reason annotation (the CTO refinement).

Plain-script harness (mirrors tests/test_failover_pool.py): each test_* is run by __main__.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib.config import SOL_SEAT  # noqa: E402
from reviewlib.pool_guard import (  # noqa: E402
    EXIT_UNSATISFIED,
    MIN_CONVERGE_MODELS,
    PROCEED,
    PROPOSE,
    UNSATISFIED,
    Candidate,
    default_distinct_key,
    evaluate_selection,
)


# === injected availability harness ==============================================
def _avail(live: set[str]):
    return lambda model: model in live


def _reason(reasons: dict[str, str]):
    """Return a `reason(model)` closure: None when live, else the mapped down-reason."""
    return lambda model: reasons.get(model)


def _cand(label: str, why: str, board, pool_size: int) -> Candidate:
    return Candidate(label=label, why=why, board=tuple(board), pool_size=pool_size)


LIVE_ALL = {
    "claude:claude-opus-4-8",
    "codex",
    "gemini:gemini-2.5-flash",
    "oc:zai/glm-5.2",
    "claude:claude-fable-5",
    SOL_SEAT,
}
DOWN_REASONS = {
    "commandcode:moonshotai/Kimi-K2.7-Code": "commandcode: insufficient credits (provider marked unpaid)",
    "commandcode:zai-org/GLM-5.2": "commandcode: insufficient credits (provider marked unpaid)",
    "gemini:gemini-2.5-flash": "gemini: GEMINI_API_KEY not found",
}
DEFAULT_BOARD_SPEC = [
    ("claude:claude-opus-4-8", "Opus"),
    ("codex", "Codex"),
    ("gemini:gemini-2.5-flash", "Gemini"),
    ("oc:zai/glm-5.2", "GLM"),
    ("commandcode:moonshotai/Kimi-K2.7-Code", "Kimi-cc"),
    ("commandcode:zai-org/GLM-5.2", "GLM-cc"),
]


# === happy path ==================================================================
def test_selection_that_converges_proceeds():
    """All requested seats live and >= requested size -> PROCEED, no message."""
    board = [("claude:claude-opus-4-8", "Opus"), ("codex", "Codex")]
    d = evaluate_selection(
        user_board=board,
        requested_size=2,
        explicit=True,
        candidates=[],
        available=_avail(LIVE_ALL),
        reason=_reason({}),
    )
    assert d.kind == PROCEED, d.kind
    assert d.exit_code == 0
    assert d.text == ""


def test_candidates_thunk_not_realized_on_proceed():
    """`candidates` may be a THUNK realized ONLY on the non-converge path — so a converging
    happy-path run never rebuilds the fallback boards (which re-resolve presets and can
    re-emit load_board stderr warnings). The thunk must NOT be called when PROCEED."""
    board = [("claude:claude-opus-4-8", "Opus"), ("codex", "Codex")]

    def _boom():
        raise AssertionError(
            "candidates thunk must not be realized on the PROCEED path"
        )

    d = evaluate_selection(
        user_board=board,
        requested_size=2,
        explicit=True,
        candidates=_boom,
        available=_avail(LIVE_ALL),
        reason=_reason({}),
    )
    assert d.kind == PROCEED, d.text


def test_default_path_with_enough_live_proceeds():
    """No -m / no --pool: proceeds as long as the board clears the convergence floor,
    even if fewer live than the nominal pool (the pool auto-shrinks)."""
    live = {"claude:claude-opus-4-8", "codex"}  # only 2 of 6 live
    d = evaluate_selection(
        user_board=DEFAULT_BOARD_SPEC,
        requested_size=4,
        explicit=False,
        candidates=[],
        available=_avail(live),
        reason=_reason(DOWN_REASONS),
    )
    assert d.kind == PROCEED, d.text


# === branch 1: explicit -m can't fill the requested size =========================
def test_explicit_narrow_cannot_converge_proposes_only_fitting_options():
    """User -m picks 4 models, 2 are down -> live subset (2) < requested (4). Propose the
    fallbacks that CAN converge; a too-small preset that can't is filtered out."""
    user = [
        ("claude:claude-opus-4-8", "Opus"),
        ("gemini:gemini-2.5-flash", "Gemini"),  # down
        ("commandcode:moonshotai/Kimi-K2.7-Code", "Kimi-cc"),  # down
        ("codex", "Codex"),
    ]
    default_cand = _cand(
        "default", "drop -m and run the default board", DEFAULT_BOARD_SPEC, 4
    )
    dead_preset = _cand(
        "preset:dead",
        "--preset dead",
        [
            ("gemini:gemini-2.5-flash", "Gemini"),
            ("commandcode:zai-org/GLM-5.2", "GLM-cc"),
        ],
        2,
    )  # both down -> can't converge -> must be hidden
    d = evaluate_selection(
        user_board=user,
        requested_size=4,
        explicit=True,
        candidates=[default_cand, dead_preset],
        available=_avail(LIVE_ALL - {"gemini:gemini-2.5-flash"}),
        reason=_reason(DOWN_REASONS),
    )
    assert d.kind == PROPOSE, d.kind
    assert d.exit_code == EXIT_UNSATISFIED
    # the viable default option is offered, the dead preset is filtered out
    assert "default board" in d.text
    assert "preset:dead" not in d.text and "--preset dead" not in d.text
    # per-seat annotation: a down seat shows its reason inline
    assert "insufficient credits" in d.text
    # the always-on current-live-models line is present
    assert "live model" in d.text.lower()


def test_proposal_annotates_every_seat_live_or_down_with_reason():
    """Each model list in a proposal shows per-seat live/down + reason (CTO refinement)."""
    user = [("commandcode:moonshotai/Kimi-K2.7-Code", "Kimi-cc")]  # single, down
    default_cand = _cand(
        "default", "drop -m and run the default board", DEFAULT_BOARD_SPEC, 4
    )
    d = evaluate_selection(
        user_board=user,
        requested_size=1,
        explicit=True,
        candidates=[default_cand],
        available=_avail(LIVE_ALL),
        reason=_reason(DOWN_REASONS),
    )
    assert d.kind == PROPOSE, d.text
    # the default option lists a live seat (Opus) AND a down seat (Kimi-cc) with reason
    assert "Opus" in d.text
    assert "Kimi-cc" in d.text
    assert "insufficient credits" in d.text
    # live seats are marked distinctly from down seats
    assert "claude:claude-opus-4-8" in d.text


# === branch 2: --pool N larger than live count ===================================
def test_pool_larger_than_live_proposes_not_overriding():
    """--pool 6 but only 2 live -> propose NOT overriding the pool; show the default
    board option (which converges) with live/down annotations."""
    live = {"claude:claude-opus-4-8", "codex"}
    default_cand = _cand(
        "default",
        "don't pass --pool; the default pool size runs the live board",
        DEFAULT_BOARD_SPEC,
        4,
    )
    d = evaluate_selection(
        user_board=DEFAULT_BOARD_SPEC,
        requested_size=6,
        explicit=True,
        candidates=[default_cand],
        available=_avail(live),
        reason=_reason(DOWN_REASONS),
    )
    assert d.kind == PROPOSE, d.text
    assert "default" in d.text
    # shows what would actually run + reasons for the down seats
    assert "insufficient credits" in d.text


# === branch 3: default board itself can't converge ===============================
def test_default_cannot_converge_targeted_per_provider_error():
    """Fewer distinct live models than the floor, and NO candidate converges -> a targeted
    per-provider error enumerating each dead seat's reason + the live-model list."""
    live = {"claude:claude-opus-4-8"}  # 1 live < MIN_CONVERGE_MODELS
    default_cand = _cand("default", "default board", DEFAULT_BOARD_SPEC, 4)
    d = evaluate_selection(
        user_board=DEFAULT_BOARD_SPEC,
        requested_size=4,
        explicit=False,
        candidates=[default_cand],
        available=_avail(live),
        reason=_reason(DOWN_REASONS),
    )
    assert d.kind == UNSATISFIED, d.kind
    assert d.exit_code == EXIT_UNSATISFIED
    # targeted: enumerate the specific dead providers/reasons
    assert "commandcode" in d.text
    assert "insufficient credits" in d.text
    assert "GEMINI_API_KEY" in d.text
    # still shows the current live list (Opus)
    assert "claude:claude-opus-4-8" in d.text


def test_error_headline_uses_per_board_best_not_union_count():
    """The UNSATISFIED headline count is the BEST any SINGLE board achieves, never the union
    across boards. Two candidate boards that each contribute ONE distinct live model (a
    different one) can't converge (floor 2), but a union count would print the
    self-contradictory 'only 2 … need >= 2'. The headline must say 'at most 1 … need >= 2'."""
    # user board: only Opus live (1 distinct). Two presets, each with exactly one DIFFERENT
    # live model, so the union of live models is 2 but no single board reaches the floor.
    user = [("claude:claude-opus-4-8", "Opus"), ("gemini:gemini-2.5-flash", "Gemini")]
    preset_a = _cand(
        "preset:a",
        "--preset a",
        [
            ("codex", "Codex"),
            ("gemini:gemini-2.5-flash", "Gemini"),  # only Codex live
        ],
        2,
    )
    preset_b = _cand(
        "preset:b",
        "--preset b",
        [
            ("oc:zai/glm-5.2", "GLM"),
            ("commandcode:zai-org/GLM-5.2", "GLM-cc"),  # only GLM live
        ],
        2,
    )
    live = {
        "claude:claude-opus-4-8",
        "codex",
        "oc:zai/glm-5.2",
    }  # 1 per board, 3 in union
    d = evaluate_selection(
        user_board=user,
        requested_size=2,
        explicit=False,
        candidates=[preset_a, preset_b],
        available=_avail(live),
        reason=_reason(DOWN_REASONS),
    )
    assert d.kind == UNSATISFIED, d.text
    assert "at most 1 distinct live" in d.text, d.text
    # never the self-contradictory union count that equals/exceeds the floor
    assert (
        "at most 2 distinct live" not in d.text
        and "at most 3 distinct live" not in d.text
    ), d.text


def test_error_when_nothing_is_live_lists_no_live_models():
    """Zero live seats -> error path that says there are no live models (never a blank)."""
    default_cand = _cand("default", "default board", DEFAULT_BOARD_SPEC, 4)
    d = evaluate_selection(
        user_board=DEFAULT_BOARD_SPEC,
        requested_size=4,
        explicit=False,
        candidates=[default_cand],
        available=_avail(set()),
        reason=_reason(DOWN_REASONS),
    )
    assert d.kind == UNSATISFIED, d.kind
    assert d.exit_code == EXIT_UNSATISFIED
    # the live-model line must be present even when empty (no silent blank)
    assert "live model" in d.text.lower()


# === codex / sol collapse to one distinct engine =================================
def test_codex_and_sol_collapse_to_one_engine_for_viability():
    """codex and Sol share an underlying model. A fallback made of ONLY codex+Sol — even
    with both live — is ONE distinct engine, so it must NOT be offered as a viable
    (>= floor) proposal; the guard must not present it as a real 2-model panel."""
    assert default_distinct_key("codex") == default_distinct_key(SOL_SEAT)
    user = [
        ("commandcode:moonshotai/Kimi-K2.7-Code", "Kimi-cc")
    ]  # single dead -> can't converge
    codex_sol_only = _cand(
        "preset:codexish",
        "--preset codexish",
        [
            ("codex", "Codex"),
            (SOL_SEAT, "Sol"),
        ],
        2,
    )  # both LIVE but ONE engine -> distinct_live 1 < floor -> filtered out
    real_default = _cand(
        "default", "drop -m and run the default board", DEFAULT_BOARD_SPEC, 4
    )
    d = evaluate_selection(
        user_board=user,
        requested_size=1,
        explicit=True,
        candidates=[codex_sol_only, real_default],
        available=_avail(LIVE_ALL),
        reason=_reason(DOWN_REASONS),
    )
    assert d.kind == PROPOSE, d.text
    assert "codexish" not in d.text  # the one-engine option is filtered out
    assert "default board" in d.text


def test_deliberate_fully_live_narrow_selection_proceeds():
    """A fully-live `-m codex,sol` proceeds (both reachable) — the guard never blocks a
    selection whose every seat is live, even when they dedupe to one engine."""
    user = [("codex", "Codex"), (SOL_SEAT, "Sol")]
    d = evaluate_selection(
        user_board=user,
        requested_size=2,
        explicit=True,
        candidates=[],
        available=_avail(LIVE_ALL),
        reason=_reason(DOWN_REASONS),
    )
    assert d.kind == PROCEED, d.text


def test_convergence_floor_is_at_least_two():
    """A multi-model panel needs >= 2 distinct live models by default."""
    assert MIN_CONVERGE_MODELS >= 2


def test_single_seat_board_floor_caps_to_board_size():
    """A deliberately configured 1-seat board must RUN its one live model, not demand two
    (the floor caps at the board's distinct total)."""
    d = evaluate_selection(
        user_board=[("codex", "Codex")],
        requested_size=0,
        explicit=False,
        candidates=[],
        available=_avail({"codex"}),
        reason=_reason({}),
    )
    assert d.kind == PROCEED, d.text


def test_provider_variants_of_one_model_are_one_distinct_engine():
    """The guard must agree with provider-failover on model identity: `zai:glm-5.2`,
    `oc:zai/glm-5.2` and `commandcode:zai-org/GLM-5.2` are ONE engine (cross-PROVIDER
    collapse), and `opus` collapses with its oc: spelling too. This keeps the guard's
    'distinct live models' count consistent with what provider-failover treats as one model.

    Consequence, mirroring codex/sol: an all-live `-m zai:glm-5.2,oc:zai/glm-5.2` is a
    1-engine request, not a 2-model panel. cli._evaluate_pool_or_bail sizes an -m request by
    the DISTINCT-engine count (`len({default_distinct_key(m)...})`), so it passes
    requested_size=1 here — and an all-live selection PROCEEDs (the guard never blocks a
    selection whose every seat is live; see test_deliberate_fully_live_narrow_selection)."""
    k = default_distinct_key
    assert k("zai:glm-5.2") == k("oc:zai/glm-5.2") == k("commandcode:zai-org/GLM-5.2")
    assert k("claude:claude-opus-4-8") == k("oc:anthropic/claude-opus-4-8")
    user = [("zai:glm-5.2", "GLM-a"), ("oc:zai/glm-5.2", "GLM-b")]
    requested = len({k(m) for m, _n in user})  # what cli passes: distinct engines == 1
    assert requested == 1
    default_cand = _cand("default", "default board", DEFAULT_BOARD_SPEC, 4)
    d = evaluate_selection(
        user_board=user,
        requested_size=requested,
        explicit=True,
        candidates=[default_cand],
        available=_avail(LIVE_ALL | {"zai:glm-5.2", "oc:zai/glm-5.2"}),
        reason=_reason(DOWN_REASONS),
    )
    assert d.kind == PROCEED, d.text


def test_distinct_key_keeps_other_models_separate():
    """The codex/sol collapse must NOT bleed into unrelated models."""
    keys = {
        default_distinct_key("claude:claude-opus-4-8"),
        default_distinct_key("claude:claude-fable-5"),
        default_distinct_key("gemini:gemini-2.5-flash"),
        default_distinct_key("codex"),
    }
    assert len(keys) == 4  # opus, fable, gemini, codex-engine all distinct


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
