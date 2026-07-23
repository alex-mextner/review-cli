#!/usr/bin/env python3
"""Unit tests for reviewlib.provider_failover — the per-model provider chain + last-working
cache that powers MID-REVIEW provider switchover.

All OFFLINE: availability + unpaid are injected closures and the cache path is a throwaway
temp file — no backend, no network, no real ~/.cache write. The mid-review switchover
itself (a provider failing on the actual review call and the model completing via the next
provider) is proven in tests/test_inseat_retry.py against the panel seat loop; here we prove
the chain ORDER + filtering + cache semantics that loop depends on.

Plain-script harness (mirrors tests/test_pool_guard.py): each test_* is run by __main__.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib.provider_failover import (  # noqa: E402
    DEFAULT_PROVIDER_CHAINS,
    forget_working_provider,
    logical_key,
    provider_chain,
    remember_working_provider,
)

_ALL = lambda _m: True  # noqa: E731 - every provider reachable
_NONE_UNPAID = lambda _m: False  # noqa: E731 - nothing paywalled


def _tmp_cache() -> Path:
    return Path(tempfile.mkdtemp()) / "last-provider.json"


# === opus defaults to claude-direct (NOT opencode) ===============================
def test_opus_chain_leads_with_claude_direct():
    """opus's FIRST provider must be claude-direct; opencode is only the failover tail."""
    chain = provider_chain(
        "opus", available=_ALL, unpaid=_NONE_UNPAID, cache_path=_tmp_cache()
    )
    assert chain[0] == "claude:claude-opus-4-8", chain
    assert any(c.startswith("oc:") for c in chain[1:]), chain  # opencode is a FALLBACK


def test_opus_logical_key_collapses_both_spellings():
    assert logical_key("opus") == logical_key("claude:claude-opus-4-8")
    assert logical_key("oc:anthropic/claude-opus-4-8") == logical_key(
        "claude:claude-opus-4-8"
    )


# === multi-provider glm chain ====================================================
def test_glm_chain_tries_zai_then_opencode_then_commandcode():
    chain = provider_chain(
        "glm52", available=_ALL, unpaid=_NONE_UNPAID, cache_path=_tmp_cache()
    )
    assert chain[0] == "zai:glm-5.2", chain
    assert "oc:zai/glm-5.2" in chain and "commandcode:zai-org/GLM-5.2" in chain, chain


# === single-provider models = a chain of themselves ==============================
def test_single_provider_model_is_its_own_chain():
    for m in ("codex", "claude:claude-fable-5"):
        chain = provider_chain(
            m, available=_ALL, unpaid=_NONE_UNPAID, cache_path=_tmp_cache()
        )
        from reviewlib.config import _expand_alias

        assert chain == [_expand_alias(m)], chain


# === unpaid providers are dropped UP FRONT (never dispatched) =====================
def test_unpaid_provider_is_dropped_from_chain():
    """A paywalled provider (commandcode) is removed pre-flight — distinct from failover."""
    unpaid = lambda m: m.startswith("commandcode:")  # noqa: E731
    chain = provider_chain(
        "glm52", available=_ALL, unpaid=unpaid, cache_path=_tmp_cache()
    )
    assert not any(c.startswith("commandcode:") for c in chain), chain
    assert "zai:glm-5.2" in chain and "oc:zai/glm-5.2" in chain, chain


# === cheaply-unavailable providers are dropped ===================================
def test_unavailable_provider_is_dropped_but_never_empties():
    """A provider with no key/CLI is skipped (calling it wastes a call); but if EVERY
    alternate is unavailable the requested spelling is kept so the seat fails loud."""
    only_oc = lambda m: m.startswith("oc:")  # noqa: E731 - only opencode reachable
    chain = provider_chain(
        "glm52", available=only_oc, unpaid=_NONE_UNPAID, cache_path=_tmp_cache()
    )
    assert chain == ["oc:zai/glm-5.2"], chain
    # nothing reachable at all -> still non-empty (the requested spelling), not silent
    none = lambda _m: False  # noqa: E731
    fallback = provider_chain(
        "glm52", available=none, unpaid=_NONE_UNPAID, cache_path=_tmp_cache()
    )
    assert fallback and "zai:glm-5.2" in fallback, fallback


