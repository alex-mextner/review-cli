#!/usr/bin/env python3
"""Unit tests for the reviewer board (HYP-741).

A board assigns each reviewer model its OWN role/lens. These tests prove, all
offline (no model call, no network — backends are monkeypatched / forced
unavailable):
  (a) DEFAULT_BOARD matches the directive table byte-exact (model -> role);
  (b) config.yaml `board:` parsing — valid entries, unknown-role fallback to the
      generic prompt (no crash), bad entries skipped, empty/absent -> DEFAULT_BOARD;
  (c) role-lens injection — an available reviewer's PanelJob prompt is
      `base_prompt + lens` and its label is `"<display> [<role>]"`;
  (d) graceful skip — an unavailable reviewer is dropped (not crashed) and surfaced
      in the `skipped` list; an all-unavailable board returns no jobs;
  (e) mode_review board path runs in parallel and returns 0 on success / 1 on a
      failed reviewer, and explicit -m either runs a flat panel (no config) or narrows
      configured board metadata.

Runs as a plain script (mirrors tests/test_provider_keys.py): each `test_*` is
invoked by the __main__ block, no pytest required.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import reviewlib.backends as backends  # noqa: E402
import reviewlib.modes.review as _review_mod  # noqa: E402
from reviewlib.backends import ReviewResult  # noqa: E402
from reviewlib.config import (  # noqa: E402
    DEFAULT_BOARD,
    DEFAULT_MODELS,
    DEFAULT_POOL_SIZE,
    DEFAULT_PRESET,
    DEFAULT_PRESET_BOARD,
    GLM_COMMANDCODE_SEAT,
    HEAVY_PRESET_BOARD,
    KIMI_SEAT,
    LIGHT_PRESET_BOARD,
    SOL_SEAT,
    REVIEW_ROLES,
    VISUAL_MODELS,
    BoardConfigError,
    BoardReviewer,
    _agentic,
    _split_models,
    board_from_models,
    load_board,
    preset_board,
    select_pool,
)
from reviewlib.panel import build_board_jobs  # noqa: E402

DEFAULT_PROMPT = "Review this diff."


class _BackendStateSandbox:
    def __enter__(self):
        self._saved_unpaid_env = os.environ.get("REVIEW_UNPAID_PROVIDERS")
        self._saved_config_unpaid = backends._CONFIG_UNPAID_PROVIDERS
        os.environ.pop("REVIEW_UNPAID_PROVIDERS", None)
        backends.configure_unpaid_providers(None)
        with backends._PAYMENT_PREFLIGHT_CACHE_LOCK:
            backends._PAYMENT_PREFLIGHT_CACHE.clear()
        return self

    def __exit__(self, *_exc):
        if self._saved_unpaid_env is None:
            os.environ.pop("REVIEW_UNPAID_PROVIDERS", None)
        else:
            os.environ["REVIEW_UNPAID_PROVIDERS"] = self._saved_unpaid_env
        backends.configure_unpaid_providers(self._saved_config_unpaid)
        with backends._PAYMENT_PREFLIGHT_CACHE_LOCK:
            backends._PAYMENT_PREFLIGHT_CACHE.clear()
        return False


# === DEFAULT_BOARD shape (byte-exact model ids, PRIORITY order from the directive) ==
def test_default_board_matches_directive_table():
    # Priority order (failover pool): strongest WORKING model first. Each seat keeps a
    # role/lens, but selection is by PRIORITY + availability, not role order.
    expected = [
        ("codex:gpt-5.6-sol", "consistency", "Sol"),
        ("claude:claude-opus-4-8", "correctness", "Opus"),
        # Seat 3: GLM-5.2 via the Command Code gateway, directly under Opus.
        # DIFF-ONLY keyed HTTP (review_commandcode) — opencode's commandcode provider does
        # not register this GLM id, so the agentic form errors; read-only by construction.
        # Role `performance` (NOT correctness) so it doesn't duplicate Opus's lens.
        ("commandcode:zai-org/GLM-5.2", "performance", "GLM-cc"),
        # Seat 4 is the first reserve and preserves lens diversity when a top seat drops.
        ("oc:commandcode/moonshotai/Kimi-K2.7-Code", "quality", "Kimi"),
        # Seat 5 is the agentic codex CLI route (see config.py / CHANGELOG for rationale).
        ("codex", "consistency", "Codex"),
        # Seats 4 and 6-7 route through opencode (`oc:`) so they run AGENTICALLY (read the repo
        # read-only), not the diff-only commandcode/z.ai REST call (review-cli#24).
        ("oc:commandcode/Qwen/Qwen3.7-Max", "security", "Qwen"),
        ("oc:commandcode/deepseek/deepseek-v4-pro", "tests", "DeepSeek"),
        ("gemini", "contracts", "Gemini"),
        # GLM-5.2 via opencode's `zai` provider (his z.ai subscription), agentic. Distinct
        # from the seat-3 commandcode GLM: same model family, different provider/transport.
        # DEPRIORITIZED to LAST-RESORT reserve (review-cli#65): it is pathologically slow
        # under load, so it is promoted before only Fable — Qwen/DeepSeek/Gemini go first.
        ("oc:zai/glm-5.2", "quality", "GLM"),
        # Fable 5 (Anthropic flagship). DEMOTED from priority 1 to the very LAST seat
        # (review-cli#fable-seat-reliability): confirmed 97.9-100% dispatch failure rate
        # (chronic session/usage-limit exhaustion) made priority 1 a near-certain-doomed
        # dispatch on every single default review. Worse than GLM's "merely slow" profile,
        # so it sits even later than GLM — still a reserve seat, not removed outright.
        ("claude:claude-fable-5", "architect", "Fable"),
    ]
    got = [(r.model, r.role, r.display) for r in DEFAULT_BOARD]
    assert got == expected, got


def test_default_board_is_priority_ordered():
    """The CTO's priority sketch (strongest-WORKING-model first): Sol, Opus,
    GLM-5.2-via-commandcode, Kimi, Codex, Qwen, DeepSeek, Gemini, GLM-5.2-via-z.ai, Fable.
    Re-ranking = reordering
    DEFAULT_BOARD; this pins the order. Seats 4 and 6-8 are the AGENTIC opencode (`oc:`)
    routes (review-cli#24); the commandcode GLM at #3 is diff-only keyed HTTP. The z.ai GLM
    seat is DEPRIORITIZED to next-to-last (review-cli#65) — pathologically slow under load.
    Fable is LAST (review-cli#fable-seat-reliability) — a confirmed ~100% dispatch failure
    rate, worse than merely slow."""
    assert [r.model for r in DEFAULT_BOARD] == [
        "codex:gpt-5.6-sol",
        "claude:claude-opus-4-8",
        "commandcode:zai-org/GLM-5.2",
        "oc:commandcode/moonshotai/Kimi-K2.7-Code",
        "codex",
        "oc:commandcode/Qwen/Qwen3.7-Max",
        "oc:commandcode/deepseek/deepseek-v4-pro",
        "gemini",
        "oc:zai/glm-5.2",
        "claude:claude-fable-5",
    ]


def test_glm_commandcode_seat_sits_directly_under_opus():
    """The CTO directive: GLM 5.2 via commandcode must sit IMMEDIATELY after Opus, so the
    pool tries Opus first, then this GLM. Pin the adjacency by index so a future re-rank that
    pulls them apart trips here. Uses the canonical constant (one source of truth)."""
    models = [r.model for r in DEFAULT_BOARD]
    opus_i = models.index("claude:claude-opus-4-8")
    glm_i = models.index(GLM_COMMANDCODE_SEAT)
    assert glm_i == opus_i + 1, (opus_i, glm_i, models)
    # Opus sits immediately after Sol (priority 1), so GLM is priority 3 (index 2).
    assert glm_i == 2, glm_i


def test_fable_seat_is_last_resort_reserve():
    """review-cli#fable-seat-reliability: Fable is the LAST seat on the board (the
    lowest-priority reserve), demoted from priority 1 after a confirmed 97.9-100%
    dispatch failure rate — see `test_default_board_matches_directive_table`'s comment
    for the full rationale. Pinned by index (not just membership) so a future re-rank
    that accidentally moves it back up trips here."""
    models = [r.model for r in DEFAULT_BOARD]
    assert models[-1] == "claude:claude-fable-5", models
    fable = next(r for r in DEFAULT_BOARD if r.model == "claude:claude-fable-5")
    assert fable.display == "Fable"
    assert fable.role == "architect"


def test_glm_commandcode_seat_is_the_canonical_constant():
    """The seat's model string is GLM_COMMANDCODE_SEAT byte-exact, and it is the verified-
    live commandcode gateway id (`zai-org/GLM-5.2`), not the z.ai-route `glm-5.2` id."""
    assert GLM_COMMANDCODE_SEAT == "commandcode:zai-org/GLM-5.2"
    seat = next(r for r in DEFAULT_BOARD if r.model == GLM_COMMANDCODE_SEAT)
    assert seat.display == "GLM-cc"


def test_glm_commandcode_seat_routes_readonly_through_review_commandcode():
    """The seat resolves to review_commandcode (a stateless keyed-HTTP REST backend that
    POSTs ONLY the diff — read-only by construction, no repo/tools/exec), NOT to an agentic
    opencode/codex/claude transport. This is what makes it caged without an `-s read-only`
    flag: a REST POST has no workspace to write to."""
    assert backends.resolve_backend(GLM_COMMANDCODE_SEAT) is backends.review_commandcode
    # It passes the #25 default-routing guard (named backend, provider not dead).
    assert backends.default_routes_live(GLM_COMMANDCODE_SEAT) is True
    assert backends.effective_provider(GLM_COMMANDCODE_SEAT) == "commandcode"


def test_glm_commandcode_seat_degrades_gracefully_without_a_key():
    """When COMMANDCODE_API_KEY is absent the seat must NOT report available (so the pool
    backfills it from the reserve) — graceful degradation, never a hard failure. Mirrors how
    every other key-gated backend behaves when its credential is missing.

    Patches the module globals manually with try/finally (NOT the pytest `monkeypatch`
    fixture), so the test runs unchanged under the standalone `__main__` runner where a
    fixture parameter would be an unfilled positional arg."""
    import reviewlib.backends as b

    saved_resolve = b._resolve_key
    saved_mode = os.environ.pop("REVIEW_COMMANDCODE_MODE", None)
    saved_fake = os.environ.pop("REVIEW_FAKE_BACKEND", None)
    # Also drop COMMANDCODE_API_KEY from the live env for the duration: a dev/CI host that
    # exports the real key would otherwise let any os.environ-direct read inside
    # backend_available see it and make this a false-green (review of #57). Stubbing
    # _resolve_key alone only covers the resolver path; popping the env var makes the
    # "no key anywhere" precondition explicit and resolver-independent.
    saved_key = os.environ.pop("COMMANDCODE_API_KEY", None)
    # Force the key resolver to find nothing (no env, no shared .env file).
    b._resolve_key = lambda *a, **k: ""
    try:
        assert backends.backend_available(GLM_COMMANDCODE_SEAT) is False
    finally:
        b._resolve_key = saved_resolve
        if saved_mode is not None:
            os.environ["REVIEW_COMMANDCODE_MODE"] = saved_mode
        if saved_fake is not None:
            os.environ["REVIEW_FAKE_BACKEND"] = saved_fake
        if saved_key is not None:
            os.environ["COMMANDCODE_API_KEY"] = saved_key


def test_glm_commandcode_seat_carries_performance_not_a_duplicate_role():
    """GLM-cc must NOT reuse Opus's `correctness` lens — that would duplicate a role in the
    default top-4 pool and silently drop the `performance` lens from a plain `review diff`
    (review of #57). It carries `performance`, so inserting this seat is a priority change,
    not a coverage loss."""
    glmcc = next(r for r in DEFAULT_BOARD if r.model == GLM_COMMANDCODE_SEAT)
    opus = next(r for r in DEFAULT_BOARD if r.model == "claude:claude-opus-4-8")
    assert glmcc.role == "performance", glmcc.role
    assert glmcc.role != opus.role, (glmcc.role, opus.role)


def test_default_pool_roles_are_distinct_no_lens_lost():
    """The default top-4 pool (DEFAULT_POOL_SIZE seats) must have FOUR DISTINCT roles, so a
    plain `review diff` always covers four non-overlapping lenses — no seat wasted on a
    duplicate lens. Coverage is consistency/correctness/performance/quality now
    (review-cli#fable-seat-reliability: Fable's `architect` lens moved to the LAST seat
    along with Fable itself; Kimi's `quality` lens fills the vacated top-4 slot)."""
    pool = [r for r in DEFAULT_BOARD[:DEFAULT_POOL_SIZE]]
    roles = [r.role for r in pool]
    assert len(set(roles)) == len(roles), f"duplicate role in default pool: {roles}"
    assert set(roles) == {"consistency", "correctness", "performance", "quality"}, roles


def test_default_board_has_ten_seats():
    assert len(DEFAULT_BOARD) == 10, len(DEFAULT_BOARD)


def test_preset_boards_pin_model_order_pool_and_effort():
    assert DEFAULT_PRESET == "default"
    assert all(r.effort == "high" for r in DEFAULT_PRESET_BOARD)
    assert SOL_SEAT not in [r.model for r in DEFAULT_PRESET_BOARD]
    assert "claude:claude-fable-5" not in [r.model for r in DEFAULT_PRESET_BOARD]
    assert [r.model for r in DEFAULT_PRESET_BOARD[:4]] == [
        "claude:claude-opus-4-8",
        GLM_COMMANDCODE_SEAT,
        "oc:commandcode/moonshotai/Kimi-K2.7-Code",
        "codex",
    ]
    assert len({r.role for r in DEFAULT_PRESET_BOARD[:4]}) == 4

    assert all(r.effort == "medium" for r in LIGHT_PRESET_BOARD)
    assert [r.model for r in LIGHT_PRESET_BOARD] == [
        r.model for r in DEFAULT_PRESET_BOARD
    ]

    # Fable is EXCLUDED from HEAVY_PRESET_BOARD entirely (review-cli#fable-seat-reliability,
    # mirrors the existing DEFAULT_PRESET_BOARD exclusion above) — a "heavy" preset must not
    # pay for a seat with a confirmed ~100% dispatch failure rate. Kimi is promoted into the
    # xhigh top-4 in its place (Fable's old DEFAULT_BOARD slot is now the LAST index, so
    # dropping it doesn't shift anyone else's tier — see HEAVY_PRESET_BOARD's own comment).
    assert "claude:claude-fable-5" not in [r.model for r in HEAVY_PRESET_BOARD]
    assert [(r.model, r.effort) for r in HEAVY_PRESET_BOARD[:4]] == [
        (SOL_SEAT, "xhigh"),
        ("claude:claude-opus-4-8", "xhigh"),
        (GLM_COMMANDCODE_SEAT, "xhigh"),
        ("oc:commandcode/moonshotai/Kimi-K2.7-Code", "xhigh"),
    ]
    assert all(r.effort == "max" for r in HEAVY_PRESET_BOARD[4:])


def test_package_facade_exports_preset_surface():
    import reviewlib

    assert reviewlib.DEFAULT_PRESET == DEFAULT_PRESET
    assert reviewlib.DEFAULT_PRESET_BOARD == DEFAULT_PRESET_BOARD
    assert reviewlib.LIGHT_PRESET_BOARD == LIGHT_PRESET_BOARD
    assert reviewlib.HEAVY_PRESET_BOARD == HEAVY_PRESET_BOARD
    assert reviewlib.SOL_SEAT == SOL_SEAT
    assert reviewlib.preset_names() == ("default", "heavy", "light")
    assert reviewlib.preset_pool_size("light") == 2
    assert [r.model for r in reviewlib.preset_board(None)] == [
        r.model for r in DEFAULT_PRESET_BOARD
    ]
    assert [r.model for r in preset_board(None)] == [
        r.model for r in DEFAULT_PRESET_BOARD
    ]
    assert preset_board(None)[0] == DEFAULT_PRESET_BOARD[0]
    assert preset_board(None)[0] is not DEFAULT_PRESET_BOARD[0]


def test_explicit_preset_board_overrides_config_board():
    cfg = {"board": [{"model": "gemini", "role": "tests", "effort": "medium"}]}
    board = load_board(cfg, preset="heavy")
    assert [r.model for r in board[:4]] == [
        SOL_SEAT,
        "claude:claude-opus-4-8",
        GLM_COMMANDCODE_SEAT,
        "oc:commandcode/moonshotai/Kimi-K2.7-Code",
    ]
    assert all(r.effort == "xhigh" for r in board[:4])
    assert all(r.effort == "max" for r in board[4:])
    assert "claude:claude-fable-5" not in [r.model for r in board]


def test_explicit_models_with_preset_preserve_config_metadata_and_use_preset_effort():
    cfg = {
        "board": [
            {
                "model": "gemini",
                "role": "tests",
                "name": "Configured Gemini",
                "effort": "medium",
            },
        ]
    }
    board = board_from_models(["gemini"], cfg, preset="heavy")

    assert len(board) == 1
    assert board[0].model == "gemini"
    assert board[0].role == "tests"
    assert board[0].display == "Configured Gemini"
    assert board[0].effort == "max"


def test_explicit_models_with_preset_apply_preset_effort_to_out_of_preset_seat():
    board = board_from_models(
        ["claude:claude-fable-5", "custom:model"], {}, preset="light"
    )

    assert [r.model for r in board] == ["claude:claude-fable-5", "custom:model"]
    assert board[0].role == "architect"
    assert board[0].effort == "medium"
    assert board[1].role == ""
    assert board[1].effort == "medium"


def test_explicit_models_with_preset_prevent_config_effort_downgrade_for_out_of_preset_seat():
    cfg = {
        "board": [
            {
                "model": "custom:model",
                "role": "tests",
                "name": "Custom",
                "effort": "low",
            },
        ]
    }
    board = board_from_models(["custom:model"], cfg, preset="heavy")

    assert len(board) == 1
    assert board[0].model == "custom:model"
    assert board[0].role == "tests"
    assert board[0].display == "Custom"
    assert board[0].effort == "xhigh"


def test_config_board_unsupported_effort_is_ignored():
    board = load_board(
        {"board": [{"model": "codex", "role": "tests", "effort": 'bad"value'}]}
    )
    assert board[0].effort is None


def test_unknown_preset_name_fails_fast_for_api_callers():
    try:
        load_board({}, preset="bogus")
    except ValueError as exc:
        assert "unknown preset" in str(exc)
    else:
        raise AssertionError("unknown preset should not fall back to DEFAULT_BOARD")

    from reviewlib.config import preset_pool_size

    try:
        preset_pool_size("bogus")
    except ValueError as exc:
        assert "unknown preset" in str(exc)
    else:
        raise AssertionError("unknown preset should not fall back to DEFAULT_POOL_SIZE")


def test_call_backend_does_not_break_legacy_custom_backend_without_effort():
    def _legacy(model, prompt, diff, cwd, timeout, round_no=0):
        return ReviewResult(
            model=model, command="legacy", returncode=0, stdout=prompt, stderr=""
        )

    result = backends.call_backend(
        _legacy,
        "custom",
        "prompt",
        "+x",
        REPO_ROOT,
        5,
        effort="high",
    )
    assert result.returncode == 0
    assert result.command == "legacy"
    assert "Use high reasoning effort." in result.stdout


def test_call_backend_does_not_pass_effort_when_signature_uninspectable():
    def _legacy(model, prompt, diff, cwd, timeout, round_no=0):
        return ReviewResult(
            model=model, command="legacy", returncode=0, stdout=prompt, stderr=""
        )

    old_signature = backends.inspect.signature
    backends.inspect.signature = lambda _backend: (_ for _ in ()).throw(
        ValueError("no signature")
    )
    try:
        result = backends.call_backend(
            _legacy,
            "custom",
            "prompt",
            "+x",
            REPO_ROOT,
            5,
            effort="high",
        )
    finally:
        backends.inspect.signature = old_signature
    assert result.returncode == 0
    assert result.command == "legacy"


def test_call_backend_does_not_pass_effort_as_keyword_to_positional_only_param():
    def _legacy(model, prompt, diff, cwd, timeout, round_no=0, effort=None, /):
        return ReviewResult(
            model=model, command="legacy", returncode=0, stdout=prompt, stderr=""
        )

    result = backends.call_backend(
        _legacy,
        "custom",
        "prompt",
        "+x",
        REPO_ROOT,
        5,
        effort="high",
    )
    assert result.returncode == 0
    assert result.command == "legacy"


def test_call_backend_passes_effort_to_var_keyword_backend_once():
    captured: dict = {}

    def _backend(model, prompt, diff, cwd, timeout, round_no=0, **kwargs):
        captured["effort"] = kwargs.get("effort")
        return ReviewResult(
            model=model, command="kwargs", returncode=0, stdout=prompt, stderr=""
        )

    result = backends.call_backend(
        _backend,
        "custom",
        "prompt",
        "+x",
        REPO_ROOT,
        5,
        effort="xhigh",
    )

    assert result.returncode == 0
    assert result.command == "kwargs"
    assert captured["effort"] == "xhigh"
    assert result.stdout.count("Use highest reasoning effort.") == 1


def test_codex_backend_threads_effort_to_cli_config():
    captured: dict = {}

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    old_which = backends._which
    old_run = backends._run_streamed
    backends._which = lambda name: f"/bin/{name}"

    def _fake_run(argv, **kw):
        captured["argv"] = argv
        captured["header_argv0"] = kw.get("header_argv0")
        return _Proc()

    backends._run_streamed = _fake_run
    try:
        result = backends.review_codex(
            SOL_SEAT,
            "prompt",
            "+x",
            REPO_ROOT,
            5,
            effort="xhigh",
        )
    finally:
        backends._which = old_which
        backends._run_streamed = old_run
    assert result.returncode == 0
    assert "-c" in captured["argv"], captured
    assert 'model_reasoning_effort="xhigh"' in captured["argv"], captured
    assert captured["header_argv0"] == "codex -m gpt-5.6-sol", captured


def test_codex_backend_sanitizes_model_selector_in_log_header():
    captured: dict = {}

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    old_which = backends._which
    old_run = backends._run_streamed
    backends._which = lambda name: f"/bin/{name}"

    def _fake_run(argv, **kw):
        captured["argv"] = argv
        captured["header_argv0"] = kw.get("header_argv0")
        return _Proc()

    backends._run_streamed = _fake_run
    try:
        result = backends.review_codex(
            "codex:gpt-5.6-sol\n\u0085\u2028\u2029[review-cli] forged: codex",
            "prompt",
            "+x",
            REPO_ROOT,
            5,
        )
    finally:
        backends._which = old_which
        backends._run_streamed = old_run
    assert result.returncode == 0
    for ch in ("\n", "\u0085", "\u2028", "\u2029"):
        assert ch not in captured["header_argv0"], captured
    assert (
        captured["header_argv0"] == "codex -m gpt-5.6-sol????[review-cli] forged: codex"
    ), captured
    assert any("\n" in arg for arg in captured["argv"]), captured


def test_codex_backend_maps_minimal_effort_to_supported_low_cli_config():
    captured: dict = {}

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    old_which = backends._which
    old_run = backends._run_streamed
    backends._which = lambda name: f"/bin/{name}"

    def _fake_run(argv, **kw):
        captured["argv"] = argv
        return _Proc()

    backends._run_streamed = _fake_run
    try:
        result = backends.review_codex(
            "codex",
            "prompt",
            "+x",
            REPO_ROOT,
            5,
            effort="minimal",
        )
    finally:
        backends._which = old_which
        backends._run_streamed = old_run
    assert result.returncode == 0
    assert 'model_reasoning_effort="low"' in captured["argv"], captured


def test_codex_backend_maps_max_effort_to_supported_xhigh_cli_config():
    captured: dict = {}

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    old_which = backends._which
    old_run = backends._run_streamed
    backends._which = lambda name: f"/bin/{name}"

    def _fake_run(argv, **kw):
        captured["argv"] = argv
        return _Proc()

    backends._run_streamed = _fake_run
    try:
        result = backends.review_codex(
            "codex",
            "prompt",
            "+x",
            REPO_ROOT,
            5,
            effort="max",
        )
    finally:
        backends._which = old_which
        backends._run_streamed = old_run
    assert result.returncode == 0
    assert 'model_reasoning_effort="xhigh"' in captured["argv"], captured


def test_claude_backend_preserves_max_effort_for_cli_flag():
    assert backends._claude_reasoning_effort("max") == "max"


def test_opencode_backend_includes_effort_hint_without_spacing_glitch():
    captured: dict = {}

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    class _GitProc:
        returncode = 0
        stdout = ""
        stderr = ""

    old_which = backends._which
    old_run = backends._run
    old_streamed = backends._run_streamed
    old_ensure = backends._ensure_opencode_readonly_agent
    old_runs_in_repo = backends._opencode_runs_in_repo
    backends._which = lambda name: f"/bin/{name}"
    backends._run = lambda *a, **k: _GitProc()
    backends._ensure_opencode_readonly_agent = lambda *_a, **_k: None
    backends._opencode_runs_in_repo = lambda _cwd: False

    def _fake_streamed(argv, **kw):
        captured["message"] = argv[-1]
        return _Proc()

    backends._run_streamed = _fake_streamed
    try:
        with _BackendStateSandbox():
            result = backends.review_opencode(
                "oc:commandcode/model",
                "prompt",
                "+x",
                REPO_ROOT,
                5,
                effort="xhigh",
            )
    finally:
        backends._which = old_which
        backends._run = old_run
        backends._run_streamed = old_streamed
        backends._ensure_opencode_readonly_agent = old_ensure
        backends._opencode_runs_in_repo = old_runs_in_repo
    assert result.returncode == 0
    assert captured["message"].count("Use highest reasoning effort.") == 1, captured
    assert (
        "Use highest reasoning effort.\n\nYou are running outside"
        in captured["message"]
    ), captured
    assert "effort.Review" not in captured["message"], captured


def test_call_backend_with_opencode_effort_hint_is_not_duplicated():
    captured: dict = {}

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    class _GitProc:
        returncode = 0
        stdout = ""
        stderr = ""

    old_which = backends._which
    old_run = backends._run
    old_streamed = backends._run_streamed
    old_ensure = backends._ensure_opencode_readonly_agent
    old_runs_in_repo = backends._opencode_runs_in_repo
    backends._which = lambda name: f"/bin/{name}"
    backends._run = lambda *a, **k: _GitProc()
    backends._ensure_opencode_readonly_agent = lambda *_a, **_k: None
    backends._opencode_runs_in_repo = lambda _cwd: False

    def _fake_streamed(argv, **kw):
        captured["message"] = argv[-1]
        return _Proc()

    backends._run_streamed = _fake_streamed
    try:
        with _BackendStateSandbox():
            result = backends.call_backend(
                backends.review_opencode,
                "oc:commandcode/model",
                "prompt",
                "+x",
                REPO_ROOT,
                5,
                effort="xhigh",
            )
    finally:
        backends._which = old_which
        backends._run = old_run
        backends._run_streamed = old_streamed
        backends._ensure_opencode_readonly_agent = old_ensure
        backends._opencode_runs_in_repo = old_runs_in_repo
    assert result.returncode == 0
    assert captured["message"].count("Use highest reasoning effort.") == 1, captured


def test_direct_rest_backend_effort_adds_prompt_hint():
    captured: dict = {}

    old_key = backends._commandcode_key
    old_preflight = backends.provider_preflight_result
    old_request = backends._openai_compatible_request
    backends._commandcode_key = lambda: "fake-key"
    backends.provider_preflight_result = lambda *a, **k: None

    def _fake_request(**kwargs):
        captured.update(kwargs)
        return ReviewResult(
            model=kwargs["model"],
            command="fake-rest",
            returncode=0,
            stdout=kwargs["prompt"],
            stderr="",
        )

    backends._openai_compatible_request = _fake_request
    try:
        with _BackendStateSandbox():
            result = backends.review_commandcode(
                "commandcode:deepseek/deepseek-v4-pro",
                "prompt",
                "+x",
                REPO_ROOT,
                5,
                effort="high",
            )
    finally:
        backends._commandcode_key = old_key
        backends.provider_preflight_result = old_preflight
        backends._openai_compatible_request = old_request

    assert result.returncode == 0
    assert "Use high reasoning effort." in result.stdout
    assert "Use high reasoning effort." in captured["prompt"]


def test_direct_claude_cli_effort_adds_prompt_hint():
    captured: dict = {}

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    old_binary = backends._claude_cli_binary
    old_argv = backends._claude_cli_argv
    old_env = backends._claude_cli_env
    old_trust = backends._ensure_workspace_trusted
    old_run = backends._run_streamed
    backends._claude_cli_binary = lambda: ("/bin/claude", True)
    backends._claude_cli_argv = lambda *_a, **_k: ["/bin/claude"]
    backends._claude_cli_env = lambda: {}
    backends._ensure_workspace_trusted = lambda _cwd: None

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["input_text"] = kwargs.get("input_text", "")
        return _Proc()

    backends._run_streamed = _fake_run
    try:
        result = backends.review_claude_cli(
            "claude:claude-opus-4-8",
            "prompt",
            "+x",
            REPO_ROOT,
            5,
            effort="high",
        )
    finally:
        backends._claude_cli_binary = old_binary
        backends._claude_cli_argv = old_argv
        backends._claude_cli_env = old_env
        backends._ensure_workspace_trusted = old_trust
        backends._run_streamed = old_run

    assert result.returncode == 0
    assert "Use high reasoning effort." in captured["input_text"]


def test_visual_models_have_separate_priority_from_review_board():
    """Visual review has its own priority list: Opus first, then vision-capable
    fallbacks, including a GLM vision model rather than the text-only GLM-5.2 seat."""
    assert VISUAL_MODELS[0] == "claude:claude-opus-4-8", VISUAL_MODELS
    assert "commandcode:zai-org/GLM-5.2" not in VISUAL_MODELS, VISUAL_MODELS
    assert any(
        "glm-4.5v" in model.lower() or "glm-4.6v" in model.lower()
        for model in VISUAL_MODELS
    ), VISUAL_MODELS
    assert all(
        "vision" in model.lower()
        or "kimi" in model.lower()
        or "glm-4." in model.lower()
        or "opus" in model.lower()
        or model == "gemini"
        for model in VISUAL_MODELS
    ), VISUAL_MODELS


# === No dead Fireworks/glide provider in the defaults (review-cli#25) ============
# The flat DEFAULT_MODELS panel used to pin `oc:fireworks/.../kimi-k2p6-turbo`, which ran
# on the suspended Fireworks `glide` account — a dead route. These pin that it is gone and
# that the flat panel + the board share ONE canonical Kimi seat (KIMI_SEAT) so they cannot
# drift back to the dead provider.

# The DEAD-ROUTE tokens this fix removed. Matched as exact substrings (not a bare
# "fireworks", which would false-trip on a future legitimately re-enabled Fireworks
# account or an unrelated `commandcode:fireworks/...` route — the ban is on THIS dead
# route, not the provider name forever).
_DEAD_ROUTE_TOKENS = ("oc:fireworks/", "kimi-k2p6-turbo", "glide")


def _all_default_model_strings() -> list[str]:
    """Every model string reachable from the CODE-defined defaults: the flat DEFAULT_MODELS
    panel and every DEFAULT_BOARD seat (the board IS pool + reserve; BoardReviewer carries
    no other model field). The dead-route ban is global across these defaults — a new
    seat/pool must not reintroduce the suspended route anywhere (review-cli#25). It does
    NOT police user-supplied config models, which are out of scope for this fix."""
    return list(DEFAULT_MODELS) + [r.model for r in DEFAULT_BOARD]


def test_no_default_model_string_uses_the_dead_route():
    for model in _all_default_model_strings():
        low = model.lower()
        for tok in _DEAD_ROUTE_TOKENS:
            assert tok not in low, f"{model!r} contains dead-route token {tok!r}"


def test_kimi_seat_itself_is_clean():
    low = KIMI_SEAT.lower()
    for tok in _DEAD_ROUTE_TOKENS:
        assert tok not in low, (
            f"KIMI_SEAT {KIMI_SEAT!r} contains dead-route token {tok!r}"
        )


def test_every_default_kimi_entry_is_the_canonical_seat():
    """Any default model string that names Kimi must be EITHER the canonical diff-only seat
    KIMI_SEAT (flat panel) or its agentic opencode form `_agentic(KIMI_SEAT)` (board) — one
    source of truth for the Kimi MODEL ID across both transports (review-cli#24/#25). A
    third hard-coded (and drift-prone) Kimi string can never slip into the flat panel, the
    board, or a reserve seat. Both forms share the same wire model id (the bit after the
    provider), so a future id bump touches ONE constant and both transports follow."""
    kimi_entries = [m for m in _all_default_model_strings() if "kimi" in m.lower()]
    assert kimi_entries, "no Kimi entry found in the defaults at all"
    assert set(kimi_entries) == {KIMI_SEAT, _agentic(KIMI_SEAT)}, kimi_entries


def test_default_models_and_board_share_one_kimi_seat():
    """The flat panel's Kimi entry is KIMI_SEAT (diff-only); the board's Kimi seat is its
    agentic opencode form `_agentic(KIMI_SEAT)` (review-cli#24). Both derive from the SAME
    constant, so a future model-id bump can't update one and leave the other pointing at a
    stale/dead provider (the exact staleness #25 removed). Matched by MODEL string, not the
    display label (which is cosmetic and may be renamed). The board has a reserve so the
    agentic seat can fail over on an opencode-less host; the flat panel has none, which is
    why it keeps the robust key-only commandcode route."""
    assert KIMI_SEAT in DEFAULT_MODELS, DEFAULT_MODELS
    board_models = {r.model for r in DEFAULT_BOARD}
    assert _agentic(KIMI_SEAT) in board_models, board_models
    # And the agentic board seat carries the SAME wire model id as the flat-panel seat
    # (everything after the provider) — transport-only difference, never an id fork.
    assert KIMI_SEAT.split(":", 1)[1] == _agentic(KIMI_SEAT).split("/", 1)[1]


def test_default_models_routes_kimi_through_commandcode():
    """The FLAT panel's Kimi seat goes through the commandcode gateway (the live account),
    not the dead `oc:fireworks` opencode route — and `_split_models` round-trips the whole
    flat panel unchanged (no alias rewrite / no drop), the normalization the review path
    applies before dispatch. The flat panel stays diff-only on purpose: it has no
    reserve/failover, so it keeps the key-only route that needs no opencode install (#24)."""
    assert KIMI_SEAT.startswith("commandcode:"), KIMI_SEAT
    assert DEFAULT_MODELS == ("codex", "gemini", KIMI_SEAT), DEFAULT_MODELS
    assert _split_models(list(DEFAULT_MODELS)) == ["codex", "gemini", KIMI_SEAT]


def test_every_default_model_routes_live():
    """Every entry in the flat DEFAULT_MODELS panel AND every DEFAULT_BOARD seat must pass
    `backends.default_routes_live` (review-cli#25): the id BOTH takes an explicitly-named
    backend route AND its underlying provider is not known-dead.

    This is the anti-rot guard. The original #25 bug was a default
    `oc:fireworks/.../kimi-k2p6-turbo` on the suspended `glide` account: resolve_backend has
    a permissive opencode catch-all, so a stale default degrades SILENTLY at runtime rather
    than erroring. `default_routes_live` closes that by checking the provider UNDER any
    `oc:`/`opencode:` transport (not the wrapper): that provider must name a real backend
    (`_match_named_backend`, so a typo'd provider — flat `comandcode:...` OR agentic
    `oc:comandcode/...` — fails, since the bare `comandcode` names nothing) AND must not be
    in the dead-provider denylist (the forward-looking half for a once-live provider that
    later dies). A future stale default trips this in CI instead of rotting at runtime. It
    checks named ROUTING of the under-transport provider + the dead-provider denylist, not
    live network reachability — a probe would need keys and can't run in CI."""
    for model in _all_default_model_strings():
        assert backends.default_routes_live(model), (
            f"default model {model!r} is not safe to ship: its under-transport provider "
            f"{backends.effective_provider(model)!r} either names no backend (would rot via "
            f"the opencode catch-all) or is in the dead-provider denylist "
            f"(this is the #25 silent-rot guard)"
        )


def test_default_routes_live_catches_dead_and_typo_defaults_through_the_transport():
    """`default_routes_live` is not a no-op: it REJECTS the dead default #25 removed AND a
    typo'd provider — in BOTH the flat and the agentic `oc:` form, because the check is on
    the provider UNDER any transport, not on the `oc:` wrapper.

    Holes closed:
      * the dead `oc:fireworks/.../kimi-k2p6-turbo`: `effective_provider` peels to
        `fireworks`, which names no backend (and is in `_DEAD_PROVIDERS`), so it is rejected;
      * a typo'd provider in the FLAT form (`comandcode:...`) — the bare `comandcode` names
        no backend, rejected;
      * a typo'd provider in the AGENTIC form (`oc:comandcode/...`) — the `oc:` transport
        does NOT mask the typo, because the check peels to `comandcode`, which names no
        backend, rejected. This is the agentic hole the first cut missed (codex review);
      * a bogus bare id (`totally-bogus-model-xyz`) — names no backend, rejected.
    The live commandcode Kimi seat passes via BOTH transports."""
    dead = "oc:fireworks/accounts/fireworks/routers/kimi-k2p6-turbo"
    assert backends.effective_provider(dead) == "fireworks"
    assert "fireworks" in backends._DEAD_PROVIDERS
    assert backends.default_routes_live(dead) is False
    # Typo'd provider — caught in the flat form AND in BOTH agentic spellings (`oc:` and the
    # `opencode:` alias). The agentic case is the one a name-only `_match_named_backend(model)`
    # check would have missed; both transports are checked for symmetry.
    assert backends.default_routes_live("comandcode:moonshotai/Kimi-K2.7-Code") is False
    assert (
        backends.default_routes_live("oc:comandcode/moonshotai/Kimi-K2.7-Code") is False
    )
    assert (
        backends.default_routes_live("opencode:comandcode/moonshotai/Kimi-K2.7-Code")
        is False
    )
    # A stale default whose id names no route at all.
    assert backends.default_routes_live("totally-bogus-model-xyz") is False
    # A bare-alias provider + model suffix that resolve_backend does NOT match on the full
    # id (it accepts gemini only as the bare `gemini-api` or a `gemini:` prefix, never the
    # `gemini-api:` form) and so falls through to opencode at runtime. The guard must agree:
    # checking the COLLAPSED provider token would wrongly bless it, so the guard validates
    # the FULL id route (codex review of #49). Same for the `claude-p:` form.
    assert (
        backends.resolve_backend("gemini-api:gemini-2.5-flash")
        is backends.review_opencode
    )
    assert backends.default_routes_live("gemini-api:gemini-2.5-flash") is False
    assert (
        backends.resolve_backend("claude-p:claude-opus-4-8") is backends.review_opencode
    )
    assert backends.default_routes_live("claude-p:claude-opus-4-8") is False
    # ...but the spellings resolve_backend DOES match on the full id still pass.
    assert backends.default_routes_live("gemini:gemini-2.5-flash") is True
    assert backends.default_routes_live("zai:glm-5.2") is True
    # Mixed case: the guard lowercases exactly like resolve_backend, so a mixed-case id gets
    # the SAME verdict as its lowercase form (the guard must mirror the dispatcher, codex #49).
    assert backends.resolve_backend("Codex") is backends.resolve_backend("codex")
    assert backends.default_routes_live("Codex") is True
    assert (
        backends.default_routes_live("OC:Commandcode/moonshotai/Kimi-K2.7-Code") is True
    )
    assert backends.default_routes_live("OC:Fireworks/x/y") is False  # dead, mixed case
    # Intentional flat-vs-agentic asymmetry: `oc:gemini-api/model` passes because the agentic
    # opencode transport DOES route an arbitrary `provider/model` (gemini-api is a real
    # opencode provider), whereas the flat keyed-HTTP `gemini-api:model` does not — they have
    # different runtime routes, and the guard tracks each (not a bug).
    assert (
        backends.resolve_backend("oc:gemini-api/gemini-2.5-flash")
        is backends.review_opencode
    )
    assert backends.default_routes_live("oc:gemini-api/gemini-2.5-flash") is True
    # The live defaults pass, via every transport spelling (flat, `oc:`, `opencode:`).
    assert backends.default_routes_live(KIMI_SEAT) is True
    assert backends.default_routes_live(_agentic(KIMI_SEAT)) is True
    assert backends.default_routes_live("opencode:zai/glm-5.2") is True
    assert backends.effective_provider(KIMI_SEAT) == "commandcode"
    assert backends.effective_provider(_agentic(KIMI_SEAT)) == "commandcode"


def test_effective_provider_peels_transport_and_splits_on_first_separator():
    """`effective_provider` peels the `oc:`/`opencode:` AGENTIC prefix, then takes the first
    segment before `:` or `/` — so the provider UNDER an agentic seat is what's checked, not
    the literal "opencode" transport. Covers the spellings the defaults actually use."""
    assert backends.effective_provider("codex") == "codex"
    assert (
        backends.effective_provider("commandcode:moonshotai/Kimi-K2.7-Code")
        == "commandcode"
    )
    assert (
        backends.effective_provider("oc:commandcode/moonshotai/Kimi-K2.7-Code")
        == "commandcode"
    )
    assert backends.effective_provider("opencode:zai/glm-5.2") == "zai"
    assert backends.effective_provider("zai:glm-5.2") == "zai"
    assert backends.effective_provider("oc:fireworks/x/y") == "fireworks"


def test_dead_provider_denylist_is_load_bearing_in_the_guard():
    """The `_DEAD_PROVIDERS` denylist actually CHANGES the verdict — it is not dead weight
    behind the named-route check. With `codex` forced into the denylist, `default_routes_live`
    rejects it even though `_match_named_backend('codex')` is non-None (a live named route).
    This exercises the denylist branch directly, so it can't silently stop mattering.

    Patches the module global manually with try/finally (NOT the pytest `monkeypatch`
    fixture): this file's `test_*` functions also run argument-less under the standalone
    `__main__` runner, where a fixture parameter would be an unfilled positional and the
    test would error. Same save/restore pattern the other tests here use."""
    assert backends._match_named_backend("codex") is not None  # codex IS a named route
    assert backends.default_routes_live("codex") is True  # ...and live by default
    saved = backends._DEAD_PROVIDERS
    backends._DEAD_PROVIDERS = frozenset({"codex"})
    try:
        assert (
            backends.default_routes_live("codex") is False
        )  # denylist flips the verdict
    finally:
        backends._DEAD_PROVIDERS = saved
    # Restored — the guard is back to its real verdict for the rest of the suite.
    assert backends.default_routes_live("codex") is True


def test_every_named_provider_bare_token_is_recognized():
    """For an AGENTIC `oc:`/`opencode:` default, `default_routes_live` checks the bare
    provider token under the transport (`_match_named_backend(effective_provider(model))`,
    e.g. `commandcode`/`zai` from `oc:commandcode/...`). That only works if every named
    provider's resolve_backend branch matches the bare token, not just the `provider:`-prefixed
    form — otherwise a legitimate agentic default on a bare-token-unmatched provider would get
    a false `False`.

    This pins that contract across ALL named providers the defaults use: each bare token
    must resolve to its backend. If a future provider is added with a branch that matches
    only `startswith('newprov:')`, an `oc:newprov/...` default (and this test) goes red — the
    fix is to make the branch accept the bare token too, so the #25 guard keeps working."""
    bare_to_backend = {
        "codex": backends.review_codex,
        "gemini": backends.review_gemini,
        "gemini-api": backends.review_gemini,
        "zai": backends.review_zai,
        "z.ai": backends.review_zai,
        "zhipu": backends.review_zai,
        "glm": backends.review_zai,
        "commandcode": backends.review_commandcode,
        "claude": backends.review_claude,
        "claude-p": backends.review_claude,
        "fable": backends.review_claude,
    }
    for token, backend in bare_to_backend.items():
        assert backends._match_named_backend(token) is backend, token
    # And the live agentic z.ai/GLM board seat passes the full guard (not just
    # effective_provider) — the bare-token contract is what makes that work.
    assert backends.default_routes_live("oc:zai/glm-5.2") is True


def test_agentic_helper_rewrites_provider_seat_idempotent_and_canonical():
    """`_agentic` flips a diff-only `provider:model` seat to its agentic `oc:provider/model`
    opencode form, and a colonless seat (e.g. `codex`) to `oc:<seat>`. It is IDEMPOTENT
    and CANONICAL: an already-agentic seat is returned in the `oc:` spelling — `oc:foo/bar`
    unchanged, and the `opencode:foo/bar` ALIAS normalized to `oc:foo/bar`. Both resolve to
    review_opencode and run the same `opencode -m foo/bar`, which the dashboard attributes
    to `oc:foo/bar`; canonicalizing keeps the board seat id == the attributed id so a seat
    never splits into a `no_data` board row plus a separate `oc:` health row (review-cli#24,
    codex review). Double-wrapping can't produce a nonsense `oc:oc/...` id."""
    assert (
        _agentic("commandcode:moonshotai/Kimi-K2.7-Code")
        == "oc:commandcode/moonshotai/Kimi-K2.7-Code"
    )
    assert _agentic("zai:glm-5.2") == "oc:zai/glm-5.2"
    assert _agentic("codex") == "oc:codex"
    # Idempotent: wrapping an already-`oc:` seat is a no-op (no `oc:oc/...`).
    assert _agentic("oc:zai/glm-5.2") == "oc:zai/glm-5.2"
    assert (
        _agentic(_agentic("commandcode:Qwen/Qwen3.7-Max"))
        == "oc:commandcode/Qwen/Qwen3.7-Max"
    )
    # Canonical: the `opencode:` alias is normalized to the canonical `oc:` spelling, so it
    # matches the dashboard's `oc:`-prefixed attribution of the same opencode run.
    assert _agentic("opencode:foo/bar") == "oc:foo/bar"
    assert _agentic(_agentic("opencode:foo/bar")) == "oc:foo/bar"


# === --pool seat selection (board redesign): default 4, first-N, reserve = rest ==
def test_default_pool_size_is_four():
    assert DEFAULT_POOL_SIZE == 4, DEFAULT_POOL_SIZE


def test_select_pool_default_picks_first_four_seats():
    """Default pool (no availability predicate) = the FIRST 4 seats by priority of the
    10-seat board (the rest are reserve). The pool now leads with Sol, Opus, then the
    GLM-5.2-via-commandcode seat, then Kimi (review-cli#fable-seat-reliability: Fable
    moved from priority 1 to the very last reserve seat — see
    `test_default_board_matches_directive_table`'s comment for the full rationale)."""
    pool = select_pool(list(DEFAULT_BOARD), DEFAULT_POOL_SIZE)
    assert len(pool) == 4
    assert [r.model for r in pool] == [r.model for r in DEFAULT_BOARD[:4]]
    assert [r.model for r in pool] == [
        "codex:gpt-5.6-sol",
        "claude:claude-opus-4-8",
        "commandcode:zai-org/GLM-5.2",
        "oc:commandcode/moonshotai/Kimi-K2.7-Code",
    ]
    # The reserve is exactly the remainder (priority order): Codex, the remaining opencode
    # routes (review-cli#24), the diff-only Gemini, the slow z.ai GLM seat (LAST-RESORT,
    # review-cli#65 deprioritization), and finally Fable (LAST-RESORT, ~100% failure rate).
    reserve = [r.model for r in DEFAULT_BOARD[4:]]
    assert reserve == [
        "codex",
        "oc:commandcode/Qwen/Qwen3.7-Max",
        "oc:commandcode/deepseek/deepseek-v4-pro",
        "gemini",
        "oc:zai/glm-5.2",
        "claude:claude-fable-5",
    ]


def test_select_pool_zero_or_negative_means_all_seats():
    for n in (0, -1, -8):
        assert [r.model for r in select_pool(list(DEFAULT_BOARD), n)] == [
            r.model for r in DEFAULT_BOARD
        ]


def test_select_pool_larger_than_board_is_clamped():
    assert len(select_pool(list(DEFAULT_BOARD), 99)) == len(DEFAULT_BOARD)


def test_select_pool_boundary_at_and_below_full_size():
    """Exact boundary: pool == len(board) -> all (the `pool >= len` short-circuit);
    pool == len(board) - 1 -> all but the last seat (GLM finding 11)."""
    n = len(DEFAULT_BOARD)
    assert [r.model for r in select_pool(list(DEFAULT_BOARD), n)] == [
        r.model for r in DEFAULT_BOARD
    ]
    assert [r.model for r in select_pool(list(DEFAULT_BOARD), n - 1)] == [
        r.model for r in DEFAULT_BOARD[: n - 1]
    ]


def test_select_pool_picks_first_n_in_order():
    assert [r.model for r in select_pool(list(DEFAULT_BOARD), 2)] == [
        r.model for r in DEFAULT_BOARD[:2]
    ]
    assert [r.model for r in select_pool(list(DEFAULT_BOARD), 1)] == [
        DEFAULT_BOARD[0].model
    ]


def test_select_pool_empty_board_stays_empty():
    assert select_pool([], 4) == []


def test_select_pool_does_not_mutate_input():
    """select_pool must return a NEW list, leaving the caller's board untouched — a
    future refactor returning a slice-view would silently mutate it (GLM finding 1)."""
    src = list(DEFAULT_BOARD)
    src_snapshot = list(src)
    out = select_pool(src, 4)
    out.append(BoardReviewer("x", "tests", "X"))  # mutate the result
    assert src == src_snapshot, "select_pool mutated its input board"
    # And the "all" branch (pool<=0) is a copy too, not the same object.
    assert select_pool(src, 0) is not src


def test_glm_seat_routes_agentically_via_opencode_zai_provider():
    """The GLM seat must run AGENTICALLY via opencode's `zai` provider (review-cli#24), so
    it reads the repo read-only instead of the diff-only z.ai REST call. resolve_backend(
    oc:zai/glm-5.2) -> review_opencode, and the opencode model selector (after `oc:`) is
    `zai/glm-5.2` — his z.ai subscription, glm-5.2 the newest GLM. This is the agentic form
    of the diff-only `zai:glm-5.2` seat (`_agentic` flips the transport, keeps the id)."""
    seat = next(r for r in DEFAULT_BOARD if r.display == "GLM")
    assert seat.model == "oc:zai/glm-5.2", seat.model
    assert seat.model == _agentic("zai:glm-5.2"), seat.model
    assert backends.resolve_backend(seat.model) is backends.review_opencode
    # The opencode model selector (after stripping `oc:`) is the z.ai provider/model pair.
    assert seat.model.split(":", 1)[1] == "zai/glm-5.2"


def test_all_repo_capable_default_seats_are_agentic():
    """review-cli#24 acceptance, as amended by the GLM-5.2-via-commandcode seat: every
    DEFAULT_BOARD seat that HAS an agentic transport uses it (the codex CLI, opencode, or the
    claude CLI) — not a stateless diff-only REST/keyed-HTTP call. Exactly TWO seats stay
    diff-only, each because NO agentic transport reaches them:
      * Gemini — a workspace-less REST API, no agentic CLI/opencode provider; and
      * GLM-cc (`commandcode:zai-org/GLM-5.2`, priority 3) — opencode's `commandcode`
        provider does NOT register this GLM id, so `oc:commandcode/zai-org/GLM-5.2` errors;
        the keyed-HTTP route is the only one that reaches it (verified live).

    This still pins the agentic-by-default contract for the seats that CAN be agentic:
    Kimi/z.ai-GLM/Qwen/DeepSeek go through opencode (`oc:`), Codex through the codex CLI, the
    two Anthropic seats through claude. A future edit that silently reverts one of THOSE to
    the diff-only REST route fails here. (claude is agentic via its CLI path; resolve_backend
    returns review_claude for both, and the board's claude seats run the CLI on a normal
    host.)"""
    agentic = {backends.review_codex, backends.review_opencode, backends.review_claude}
    # The seats that are legitimately diff-only (no agentic transport exists), keyed by the
    # backend they MUST route to. Any OTHER seat falling to a diff-only backend is the
    # silent regression #24 guards against.
    allowed_diff_only = {
        "Gemini": backends.review_gemini,
        "GLM-cc": backends.review_commandcode,
    }
    for seat in DEFAULT_BOARD:
        backend = backends.resolve_backend(seat.model)
        if seat.display in allowed_diff_only:
            assert backend is allowed_diff_only[seat.display], seat.model
            continue
        assert backend in agentic, f"{seat.display} ({seat.model}) is not agentic"
    # Belt-and-suspenders: the ONLY diff-only commandcode/z.ai REST seat on the default board
    # is the deliberate priority-3 GLM-cc one; every other seat is agentic (or Gemini). A
    # second keyed-HTTP commandcode/z.ai seat slipping in is the regression to catch.
    diff_only_backends = {backends.review_commandcode, backends.review_zai}
    rest_seats = [
        s.display
        for s in DEFAULT_BOARD
        if backends.resolve_backend(s.model) in diff_only_backends
    ]
    assert rest_seats == ["GLM-cc"], rest_seats


def test_install_skill_text_documents_agentic_default_board():
    """The embedded skill text `review install-skill` writes into agent harnesses must
    reflect the agentic default board (review-cli#24, codex review) — otherwise agents keep
    the stale diff-only mental model. It must mention the `oc:` default seats AND clarify
    that the keyed-HTTP commandcode/z.ai backends back only the explicit `-m cc`/`-m glm`
    paths, not the default board."""
    from reviewlib.install import SKILL_MD

    low = SKILL_MD.lower()
    assert "oc:commandcode/" in SKILL_MD, (
        "skill text must name the agentic oc: default seats"
    )
    assert "oc:zai/glm-5.2" in SKILL_MD, SKILL_MD[:0]
    # The keyed-HTTP section must be scoped to explicit `-m cc`/`-m glm`, not the default board.
    assert "diff-only" in low and "default board" in low
    assert "selected preset/board" in low
    assert "--preset heavy --pool 0" in SKILL_MD
    # review-cli#fable-seat-reliability: the heavy preset excludes Fable (~100%
    # dispatch failure rate), so --pool 0 covers 9 built-ins, not 10. Normalized
    # whitespace (GLM review finding): matching the raw string coupled this assertion
    # to exactly where install.py's paragraph happens to line-wrap — a future reflow
    # of that markdown with no content change would break it for no real reason.
    assert "covers all 9 heavy-preset-built-ins" in " ".join(SKILL_MD.split())
    """review-cli#24 contract (codex review): the agentic `oc:` board seats authenticate via
    opencode's OWN provider config — NOT review-cli's `COMMANDCODE_API_KEY`/`ZAI_API_KEY`.
    So their availability gates on the `opencode` BINARY plus opencode's OWN provider
    config — NOT review-cli's `COMMANDCODE_API_KEY`/`ZAI_API_KEY`. A host whose keys live only
    in review-cli's `.env` (and never configured opencode's `commandcode` provider) must NOT
    have an `oc:commandcode/...` seat falsely reported available on key-presence alone."""
    oc_seats = [r.model for r in DEFAULT_BOARD if r.model.startswith("oc:")]
    assert oc_seats, "expected agentic oc: seats on the default board"

    saved_which = backends._which
    # Snapshot EVERY env var we mutate (incl. GEMINI_ENV_FILE, which we overwrite below) so
    # a dev/CI environment that already sets any of them is restored exactly — otherwise
    # later tests become order-dependent on this one's teardown (codex review).
    saved_env = {
        k: os.environ.get(k)
        for k in (
            "COMMANDCODE_API_KEY",
            "ZAI_API_KEY",
            "ZHIPU_API_KEY",
            "GEMINI_ENV_FILE",
            "OC_AUTH_FILE",
            "OC_CONFIG_FILE",
        )
    }
    try:
        # Scrub review-cli's commandcode/z.ai keys AND point the env-file at nothing, so the
        # only things that could make a seat available are opencode itself and opencode auth.
        for k in saved_env:
            os.environ.pop(k, None)
        os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            auth_file = tmp / "auth.json"
            config_file = tmp / "opencode.json"
            auth_file.write_text("{}", encoding="utf-8")
            config_file.write_text('{"provider": {}}', encoding="utf-8")
            os.environ["OC_AUTH_FILE"] = str(auth_file)
            os.environ["OC_CONFIG_FILE"] = str(config_file)

            # opencode present but commandcode provider auth missing -> commandcode seats
            # are skipped at startup. Other oc providers keep their existing semantics.
            backends._which = lambda name: f"/fake/bin/{name}"
            commandcode_seats = [
                seat for seat in oc_seats if seat.startswith("oc:commandcode/")
            ]
            assert commandcode_seats, oc_seats
            for seat in commandcode_seats:
                assert backends.backend_available(seat) is False, seat

            # opencode provider auth present -> commandcode oc seats are available, still
            # without consulting review-cli's COMMANDCODE_API_KEY.
            config_file.write_text(
                '{"provider": {"commandcode": {"options": {"apiKey": "user_opencode"}}}}',
                encoding="utf-8",
            )
            for seat in commandcode_seats:
                assert backends.backend_available(seat) is True, seat

            # opencode absent -> unavailable (so the board backfills), even if review-cli
            # keys existed they would not rescue an `oc:` seat — it needs the opencode binary.
            def _no_opencode(name: str) -> str:
                if name == "opencode":
                    raise RuntimeError("opencode not found")
                return f"/fake/bin/{name}"

            backends._which = _no_opencode
            for seat in oc_seats:
                assert backends.backend_available(seat) is False, seat
    finally:
        backends._which = saved_which
        # Restore each mutated var to its exact prior state (set it back, or remove it if it
        # was unset before) — never leave a stale override for the next test.
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_contracts_role_has_a_lens():
    """The contracts lens still exists and is non-overlapping (public-API/backward-compat),
    regardless of which priority seat carries it now."""
    assert "contracts" in REVIEW_ROLES
    lens = REVIEW_ROLES["contracts"].lower()
    assert "public api" in lens or "api shape" in lens, lens
    assert "backward" in lens or "compat" in lens, lens
    # Some default seat carries the contracts lens.
    assert any(r.role == "contracts" for r in DEFAULT_BOARD)


def test_every_default_role_has_a_lens():
    for reviewer in DEFAULT_BOARD:
        assert reviewer.role in REVIEW_ROLES, reviewer.role
        assert reviewer.role_lens.strip(), reviewer.role


def test_roles_are_non_overlapping_focus_sentences():
    # Each lens names its own focus word — a cheap guard that they aren't identical.
    focuses = {role: lens.split(":", 1)[0] for role, lens in REVIEW_ROLES.items()}
    assert len(set(focuses.values())) == len(REVIEW_ROLES), focuses


# === config.yaml board parsing ==================================================
def test_no_board_key_falls_back_to_default():
    assert [r.model for r in load_board({})] == [r.model for r in DEFAULT_PRESET_BOARD]
    assert [r.model for r in load_board({"models": ["codex"]})] == [
        r.model for r in DEFAULT_PRESET_BOARD
    ]


def test_empty_or_wrong_typed_board_falls_back_to_default():
    for bad in ([], {"board": []}, {"board": "codex"}, {"board": 42}):
        cfg = bad if isinstance(bad, dict) else {"board": bad}
        assert len(load_board(cfg)) == len(DEFAULT_PRESET_BOARD), cfg


def test_board_config_overrides_default():
    cfg = {
        "board": [
            {"model": "codex", "role": "correctness"},
            {"model": "gemini", "role": "security", "name": "G"},
        ]
    }
    board = load_board(cfg)
    assert len(board) == 2
    assert board[0].model == "codex" and board[0].role == "correctness"
    # Explicit name honored; default name derived from the model tail otherwise.
    assert board[1].display == "G"
    assert board[0].display == "codex"


def test_board_alias_in_model_is_expanded():
    # `glm46` is an alias for `zai:glm-4.6` (config._expand_alias).
    board = load_board({"board": [{"model": "glm46", "role": "tests"}]})
    assert board[0].model == "zai:glm-4.6", board[0].model


def test_unknown_role_keeps_reviewer_with_generic_prompt():
    board = load_board({"board": [{"model": "codex", "role": "made-up-role"}]})
    assert len(board) == 1
    assert board[0].role == "made-up-role"
    # Unknown role -> empty lens -> the job uses the generic prompt (no crash).
    assert board[0].role_lens == ""


def test_bad_entries_are_skipped_not_crashed():
    cfg = {
        "board": [
            "not-a-mapping",
            {"role": "correctness"},  # missing model
            {"model": "   "},  # blank model
            {"model": "codex", "role": "correctness"},  # the only valid one
        ]
    }
    board = load_board(cfg)
    assert [r.model for r in board] == ["codex"], [r.model for r in board]


def test_role_omitted_is_general_with_generic_prompt():
    board = load_board({"board": [{"model": "codex"}]})
    assert board[0].role == ""
    assert board[0].role_lens == ""


# === cost-safety: PRESENT-but-all-malformed `board:` ERRORS (does not silently
# fall back to any built-in board). Absent -> default preset; partial -> keep valid. ==
def test_present_board_all_malformed_raises_not_silent_default():
    """A non-empty `board:` whose entries are ALL malformed must ERROR, NOT
    silently run a built-in board."""
    cfgs = [
        {"board": ["not-a-mapping", "still-not"]},  # no mappings at all
        {"board": [{"role": "correctness"}, {"role": "security"}]},  # all missing model
        {"board": [{"model": "   "}, {"model": ""}]},  # all blank models
        {"board": [{"model": 42}, "x", {"no": "model"}]},  # mixed garbage, none usable
    ]
    for cfg in cfgs:
        try:
            load_board(cfg)
        except BoardConfigError:
            pass  # expected
        else:
            raise AssertionError(f"expected BoardConfigError for {cfg!r}")


def test_absent_board_key_uses_default_not_error():
    """Absent `board:` -> DEFAULT_PRESET_BOARD (no error): no preference was expressed."""
    assert [r.model for r in load_board({})] == [r.model for r in DEFAULT_PRESET_BOARD]
    # An explicitly empty list is "no preference" too -> default, not an error.
    assert [r.model for r in load_board({"board": []})] == [
        r.model for r in DEFAULT_PRESET_BOARD
    ]


def test_partial_malformed_board_keeps_valid_entries_no_error():
    """SOME valid + SOME malformed -> keep the valid ones, warn on bad (no error)."""
    cfg = {
        "board": [
            "not-a-mapping",
            {"role": "correctness"},  # missing model -> skipped
            {"model": "codex", "role": "correctness"},  # valid
            {"model": "gemini", "role": "security"},  # valid
        ]
    }
    board = load_board(cfg)
    assert [r.model for r in board] == ["codex", "gemini"], [r.model for r in board]


# === role-lens injection into PanelJobs =========================================
class _AvailabilityPatch:
    """Force backend_available to a fixed predicate so the board path is offline."""

    def __init__(self, available_models: set[str]):
        self._available = available_models

    def __enter__(self):
        self._old = backends.backend_available

        def _fake(model: str) -> bool:
            return model in self._available

        backends.backend_available = _fake
        # build_board_jobs imported the name into reviewlib.panel; patch there too.
        import reviewlib.panel as panel

        self._old_panel = panel.backend_available
        panel.backend_available = _fake
        # _mode_review_board also does `from ..backends import backend_available` (a
        # direct binding in reviewlib.modes.review), so the board-pool split there must
        # be patched too — otherwise the "no reviewers available" test silently hits the
        # REAL probe (and now that `codex` is on PATH, a real seat could leak through).
        import reviewlib.modes.review as review_mod

        self._old_review_mod = review_mod.backend_available
        review_mod.backend_available = _fake
        return self

    def __exit__(self, *exc):
        backends.backend_available = self._old
        import reviewlib.panel as panel

        panel.backend_available = self._old_panel
        import reviewlib.modes.review as review_mod

        review_mod.backend_available = self._old_review_mod
        return False


def test_build_board_jobs_injects_lens_label_and_carries_effort_metadata():
    board = [BoardReviewer("codex", "performance", "Codex", "high")]
    with _AvailabilityPatch({"codex"}):
        jobs, skipped = build_board_jobs(board, DEFAULT_PROMPT, "+x")
    assert skipped == []
    assert len(jobs) == 1
    job = jobs[0]
    assert job.model == "codex"
    assert job.diff == "+x"
    assert job.label == "Codex [performance]"
    assert job.effort == "high"
    # The lens is appended to the base prompt; effort is dispatched at backend-call time.
    assert job.prompt.startswith(DEFAULT_PROMPT + "\n\n")
    assert "Use high reasoning effort." not in job.prompt
    assert REVIEW_ROLES["performance"] in job.prompt


def test_panel_job_keeps_round_no_as_sixth_positional_argument():
    from reviewlib.panel import PanelJob

    job = PanelJob("codex", "prompt", "+x", "Codex", (), 3)
    assert job.round_no == 3
    assert job.effort is None

    effort_job = PanelJob("codex", "prompt", "+x", "Codex", (), 3, "high")
    assert effort_job.round_no == 3
    assert effort_job.effort == "high"


def test_run_panel_codex_uses_native_effort_and_text_prompt_hint():
    from reviewlib import backends
    from reviewlib.panel import build_board_job, run_panel

    captured: dict = {}

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    old_which = backends._which
    old_run = backends._run_streamed
    backends._which = lambda name: f"/bin/{name}"

    def _fake_run(argv, **kw):
        captured["argv"] = argv
        captured["input_text"] = kw["input_text"]
        return _Proc()

    backends._run_streamed = _fake_run
    try:
        job = build_board_job(
            BoardReviewer(SOL_SEAT, "consistency", "Sol", "xhigh"), DEFAULT_PROMPT, "+x"
        )
        results = run_panel([job], REPO_ROOT, 5)
    finally:
        backends._which = old_which
        backends._run_streamed = old_run
    assert results[0].returncode == 0
    assert 'model_reasoning_effort="xhigh"' in captured["argv"], captured
    assert "Use highest reasoning effort." in captured["input_text"], captured


def test_build_board_jobs_unknown_role_uses_generic_prompt():
    board = [BoardReviewer("codex", "", "Codex")]
    with _AvailabilityPatch({"codex"}):
        jobs, _ = build_board_jobs(board, DEFAULT_PROMPT, "")
    assert jobs[0].prompt == DEFAULT_PROMPT  # no lens appended
    assert jobs[0].label == "Codex [general]"


# === graceful skip of unavailable reviewers =====================================
def test_unavailable_reviewers_are_skipped_not_crashed():
    board = list(DEFAULT_BOARD)
    reachable = {"gemini", "oc:zai/glm-5.2"}  # only two reachable (GLM is now agentic)
    with _AvailabilityPatch(reachable):
        jobs, skipped = build_board_jobs(board, DEFAULT_PROMPT, "+x")
    assert {j.model for j in jobs} == reachable
    assert {r.model for r in skipped} == {
        r.model for r in board if r.model not in reachable
    }
    assert len(jobs) + len(skipped) == len(board)


def test_all_unavailable_board_returns_no_jobs():
    with _AvailabilityPatch(set()):
        jobs, skipped = build_board_jobs(list(DEFAULT_BOARD), DEFAULT_PROMPT, "+x")
    assert jobs == []
    assert len(skipped) == len(DEFAULT_BOARD)


# === mode_review board path (parallel run, exit codes) ==========================
def test_mode_review_board_runs_and_succeeds():
    from reviewlib.modes import review as review_mod

    calls: list[tuple[str, str]] = []

    # round_no is the 6th positional arg run_panel passes to every backend (HYP-742
    # dashboard threading); a board reviewer fake must accept it or run_panel turns the
    # TypeError into an internal 127.
    def _fake_backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
        calls.append((model, prompt))
        return ReviewResult(
            model=model, command="fake", returncode=0, stdout="ok", stderr=""
        )

    board = [
        BoardReviewer("codex", "correctness", "Codex"),
        BoardReviewer("gemini", "consistency", "Gemini"),
    ]
    old_resolve = review_mod.resolve_backend
    review_mod.resolve_backend = lambda _m: _fake_backend
    # also patch the one build_board_jobs uses (reviewlib.panel.resolve_backend)
    import reviewlib.panel as panel

    old_panel_resolve = panel.resolve_backend
    panel.resolve_backend = lambda _m: _fake_backend
    try:
        with _AvailabilityPatch({"codex", "gemini"}):
            rc = review_mod.mode_review(
                [], DEFAULT_PROMPT, "+x", REPO_ROOT, 5, False, board=board
            )
    finally:
        review_mod.resolve_backend = old_resolve
        panel.resolve_backend = old_panel_resolve
    assert rc == 0, rc
    # Each reviewer got a role-lensed prompt.
    prompts = {model: prompt for model, prompt in calls}
    assert REVIEW_ROLES["correctness"] in prompts["codex"]
    assert REVIEW_ROLES["consistency"] in prompts["gemini"]


def test_mode_review_board_fails_when_a_reviewer_fails():
    from reviewlib.modes import review as review_mod

    def _fake_backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
        rc = 0 if model == "codex" else 1
        return ReviewResult(
            model=model, command="fake", returncode=rc, stdout="x", stderr="boom"
        )

    board = [
        BoardReviewer("codex", "correctness", "Codex"),
        BoardReviewer("gemini", "consistency", "Gemini"),
    ]
    import reviewlib.panel as panel

    old_panel_resolve = panel.resolve_backend
    panel.resolve_backend = lambda _m: _fake_backend
    try:
        with _AvailabilityPatch({"codex", "gemini"}):
            rc = review_mod.mode_review(
                [], DEFAULT_PROMPT, "+x", REPO_ROOT, 5, False, board=board
            )
    finally:
        panel.resolve_backend = old_panel_resolve
    assert rc == 1, rc


def test_mode_review_board_backfills_preflight_skip_from_reserve():
    from reviewlib.modes import review as review_mod

    calls: list[str] = []

    def _fake_backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
        calls.append(model)
        if model == "commandcode:deepseek/deepseek-v4-pro":
            return ReviewResult(
                model=model,
                command="commandcode API deepseek/deepseek-v4-pro",
                returncode=1,
                stdout="",
                stderr="provider 'commandcode' failed payment/availability preflight (HTTP 402); skipping",
            )
        return ReviewResult(
            model=model, command="fake", returncode=0, stdout="ok", stderr=""
        )

    board = [
        BoardReviewer("commandcode:deepseek/deepseek-v4-pro", "tests", "CommandCode"),
        BoardReviewer("codex", "correctness", "Codex"),
    ]
    import reviewlib.panel as panel

    old_panel_resolve = panel.resolve_backend
    panel.resolve_backend = lambda _m: _fake_backend
    try:
        with _AvailabilityPatch({"commandcode:deepseek/deepseek-v4-pro", "codex"}):
            rc = review_mod.mode_review(
                [],
                DEFAULT_PROMPT,
                "+x",
                REPO_ROOT,
                5,
                False,
                board=board,
                pool_size=1,
            )
    finally:
        panel.resolve_backend = old_panel_resolve
    assert rc == 0, rc
    assert calls == ["commandcode:deepseek/deepseek-v4-pro", "codex"], calls


def test_mode_review_failover_threads_effort_to_pool_and_reserve():
    from reviewlib.modes import review as review_mod

    calls: list[tuple[str, str | None, str]] = []

    def _fake_backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
        calls.append((model, effort, prompt))
        if model == "claude:claude-fable-5":
            return ReviewResult(
                model=model,
                command="fake",
                returncode=1,
                stdout="",
                stderr="authentication failed",
            )
        return ReviewResult(
            model=model, command="fake", returncode=0, stdout="ok", stderr=""
        )

    board = [
        BoardReviewer("claude:claude-fable-5", "architect", "Fable", effort="xhigh"),
        BoardReviewer(SOL_SEAT, "consistency", "Sol", effort="medium"),
    ]
    import reviewlib.panel as panel

    old_panel_resolve = panel.resolve_backend
    panel.resolve_backend = lambda _m: _fake_backend
    try:
        with _AvailabilityPatch({"claude:claude-fable-5", SOL_SEAT}):
            rc = review_mod.mode_review(
                [],
                DEFAULT_PROMPT,
                "+x",
                REPO_ROOT,
                5,
                False,
                board=board,
                pool_size=1,
            )
    finally:
        panel.resolve_backend = old_panel_resolve

    assert rc == 0, rc
    assert [model for model, _effort, _prompt in calls] == [
        "claude:claude-fable-5",
        SOL_SEAT,
    ]
    assert [effort for _model, effort, _prompt in calls] == ["xhigh", "medium"]
    assert "Use highest reasoning effort." in calls[0][2]
    assert "Use medium reasoning effort." in calls[1][2]


def test_mode_review_board_with_no_available_reviewers_returns_1():
    from reviewlib.modes import review as review_mod

    with _AvailabilityPatch(set()):
        rc = review_mod.mode_review(
            [], DEFAULT_PROMPT, "+x", REPO_ROOT, 5, False, board=list(DEFAULT_BOARD)
        )
    assert rc == 1, rc


def test_mode_review_exact_board_runs_unavailable_explicit_seat_and_fails():
    """Explicit -m with board metadata is exact: unavailable requested seats must not vanish."""
    from reviewlib.modes import review as review_mod

    calls: list[str] = []

    def _fake_backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
        calls.append(model)
        rc = 0 if model == "codex" else 1
        return ReviewResult(
            model=model,
            command="fake",
            returncode=rc,
            stdout="ok" if rc == 0 else "",
            stderr="missing",
        )

    board = [
        BoardReviewer("codex", "correctness", "Codex"),
        BoardReviewer("missing-provider:model", "tests", "Missing"),
    ]
    import reviewlib.panel as panel

    old_panel_resolve = panel.resolve_backend
    panel.resolve_backend = lambda _m: _fake_backend
    try:
        with _AvailabilityPatch({"codex"}):
            rc = review_mod.mode_review(
                [],
                DEFAULT_PROMPT,
                "+x",
                REPO_ROOT,
                5,
                False,
                board=board,
                pool_size=len(board),
                exact_board=True,
            )
    finally:
        panel.resolve_backend = old_panel_resolve
    assert rc == 1, rc
    assert set(calls) == {"codex", "missing-provider:model"}, calls


# === CLI wiring: explicit -m precedence (no --no-board flag exists) ==============
def test_cli_explicit_models_disable_board():
    """An explicit -m must run the flat legacy panel (board=None), NOT the board."""
    from reviewlib import cli

    captured: dict = {}

    def _fake_mode_review(models, prompt, diff, cwd, timeout, staged, board=None, **kw):
        captured["models"] = models
        captured["board"] = board
        return 0

    old = _review_mod.mode_review
    _review_mod.mode_review = _fake_mode_review
    old_load_config = cli.load_config
    cli.load_config = lambda: {}
    # Avoid touching a real config file / git diff: feed the diff via stdin and
    # point the env file at nothing so no provider key resolves.
    old_stdin = sys.stdin
    # ROUTING test (explicit -m disables the board), not a pool-assembly test. The piped diff
    # is non-empty, so on a backend-less host the review-mode pool guard would bail (exit 10)
    # before the stubbed handler runs; force every seat live via the fake backend seam.
    old_fake = os.environ.get("REVIEW_FAKE_BACKEND")
    try:
        os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
        os.environ["REVIEW_FAKE_BACKEND"] = "1"
        import io

        sys.stdin = io.StringIO("+added line\n")
        # explicit -m codex -> board must be None
        cli.main(["diff", "--task", "TEST-1", "-m", "codex", "-C", str(REPO_ROOT)])
        assert captured["board"] is None, captured["board"]
        assert captured["models"] == ["codex"], captured["models"]
    finally:
        _review_mod.mode_review = old
        cli.load_config = old_load_config
        sys.stdin = old_stdin
        if old_fake is None:
            os.environ.pop("REVIEW_FAKE_BACKEND", None)
        else:
            os.environ["REVIEW_FAKE_BACKEND"] = old_fake


def test_cli_explicit_models_override_config_models_and_board():
    """-m narrows configured board/model preferences to the requested models only."""
    from reviewlib import cli

    captured: dict = {}

    def _fake_mode_review(models, prompt, diff, cwd, timeout, staged, board=None, **kw):
        captured["models"] = models
        captured["board"] = board
        return 0

    old_mode = _review_mod.mode_review
    _review_mod.mode_review = _fake_mode_review
    old_load_config = cli.load_config
    old_avail = backends.backend_available
    cli.load_config = lambda: {
        "models": ["commandcode:deepseek/deepseek-v4-pro", "codex"],
        "board": [
            {
                "model": "commandcode:deepseek/deepseek-v4-pro",
                "role": "tests",
                "name": "DeepSeek-cc",
            },
            {"model": "codex", "role": "consistency", "name": "Codex"},
            {"model": "claude:claude-fable-5", "role": "architect", "name": "Fable"},
        ],
    }
    backends.backend_available = lambda _m: True
    old_stdin = sys.stdin
    try:
        import io

        sys.stdin = io.StringIO("+added line\n")
        cli.main(["diff", "--task", "TEST-1", "-m", "fable5", "-C", str(REPO_ROOT)])
        assert captured["board"] is not None, captured
        assert [(r.model, r.role, r.display) for r in captured["board"]] == [
            ("claude:claude-fable-5", "architect", "Fable"),
        ], captured
        assert captured["models"] == ["claude:claude-fable-5"], captured
    finally:
        _review_mod.mode_review = old_mode
        cli.load_config = old_load_config
        backends.backend_available = old_avail
        sys.stdin = old_stdin


def test_cli_explicit_models_are_not_sliced_by_pool():
    """When -m is explicit, --pool must not drop requested models from the review panel."""
    from reviewlib import cli

    captured: dict = {}

    def _fake_mode_review(
        models,
        prompt,
        diff,
        cwd,
        timeout,
        staged,
        board=None,
        pool_size=DEFAULT_POOL_SIZE,
        **kw,
    ):
        captured["models"] = models
        captured["board"] = board
        captured["pool_size"] = pool_size
        captured["exact_board"] = kw.get("exact_board")
        return 0

    old_mode = _review_mod.mode_review
    _review_mod.mode_review = _fake_mode_review
    old_load_config = cli.load_config
    old_avail = backends.backend_available
    cli.load_config = lambda: {
        "models": ["codex", "gemini", "claude:claude-fable-5"],
        "board": [
            {"model": "codex", "role": "correctness", "name": "Codex"},
            {"model": "gemini", "role": "tests", "name": "Gemini"},
            {"model": "claude:claude-fable-5", "role": "architect", "name": "Fable"},
        ],
    }
    backends.backend_available = lambda _m: True
    old_stdin = sys.stdin
    try:
        import io

        sys.stdin = io.StringIO("+added line\n")
        cli.main(
            [
                "diff",
                "--task",
                "TEST-1",
                "-m",
                "codex",
                "-m",
                "fable5",
                "--pool",
                "1",
                "-C",
                str(REPO_ROOT),
            ]
        )
        assert [r.model for r in captured["board"]] == [
            "codex",
            "claude:claude-fable-5",
        ], captured
        assert captured["pool_size"] == 2, captured
        assert captured["exact_board"] is True, captured
        assert captured["models"] == ["codex", "claude:claude-fable-5"], captured
    finally:
        _review_mod.mode_review = old_mode
        cli.load_config = old_load_config
        backends.backend_available = old_avail
        sys.stdin = old_stdin


def test_cli_explicit_models_override_preset_and_config_models():
    """-m is exact: neither --preset nor config models may append or reorder seats."""
    from reviewlib import cli

    captured: dict = {}

    def _fake_mode_review(
        models,
        prompt,
        diff,
        cwd,
        timeout,
        staged,
        board=None,
        pool_size=DEFAULT_POOL_SIZE,
        **kw,
    ):
        captured["models"] = models
        captured["board"] = board
        captured["pool_size"] = pool_size
        captured["exact_board"] = kw.get("exact_board")
        return 0

    old_mode = _review_mod.mode_review
    _review_mod.mode_review = _fake_mode_review
    old_load_config = cli.load_config
    old_avail = backends.backend_available
    cli.load_config = lambda: {
        "models": ["codex", "gemini"],
        "board": [
            {"model": "claude:claude-opus-4-8", "role": "tests", "effort": "low"}
        ],
    }
    backends.backend_available = lambda _m: True
    old_stdin = sys.stdin
    try:
        import io

        sys.stdin = io.StringIO("+added line\n")
        cli.main(
            [
                "diff",
                "--task",
                "TEST-1",
                "--preset",
                "heavy",
                "-m",
                "gemini",
                "-m",
                "claude:claude-opus-4-8",
                "--pool",
                "1",
                "-C",
                str(REPO_ROOT),
            ]
        )
        assert captured["models"] == ["gemini", "claude:claude-opus-4-8"], captured
        assert [r.model for r in captured["board"]] == [
            "gemini",
            "claude:claude-opus-4-8",
        ], captured
        assert [r.effort for r in captured["board"]] == ["max", "xhigh"], captured
        assert captured["pool_size"] == 2, captured
        assert captured["exact_board"] is True, captured
    finally:
        _review_mod.mode_review = old_mode
        cli.load_config = old_load_config
        backends.backend_available = old_avail
        sys.stdin = old_stdin


def test_cli_explicit_model_not_in_config_board_is_still_requested():
    """Configured board metadata must not drop an explicit model that lacks a board entry."""
    from reviewlib import cli

    captured: dict = {}

    def _fake_mode_review(models, prompt, diff, cwd, timeout, staged, board=None, **kw):
        captured["models"] = models
        captured["board"] = board
        captured["exact_board"] = kw.get("exact_board")
        return 0

    old_mode = _review_mod.mode_review
    _review_mod.mode_review = _fake_mode_review
    old_load_config = cli.load_config
    old_avail = backends.backend_available
    cli.load_config = lambda: {
        "models": ["codex"],
        "board": [{"model": "codex", "role": "correctness", "name": "Codex"}],
    }
    backends.backend_available = lambda _m: True
    old_stdin = sys.stdin
    try:
        import io

        sys.stdin = io.StringIO("+added line\n")
        cli.main(["diff", "--task", "TEST-1", "-m", "gemini", "-C", str(REPO_ROOT)])
        assert captured["models"] == ["gemini"], captured
        assert [r.model for r in captured["board"]] == ["gemini"], captured
        assert captured["exact_board"] is True, captured
    finally:
        _review_mod.mode_review = old_mode
        cli.load_config = old_load_config
        backends.backend_available = old_avail
        sys.stdin = old_stdin


def test_cli_explicit_models_ignore_malformed_config_board():
    """Explicit -m is an override: a broken config board must not abort the requested run."""
    from reviewlib import cli

    captured: dict = {}

    def _fake_mode_review(models, prompt, diff, cwd, timeout, staged, board=None, **kw):
        captured["models"] = models
        captured["board"] = board
        captured["exact_board"] = kw.get("exact_board")
        return 0

    old_mode = _review_mod.mode_review
    _review_mod.mode_review = _fake_mode_review
    old_load_config = cli.load_config
    old_avail = backends.backend_available
    cli.load_config = lambda: {"board": [{"role": "tests"}]}
    backends.backend_available = lambda _m: True
    old_stdin = sys.stdin
    try:
        import io

        sys.stdin = io.StringIO("+added line\n")
        rc = cli.main(["diff", "--task", "TEST-1", "-m", "codex", "-C", str(REPO_ROOT)])
    finally:
        _review_mod.mode_review = old_mode
        cli.load_config = old_load_config
        backends.backend_available = old_avail
        sys.stdin = old_stdin
    assert rc == 0
    assert captured["models"] == ["codex"], captured
    assert [r.model for r in captured["board"]] == ["codex"], captured
    assert captured["exact_board"] is True, captured


def test_cli_leading_model_option_before_subcommand_is_honored():
    """Leading -m keeps the same precedence and narrows config models after normalization."""
    from reviewlib import cli

    captured: dict = {}

    def _fake_mode_review(models, prompt, diff, cwd, timeout, staged, board=None, **kw):
        captured["models"] = models
        captured["board"] = board
        captured["cwd"] = cwd
        return 0

    old_mode = _review_mod.mode_review
    _review_mod.mode_review = _fake_mode_review
    old_load_config = cli.load_config
    old_avail = backends.backend_available
    cli.load_config = lambda: {
        "models": ["commandcode:deepseek/deepseek-v4-pro", "codex"]
    }
    backends.backend_available = lambda _m: True
    old_stdin = sys.stdin
    try:
        import io

        sys.stdin = io.StringIO("+added line\n")
        cli.main(["-m", "fable5", "-C", str(REPO_ROOT), "diff", "--task", "TEST-1"])
        assert captured["board"] is not None, captured
        assert [r.model for r in captured["board"]] == ["claude:claude-fable-5"], (
            captured
        )
        assert captured["models"] == ["claude:claude-fable-5"], captured
        assert captured["cwd"] == REPO_ROOT, captured
    finally:
        _review_mod.mode_review = old_mode
        cli.load_config = old_load_config
        backends.backend_available = old_avail
        sys.stdin = old_stdin


def test_cli_leading_model_option_does_not_reorder_management_subcommands():
    """Management subcommands are not review modes, so leading -m remains an argparse error."""
    from reviewlib import cli

    argv = ["-m", "fable5", "dashboard"]
    assert cli._normalize_leading_mode_options(argv) == argv


def test_cli_leading_mode_options_do_not_treat_long_flags_as_short_m():
    """Unknown long options such as --markdown must not be mistaken for glued -m values."""
    from reviewlib import cli

    argv = ["--markdown", "diff"]
    assert cli._normalize_leading_mode_options(argv) == argv


def test_cli_no_board_flag_is_gone():
    """The board can NEVER be disabled: --no-board was removed, so argparse must
    reject it (SystemExit) rather than silently accepting + disabling the board."""
    from reviewlib import cli

    old_stdin = sys.stdin
    try:
        import io

        sys.stdin = io.StringIO("+added line\n")
        raised = False
        try:
            cli.main(["--no-board", "-C", str(REPO_ROOT)])
        except SystemExit:
            raised = True  # argparse rejects the unknown flag
        assert raised, "--no-board must be an unknown flag now (removed)"
    finally:
        sys.stdin = old_stdin


def test_cli_config_models_form_priority_board():
    """A config `models:` list is the priority roster for the diff-review board.

    It must NOT take the legacy flat path: `review diff` still gets a board, `--pool`
    still sizes the active pool, and the remaining configured models are reserve seats.
    Explicit CLI `-m` narrows this configured board to the requested models."""
    from reviewlib import cli

    captured: dict = {}

    def _fake_mode_review(
        models,
        prompt,
        diff,
        cwd,
        timeout,
        staged,
        board=None,
        pool_size=DEFAULT_POOL_SIZE,
        **kw,
    ):
        captured["models"] = models
        captured["board"] = board
        captured["pool_size"] = pool_size
        return 0

    old = _review_mod.mode_review
    _review_mod.mode_review = _fake_mode_review
    old_load_config = cli.load_config
    cli.load_config = lambda: {
        "models": ["codex", "gemini", "commandcode:deepseek/deepseek-v4-pro"]
    }
    # Force every seat live so the pre-dispatch pool-selection guard (reviewlib.pool_guard)
    # doesn't bail on a host without these backends' keys/CLIs — this test checks BOARD
    # ROUTING, not liveness.
    old_avail = backends.backend_available
    backends.backend_available = lambda _m: True
    old_stdin = sys.stdin
    try:
        os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
        import io

        sys.stdin = io.StringIO("+added line\n")
        cli.main(["diff", "--task", "TEST-1", "--pool", "2", "-C", str(REPO_ROOT)])
        assert captured["board"] is not None, captured
        assert [r.model for r in captured["board"]] == [
            "codex",
            "gemini",
            "commandcode:deepseek/deepseek-v4-pro",
        ], captured["board"]
        assert captured["pool_size"] == 2, captured["pool_size"]
        # The flat model list is still passed for compatibility, but the board path wins.
        assert captured["models"] == [
            "codex",
            "gemini",
            "commandcode:deepseek/deepseek-v4-pro",
        ], captured["models"]
    finally:
        _review_mod.mode_review = old
        cli.load_config = old_load_config
        backends.backend_available = old_avail
        sys.stdin = old_stdin


def test_cli_empty_config_models_does_not_disable_board():
    """An "effectively empty" `models:` — absent, [], or only blank/whitespace
    entries — is NOT a real preference, so it must NOT disable the board NOR feed
    blank model names to the panel (codex P2). The board still runs in every case."""
    from reviewlib import cli

    for empty_models in ([], ["", "  ", "\t"]):
        captured: dict = {}

        def _fake_mode_review(
            models, prompt, diff, cwd, timeout, staged, board=None, **kw
        ):
            captured["board"] = board
            captured["models"] = models
            return 0

        old = _review_mod.mode_review
        _review_mod.mode_review = _fake_mode_review
        old_load_config = cli.load_config
        cli.load_config = lambda em=empty_models: {"models": em}
        old_load_board = cli.load_board
        cli.load_board = lambda _cfg, **_kw: list(DEFAULT_PRESET_BOARD)
        # Force liveness: this checks the board is not DISABLED by an empty models list,
        # not backend availability — keep the pool-selection guard from bailing on a host
        # lacking these backends.
        old_avail = backends.backend_available
        backends.backend_available = lambda _m: True
        old_stdin = sys.stdin
        try:
            os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
            import io

            sys.stdin = io.StringIO("+added line\n")
            cli.main(["diff", "--task", "TEST-1", "-C", str(REPO_ROOT)])
            assert captured["board"] is not None, (
                f"{empty_models!r} must not disable the board"
            )
            # And no blank model name leaked into the (unused) flat models list.
            assert all(m.strip() for m in captured["models"]), captured["models"]
        finally:
            _review_mod.mode_review = old
            cli.load_config = old_load_config
            cli.load_board = old_load_board
            backends.backend_available = old_avail
            sys.stdin = old_stdin


def test_cli_all_malformed_board_errors_nonzero():
    """A present-but-all-malformed `board:` makes the CLI exit non-zero with a
    message, not silently run the paid default board."""
    from reviewlib import cli

    called = {"mode_review": False}

    def _fake_mode_review(*_a, **_k):
        called["mode_review"] = True
        return 0

    old = _review_mod.mode_review
    _review_mod.mode_review = _fake_mode_review
    old_load_config = cli.load_config
    cli.load_config = lambda: {"board": ["not-a-mapping", {"role": "x"}]}
    old_stdin = sys.stdin
    try:
        os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
        import io

        sys.stdin = io.StringIO("+added line\n")
        rc = cli.main(["diff", "--task", "TEST-1", "-C", str(REPO_ROOT)])
        assert rc != 0, rc
        assert called["mode_review"] is False, "must not run the panel on a bad board"
    finally:
        _review_mod.mode_review = old
        cli.load_config = old_load_config
        sys.stdin = old_stdin


def test_cli_standalone_visual_ignores_malformed_board():
    """(codex P2) Standalone `review visual image` (no diff) does NOT use the reviewer
    board, so a present-but-malformed `board:` must NOT block it — board validation runs
    only on the board path, after the standalone pipeline has had its chance."""
    from reviewlib import cli
    import reviewlib.features.visual.visual_cli as visual_cli

    reached = {"standalone": False}

    def _fake_standalone(*_a, **_k):
        reached["standalone"] = True
        return 0

    old_standalone = visual_cli.run_visual_standalone
    visual_cli.run_visual_standalone = _fake_standalone
    old_load_config = cli.load_config
    cli.load_config = lambda: {
        "board": ["not-a-mapping"]
    }  # malformed board, irrelevant here
    old_stdin = sys.stdin
    try:
        os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
        import io

        # No piped diff and the cwd is not inside a diff-producing repo path we control;
        # _git_diff degrades to "" for --visual, so this routes to the standalone pipeline.
        sys.stdin = io.StringIO("")
        rc = cli.main(
            [
                "diff",
                "--task",
                "TEST-1",
                "--visual",
                "/tmp/does-not-exist-zzz.png",
                "-C",
                "/tmp",
            ]
        )
        assert rc == 0, rc
        assert reached["standalone"] is True, (
            "standalone visual must run despite the malformed board"
        )
    finally:
        visual_cli.run_visual_standalone = old_standalone
        cli.load_config = old_load_config
        sys.stdin = old_stdin


def test_cli_all_malformed_board_fails_before_visual_fanout():
    """(codex P2) On the default-review path, an all-malformed `board:` must error
    BEFORE the --visual fan-out, so a doomed config never spends a paid vision call."""
    from reviewlib import cli
    import reviewlib.features.visual.compose as compose

    fanout_calls = {"n": 0}

    def _fake_build(*_a, **_k):
        fanout_calls["n"] += 1
        raise AssertionError(
            "visual fan-out must not run when the board config is invalid"
        )

    old_mode_review = _review_mod.mode_review
    _review_mod.mode_review = lambda *_a, **_k: 0
    old_load_config = cli.load_config
    cli.load_config = lambda: {"board": ["not-a-mapping"]}
    old_build = compose.build_mode_visual_context
    compose.build_mode_visual_context = _fake_build
    old_stdin = sys.stdin
    try:
        os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
        import io

        sys.stdin = io.StringIO("+added line\n")
        rc = cli.main(
            [
                "diff",
                "--task",
                "TEST-1",
                "--visual",
                "/tmp/does-not-exist-zzz.png",
                "-C",
                str(REPO_ROOT),
            ]
        )
        assert rc == 2, rc
        assert fanout_calls["n"] == 0, "fan-out ran before the board was validated"
    finally:
        _review_mod.mode_review = old_mode_review
        cli.load_config = old_load_config
        compose.build_mode_visual_context = old_build
        sys.stdin = old_stdin


def _capture_default_review_board(argv: list[str]) -> dict:
    """Run `cli.main(argv)` on the default-review path with the board pinned to
    DEFAULT_BOARD and an empty config, capturing the (full) board and pool_size passed
    into mode_review. All seats forced AVAILABLE so the test is offline + deterministic.
    Shared by the default-pool and --pool wiring tests."""
    from reviewlib import cli

    captured: dict = {}

    def _fake_mode_review(
        models,
        prompt,
        diff,
        cwd,
        timeout,
        staged,
        board=None,
        pool_size=DEFAULT_POOL_SIZE,
        outcome_sink=None,
        **kw,
    ):
        captured["board"] = board
        captured["pool_size"] = pool_size
        captured["timeout"] = timeout
        return 0

    old = _review_mod.mode_review
    _review_mod.mode_review = _fake_mode_review
    # Pin the board to DEFAULT_BOARD AND stub load_config to an empty dict so the
    # test is independent of the dev machine's ~/.config/review-cli/config.yaml.
    # The true default path has neither -m nor config models.
    old_load_board = cli.load_board

    def _load_board(_cfg, **kw):
        preset = kw.get("preset")
        if preset == "default":
            return list(DEFAULT_PRESET_BOARD)
        if preset == "light":
            return list(LIGHT_PRESET_BOARD)
        if preset == "heavy":
            return list(HEAVY_PRESET_BOARD)
        return list(DEFAULT_BOARD)

    cli.load_board = _load_board
    old_load_config = cli.load_config
    cli.load_config = lambda: {}
    # Force every seat available so the CLI's ETA planned_pool slice is deterministic
    # and never probes a real key/CLI (offline).
    old_avail = backends.backend_available
    backends.backend_available = lambda _m: True
    old_stdin = sys.stdin
    try:
        os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
        import io

        sys.stdin = io.StringIO("+added line\n")
        cli.main(argv)
    finally:
        _review_mod.mode_review = old
        cli.load_board = old_load_board
        cli.load_config = old_load_config
        backends.backend_available = old_avail
        sys.stdin = old_stdin
    return captured


def test_cli_default_path_passes_full_preset_board_and_default_pool():
    """No -m -> the full default-preset board is passed into mode_review (reserve incl.)
    with pool_size = the default preset pool (4)."""
    captured = _capture_default_review_board(
        ["diff", "--task", "TEST-1", "-C", str(REPO_ROOT)]
    )
    assert captured["board"] is not None, "board should be active by default"
    assert len(captured["board"]) == len(DEFAULT_PRESET_BOARD), (
        "full board passed (reserve incl.)"
    )
    assert [r.model for r in captured["board"]] == [
        r.model for r in DEFAULT_PRESET_BOARD
    ]
    assert captured["pool_size"] == DEFAULT_POOL_SIZE, captured["pool_size"]


def test_cli_builtin_default_uses_default_preset_board():
    captured = _capture_default_review_board(
        ["diff", "--task", "TEST-1", "-C", str(REPO_ROOT)]
    )
    assert [r.model for r in captured["board"]] == [
        r.model for r in DEFAULT_PRESET_BOARD
    ]
    assert all(r.effort == "high" for r in captured["board"])
    assert captured["pool_size"] == 4


def test_cli_light_and_heavy_presets_set_board_and_default_pool():
    light = _capture_default_review_board(
        [
            "diff",
            "--task",
            "TEST-1",
            "--preset",
            "light",
            "-C",
            str(REPO_ROOT),
        ]
    )
    assert [r.model for r in light["board"]] == [r.model for r in LIGHT_PRESET_BOARD]
    assert all(r.effort == "medium" for r in light["board"])
    assert light["pool_size"] == 2

    heavy = _capture_default_review_board(
        [
            "diff",
            "--task",
            "TEST-1",
            "--preset",
            "heavy",
            "-C",
            str(REPO_ROOT),
        ]
    )
    # review-cli#fable-seat-reliability: Fable is EXCLUDED from the heavy preset (a
    # confirmed ~100% dispatch failure rate makes it pure waste at any effort tier) —
    # Sol now leads, with Kimi promoted into the vacated 4th xhigh slot.
    assert [r.model for r in heavy["board"][:4]] == [
        SOL_SEAT,
        "claude:claude-opus-4-8",
        GLM_COMMANDCODE_SEAT,
        "oc:commandcode/moonshotai/Kimi-K2.7-Code",
    ]
    assert all(r.effort == "xhigh" for r in heavy["board"][:4])
    assert all(r.effort == "max" for r in heavy["board"][4:])
    assert "claude:claude-fable-5" not in [r.model for r in heavy["board"]]
    assert heavy["pool_size"] == 4


def _capture_review_with_config(argv: list[str], config: dict) -> dict:
    """Run the default-review path with a CUSTOM config (all seats live), capturing the
    pool_size passed into mode_review. For config `pool:` default-size wiring tests."""
    from reviewlib import cli

    captured: dict = {}

    def _fake_mode_review(
        models,
        prompt,
        diff,
        cwd,
        timeout,
        staged,
        board=None,
        pool_size=DEFAULT_POOL_SIZE,
        outcome_sink=None,
        **kw,
    ):
        captured["board"] = board
        captured["pool_size"] = pool_size
        return 0

    old = _review_mod.mode_review
    _review_mod.mode_review = _fake_mode_review
    old_load_config = cli.load_config
    cli.load_config = lambda: config
    old_avail = backends.backend_available
    backends.backend_available = lambda _m: True
    old_stdin = sys.stdin
    try:
        os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
        import io

        sys.stdin = io.StringIO("+added line\n")
        cli.main(argv)
    finally:
        _review_mod.mode_review = old
        cli.load_config = old_load_config
        backends.backend_available = old_avail
        sys.stdin = old_stdin
    return captured


def test_config_pool_key_sets_default_pool_size():
    """A config `pool:` int becomes the default pool size when no --pool is passed."""
    cap = _capture_review_with_config(
        ["diff", "--task", "TEST-1", "-C", str(REPO_ROOT)],
        {"models": ["codex", "claude:claude-opus-4-8", "oc:zai/glm-5.2"], "pool": 3},
    )
    assert cap["pool_size"] == 3, cap["pool_size"]


def test_explicit_pool_flag_overrides_config_pool():
    """An explicit `--pool N` wins over the config `pool:` default."""
    cap = _capture_review_with_config(
        ["diff", "--task", "TEST-1", "--pool", "2", "-C", str(REPO_ROOT)],
        {"models": ["codex", "claude:claude-opus-4-8", "oc:zai/glm-5.2"], "pool": 3},
    )
    assert cap["pool_size"] == 2, cap["pool_size"]


def test_explicit_preset_overrides_config_pool():
    """An explicit `--preset` sizes the pool from the preset, not the config `pool:` key."""
    cap = _capture_review_with_config(
        ["diff", "--task", "TEST-1", "--preset", "light", "-C", str(REPO_ROOT)],
        {"pool": 3},
    )
    assert cap["pool_size"] == 2, cap[
        "pool_size"
    ]  # light preset pool = 2, not config 3


def test_invalid_config_pool_falls_back_to_preset_default():
    """A non-positive / non-int config `pool:` is ignored (falls back to the preset)."""
    cap = _capture_review_with_config(
        ["diff", "--task", "TEST-1", "-C", str(REPO_ROOT)],
        {"pool": 0},  # 0 = "all" is a CLI-only knob; as a config default it's ignored
    )
    assert cap["pool_size"] == DEFAULT_POOL_SIZE, cap["pool_size"]


def test_cli_explicit_preset_overrides_config_board():
    from reviewlib import cli

    captured: dict = {}

    def _fake_mode_review(
        models,
        prompt,
        diff,
        cwd,
        timeout,
        staged,
        board=None,
        pool_size=DEFAULT_POOL_SIZE,
        **kw,
    ):
        captured["board"] = board
        captured["pool_size"] = pool_size
        return 0

    old_mode = _review_mod.mode_review
    _review_mod.mode_review = _fake_mode_review
    old_load_config = cli.load_config
    old_avail = backends.backend_available
    cli.load_config = lambda: {
        "board": [
            {
                "model": "gemini",
                "role": "tests",
                "name": "Configured",
                "effort": "medium",
            }
        ],
    }
    backends.backend_available = lambda _m: True
    old_stdin = sys.stdin
    try:
        import io

        sys.stdin = io.StringIO("+added line\n")
        cli.main(
            ["diff", "--task", "TEST-1", "--preset", "heavy", "-C", str(REPO_ROOT)]
        )
        # review-cli#fable-seat-reliability: Fable is excluded from the heavy preset.
        assert [r.model for r in captured["board"][:4]] == [
            SOL_SEAT,
            "claude:claude-opus-4-8",
            GLM_COMMANDCODE_SEAT,
            "oc:commandcode/moonshotai/Kimi-K2.7-Code",
        ], captured
        assert captured["board"][0].effort == "xhigh", captured
        assert captured["pool_size"] == 4, captured
        assert "claude:claude-fable-5" not in [r.model for r in captured["board"]]
    finally:
        _review_mod.mode_review = old_mode
        cli.load_config = old_load_config
        backends.backend_available = old_avail
        sys.stdin = old_stdin


def test_cli_pool_flag_threads_pool_size():
    """--pool N threads N as pool_size into mode_review (the full board still flows so
    the reserve is available); --pool 0 = all seats."""
    cap2 = _capture_default_review_board(
        ["diff", "--task", "TEST-1", "--pool", "2", "-C", str(REPO_ROOT)]
    )
    assert cap2["pool_size"] == 2, cap2["pool_size"]
    assert len(cap2["board"]) == len(DEFAULT_PRESET_BOARD), "full board still passed"
    cap_all = _capture_default_review_board(
        ["diff", "--task", "TEST-1", "--pool", "0", "-C", str(REPO_ROOT)]
    )
    assert cap_all["pool_size"] == 0, cap_all["pool_size"]


def test_cli_leading_pool_and_timeout_options_before_subcommand_are_honored():
    """Value-taking global flags before the mode verb must land on the mode parser."""
    captured = _capture_default_review_board(
        [
            "--pool",
            "2",
            "--timeout",
            "77",
            "diff",
            "--task",
            "TEST-1",
            "-C",
            str(REPO_ROOT),
        ]
    )
    assert captured["pool_size"] == 2, captured
    assert captured["timeout"] == 77, captured


def test_cli_leading_meta_flags_before_subcommand_are_honored():
    """Zero-arg global flags before the mode verb must land on the mode parser too."""
    from reviewlib import cli

    assert cli._normalize_leading_mode_options(["--list-defaults", "diff"]) == [
        "diff",
        "--list-defaults",
    ]
    captured: dict = {}
    old_show_board = cli._show_board
    old_load_config = cli.load_config
    cli.load_config = lambda: {}
    cli._show_board = lambda config, pool_size, cwd=None, **kw: (
        captured.update(
            {
                "pool_size": pool_size,
                "cwd": cwd,
                "preset": kw.get("preset"),
                "explicit_models": kw.get("explicit_models"),
            }
        )
        or 0
    )
    try:
        rc = cli.main(
            [
                "--show-board",
                "--pool",
                "2",
                "-m",
                "codex",
                "diff",
                "--preset",
                "heavy",
                "-C",
                str(REPO_ROOT),
            ]
        )
    finally:
        cli._show_board = old_show_board
        cli.load_config = old_load_config
    assert rc == 0
    assert captured["pool_size"] == 2, captured
    assert captured["cwd"] == REPO_ROOT, captured
    assert captured["preset"] == "heavy", captured
    assert captured["explicit_models"] == ["codex"], captured


def _show_board_lines(
    pool_size: int,
    board_models: list[str] | None = None,
    available: set[str] | None = None,
    *,
    preset: str | None = None,
    explicit_models: list[str] | None = None,
) -> list[str]:
    """Run cli._show_board(config, pool_size) and return its stdout lines. When
    board_models is given, a config `board:` of those models is used (each role tests).
    `available` forces the availability probe to a fixed set (None = all available) so
    the tag split is deterministic + offline."""
    import contextlib
    import io

    from reviewlib import cli

    cfg: dict = {}
    if board_models is not None:
        cfg = {"board": [{"model": m, "role": "tests"} for m in board_models]}
    old_avail = backends.backend_available
    backends.backend_available = lambda m: (
        True if available is None else (m in available)
    )
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = cli._show_board(
                cfg, pool_size, preset=preset, explicit_models=explicit_models
            )
    finally:
        backends.backend_available = old_avail
    assert rc == 0, rc
    return buf.getvalue().splitlines()


def test_show_board_honors_pool_flag_tagging():
    """`--show-board --pool N` (all seats available) must tag the top N priority seats
    `pool`, the rest `reserve`."""
    seat_lines = [
        ln for ln in _show_board_lines(2) if "[pool" in ln or "[reserve]" in ln
    ]
    assert len(seat_lines) == len(DEFAULT_PRESET_BOARD)
    pool_lines = [ln for ln in seat_lines if "[pool" in ln]
    reserve_lines = [ln for ln in seat_lines if "[reserve]" in ln]
    assert len(pool_lines) == 2, pool_lines
    assert len(reserve_lines) == len(DEFAULT_PRESET_BOARD) - 2, reserve_lines
    # The default preset excludes Fable/Sol, so the first two preset seats are the pool.
    assert "Opus" in pool_lines[0]
    assert "GLM-cc" in pool_lines[1]


def test_show_board_explicit_model_is_exact():
    lines = _show_board_lines(4, explicit_models=["codex"])
    seat_lines = [ln for ln in lines if "[explicit]" in ln]
    assert len(seat_lines) == 1, lines
    assert "codex  [available]" in seat_lines[0], lines
    assert "effort=-" in seat_lines[0], lines
    assert "source: explicit -m" in lines[0], lines
    assert "exact -m run = every LIVE listed seat is attempted" in lines[0], lines
    assert any("--pool` and reserve failover do not slice" in ln for ln in lines), lines
    assert not any("claude:claude-opus-4-8" in ln for ln in seat_lines), lines


def test_show_board_explicit_model_with_preset_keeps_exact_model_and_preset_effort_metadata():
    lines = _show_board_lines(4, preset="heavy", explicit_models=["codex"])
    seat_lines = [ln for ln in lines if "[explicit]" in ln]
    assert len(seat_lines) == 1, lines
    assert "codex  [available]" in seat_lines[0], lines
    assert "effort=max" in seat_lines[0], lines
    assert "source: explicit -m + preset:heavy" in lines[0], lines


def test_show_board_explicit_models_are_all_live_and_pool_is_ignored():
    lines = _show_board_lines(
        1,
        explicit_models=["codex", "gemini", "missing-provider:model"],
        available={"codex", "gemini"},
    )
    seat_lines = [ln for ln in lines if "[explicit]" in ln]
    assert len(seat_lines) == 3, lines
    assert not any(
        "[pool" in ln or "[reserve]" in ln or "[unavail]" in ln for ln in lines
    ), lines
    assert any(
        "missing-provider:model" in ln and "will attempt (no key/CLI)" in ln
        for ln in seat_lines
    ), lines
    assert any("--pool` and reserve failover do not slice" in ln for ln in lines), lines


def test_show_board_startup_failover_skips_unavailable_top_seat():
    """The live pool is the top-N AVAILABLE seats by priority: an unavailable higher
    priority seat is tagged `unavail` and the next available seat fills the pool."""
    # Sol (#1 in the heavy preset — review-cli#fable-seat-reliability demoted Fable to
    # LAST, so Fable is no longer in HEAVY_PRESET_BOARD at all) unavailable -> the pool
    # of 2 is Opus (#2) + GLM-cc (#3); Sol is unavail.
    avail = {r.model for r in DEFAULT_BOARD if r.model != SOL_SEAT}
    seat_lines = [
        ln
        for ln in _show_board_lines(2, available=avail, preset="heavy")
        if "[pool" in ln or "[reserve]" in ln or "[unavail]" in ln
    ]
    by_tier = {"pool": [], "reserve": [], "unavail": []}
    for ln in seat_lines:
        for tier in by_tier:
            if f"[{tier}" in ln:
                by_tier[tier].append(ln)
                break
    assert len(by_tier["pool"]) == 2, by_tier["pool"]
    assert "Sol" in by_tier["unavail"][0], by_tier["unavail"]
    assert "Opus" in by_tier["pool"][0]
    assert "GLM-cc" in by_tier["pool"][1]


def test_show_board_marks_claude_commandcode_api_gateway_unpaid():
    import contextlib
    import io

    from reviewlib import cli

    saved = {
        k: os.environ.get(k)
        for k in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_BASE_URL",
            "REVIEW_CLAUDE_MODE",
            "REVIEW_UNPAID_PROVIDERS",
        )
    }
    old_which = backends._which_optional
    buf = io.StringIO()
    try:
        backends._which_optional = lambda _name: None
        os.environ["ANTHROPIC_API_KEY"] = "user_x"
        os.environ["ANTHROPIC_BASE_URL"] = "https://api.commandcode.ai/provider"
        os.environ.pop("REVIEW_CLAUDE_MODE", None)
        os.environ["REVIEW_UNPAID_PROVIDERS"] = "commandcode"
        cfg = {"board": [{"model": "claude:claude-fable-5", "role": "tests"}]}
        with contextlib.redirect_stdout(buf):
            rc = cli._show_board(cfg, 1, REPO_ROOT)
    finally:
        backends._which_optional = old_which
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    assert rc == 0, buf.getvalue()
    lines = buf.getvalue().splitlines()
    row = next(line for line in lines if "claude:claude-fable-5" in line)
    assert "SKIPPED (provider unpaid/disabled)" in row, row


def test_show_board_pool_zero_marks_all_seats_pool():
    lines = _show_board_lines(0)
    assert any("live pool = all AVAILABLE seats by priority" in ln for ln in lines), (
        lines
    )
    assert any("runs all AVAILABLE seats" in ln for ln in lines), lines
    assert not any("top 0 AVAILABLE" in ln for ln in lines), lines
    seat_lines = [ln for ln in lines if "[pool" in ln or "[reserve]" in ln]
    assert all("[pool" in ln for ln in seat_lines), seat_lines


def test_show_board_heavy_preset_displays_effort_values():
    """review-cli#fable-seat-reliability: Fable is excluded from the heavy preset
    entirely (a confirmed ~100% dispatch failure rate), so it no longer has a row
    here at all — Sol (the new #1) and Codex (still outside the xhigh top-4) stand
    in as the xhigh/max examples instead."""
    lines = _show_board_lines(4, preset="heavy")
    assert not any("claude:claude-fable-5" in ln for ln in lines), lines
    sol = next(ln for ln in lines if SOL_SEAT in ln)
    # Match the DISPLAY name ("Codex"), not the bare model string — "codex" is also a
    # substring of Sol's model id (`codex:gpt-5.6-sol`), which would false-match here.
    codex = next(ln for ln in lines if "Codex" in ln)
    assert "effort=xhigh" in sol, sol
    assert "effort=max" in codex, codex


def test_show_board_tags_by_seat_not_model_for_duplicate_models():
    """A board with the SAME model in two seats must tag each per-SEAT (by object
    identity / priority position), so a duplicate model in the reserve isn't mislabeled
    `pool`. Pool of 1 (all available) -> seat #1 pool, the rest reserve."""
    lines = _show_board_lines(1, board_models=["codex", "codex", "gemini"])
    seat_lines = [ln for ln in lines if "[pool" in ln or "[reserve]" in ln]
    assert len(seat_lines) == 3
    assert "[pool" in seat_lines[0]
    assert "[reserve]" in seat_lines[1], seat_lines[1]  # the duplicate codex, by seat
    assert "[reserve]" in seat_lines[2]


def test_show_board_uses_config_models_as_priority_roster():
    """`review --show-board` must reflect config `models:` as the active priority roster,
    not silently show the unrelated built-in board."""
    import contextlib
    import io

    from reviewlib import cli

    cfg = {"models": ["codex", "gemini", "commandcode:deepseek/deepseek-v4-pro"]}
    old_avail = backends.backend_available
    backends.backend_available = lambda _m: True
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = cli._show_board(cfg, 2)
    finally:
        backends.backend_available = old_avail
    assert rc == 0, rc
    out = buf.getvalue()
    assert "source: config.yaml (models:)" in out, out
    seat_lines = [ln for ln in out.splitlines() if "[pool" in ln or "[reserve]" in ln]
    assert [
        "codex" in seat_lines[0],
        "gemini" in seat_lines[1],
        "commandcode:deepseek/deepseek-v4-pro" in seat_lines[2],
    ] == [True, True, True], seat_lines
    assert "[pool" in seat_lines[0] and "[pool" in seat_lines[1], seat_lines
    assert "[reserve]" in seat_lines[2], seat_lines


def test_cli_list_defaults_reports_normalized_config_models(capfd=None):
    """(codex P3) `--list-defaults` must report the SAME normalized models the review
    path uses: comma-joined entries split, blanks dropped, aliases expanded."""
    import io
    import contextlib

    from reviewlib import cli

    old_load_config = cli.load_config
    cli.load_config = lambda: {"models": ["codex, fable5", "  ", ""]}
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["--list-defaults"])
        out = buf.getvalue().strip().splitlines()
    finally:
        cli.load_config = old_load_config
    assert rc == 0, rc
    # "codex, fable5" splits into two; blanks dropped; fable5 -> claude:claude-fable-5.
    assert out == ["codex", "claude:claude-fable-5"], out


def test_cli_list_defaults_reports_config_board_without_models():
    """A board-only config must report the same reviewer ids the diff path will run."""
    import contextlib
    import io

    from reviewlib import cli

    old_load_config = cli.load_config
    cli.load_config = lambda: {
        "board": [
            {"model": "gemini", "role": "contracts", "name": "Gemini"},
            {"model": "codex", "role": "consistency", "name": "Codex"},
        ]
    }
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["--list-defaults"])
        out = buf.getvalue().strip().splitlines()
    finally:
        cli.load_config = old_load_config
    assert rc == 0, rc
    assert out == ["gemini", "codex"], out


def test_cli_list_defaults_prefers_config_models_over_config_board():
    import contextlib
    import io

    from reviewlib import cli

    old_load_config = cli.load_config
    cli.load_config = lambda: {
        "models": ["codex", "gemini"],
        "board": [
            {"model": "claude:claude-opus-4-8", "role": "correctness", "name": "Opus"},
        ],
    }
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["diff", "--list-defaults"])
        out = buf.getvalue().strip().splitlines()
    finally:
        cli.load_config = old_load_config
    assert rc == 0, rc
    assert out == ["codex", "gemini"], out


