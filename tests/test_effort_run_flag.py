#!/usr/bin/env python3
"""Run-scoped `--effort` flag (built ON TOP of the per-seat effort mechanism).

Proves the run-scoped override:
  * parses a global level + `provider=level` per-backend overrides (repeat/comma), and
    fails loudly on an unknown level;
  * resolves `effort_for(provider) or seat_effort` — the flag WINS over the per-seat
    config effort, and falls back to the seat value where the flag is silent;
  * applies onto a reviewer board (the diff-review path) so the failover board carries the
    effective effort — applied exactly ONCE (no double);
  * reaches the flat-panel modes (quorum / just-ask / brainstorm) via ModeContext so each
    PanelJob carries the resolved effort;
  * maps opencode effort to a single `--variant` (the opencode reasoning lever), no dupes.

The vision-client threading (`--effort` reaching the multimodal call + no double
`--effort`/`--variant` in any vision argv) is proven in test_vision_client.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import backends  # noqa: E402
from reviewlib.backends import ReviewResult  # noqa: E402
from reviewlib.config import (  # noqa: E402
    BoardReviewer,
    EffortValueError,
    apply_effort_override,
    parse_effort_flag,
)
from reviewlib.panel import build_board_jobs  # noqa: E402


def _ok(model: str = "m") -> ReviewResult:
    return ReviewResult(model=model, command="c", returncode=0, stdout="ok", stderr="")


# --- parse -------------------------------------------------------------------------
def test_parse_global_level():
    o = parse_effort_flag(["high"])
    assert o.default == "high"
    assert o.by_provider == {}
    assert not o.is_empty


def test_parse_per_provider_override_normalises_route():
    o = parse_effort_flag(["codex=high", "oc=max"])
    assert o.default is None
    # oc: normalises to the opencode ROUTE key (same as a seat resolves to).
    assert o.by_provider == {"codex": "high", "opencode": "max"}


def test_parse_repeat_and_comma_and_precedence():
    o = parse_effort_flag(["medium,codex=low", "opencode=max"])
    assert o.default == "medium"
    assert o.by_provider == {"codex": "low", "opencode": "max"}


def test_parse_empty_is_empty():
    assert parse_effort_flag([]).is_empty
    assert parse_effort_flag(None).is_empty
    assert parse_effort_flag(["  "]).is_empty


def test_parse_rejects_unknown_level():
    with pytest.raises(EffortValueError):
        parse_effort_flag(["bogus"])
    with pytest.raises(EffortValueError):
        parse_effort_flag(["codex=bogus"])
    with pytest.raises(EffortValueError):
        parse_effort_flag(["=high"])


def test_parse_rejects_unknown_provider_instead_of_silent_opencode():
    """A typo'd provider (`claud`, `codexx`) must fail loudly, not silently land on the
    opencode catch-all route."""
    with pytest.raises(EffortValueError):
        parse_effort_flag(["claud=high"])
    with pytest.raises(EffortValueError):
        parse_effort_flag(["codexx=high"])
    # The real routes (and the oc/opencode aliases) still parse.
    o = parse_effort_flag(["codex=high", "claude=low", "oc=max", "commandcode=medium"])
    assert o.by_provider == {
        "codex": "high",
        "claude": "low",
        "opencode": "max",
        "commandcode": "medium",
    }


# --- resolution seam: effort_for(provider) or seat_effort --------------------------
def test_effort_for_prefers_provider_over_global_default():
    o = parse_effort_flag(["high", "codex=low"])
    assert o.effort_for("codex:gpt-5") == "low"  # per-provider wins
    assert o.effort_for("claude:opus") == "high"  # global default
    assert o.effort_for("oc:zai/glm-5") == "high"  # global default (no oc override)


def test_resolve_flag_overrides_seat_and_falls_back_when_silent():
    o = parse_effort_flag(["codex=low"])
    # flag present for codex -> overrides the seat's "xhigh"
    assert o.resolve("codex:gpt-5", "xhigh") == "low"
    # flag silent for claude AND no global default -> seat value stands
    assert o.resolve("claude:opus", "medium") == "medium"


def test_empty_override_is_noop_for_resolve():
    o = parse_effort_flag([])
    assert o.resolve("codex:gpt-5", "medium") == "medium"
    assert o.effort_for("codex:gpt-5") is None


# --- board application (the diff-review path) --------------------------------------
def _board():
    return [
        BoardReviewer(model="codex:gpt-5", role="", display="Codex", effort="xhigh"),
        BoardReviewer(model="claude:opus", role="", display="Claude", effort="medium"),
    ]


def test_apply_effort_override_high_overrides_seat_config_effort():
    o = parse_effort_flag(["high"])
    board = apply_effort_override(_board(), o)
    assert [r.effort for r in board] == ["high", "high"]


def test_apply_effort_override_per_provider_and_fallback():
    o = parse_effort_flag(["codex=low"])
    board = apply_effort_override(_board(), o)
    # codex overridden; claude keeps its seat effort (flag silent, no global default).
    assert [(r.model, r.effort) for r in board] == [
        ("codex:gpt-5", "low"),
        ("claude:opus", "medium"),
    ]


def test_apply_empty_override_keeps_seat_effort_untouched():
    original = _board()
    board = apply_effort_override(original, parse_effort_flag([]))
    # Fresh list (safe to hand on), same seats/efforts — nothing overridden.
    assert board is not original
    assert [(r.model, r.effort) for r in board] == [
        (r.model, r.effort) for r in original
    ]


def test_direct_construction_canonicalises_provider_keys():
    """A programmatic EffortOverride with a non-canonical key still matches the route a
    model resolves to (parse canonicalizes; __post_init__ keeps direct callers coherent)."""
    from reviewlib.config import EffortOverride

    o = EffortOverride(by_provider={"oc": "high"})
    assert o.by_provider == {"opencode": "high"}
    assert o.effort_for("oc:zai/glm-5") == "high"


def test_direct_construction_rejects_unknown_provider_key():
    from reviewlib.config import EffortOverride

    with pytest.raises(EffortValueError):
        EffortOverride(by_provider={"claud": "high"})


def test_by_provider_is_immutable():
    """frozen=True must not be defeated by an in-place mutation of the dict field."""
    o = parse_effort_flag(["codex=high"])
    with pytest.raises(TypeError):
        o.by_provider["claude"] = "low"  # type: ignore[index]


def test_overridden_board_flows_into_panel_jobs_once():
    """The override is applied to the board ONCE; the built PanelJobs carry that effort
    (the backend applies it — no second application on the board seam)."""
    board = apply_effort_override(_board(), parse_effort_flag(["high", "codex=low"]))
    old = backends.backend_available
    backends.backend_available = lambda _m: True
    try:
        jobs, skipped = build_board_jobs(board, "prompt", "diff")
    finally:
        backends.backend_available = old
    assert skipped == []
    assert {j.model: j.effort for j in jobs} == {
        "codex:gpt-5": "low",
        "claude:opus": "high",
    }


# --- flat-panel modes receive the override via ModeContext -------------------------
def test_quorum_jobs_carry_run_scoped_effort(monkeypatch):
    import reviewlib.modes.quorum as quorum

    captured = {}

    def fake_run_panel(jobs, cwd, timeout):
        captured["jobs"] = jobs
        return [_ok(j.model) for j in jobs]

    monkeypatch.setattr(quorum, "run_panel", fake_run_panel)
    monkeypatch.setattr(quorum, "run_moderator", lambda *a, **k: _ok("moderator"))

    o = parse_effort_flag(["high", "codex=low"])
    quorum.mode_quorum(
        "Q?",
        ["codex:gpt-5", "claude:opus"],
        "",
        Path("."),
        60,
        [],
        effort_override=o,
    )
    assert {j.model: j.effort for j in captured["jobs"]} == {
        "codex:gpt-5": "low",
        "claude:opus": "high",
    }


def test_just_ask_jobs_default_to_none_without_flag(monkeypatch):
    import reviewlib.modes.just_ask as just_ask

    captured = {}

    def fake_run_panel(jobs, cwd, timeout):
        captured["jobs"] = jobs
        return [_ok(j.model) for j in jobs]

    monkeypatch.setattr(just_ask, "run_panel", fake_run_panel)
    just_ask.mode_just_ask(
        "Q?", ["codex:gpt-5"], "", Path("."), 60, effort_override=parse_effort_flag([])
    )
    assert captured["jobs"][0].effort is None


# --- opencode --variant mapping (single lever, no dupes) ---------------------------
def test_opencode_variant_maps_effort():
    assert backends._opencode_variant(None) is None
    assert backends._opencode_variant("") is None
    assert backends._opencode_variant("high") == "high"
    assert backends._opencode_variant("max") == "max"
    assert backends._opencode_variant("minimal") == "minimal"


def test_flat_dash_m_review_path_threads_effort_to_backend(monkeypatch):
    """`review diff -m X --effort high` (no board) reaches the backend: the flat seat
    dispatch resolves the run-scoped effort and passes it through, and stays byte-identical
    (no effort kwarg) when the flag is absent."""
    import reviewlib.modes.review as review_mod

    calls: list[dict] = []

    def fake_backend(model, prompt, diff, cwd, timeout, round_no=0, *, effort=None):
        calls.append({"model": model, "effort": effort})
        return _ok(model)

    monkeypatch.setattr(review_mod, "resolve_backend", lambda _m: fake_backend)
    monkeypatch.setattr(review_mod, "run_seat_with_retry", lambda _model, fn: fn())

    review_mod.mode_review(
        ["codex:gpt-5"],
        "prompt",
        "a diff",
        Path("."),
        60,
        False,
        board=None,
        effort_override=parse_effort_flag(["high"]),
    )
    assert calls[-1] == {"model": "codex:gpt-5", "effort": "high"}

    calls.clear()
    review_mod.mode_review(
        ["codex:gpt-5"],
        "prompt",
        "a diff",
        Path("."),
        60,
        False,
        board=None,
        effort_override=parse_effort_flag([]),
    )
    # No flag -> the seat dispatch never sets effort (byte-identical to the legacy call).
    assert calls[-1] == {"model": "codex:gpt-5", "effort": None}


def test_flat_failover_effort_follows_the_actually_dispatched_provider_route(
    monkeypatch, tmp_path
):
    """When a flat seat FAILS OVER to a different-ROUTE provider (codex P2 on
    review-cli#157's provider-failover cascade, now also applied to the flat `-m` path), a
    per-provider `--effort <route>=<level>` override must apply to whichever route ACTUALLY
    executes, not the originally-requested route.

    `--effort opencode=high` exists to size the backend that RUNS (e.g. opencode seats are
    agentic and want a bigger reasoning budget); after `zai:glm-5.2` fails over to
    `oc:zai/glm-5.2`, the seat that actually runs IS an opencode seat, so it must get the
    opencode-route effort, not the now-irrelevant zai-route one. This documents/locks the
    intended behavior (raised as a 'please confirm' item on review of #157)."""
    import reviewlib.modes.review as review_mod

    calls: list[dict] = []

    def fake_backend(model, prompt, diff, cwd, timeout, round_no=0, *, effort=None):
        calls.append({"model": model, "effort": effort})
        if model == "zai:glm-5.2":
            return ReviewResult(
                model=model, command="c", returncode=1, stdout="down", stderr=""
            )
        return _ok(model)

    monkeypatch.setattr(review_mod, "resolve_backend", lambda _m: fake_backend)
    monkeypatch.setattr(review_mod, "run_seat_with_retry", lambda _model, fn: fn())
    monkeypatch.setattr(review_mod, "backend_available", lambda _m: True)
    monkeypatch.setattr(review_mod, "runtime_provider_marked_unpaid", lambda _m: False)
    monkeypatch.setenv("REVIEW_PROVIDER_CACHE", str(tmp_path / "last-provider.json"))

    override = parse_effort_flag(["opencode=high", "zai=low"])
    review_mod.mode_review(
        ["zai:glm-5.2"],
        "prompt",
        "a diff",
        Path("."),
        60,
        False,
        board=None,
        effort_override=override,
    )
    assert calls[0] == {"model": "zai:glm-5.2", "effort": "low"}, calls
    assert calls[1] == {"model": "oc:zai/glm-5.2", "effort": "high"}, calls


def test_board_review_path_applies_effort_override_for_direct_callers(monkeypatch):
    """`mode_review` called DIRECTLY (lib/MCP style) with a board + effort_override must
    resolve the override onto the seats itself — not assume the CLI pre-applied it. A board
    seat configured `effort='low'` with `--effort high` reaches the failover run at `high`."""
    import reviewlib.modes.review as review_mod
    from reviewlib.panel import FailoverOutcome

    seen: list = []

    def fake_run_board(pool, reserve, prompt, diff, cwd, timeout, images=()):
        seen.append(list(pool))
        return FailoverOutcome(
            results=[_ok(s.model) for s in pool],
            usable=[_ok(s.model) for s in pool],
            target=len(pool),
            degraded=False,
            usable_models=[s.model for s in pool],
        )

    monkeypatch.setattr(review_mod, "run_board_with_failover", fake_run_board)

    board = [BoardReviewer("codex:gpt-5", "correctness", "Codex", effort="low")]
    review_mod.mode_review(
        ["codex:gpt-5"],
        "prompt",
        "a diff",
        Path("."),
        60,
        False,
        board=board,
        exact_board=True,
        effort_override=parse_effort_flag(["high"]),
    )
    assert seen[-1][0].effort == "high"


def test_board_review_path_effort_override_is_idempotent_after_cli_apply(monkeypatch):
    """The CLI pre-applies the override; mode_review applying it AGAIN is a no-op (the seat
    already carries the resolved effort), so the double-application never mangles it."""
    import reviewlib.modes.review as review_mod
    from reviewlib.panel import FailoverOutcome

    seen: list = []

    def fake_run_board(pool, reserve, prompt, diff, cwd, timeout, images=()):
        seen.append(list(pool))
        return FailoverOutcome(
            results=[_ok(s.model) for s in pool],
            usable=[_ok(s.model) for s in pool],
            target=len(pool),
            degraded=False,
            usable_models=[s.model for s in pool],
        )

    monkeypatch.setattr(review_mod, "run_board_with_failover", fake_run_board)

    override = parse_effort_flag(["high"])
    pre_applied = apply_effort_override(
        [BoardReviewer("codex:gpt-5", "correctness", "Codex", effort="low")], override
    )
    review_mod.mode_review(
        ["codex:gpt-5"],
        "prompt",
        "a diff",
        Path("."),
        60,
        False,
        board=pre_applied,
        exact_board=True,
        effort_override=override,
    )
    assert seen[-1][0].effort == "high"


def test_every_real_backend_accepts_effort():
    """Contract guard: every backend `resolve_backend` can return (plus review_with_images)
    must accept an `effort` kwarg, so threading `--effort` through call_backend /
    review_with_images can never raise a TypeError at runtime. A new backend that forgets
    the parameter fails HERE, not in production."""
    import inspect

    def accepts_effort(fn) -> bool:
        params = inspect.signature(fn).parameters
        return "effort" in params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )

    probe_models = [
        "codex:gpt-5",
        "claude:opus",
        "gemini",
        "oc:zai/glm-5",
        "opencode",
        "omp:kimi-code/k3",
        "commandcode:deepseek",
        "zai:glm-5.2",
        "openrouter:anthropic/claude",
    ]
    for model in probe_models:
        fn = backends.resolve_backend(model)
        assert accepts_effort(fn), f"{fn.__name__} (for {model!r}) must accept effort"
    assert accepts_effort(backends.review_with_images)


def test_provider_route_name_maps_seats_to_routes():
    assert backends.provider_route_name("codex:gpt-5") == "codex"
    assert backends.provider_route_name("claude:opus") == "claude"
    assert backends.provider_route_name("gemini") == "gemini"
    assert backends.provider_route_name("oc:zai/glm-5") == "opencode"
    assert backends.provider_route_name("opencode") == "opencode"
    assert backends.provider_route_name("omp:kimi-code/k3") == "omp"
    assert backends.provider_route_name("commandcode:deepseek") == "commandcode"