# === last-working cache: tried first, rotates on failure =========================
def test_cached_provider_is_tried_first():
    cache = _tmp_cache()
    remember_working_provider("glm52", "commandcode:zai-org/GLM-5.2", cache_path=cache)
    chain = provider_chain(
        "glm52", available=_ALL, unpaid=_NONE_UNPAID, cache_path=cache
    )
    assert chain[0] == "commandcode:zai-org/GLM-5.2", chain
    # the rest of the chain is still present, just after the cached head
    assert set(chain) == {
        "zai:glm-5.2",
        "oc:zai/glm-5.2",
        "commandcode:zai-org/GLM-5.2",
    }, chain


def test_cache_is_keyed_by_logical_model_across_spellings():
    """Remembering a winner under one spelling is seen when the seat is requested under
    another spelling of the SAME logical model."""
    cache = _tmp_cache()
    remember_working_provider(
        "claude:claude-opus-4-8", "oc:anthropic/claude-opus-4-8", cache_path=cache
    )
    chain = provider_chain(
        "opus", available=_ALL, unpaid=_NONE_UNPAID, cache_path=cache
    )
    assert chain[0] == "oc:anthropic/claude-opus-4-8", chain


def test_forget_rotates_cached_provider_out():
    cache = _tmp_cache()
    remember_working_provider("glm52", "oc:zai/glm-5.2", cache_path=cache)
    forget_working_provider("glm52", cache_path=cache)
    chain = provider_chain(
        "glm52", available=_ALL, unpaid=_NONE_UNPAID, cache_path=cache
    )
    assert chain[0] == "zai:glm-5.2", chain  # back to the default head


def test_cached_provider_that_is_now_unpaid_is_not_forced_first():
    """A cached provider that has since been paywalled must not be resurrected to the front."""
    cache = _tmp_cache()
    remember_working_provider("glm52", "commandcode:zai-org/GLM-5.2", cache_path=cache)
    unpaid = lambda m: m.startswith("commandcode:")  # noqa: E731
    chain = provider_chain("glm52", available=_ALL, unpaid=unpaid, cache_path=cache)
    assert "commandcode:zai-org/GLM-5.2" not in chain, chain
    assert chain[0] == "zai:glm-5.2", chain


# === config overrides ============================================================
def test_config_overrides_replace_the_default_failover_order():
    """A config override replaces the DEFAULT failover alternates. The requested spelling
    still leads (explicit routing), then the override's alternates follow — NOT the built-in
    default alternate. Here `-m glm52` (=zai:glm-5.2) with an override that lists only the
    commandcode alternate must NOT fall over to the default oc:zai seat."""
    override = {"glm-5.2": ["commandcode:zai-org/GLM-5.2"]}
    chain = provider_chain(
        "glm52",
        available=_ALL,
        unpaid=_NONE_UNPAID,
        overrides=override,
        cache_path=_tmp_cache(),
    )
    assert chain == ["zai:glm-5.2", "commandcode:zai-org/GLM-5.2"], chain
    assert "oc:zai/glm-5.2" not in chain, chain  # default alternate replaced


def test_explicit_concrete_provider_leads_the_chain():
    """An explicitly requested CONCRETE provider spelling is the HEAD (honor routing), never
    silently reordered onto the default chain's head (which could hit a different billing
    path). Requesting the commandcode GLM seat tries commandcode FIRST."""
    chain = provider_chain(
        "commandcode:zai-org/GLM-5.2",
        available=_ALL,
        unpaid=_NONE_UNPAID,
        cache_path=_tmp_cache(),
    )
    assert chain[0] == "commandcode:zai-org/GLM-5.2", chain
    # its logical siblings are still available as failover, after the requested head
    assert "zai:glm-5.2" in chain and "oc:zai/glm-5.2" in chain, chain


def test_cached_provider_does_not_override_an_explicit_concrete_pin():
    """A cached last-working provider for a logical model must NOT jump ahead of an
    EXPLICITLY pinned concrete provider (codex P2 on review-cli#157: 'Keep explicit
    provider ahead of cache'). After a `glm52` run caches `oc:zai/glm-5.2`, a later
    `-m commandcode:zai-org/GLM-5.2` must still dispatch commandcode FIRST — reordering it
    behind the cached opencode seat would silently break the explicit routing/billing/auth
    guarantee `_base_chain` gives an explicit pin. Cache reordering only applies to a
    logical alias / the default chain head, never to an explicit alternate pin."""
    cache = _tmp_cache()
    remember_working_provider("glm52", "oc:zai/glm-5.2", cache_path=cache)
    chain = provider_chain(
        "commandcode:zai-org/GLM-5.2",
        available=_ALL,
        unpaid=_NONE_UNPAID,
        cache_path=cache,
    )
    assert chain[0] == "commandcode:zai-org/GLM-5.2", chain
    assert "oc:zai/glm-5.2" in chain, chain