def test_panel_list_defaults_ignore_config_board():
    import contextlib
    import io

    from reviewlib import cli
    from reviewlib.config import DEFAULT_MODELS, _expand_alias

    old_load_config = cli.load_config
    cli.load_config = lambda: {
        "board": [
            {"model": "gemini", "role": "contracts", "name": "Gemini"},
        ]
    }
    try:
        for argv in (
            ["just-ask", "q", "--list-defaults"],
            ["quorum", "q", "--list-defaults"],
        ):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.main(argv)
            assert rc == 0, (argv, rc)
            assert buf.getvalue().strip().splitlines() == [
                _expand_alias(m) for m in DEFAULT_MODELS
            ], argv
    finally:
        cli.load_config = old_load_config


def test_brainstorm_list_defaults_prefers_brainstorm_models_and_ignores_config_board():
    import contextlib
    import io

    from reviewlib import cli

    old_load_config = cli.load_config
    cli.load_config = lambda: {
        "brainstorm_models": ["gemini"],
        "models": ["codex"],
        "board": [
            {"model": "claude:claude-opus-4-8", "role": "correctness", "name": "Opus"},
        ],
    }
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["brainstorm", "topic", "--list-defaults"])
        out = buf.getvalue().strip().splitlines()
    finally:
        cli.load_config = old_load_config
    assert rc == 0, rc
    assert out == ["gemini"], out


