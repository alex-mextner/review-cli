"""Pool/model-selection FOOLPROOFING (защита от дурака) for `review diff`.

WHAT: a pre-dispatch guard. When the user narrows the reviewer set with `-m` or sizes it
with `--pool N` and the LIVE subset can't satisfy that request — or the default board
itself can't muster a minimum panel — this turns the (previously silent, degenerate) run
into an ACTIONABLE outcome:
  * PROCEED     — the selection has enough distinct live models; run unchanged.
  * PROPOSE     — the request can't converge, but at least one fallback (the default board
                  or a preset) CAN. Print those options, each with its FULL model list
                  annotated live/DOWN + per-seat reason, and exit non-zero so the user
                  re-runs a viable one. Options that can't converge are filtered out.
  * UNSATISFIED — not even a fallback converges. Print a TARGETED per-provider error
                  (which seats are down and WHY), plus the current live-model list.

REACHED FROM: `reviewlib.cli._dispatch` in the `review` mode, right after the board is
resolved and BEFORE the failover pool dispatches (see `cli.evaluate_pool_or_bail`).

WHY A SEPARATE MODULE: the decision logic is pure and inject-testable — availability and
down-reason are passed in as closures over `reviewlib.backends` (`backend_available` /
`backend_unavailable_reason`), so tests exercise every branch OFFLINE with no backend and
no network. See tests/test_pool_guard.py.

INVARIANT: EVERY model list this module prints (proposals AND the error) carries the
per-seat live/DOWN+reason annotation — the user always sees exactly what they'd get and
what's degraded, never an opaque non-convergence.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Sequence

from .config import SOL_SEAT, _expand_alias
from .provider_failover import logical_key

# A multi-model review needs at least this many DISTINCT live models to be a meaningful
# panel. Below it, the default path can't converge (branch 3). A deliberate single-model
# run (`-m opus`) is an EXPLICIT request of size 1 and still proceeds — the floor only
# gates the default path and candidate viability, never an explicit size the user chose.
MIN_CONVERGE_MODELS = 2

# Exit code for a non-convergent selection (proposal printed OR targeted error). Distinct
# from argparse-2, EXIT_NOT_A_REPO=3, EXIT_GIT_DIFF_FAILED=4, brainstorm-5, qa-6..9 so a
# caller/script can tell "the pool couldn't be assembled" apart from every other class.
EXIT_UNSATISFIED = 10

# Decision kinds.
PROCEED = "proceed"
PROPOSE = "propose"
UNSATISFIED = "unsatisfied"

# A bare `codex` model string uses the codex CLI's OWN default (`~/.codex/config.toml`'s
# `model =`), which today happens to be gpt-5.6-sol — the SAME model SOL_SEAT pins
# explicitly. That equivalence is a fact about the codex CLI's local default, not about
# DEFAULT_BOARD (whose priority-5 seat is now pinned to ASTRA_SEAT, gpt-6-astra, a
# genuinely distinct model — see config.py). It still matters here because a user can
# type `-m codex,sol` directly: without this collapse that would count as a 2-model
# panel when it's one engine reviewed twice. For DISTINCT-engine counting the two must
# collapse to one. Only these two collapse; every other `codex:<model>` (including
# ASTRA_SEAT) stays its own key.
_CODEX_ENGINE_KEY = "codex-engine"
_CODEX_SOL_EQUIVALENTS = frozenset({"codex", SOL_SEAT.lower()})


def default_distinct_key(model: str) -> str:
    """Canonical key for counting DISTINCT reviewer engines. Collapses codex/Sol (same
    underlying model) AND provider-variants of one logical model (via provider_failover's
    logical_key — so `zai:glm-5.2`, `oc:zai/glm-5.2`, `commandcode:zai-org/GLM-5.2` are ONE
    engine, and `opus`/`oc:anthropic/claude-opus-4-8` collapse too). This keeps the guard's
    'distinct live models' count consistent with what provider-failover treats as one model,
    so `-m zai:glm-5.2,oc:zai/glm-5.2` is correctly seen as one engine, not a 2-model panel."""
    m = _expand_alias(model).lower()
    if m in _CODEX_SOL_EQUIVALENTS:
        return _CODEX_ENGINE_KEY
    return logical_key(model)


Seat = tuple[str, str]  # (model, display-name)


@dataclass(frozen=True)
class SeatHealth:
    """One seat with its resolved live/down status. `reason` is None iff `live`."""

    model: str
    name: str
    live: bool
    reason: str | None


@dataclass(frozen=True)
class Candidate:
    """A fallback option offered in a proposal: a named board + the pool it would run."""

    label: str
    why: str
    board: tuple[Seat, ...]
    pool_size: int


@dataclass(frozen=True)
class PoolDecision:
    kind: str
    exit_code: int
    text: str


def _annotate(board: Sequence[Seat], available, reason) -> list[SeatHealth]:
    """Resolve each seat's live/down status via the injected closures."""
    out: list[SeatHealth] = []
    for model, name in board:
        live = bool(available(model))
        out.append(
            SeatHealth(
                model,
                name,
                live,
                None if live else (reason(model) or "backend unavailable"),
            )
        )
    return out