def test_cached_provider_still_reorders_a_logical_alias_request():
    """The cache DOES still apply to a plain logical-alias / default-head request (the
    normal case this cache exists for) — only an explicit alternate pin is exempt."""
    cache = _tmp_cache()
    remember_working_provider("glm52", "commandcode:zai-org/GLM-5.2", cache_path=cache)
    chain = provider_chain(
        "glm52", available=_ALL, unpaid=_NONE_UNPAID, cache_path=cache
    )
    assert chain[0] == "commandcode:zai-org/GLM-5.2", chain


def test_cached_provider_still_reorders_the_default_head_spelled_out_directly():
    """`-m zai:glm-5.2` (the default head spelled out directly, not via the `glm52` alias)
    is INDISTINGUISHABLE from the bare alias once alias-expanded, so it is ALSO eligible for
    the cache reorder — the finding's 'default head' carve-out (as opposed to an explicit
    ALTERNATE pin like commandcode) applies here too. Documents the intended boundary."""
    cache = _tmp_cache()
    remember_working_provider("glm52", "oc:zai/glm-5.2", cache_path=cache)
    chain = provider_chain(
        "zai:glm-5.2", available=_ALL, unpaid=_NONE_UNPAID, cache_path=cache
    )
    assert chain[0] == "oc:zai/glm-5.2", chain


# === any_provider_available: a plain liveness probe across the failover chain =====
def test_any_provider_available_true_when_a_later_chain_provider_is_live():
    """A caller (the pool guard) must not mark a model 'down' just because its DEFAULT
    provider lacks a key/CLI when a live failover alternate exists (codex P2 on
    review-cli#157: no ZAI_API_KEY but authenticated oc:zai)."""
    from reviewlib.provider_failover import any_provider_available

    only_oc = lambda m: m.startswith("oc:")  # noqa: E731
    assert any_provider_available("glm52", available=only_oc, unpaid=_NONE_UNPAID)


def test_any_provider_available_false_when_every_provider_is_down_or_unpaid():
    from reviewlib.provider_failover import any_provider_available

    assert not any_provider_available(
        "glm52", available=lambda _m: False, unpaid=_NONE_UNPAID
    )
    only_commandcode = lambda m: m.startswith("commandcode:")  # noqa: E731
    unpaid_commandcode = lambda m: m.startswith("commandcode:")  # noqa: E731
    assert not any_provider_available(
        "glm52", available=only_commandcode, unpaid=unpaid_commandcode
    )


def test_any_provider_available_for_a_single_provider_no_chain_model():
    """A model with NO multi-provider chain (`_base_chain` degenerates to itself alone,
    never empty) must still be correctly reported live/down — guards against the guard
    (`_evaluate_pool_or_bail`'s `_guard_available`) silently mismarking a plain custom `-m`
    spelling as down just because it has no failover chain entry."""
    from reviewlib.provider_failover import any_provider_available

    assert any_provider_available("codex", available=_ALL, unpaid=_NONE_UNPAID)
    assert not any_provider_available(
        "codex", available=lambda _m: False, unpaid=_NONE_UNPAID
    )


def test_all_providers_unpaid_falls_back_to_requested_alone():
    """When EVERY provider of a model is unpaid, the chain is the requested spelling ALONE
    (fails once with its unpaid reason) — it must NOT resurrect and iterate the whole
    disabled chain (misleading failover attempts over dead providers)."""
    all_unpaid = lambda _m: True  # noqa: E731
    chain = provider_chain(
        "glm52", available=_ALL, unpaid=all_unpaid, cache_path=_tmp_cache()
    )
    assert chain == ["zai:glm-5.2"], chain  # the requested spelling only, no iteration