def test_cli_list_defaults_all_malformed_board_errors_cleanly():
    import contextlib
    import io

    from reviewlib import cli

    old_load_config = cli.load_config
    cli.load_config = lambda: {"board": ["not-a-mapping", {"role": "tests"}]}
    try:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = cli.main(["--list-defaults"])
    finally:
        cli.load_config = old_load_config
    assert rc == 2, rc
    assert stdout.getvalue() == ""
    assert "config.yaml `board:` has 2 entries but none is usable" in stderr.getvalue()


def test_diff_model_help_reports_config_board_without_models():
    from reviewlib import cli

    old_load_config = cli.load_config
    cli.load_config = lambda: {
        "board": [
            {"model": "gemini", "role": "contracts", "name": "Gemini"},
            {"model": "codex", "role": "consistency", "name": "Codex"},
        ]
    }
    try:
        help_text = cli._build_mode_parser(cli.diff_mode()).format_help()
    finally:
        cli.load_config = old_load_config
    assert "your config.yaml board: gemini, codex" in help_text, help_text


def test_cli_list_defaults_empty_config_uses_code_defaults():
    """An effectively-empty config `models:` -> --list-defaults shows the default preset."""
    import io
    import contextlib

    from reviewlib import cli
    from reviewlib.config import DEFAULT_PRESET_BOARD

    old_load_config = cli.load_config
    cli.load_config = lambda: {"models": ["", "  "]}
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["--list-defaults"])
        out = buf.getvalue().strip().splitlines()
    finally:
        cli.load_config = old_load_config
    assert out == [r.model for r in DEFAULT_PRESET_BOARD], out