def _distinct_live(seats: Sequence[SeatHealth], distinct_key) -> int:
    return len({distinct_key(s.model) for s in seats if s.live})


def _live_universe(
    user: Sequence[SeatHealth], candidates: Sequence[tuple[Candidate, list[SeatHealth]]]
) -> list[SeatHealth]:
    """Distinct live seats across the user board + every candidate, in first-seen order —
    the 'current live models' line shown on every proposal and error."""
    seen: OrderedDict[str, SeatHealth] = OrderedDict()
    for seat in user:
        seen.setdefault(seat.model, seat)
    for _cand, seats in candidates:
        for seat in seats:
            seen.setdefault(seat.model, seat)
    return [s for s in seen.values() if s.live]


def evaluate_selection(
    *,
    user_board: Sequence[Seat],
    requested_size: int,
    explicit: bool,
    candidates: Sequence[Candidate] | Callable[[], Sequence[Candidate]],
    available: Callable[[str], bool],
    reason: Callable[[str], str | None],
    distinct_key: Callable[[str], str] = default_distinct_key,
    min_converge: int = MIN_CONVERGE_MODELS,
) -> PoolDecision:
    """Decide PROCEED / PROPOSE / UNSATISFIED for a resolved review selection.

    `requested_size` is the number of distinct live models the selection targets (the -m
    count, or `--pool N`). `explicit` is True when the user narrowed with -m or sized with
    --pool. The default path (explicit=False) only needs the convergence floor.

    `candidates` may be a ready sequence OR a zero-arg THUNK. It is realized ONLY on the
    non-PROCEED path, so the happy path never builds the fallback boards (which re-resolve
    presets and can re-emit `load_board` stderr warnings for a malformed config board).
    """
    user = _annotate(user_board, available, reason)
    user_live = _distinct_live(user, distinct_key)
    # The convergence floor never exceeds how many distinct seats the board even HAS: a
    # deliberately configured 1-seat board must run its single model, not demand two, and a
    # `--pool N` past the board size clamps (existing behaviour) rather than nagging. So the
    # target is capped at the board's distinct total.
    board_distinct = len({distinct_key(m) for m, _name in user_board})
    target = max(requested_size, 1) if explicit else min_converge
    required = min(target, board_distinct)
    if user_live >= required:
        return PoolDecision(PROCEED, 0, "")

    resolved_candidates = candidates() if callable(candidates) else candidates
    annotated = [
        (c, _annotate(c.board, available, reason)) for c in resolved_candidates
    ]
    viable = [
        (c, seats)
        for c, seats in annotated
        if _distinct_live(seats, distinct_key) >= min_converge
    ]
    live_now = _live_universe(user, annotated)
    if viable:
        text = _render_proposal(
            user, user_live, requested_size, explicit, viable, live_now, distinct_key
        )
        return PoolDecision(PROPOSE, EXIT_UNSATISFIED, text)
    text = _render_error(user, annotated, live_now, min_converge, distinct_key)
    return PoolDecision(UNSATISFIED, EXIT_UNSATISFIED, text)