def test_alias_targets_agree_with_chain_heads():
    """A multi-provider model's chain HEAD must equal its MODEL_ALIASES target — otherwise a
    bumped alias would leave a stale chain head as a live failover target (finding: three
    copies of 'opus -> claude'). Guards that drift."""
    from reviewlib.config import MODEL_ALIASES, _expand_alias

    # opus alias -> chain head
    assert _expand_alias("opus") == DEFAULT_PROVIDER_CHAINS["opus"][0], (
        MODEL_ALIASES.get("opus")
    )
    # glm52 alias -> chain head
    assert _expand_alias("glm52") == DEFAULT_PROVIDER_CHAINS["glm-5.2"][0]


def test_configure_provider_chains_from_config_is_honored():
    """A config `provider_chains:` mapping set via configure_provider_chains overrides the
    default failover alternates for the panel (which builds chains without a config handle)."""
    from reviewlib.provider_failover import configure_provider_chains

    try:
        configure_provider_chains({"glm-5.2": ["commandcode:zai-org/GLM-5.2"]})
        chain = provider_chain(
            "glm52", available=_ALL, unpaid=_NONE_UNPAID, cache_path=_tmp_cache()
        )
        assert chain == ["zai:glm-5.2", "commandcode:zai-org/GLM-5.2"], chain
    finally:
        configure_provider_chains(None)  # reset process state
    # after reset, the default alternate returns
    chain = provider_chain(
        "glm52", available=_ALL, unpaid=_NONE_UNPAID, cache_path=_tmp_cache()
    )
    assert "oc:zai/glm-5.2" in chain, chain


def test_default_chains_are_wellformed():
    """Every default chain has >= 1 provider and no blanks (guards a typo)."""
    for key, chain in DEFAULT_PROVIDER_CHAINS.items():
        assert chain and all(isinstance(c, str) and c.strip() for c in chain), (
            key,
            chain,
        )


# === INTEGRATION: mid-review switchover through the real panel seat loop ==========
# These drive reviewlib.panel.run_board_with_failover with a fake backend keyed on the
# per-provider model id, proving the seat SWITCHES provider mid-review and the review
# CONTINUES on the working one (not the pure chain, but the actual call loop the CTO asked
# to see). Availability + unpaid are stubbed; the cache + retry budget are env-controlled.
import json  # noqa: E402
import os  # noqa: E402

import reviewlib.backends as _backends  # noqa: E402
import reviewlib.panel as _panel  # noqa: E402
from reviewlib.config import BoardReviewer  # noqa: E402


class _ProviderFakeBackends:
    """Stub panel.resolve_backend keyed on the CONCRETE provider model id, so a chain's
    provider A can fail while provider B succeeds. Also forces every provider reachable +
    paid, points the cache at a throwaway file, and disables in-seat retry (so a failure
    goes STRAIGHT to provider-failover, no backoff delay)."""

    def __init__(self, behaviour: dict[str, tuple[int, str]]):
        self.behaviour = behaviour
        self.dispatched: list[str] = []
        self.cache = str(Path(tempfile.mkdtemp()) / "last-provider.json")

    def __enter__(self):
        self._old_resolve = _panel.resolve_backend
        self._old_avail = _panel.backend_available
        # The panel builds the chain with `unpaid=runtime_provider_marked_unpaid` (it imports
        # that name lazily inside run_panel_with_retry), so THAT is the predicate to stub —
        # patching the older `provider_marked_unpaid` alias is a no-op and lets leaked global
        # unpaid state (e.g. a commandcode mark from another suite) silently drop an alternate
        # provider from the chain. Force every provider PAID here so the switchover is exercised.
        self._old_unpaid = _backends.runtime_provider_marked_unpaid
        self._env = {
            k: os.environ.get(k)
            for k in ("REVIEW_PROVIDER_CACHE", "REVIEW_RETRY_COUNT")
        }

        def _resolve(_model):
            def _backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
                self.dispatched.append(model)
                rc, out = self.behaviour.get(model, (0, f"ok review from {model}"))
                return _backends.ReviewResult(
                    model=model, command="fake", returncode=rc, stdout=out, stderr=""
                )

            return _backend

        _panel.resolve_backend = _resolve
        _panel.backend_available = lambda _m: True
        _backends.runtime_provider_marked_unpaid = lambda _m: False
        os.environ["REVIEW_PROVIDER_CACHE"] = self.cache
        os.environ["REVIEW_RETRY_COUNT"] = "0"
        return self

    def __exit__(self, *exc):
        _panel.resolve_backend = self._old_resolve
        _panel.backend_available = self._old_avail
        _backends.runtime_provider_marked_unpaid = self._old_unpaid
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