def test_cli_panel_list_defaults_do_not_use_diff_preset():
    import io
    import contextlib

    from reviewlib import cli
    from reviewlib.config import DEFAULT_MODELS, _expand_alias

    old_load_config = cli.load_config
    cli.load_config = lambda: {}
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["brainstorm", "topic", "--list-defaults"])
        out = buf.getvalue().strip().splitlines()
    finally:
        cli.load_config = old_load_config
    assert out == [_expand_alias(m) for m in DEFAULT_MODELS], out


def test_cli_rejects_preset_outside_diff_review():
    import contextlib
    import io

    from reviewlib import cli

    old_load_config = cli.load_config
    cli.load_config = lambda: {}
    try:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                cli.main(
                    ["brainstorm", "topic", "--preset", "heavy", "--list-defaults"]
                )
            except SystemExit as exc:
                code = exc.code
            else:
                code = 0
    finally:
        cli.load_config = old_load_config
    assert code == 2
    assert "unrecognized arguments: --preset" in stderr.getvalue()


def test_cli_list_defaults_explicit_models_win_over_preset():
    import io
    import contextlib

    from reviewlib import cli

    old_load_config = cli.load_config
    cli.load_config = lambda: {}
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(
                ["diff", "--preset", "heavy", "-m", "gemini,codex", "--list-defaults"]
            )
        out = buf.getvalue().strip().splitlines()
    finally:
        cli.load_config = old_load_config
    assert out == ["gemini", "codex"], out


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