# --- rendering ------------------------------------------------------------------
def _seat_line(seat: SeatHealth) -> str:
    mark = "✓" if seat.live else "✗"  # ✓ / ✗
    head = f"      {mark} {seat.name:<10} {seat.model}"
    return head if seat.live else f"{head}  — {seat.reason}"


def _seat_block(seats: Sequence[SeatHealth]) -> str:
    return "\n".join(_seat_line(s) for s in seats)


def _live_line(live_now: Sequence[SeatHealth], distinct_key) -> str:
    if not live_now:
        return "Current live models: (none)."
    seen: OrderedDict[str, str] = OrderedDict()
    for s in live_now:
        seen.setdefault(distinct_key(s.model), s.model)
    return "Current live models: " + ", ".join(seen.values()) + "."


def _render_proposal(
    user, user_live, requested_size, explicit, viable, live_now, distinct_key
) -> str:
    lines = ["[review-cli] your model selection can't fill the requested review pool."]
    if explicit and requested_size:
        lines.append(
            f"  requested: {requested_size} distinct live model(s); live in your selection: {user_live}."
        )
    lines.append("  your selection right now:")
    lines.append(_seat_block(user))
    lines.append("")
    lines.append("Options that CAN converge (each shows what would actually run):")
    for cand, seats in viable:
        ran = _distinct_live(seats, distinct_key)
        lines.append(
            f"\n  • {cand.why}  [{cand.label}] — {ran} live of {cand.pool_size} pool:"
        )
        lines.append(_seat_block(seats))
    lines.append("")
    lines.append("Re-run one of the above.")
    lines.append(_live_line(live_now, distinct_key))
    return "\n".join(lines)


def _group_down_by_reason(seats: Sequence[SeatHealth]) -> "OrderedDict[str, list[str]]":
    """Map a down-reason -> the seat names hitting it, over a flat seat list (deduped)."""
    groups: OrderedDict[str, list[str]] = OrderedDict()
    seen: set[tuple[str, str]] = set()
    for s in seats:
        if s.live:
            continue
        key = (s.model, s.reason or "")
        if key in seen:
            continue
        seen.add(key)
        groups.setdefault(s.reason or "backend unavailable", []).append(s.name)
    return groups


def _render_error(user, annotated, live_now, min_converge, distinct_key) -> str:
    # Enumerate down reasons over EVERYTHING the user could reach: the user's own board AND
    # every candidate board (not candidates alone — with no candidates the user board would
    # otherwise be ignored, giving an empty fix list).
    all_seats = [*user, *(s for _c, seats in annotated for s in seats)]
    # The headline count is the BEST any SINGLE board achieves, NOT the union across boards.
    # This branch is only reached when no single board clears the floor, so the per-board max
    # is always < min_converge — a union count could be >= the floor (two boards each add one
    # distinct live model) and print the self-contradictory "only 2 … need >= 2" (glm review).
    best_live = max(
        (_distinct_live(seats, distinct_key) for _c, seats in annotated),
        default=0,
    )
    best_live = max(best_live, _distinct_live(user, distinct_key))
    lines = [
        f"[review-cli] cannot assemble a review pool: at most {best_live} distinct live "
        f"model(s) in any single board, need >= {min_converge}. Nothing in the default board "
        "or presets converges.",
        "",
        "Per-seat status:",
        _seat_block(user),
        "",
        "Fix at least one of these to restore a pool:",
    ]
    for reason, names in _group_down_by_reason(all_seats).items():
        lines.append(f"  - {', '.join(names)}: {reason}")
    lines.append("")
    lines.append(_live_line(live_now, distinct_key))
    return "\n".join(lines)