def test_seat_switches_provider_midreview_and_completes():
    """Provider A (z.ai) FAILS on the actual review call; the SAME model completes via
    provider B (opencode) — the seat produces a verdict, the board does NOT degrade, and B
    is cached as last-working. This is the in-flight switchover, not pre-flight selection."""
    seat = BoardReviewer("zai:glm-5.2", "quality", "GLM")
    behaviour = {
        "zai:glm-5.2": (
            1,
            "[Errno 54] Connection reset by peer",
        ),  # provider A infra-fails
        "oc:zai/glm-5.2": (
            0,
            "Real review via opencode. Finding: guard the null case.",
        ),  # B works
        "commandcode:zai-org/GLM-5.2": (1, "should never be reached"),
    }
    with _ProviderFakeBackends(behaviour) as fb:
        outcome = _panel.run_board_with_failover(
            [seat], [], "Review this.", "+x", REPO_ROOT, 5
        )
        assert not outcome.degraded, (
            "board must NOT degrade — the review continued via provider B"
        )
        assert len(outcome.usable) == 1, outcome
        assert "opencode" in outcome.usable[0].stdout, outcome.usable[0].stdout
        # A was tried and FAILED, then B was tried and SUCCEEDED (the switchover); C never ran
        assert fb.dispatched[:2] == ["zai:glm-5.2", "oc:zai/glm-5.2"], fb.dispatched
        assert "commandcode:zai-org/GLM-5.2" not in fb.dispatched, fb.dispatched
        # last-working provider cached = B, keyed by the logical model
        assert json.loads(Path(fb.cache).read_text()).get("glm-5.2") == "oc:zai/glm-5.2"


def test_all_providers_fail_then_board_reserve_replaces_the_seat():
    """When EVERY provider of a seat's model fails, provider-failover is exhausted and the
    BOARD reserve-replace takes over (the two layers compose)."""
    seat = BoardReviewer("zai:glm-5.2", "quality", "GLM")
    reserve = [BoardReviewer("codex", "consistency", "Codex")]
    behaviour = {
        "zai:glm-5.2": (1, "connection refused"),
        "oc:zai/glm-5.2": (1, "503 service unavailable"),
        "commandcode:zai-org/GLM-5.2": (1, "nodename nor servname"),
        # codex (the reserve) is not in the chain -> default (0, ok) -> backfills.
    }
    with _ProviderFakeBackends(behaviour) as fb:
        outcome = _panel.run_board_with_failover(
            [seat], reserve, "Review this.", "+x", REPO_ROOT, 5
        )
        # all three glm providers were exhausted...
        for pm in ("zai:glm-5.2", "oc:zai/glm-5.2", "commandcode:zai-org/GLM-5.2"):
            assert pm in fb.dispatched, (pm, fb.dispatched)
        # ...then the reserve (codex) backfilled and produced the usable verdict
        assert len(outcome.usable) == 1, outcome
        assert "codex" in outcome.usable_models, outcome.usable_models
        assert not outcome.degraded, outcome


# === INTEGRATION: the FLAT `-m` path (no board) must ALSO get provider failover ====
# codex P2 on review-cli#157 ("Apply provider failover to flat -m reviews"): with no
# configured board/models, `review diff -m glm52` went through `mode_review`'s flat
# executor, which only retried the SAME provider (run_seat_with_retry) and never consulted
# `provider_chain` — so a transient/non-usable head-provider result failed the seat instead
# of switching to the next provider. These drive the REAL flat `mode_review(..., board=None)`
# path with a fake backend keyed on the per-provider model id, mirroring
# `_ProviderFakeBackends` above but patching `reviewlib.modes.review`'s OWN imported names
# (it imports `resolve_backend`/`backend_available` directly, not via `reviewlib.panel`).
import reviewlib.modes.review as _review_mod  # noqa: E402


