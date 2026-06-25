#!/usr/bin/env python3
"""Unit tests for capability-aware seat resolution from the shared models.yaml manifest.

The consumer-side residual of rig-cli#8: review-cli's board can RESOLVE a seat by capability
or role from `agent-tools/lib/contracts/models.yaml`, ADDITIVELY (literal `model:` + `-m`
untouched) and with GRACEFUL DEGRADATION when the manifest isn't reachable.

All offline: each test writes a self-contained fixture manifest to a temp file and points
`$REVIEW_MODELS_MANIFEST` at it (or clears it for the absent-manifest path). No network, no
real agent-tools checkout, no model call. Runs as a plain script (the repo convention) and is
also pytest-collectable.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import config as _config  # noqa: E402
from reviewlib import manifest as _manifest  # noqa: E402
from reviewlib.config import BoardConfigError, load_board  # noqa: E402

# A self-contained manifest mirroring the real models.yaml shape (a subset, enough to exercise
# every resolution path): per-provider concrete ids with capability tags + a roles: map.
FIXTURE_MANIFEST = """
version: 1
models:
  - id: claude-fable-5
    provider: anthropic
    capabilities: [vision, reasoning, code]
  - id: claude-opus-4-8
    provider: anthropic
    capabilities: [vision, reasoning, code]
  - id: gpt-5.5
    provider: openai
    capabilities: [vision, reasoning, code]
  - id: gemini-2.5-flash
    provider: gemini
    capabilities: [vision, reasoning, code]
  - id: moonshotai/Kimi-K2.7-Code
    provider: commandcode
    capabilities: [code, reasoning]
  - id: kimi-k2p6-turbo
    provider: commandcode
    capabilities: [vision, code, reasoning]
  - id: glm-5.2
    provider: zai
    capabilities: [reasoning, code]
roles:
  architect: claude-fable-5
  reasoning: claude-opus-4-8
  code: moonshotai/Kimi-K2.7-Code
  vision: kimi-k2p6-turbo
  fast: gemini-2.5-flash