class _FlatProviderFakeBackends:
    """Like `_ProviderFakeBackends`, but stubs the flat review-mode module directly."""

    def __init__(self, behaviour: dict[str, tuple[int, str]]):
        self.behaviour = behaviour
        self.dispatched: list[str] = []
        self.cache = str(Path(tempfile.mkdtemp()) / "last-provider.json")

    def __enter__(self):
        self._old_resolve = _review_mod.resolve_backend
        self._old_avail = _review_mod.backend_available
        # `_flat_seat_with_provider_failover` calls provider_chain(unpaid=...) using the name
        # IMPORTED INTO `modes.review`'s own namespace (`from ..backends import ...
        # runtime_provider_marked_unpaid`), NOT a live lookup through `reviewlib.backends` —
        # so patching `_backends.runtime_provider_marked_unpaid` alone is a no-op for the
        # flat path (codex review of #157: this exact leak class already bit
        # tests/test_run_stats.py's `_with_backend_stub`, which patches both namespaces for
        # the same reason). Patch `_review_mod`'s copy here.
        self._old_unpaid = _review_mod.runtime_provider_marked_unpaid
        self._env = {
            k: os.environ.get(k)
            for k in ("REVIEW_PROVIDER_CACHE", "REVIEW_RETRY_COUNT")
        }

        def _resolve(model):
            def _backend(m, prompt, diff, cwd, timeout, round_no=0, effort=None):
                self.dispatched.append(m)
                behaviour = self.behaviour.get(m, (0, f"ok review from {m}"))
                # A behaviour value of "RAISE" simulates a backend call that RAISES
                # (network layer / subprocess spawn error) rather than returning a failed
                # ReviewResult — proves the failover loop is exception-safe per attempt.
                if behaviour == "RAISE":
                    raise ConnectionError(f"simulated raise dispatching {m}")
                rc, out = behaviour
                return _backends.ReviewResult(
                    model=m, command="fake", returncode=rc, stdout=out, stderr=""
                )

            return _backend

        _review_mod.resolve_backend = _resolve
        _review_mod.backend_available = lambda _m: True
        _review_mod.runtime_provider_marked_unpaid = lambda _m: False
        os.environ["REVIEW_PROVIDER_CACHE"] = self.cache
        os.environ["REVIEW_RETRY_COUNT"] = "0"
        return self

    def __exit__(self, *exc):
        _review_mod.resolve_backend = self._old_resolve
        _review_mod.backend_available = self._old_avail
        _review_mod.runtime_provider_marked_unpaid = self._old_unpaid
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


def test_flat_m_seat_switches_provider_midreview_and_completes():
    """`review diff -m glm52` (no board): provider A (z.ai) FAILS, the SAME model completes
    via provider B (opencode) through the FLAT panel path — not just the board path."""
    behaviour = {
        "zai:glm-5.2": (1, "[Errno 54] Connection reset by peer"),
        "oc:zai/glm-5.2": (
            0,
            "Real review via opencode. Finding: guard the null case.",
        ),
        "commandcode:zai-org/GLM-5.2": (1, "should never be reached"),
    }
    with _FlatProviderFakeBackends(behaviour) as fb:
        rc = _review_mod.mode_review(
            ["zai:glm-5.2"], "Review this.", "+x", REPO_ROOT, 5, False, board=None
        )
        assert rc == 0, "flat seat should have completed via the failover provider"
        assert fb.dispatched[:2] == ["zai:glm-5.2", "oc:zai/glm-5.2"], fb.dispatched
        assert "commandcode:zai-org/GLM-5.2" not in fb.dispatched, fb.dispatched
        assert json.loads(Path(fb.cache).read_text()).get("glm-5.2") == "oc:zai/glm-5.2"


def test_flat_m_all_providers_fail_reports_the_final_failure():
    """When EVERY provider of a flat seat's model fails, the flat path (no board, no
    reserve) reports the seat's failure rather than silently succeeding."""
    behaviour = {
        "zai:glm-5.2": (1, "connection refused"),
        "oc:zai/glm-5.2": (1, "503 service unavailable"),
        "commandcode:zai-org/GLM-5.2": (1, "nodename nor servname"),
    }
    with _FlatProviderFakeBackends(behaviour) as fb:
        rc = _review_mod.mode_review(
            ["zai:glm-5.2"], "Review this.", "+x", REPO_ROOT, 5, False, board=None
        )
        assert rc == 1, (
            "flat seat should report failure once every provider is exhausted"
        )
        for pm in ("zai:glm-5.2", "oc:zai/glm-5.2", "commandcode:zai-org/GLM-5.2"):
            assert pm in fb.dispatched, (pm, fb.dispatched)


def test_flat_m_failover_survives_a_provider_that_raises_instead_of_returning():
    """codex review of #157 (finding: 'exceptions from a provider skip failover'): the board
    path is exception-safe per attempt because `run_panel`'s `_run_job` catches ANY exception
    and normalizes it to a failed `ReviewResult` before the provider-failover loop ever sees
    it. The flat path's `dispatch` has NO such wrapper, so if a provider's backend call
    RAISES (e.g. a network-layer error surfacing as an exception, not just a nonzero
    ReviewResult), the seat must still fail over to the NEXT provider — not abort the whole
    seat and skip the remaining chain."""
    behaviour = {
        "zai:glm-5.2": "RAISE",  # provider A raises instead of returning a failed result
        "oc:zai/glm-5.2": (
            0,
            "Real review via opencode. Finding: guard the null case.",
        ),
        "commandcode:zai-org/GLM-5.2": (1, "should never be reached"),
    }
    with _FlatProviderFakeBackends(behaviour) as fb:
        rc = _review_mod.mode_review(
            ["zai:glm-5.2"], "Review this.", "+x", REPO_ROOT, 5, False, board=None
        )
        assert rc == 0, "flat seat should have failed over past the raising provider"
        assert fb.dispatched[:2] == ["zai:glm-5.2", "oc:zai/glm-5.2"], fb.dispatched
        assert "commandcode:zai-org/GLM-5.2" not in fb.dispatched, fb.dispatched


def test_flat_m_explicit_pin_success_does_not_train_the_default_cache():
    """Opus review of #157 ('cache write isn't gated the way the read is'): an EXPLICIT
    alternate pin (`-m commandcode:zai-org/GLM-5.2`) succeeding must NOT write the shared
    logical-key cache entry — a later BARE alias request (`-m glm52`) would read that entry
    and get silently rebiased toward the one-off pin's provider."""
    behaviour = {"commandcode:zai-org/GLM-5.2": (0, "ok via commandcode")}
    with _FlatProviderFakeBackends(behaviour) as fb:
        rc = _review_mod.mode_review(
            ["commandcode:zai-org/GLM-5.2"],
            "Review this.",
            "+x",
            REPO_ROOT,
            5,
            False,
            board=None,
        )
        assert rc == 0, "the explicit pin should have succeeded"
    cache_path = Path(fb.cache)
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    assert "glm-5.2" not in cache, cache


def test_flat_m_explicit_pin_failure_does_not_clear_the_default_cache():
    """The forget side of the same gate: an explicit pin's TOTAL failure must not wipe a
    cache entry a prior DEFAULT (bare-alias) run legitimately learned."""
    fb = _FlatProviderFakeBackends(
        {
            "commandcode:zai-org/GLM-5.2": (1, "fails"),
            "zai:glm-5.2": (1, "also fails"),
            "oc:zai/glm-5.2": (1, "also fails"),
        }
    )
    remember_working_provider("glm52", "oc:zai/glm-5.2", cache_path=Path(fb.cache))
    with fb:
        rc = _review_mod.mode_review(
            ["commandcode:zai-org/GLM-5.2"],
            "Review this.",
            "+x",
            REPO_ROOT,
            5,
            False,
            board=None,
        )
        assert rc == 1, "every provider failed, the seat should report failure"
    cache = json.loads(Path(fb.cache).read_text())
    assert cache.get("glm-5.2") == "oc:zai/glm-5.2", cache


def test_board_explicit_pin_success_does_not_train_the_default_cache():
    """Same write-side gate, board path (`reviewlib.panel`'s `_seat`): parity with the flat
    path above — the board path was ALSO writing the cache unconditionally before this fix,
    even though it isn't the codepath the original codex finding named."""
    seat = BoardReviewer("commandcode:zai-org/GLM-5.2", "quality", "GLM-cc")
    behaviour = {"commandcode:zai-org/GLM-5.2": (0, "ok via commandcode")}
    with _ProviderFakeBackends(behaviour) as fb:
        outcome = _panel.run_board_with_failover(
            [seat], [], "Review this.", "+x", REPO_ROOT, 5
        )
        assert not outcome.degraded, outcome
    cache_path = Path(fb.cache)
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    assert "glm-5.2" not in cache, cache


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