"""


# Temp dirs created by the fixtures, cleaned up after each test so a full suite run doesn't leak.
_TMP_DIRS: list[str] = []


def _mkdtemp() -> str:
    d = tempfile.mkdtemp()
    _TMP_DIRS.append(d)
    return d


def _write_manifest(body: str = FIXTURE_MANIFEST) -> str:
    path = Path(_mkdtemp()) / "models.yaml"
    path.write_text(body, encoding="utf-8")
    return str(path)


def _set_manifest(body: str = FIXTURE_MANIFEST) -> None:
    """Point review-cli at a fixture manifest AND drop the parse cache (it is keyed on the
    path string, so a new temp file is a new key — but clear defensively so tests don't bleed)."""
    os.environ[_manifest.MANIFEST_ENV] = _write_manifest(body)
    os.environ.pop("AGENT_TOOLS_DIR", None)
    _manifest._load_manifest_cached.cache_clear()


def _clear_manifest() -> None:
    """Simulate a host with NO manifest reachable: clear the env override AND the conventional
    checkout fallbacks (point HOME at an empty temp dir so no real models.yaml is found)."""
    os.environ.pop(_manifest.MANIFEST_ENV, None)
    os.environ.pop("AGENT_TOOLS_DIR", None)
    os.environ["HOME"] = _mkdtemp()
    _manifest._load_manifest_cached.cache_clear()


# === manifest load ===========================================================
def test_load_manifest_reads_fixture():
    _set_manifest()
    data = _manifest.load_manifest()
    assert isinstance(data, dict) and data.get("version") == 1, data
    ids = [m["id"] for m in data["models"]]
    assert "claude-opus-4-8" in ids and "kimi-k2p6-turbo" in ids, ids


def test_load_manifest_absent_returns_empty():
    _clear_manifest()
    assert _manifest.load_manifest() == {}, "absent manifest must be {}, never a crash"
    assert _manifest.manifest_path() is None


def test_load_manifest_unparseable_returns_empty():
    """A broken YAML manifest degrades to {} (no crash) — a corrupt manifest must not wedge a
    review."""
    os.environ[_manifest.MANIFEST_ENV] = _write_manifest("{ this: is: not: valid: yaml ][")
    os.environ.pop("AGENT_TOOLS_DIR", None)
    _manifest._load_manifest_cached.cache_clear()
    assert _manifest.load_manifest() == {}


def test_load_manifest_non_dict_toplevel_returns_empty():
    """A YAML manifest whose top level is a list/scalar (valid YAML, wrong shape) -> {}."""
    _set_manifest("- just\n- a\n- list\n")
    assert _manifest.load_manifest() == {}


def test_env_override_beats_conventional_paths():
    """`$REVIEW_MODELS_MANIFEST` takes priority over the conventional $HOME checkout paths."""
    # Put a real manifest at a conventional $HOME path AND a distinct one at the env override;
    # the env override must win.
    home = Path(_mkdtemp())
    conv = home / "xp" / "agent-tools" / "lib" / "contracts"
    conv.mkdir(parents=True)
    (conv / "models.yaml").write_text(
        "version: 1\nmodels:\n  - id: conv-model\n    provider: anthropic\n"
        "    capabilities: [code]\nroles:\n  code: conv-model\n", encoding="utf-8")
    os.environ["HOME"] = str(home)
    os.environ[_manifest.MANIFEST_ENV] = _write_manifest()  # the FIXTURE manifest
    os.environ.pop("AGENT_TOOLS_DIR", None)
    _manifest._load_manifest_cached.cache_clear()
    # The env-override fixture wins: its `code` role is Kimi-Code, not `conv-model`.
    assert _manifest.resolve_role("code") == "commandcode:moonshotai/Kimi-K2.7-Code"


# === role resolution =========================================================
def test_resolve_role_maps_provider_to_seat():
    _set_manifest()
    # anthropic -> claude:<id>; the reasoning role points at opus.
    assert _manifest.resolve_role("reasoning") == "claude:claude-opus-4-8"
    assert _manifest.resolve_role("architect") == "claude:claude-fable-5"
    # commandcode -> commandcode:<id>; code role points at Kimi-Code.
    assert _manifest.resolve_role("code") == "commandcode:moonshotai/Kimi-K2.7-Code"
    # commandcode vision seat.
    assert _manifest.resolve_role("vision") == "commandcode:kimi-k2p6-turbo"
    # gemini is the bare seat name (no id tail).
    assert _manifest.resolve_role("fast") == "gemini"


def test_resolve_role_unknown_returns_none():
    _set_manifest()
    assert _manifest.resolve_role("nonexistent-role") is None


def test_resolve_role_absent_manifest_returns_none():
    _clear_manifest()
    assert _manifest.resolve_role("reasoning") is None


def test_resolve_role_target_not_in_models_returns_none():
    """A `roles:` entry pointing at an id NOT present in `models:` (e.g. a typo, or it points at
    an alias) resolves to None — no crash."""
    _set_manifest("""
version: 1
models:
  - id: claude-opus-4-8
    provider: anthropic
    capabilities: [reasoning]
roles:
  reasoning: nonexistent-id
""")
    assert _manifest.resolve_role("reasoning") is None


def test_resolve_role_on_unmapped_provider_returns_none():
    """A role whose target model is on a provider with no review-cli route (e.g. fireworks) ->
    None (the role path, not just `_seat_for`)."""
    _set_manifest("""
version: 1
models:
  - id: accounts/fireworks/models/fable-5
    provider: fireworks
    capabilities: [vision]
roles:
  vision: accounts/fireworks/models/fable-5
""")
    assert _manifest.resolve_role("vision") is None


def test_role_names_lists_manifest_roles():
    _set_manifest()
    assert _manifest.role_names() == frozenset(
        {"architect", "reasoning", "code", "vision", "fast"})
    _clear_manifest()
    assert _manifest.role_names() == frozenset()


def test_role_names_drops_non_string_keys():
    """A manifest with a NON-STRING `roles:` key (an externally-edited source-of-truth could
    carry `null`/a number) must not crash a consumer doing `.lower()` on the names — role_names
    drops non-string keys so the 'broken manifest degrades, never crashes' invariant holds."""
    _set_manifest("""
version: 1
models:
  - id: claude-opus-4-8
    provider: anthropic
    capabilities: [reasoning]
roles:
  ? null
  : claude-opus-4-8
  reasoning: claude-opus-4-8
""")
    names = _manifest.role_names()
    assert all(isinstance(n, str) for n in names), names
    assert "reasoning" in names
    # A board entry whose `capability:` is a PURE role name (`architect` — NOT a capability tag)
    # forces `_capability_fail_reason` -> `role_names()` -> the `.lower()` scan over the keys,
    # which would crash on the non-string key without the filter. It must skip gracefully (and a
    # sibling literal seat keeps the board alive), not raise.
    board = load_board({"board": [
        {"capability": "architect", "role": "correctness"},  # pure role, drives the scan
        {"model": "claude:claude-opus-4-8", "role": "correctness"},
    ]})
    assert [r.model for r in board] == ["claude:claude-opus-4-8"]


# === capability filtering ====================================================
def test_models_with_capability_vision():
    _set_manifest()
    seats = _manifest.models_with_capability("vision")
    # Manifest (priority) order, mapped to seats; the two anthropic, openai->codex,
    # gemini, and the commandcode turbo — NOT the code-only Kimi or the zai GLM.
    assert seats == [
        "claude:claude-fable-5",
        "claude:claude-opus-4-8",
        "codex",  # openai -> the agentic codex route
        "gemini",
        "commandcode:kimi-k2p6-turbo",
    ], seats


def test_models_with_capability_code_includes_codeonly_kimi_and_glm():
    _set_manifest()
    seats = _manifest.models_with_capability("code")
    assert "commandcode:moonshotai/Kimi-K2.7-Code" in seats
    assert "zai:glm-5.2" in seats


def test_models_with_capability_unknown_is_empty():
    _set_manifest()
    assert _manifest.models_with_capability("telepathy") == []


def test_resolve_capability_returns_strongest():
    _set_manifest()
    # First (priority) vision seat is Fable.
    assert _manifest.resolve_capability("vision") == "claude:claude-fable-5"


def test_capability_absent_manifest_is_empty():
    _clear_manifest()
    assert _manifest.models_with_capability("vision") == []
    assert _manifest.resolve_capability("vision") is None


def test_models_with_capability_dedupes_provider_level_seats():
    """Two openai entries with the same capability both map to the bare `codex` seat — the
    output must contain `codex` ONCE, not twice (provider-level seats can't duplicate)."""
    body = """
version: 1
models:
  - id: gpt-5.5
    provider: openai
    capabilities: [reasoning]
  - id: gpt-6
    provider: openai
    capabilities: [reasoning]
roles: {}
"""
    _set_manifest(body)
    assert _manifest.models_with_capability("reasoning") == ["codex"]


# === provider -> seat mapping ================================================
def test_seat_for_provider_mapping():
    assert _manifest._seat_for("claude-opus-4-8", "anthropic") == "claude:claude-opus-4-8"
    assert _manifest._seat_for("gpt-5.5", "openai") == "codex"
    assert _manifest._seat_for("gemini-2.5-flash", "gemini") == "gemini"
    assert _manifest._seat_for("x/y", "commandcode") == "commandcode:x/y"
    assert _manifest._seat_for("glm-5.2", "zai") == "zai:glm-5.2"
    # Unknown provider -> no review-cli route.
    assert _manifest._seat_for("whatever", "unknown-provider") is None


def test_seat_for_fireworks_is_unmapped():
    """`fireworks` is a known-dead route (review-cli#25) and is DELIBERATELY unmapped, so a
    capability scan never mints a fireworks seat."""
    assert _manifest._seat_for("accounts/fireworks/x", "fireworks") is None
    body = """
version: 1
models:
  - id: accounts/fireworks/models/fable-5
    provider: fireworks
    capabilities: [vision]
roles: {}
"""
    _set_manifest(body)
    assert _manifest.models_with_capability("vision") == []


# === board wiring (the actual consumer of the manifest) ======================
def test_board_capability_entry_resolves_role():
    """A config `board:` entry with `capability: role:reasoning` resolves to the manifest's
    concrete seat — additive to literal `model:` entries."""
    _set_manifest()
    board = load_board({"board": [
        {"capability": "role:reasoning", "role": "correctness", "name": "R"},
        {"model": "gemini", "role": "contracts"},
    ]})
    assert [(r.model, r.role, r.display) for r in board] == [
        ("claude:claude-opus-4-8", "correctness", "R"),
        ("gemini", "contracts", "gemini"),  # display derived from the bare model id
    ], [(r.model, r.role, r.display) for r in board]


def test_board_capability_entry_resolves_bare_capability():
    _set_manifest()
    board = load_board({"board": [{"capability": "vision", "role": "quality"}]})
    # Strongest vision seat is Fable; display derived from the model id.
    assert board[0].model == "claude:claude-fable-5"
    assert board[0].role == "quality"


def test_board_literal_model_still_works_unchanged():
    """Backward-compat: a literal `model:` entry is untouched by capability resolution and
    works with NO manifest present."""
    _clear_manifest()
    board = load_board({"board": [{"model": "claude:claude-opus-4-8", "role": "correctness"}]})
    assert board[0].model == "claude:claude-opus-4-8"
    assert board[0].role == "correctness"


def test_board_capability_entry_skipped_when_manifest_absent():
    """A `capability:` entry with NO manifest is skipped with a warning; a sibling literal
    entry keeps the board alive (graceful degradation, not a crash)."""
    _clear_manifest()
    board = load_board({"board": [
        {"capability": "role:reasoning", "role": "correctness"},
        {"model": "gemini", "role": "contracts"},
    ]})
    assert [r.model for r in board] == ["gemini"], [r.model for r in board]


def test_board_literal_model_wins_over_capability():
    """`_resolve_entry_model` promises `model:` wins when BOTH keys are present — pin it."""
    _set_manifest()
    board = load_board({"board": [
        {"model": "gemini", "capability": "role:reasoning", "role": "contracts"},
    ]})
    assert board[0].model == "gemini", board[0].model  # the literal model, NOT the resolved opus


def test_board_capability_seat_with_unknown_role_uses_generic_prompt():
    """A capability-resolved seat with a role NOT in REVIEW_ROLES is kept (warned), using the
    generic prompt (role_lens == "") — the board degrades, never crashes."""
    _set_manifest()
    board = load_board({"board": [{"capability": "role:reasoning", "role": "made-up-role"}]})
    assert board[0].model == "claude:claude-opus-4-8"
    assert board[0].role == "made-up-role"
    assert board[0].role_lens == ""  # unknown role -> generic prompt


def test_board_keeps_duplicate_seats_for_per_seat_tagging():
    """Duplicate models across seats are INTENTIONALLY kept (show-board tags per seat, not per
    model — see test_show_board_tags_by_seat_not_model_for_duplicate_models). A `capability:`
    resolving to a model a literal seat already names yields TWO seats, not one."""
    _set_manifest()
    board = load_board({"board": [
        {"capability": "vision", "role": "architect", "name": "First"},  # -> claude:claude-fable-5
        {"model": "claude:claude-fable-5", "role": "quality", "name": "Dup"},  # same seat, kept
    ]})
    assert [(r.model, r.display) for r in board] == [
        ("claude:claude-fable-5", "First"),
        ("claude:claude-fable-5", "Dup"),
    ], [(r.model, r.display) for r in board]


def test_capability_fail_reason_hints_role_for_role_name():
    """A bare `capability:` whose value is a ROLE name (the overlapping namespace footgun) gets
    a 'write role:<x>' hint, not the generic 'unknown' message."""
    _set_manifest()
    # `architect` is a role name but NOT a capability tag -> resolves to nothing.
    reason = _config._capability_fail_reason("architect")
    assert "role:architect" in reason, reason
    # A genuinely-unknown value gets the generic reason.
    generic = _config._capability_fail_reason("telepathy")
    assert "could not be resolved" in generic and "role:" not in generic, generic
    # Case-insensitive: `Architect` (wrong case, not a tag) hints the CANONICAL `role:architect`,
    # which actually resolves — the hint never dead-ends on a case mismatch.
    cased = _config._capability_fail_reason("Architect")
    assert "role:architect" in cased, cased


def test_bare_capability_diverges_from_same_named_role():
    """The footgun: `capability: vision` (tag scan -> strongest vision model = Fable) resolves
    to a DIFFERENT seat than `capability: role:vision` (the curated vision role = Kimi turbo).
    This pins the divergence so it can't change silently."""
    _set_manifest()
    board_tag = load_board({"board": [{"capability": "vision", "role": "quality"}]})
    board_role = load_board({"board": [{"capability": "role:vision", "role": "quality"}]})
    assert board_tag[0].model == "claude:claude-fable-5"      # strongest VISION-tagged
    assert board_role[0].model == "commandcode:kimi-k2p6-turbo"  # the curated vision ROLE seat
    assert board_tag[0].model != board_role[0].model


def test_no_resolvable_home_does_not_crash():
    """A host with NO resolvable home (`Path.home()` raises — real in containers under an
    arbitrary uid) must degrade to 'no candidates', not crash. With the env override unset, the
    manifest is simply absent; with it set, the override still resolves DESPITE the broken home."""
    os.environ.pop(_manifest.MANIFEST_ENV, None)
    os.environ.pop("AGENT_TOOLS_DIR", None)
    _manifest._load_manifest_cached.cache_clear()
    orig_home = _manifest.Path.home

    def _boom():
        raise RuntimeError("Could not determine home directory")

    _manifest.Path.home = staticmethod(_boom)
    try:
        # No manifest, broken home -> absent, not a crash.
        assert _manifest.manifest_path() is None
        assert _manifest.load_manifest() == {}
        # A board with a literal seat still loads (capability path degrades, doesn't crash).
        board = load_board({"board": [{"model": "gemini", "role": "contracts"}]})
        assert board[0].model == "gemini"
        # The env override resolves even with a broken home (it's a candidate before the home block).
        os.environ[_manifest.MANIFEST_ENV] = _write_manifest()
        _manifest._load_manifest_cached.cache_clear()
        assert _manifest.resolve_role("reasoning") == "claude:claude-opus-4-8"
    finally:
        _manifest.Path.home = orig_home


def test_agent_tools_dir_path_resolves_manifest():
    """The `$AGENT_TOOLS_DIR/lib/contracts/models.yaml` candidate path resolves the manifest."""
    root = Path(_mkdtemp())
    contracts = root / "lib" / "contracts"
    contracts.mkdir(parents=True)
    (contracts / "models.yaml").write_text(FIXTURE_MANIFEST, encoding="utf-8")
    os.environ.pop(_manifest.MANIFEST_ENV, None)
    os.environ["AGENT_TOOLS_DIR"] = str(root)
    os.environ["HOME"] = _mkdtemp()  # no conventional manifest under HOME
    _manifest._load_manifest_cached.cache_clear()
    assert _manifest.resolve_role("reasoning") == "claude:claude-opus-4-8"


def test_board_all_capability_entries_unresolvable_raises():
    """An all-`capability:` board with no manifest resolves to nothing -> BoardConfigError
    (cost-safe: never silently substitute the paid DEFAULT_BOARD)."""
    _clear_manifest()
    try:
        load_board({"board": [{"capability": "role:reasoning", "role": "correctness"}]})
    except BoardConfigError:
        return
    raise AssertionError("expected BoardConfigError when every entry is an unresolvable capability")


# Each test mutates env (HOME / $REVIEW_MODELS_MANIFEST / $AGENT_TOOLS_DIR) and the parse
# cache; restore them around every test so this file can't bleed manifest state into the rest
# of the suite when collected by pytest in one process (a fixture does the same under pytest).
_ENV_KEYS = ("HOME", _manifest.MANIFEST_ENV, "AGENT_TOOLS_DIR")


def _snapshot_env() -> dict:
    return {k: os.environ.get(k) for k in _ENV_KEYS}


def _restore_env(snap: dict) -> None:
    for k, v in snap.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    _manifest._load_manifest_cached.cache_clear()
    import shutil

    while _TMP_DIRS:
        shutil.rmtree(_TMP_DIRS.pop(), ignore_errors=True)


try:
    import pytest

    @pytest.fixture(autouse=True)
    def _isolate_manifest_env():
        snap = _snapshot_env()
        try:
            yield
        finally:
            _restore_env(snap)
except ImportError:  # pragma: no cover — standalone runner has no pytest
    pass


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            snap = _snapshot_env()
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
            finally:
                _restore_env(snap)
    sys.exit(1 if failures else 0)
