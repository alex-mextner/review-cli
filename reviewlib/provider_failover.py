"""Provider-failover: keep a review seat alive by trying the SAME model across MULTIPLE
providers, switching MID-REVIEW when a provider fails.

WHY: a logical model (opus, glm-5.2, …) can be served by several providers/transports
(claude-direct vs opencode; z.ai vs opencode vs commandcode). When the provider a seat is
running on fails DURING the review call — DNS/timeout/5xx/connection-reset/exit-nonzero —
the seat must NOT abort and the board must NOT degrade: the CLI transparently switches that
SAME model to its NEXT provider and the review CONTINUES, completing on the working
provider. Only when ALL of a model's providers are exhausted does the seat fail (then the
board's reserve-replace takes over).

THE FULL RELIABILITY CASCADE (per seat), outermost last:
  transient error  -> retry the SAME provider with exponential backoff (reviewlib.retry) ->
  still failing     -> FAIL OVER to the model's NEXT provider (THIS module) ->
  all providers out -> the seat fails; the board reserve-replaces it (reviewlib.panel).
An `unpaid_providers` provider is dropped from the chain UP FRONT (never dispatched), which
is distinct from failover: paywalled = skipped pre-flight; flaky = failed-over at call time.

LAST-WORKING CACHE: the provider that last produced a usable verdict for a model is
remembered (a small JSON under ~/.cache/review-cli/) and tried FIRST next time; a failure
rotates it out. So a chronically-flaky first provider stops costing a failover every run.

REACHED FROM: `reviewlib.panel.run_panel_with_retry` builds a seat's provider chain here
and loops it, running each provider through `run_seat_with_retry`. Pure/injectable: the
availability + unpaid predicates and the cache path are parameters, so tests exercise the
chain and the cache with no backend and no real cache file. See tests/test_provider_failover.py.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Callable

from .config import _expand_alias

# Logical model -> ordered provider spellings, strongest/most-reliable FIRST. The head is
# the DEFAULT provider a seat uses; the tail are failover targets tried in order. Only
# models with a genuine multi-provider option are listed; anything else is single-provider
# (its chain is just itself). Extendable via a config `provider_chains:` map (see
# `provider_chain(..., overrides=...)`).
#
# opus leads with claude-DIRECT (NOT the opencode catch-all): a bare opus routed through
# opencode is the exact agentic path observed to infra-fail; claude-direct is reliable and
# opencode is only the fallback. glm-5.2 leads with z.ai direct, then opencode's zai
# provider, then the commandcode gateway.
DEFAULT_PROVIDER_CHAINS: dict[str, tuple[str, ...]] = {
    "opus": ("claude:claude-opus-4-8", "oc:anthropic/claude-opus-4-8"),
    "glm-5.2": ("zai:glm-5.2", "oc:zai/glm-5.2", "commandcode:zai-org/GLM-5.2"),
}

# Reverse index: every concrete provider spelling -> its logical key, so ANY seat model
# (whichever provider it was written as) resolves to the same chain.
_CONCRETE_TO_LOGICAL: dict[str, str] = {
    concrete.lower(): logical
    for logical, chain in DEFAULT_PROVIDER_CHAINS.items()
    for concrete in chain
}

_CACHE_ENV = "REVIEW_PROVIDER_CACHE"  # tests point this at a throwaway file
_CACHE_LOCK = threading.Lock()

# Per-process config override for the chains (logical-model -> ordered provider list). Set
# once by the CLI after load_config (mirrors backends.configure_unpaid_providers); the panel
# builds chains without a config handle, so it reads this. Empty = use DEFAULT_PROVIDER_CHAINS.
_CONFIG_OVERRIDES: dict[str, list[str]] = {}


def configure_provider_chains(raw: object) -> None:
    """Set per-process provider-chain overrides from a config `provider_chains:` mapping
    (`{logical: [provider, ...]}`). Non-mapping / malformed entries are ignored."""
    global _CONFIG_OVERRIDES
    parsed: dict[str, list[str]] = {}
    if isinstance(raw, dict):
        for key, providers in raw.items():
            if isinstance(key, str) and isinstance(providers, (list, tuple)):
                clean = [p for p in providers if isinstance(p, str) and p.strip()]
                if clean:
                    parsed[key.strip().lower()] = clean
    _CONFIG_OVERRIDES = parsed


def logical_key(model: str) -> str:
    """The logical-model key for `model` (alias-expanded), or the model itself when it has
    no multi-provider chain."""
    m = _expand_alias(model).lower()
    return _CONCRETE_TO_LOGICAL.get(m, m)


def _base_chain(model: str, overrides: dict[str, list[str]] | None) -> list[str]:
    """The ordered provider list for `model`, with the REQUESTED spelling as the HEAD.

    The requested concrete spelling always leads — an explicit `-m commandcode:zai-org/GLM-5.2`
    (or a config board seat pinned to a specific provider) must be tried on THAT provider
    first, never silently reordered onto the default chain's head (which could hit a
    different billing/auth path). The remaining providers of the same logical model follow,
    in their default/override order, as failover targets. De-dup is case-insensitive so a
    spelling that differs only in case can't be dispatched twice."""
    key = logical_key(model)
    expanded = _expand_alias(model)
    if overrides and key in overrides:
        rest = [c for c in overrides[key] if isinstance(c, str) and c.strip()]
    else:
        rest = list(DEFAULT_PROVIDER_CHAINS.get(key, ()))
    ordered = [expanded, *rest]  # requested spelling leads; alternates follow
    seen: set[str] = set()
    chain: list[str] = []
    for c in ordered:
        low = c.lower()
        if low not in seen:
            seen.add(low)
            chain.append(c)
    return chain


def _default_head(key: str, overrides: dict[str, list[str]] | None) -> str | None:
    """The FIRST provider of `key`'s chain per config overrides / DEFAULT_PROVIDER_CHAINS,
    or None for a single-provider model with no chain at all."""
    if overrides and key in overrides and overrides[key]:
        return overrides[key][0]
    chain = DEFAULT_PROVIDER_CHAINS.get(key)
    return chain[0] if chain else None


def _requested_is_default_selection(
    model: str, expanded: str, overrides: dict[str, list[str]] | None
) -> bool:
    """True iff `model` is a bare logical alias (`opus`, `glm52`) or the default provider
    spelled out directly — as opposed to an EXPLICIT pin of a specific alternate concrete
    provider (e.g. `-m commandcode:zai-org/GLM-5.2`). Only this default-selection case is
    eligible for the last-working-cache reorder (codex P2 on review-cli#157: 'Keep explicit
    provider ahead of cache') — reordering an explicit pin behind a cached alternate would
    silently break its routing/billing/auth guarantee.

    A BARE ALIAS is always eligible, full stop: `model != expanded` proves `_expand_alias`
    actually rewrote it, which only happens for a known alias — regardless of where a
    config `provider_chains:` override puts that alias's own head. Comparing the alias's
    canonical expansion against the (possibly REORDERED) override head would otherwise make
    a legitimate bare-alias request lose the reorder whenever an override's head differs
    from the alias's canonical target (e.g. an override putting `oc:zai/glm-5.2` first while
    `glm52` still canonically expands to `zai:glm-5.2`) — a real gap a review flagged.
    Only when NO alias fired (`model == expanded`, a concrete spelling typed directly) do we
    fall back to comparing against the resolved default head, to still allow the head
    spelled out literally (`-m zai:glm-5.2`) while excluding a genuine alternate pin."""
    # Both sides normalized identically (strip + lower) before comparing — `_expand_alias`
    # itself does not strip, so comparing an unstripped `expanded` against a stripped
    # `model` could disagree on whitespace-padded input (review of #157, finding 3).
    if model.strip().lower() != expanded.strip().lower():
        return True
    default_head = _default_head(logical_key(model), overrides)
    return default_head is None or expanded.lower() == default_head.lower()


def is_default_provider_selection(
    model: str, overrides: dict[str, list[str]] | None = None
) -> bool:
    """Public wrapper over `_requested_is_default_selection`: True iff `model` is a bare
    logical alias / the default provider spelled out directly, False for an EXPLICIT pin of
    a specific alternate concrete provider.

    A CALLER writing the last-working cache (`remember_working_provider`/
    `forget_working_provider`) must gate the write the SAME way `provider_chain` gates the
    cache-reorder READ — otherwise a one-off explicit pin (`-m commandcode:zai-org/GLM-5.2`)
    silently trains or clears the cache entry a later BARE alias request (`-m glm52`) reads,
    rebiasing (or wiping) default routing based on a request that was never the default
    selection to begin with (review of #157: 'cache write isn't gated the way the read
    is'). Both `reviewlib.panel`'s board path and `reviewlib.modes.review`'s flat path use
    this before their `remember_working_provider`/`forget_working_provider` calls."""
    return _requested_is_default_selection(
        model,
        _expand_alias(model),
        overrides if overrides is not None else _CONFIG_OVERRIDES,
    )


def provider_chain(
    model: str,
    *,
    available: Callable[[str], bool],
    unpaid: Callable[[str], bool],
    overrides: dict[str, list[str]] | None = None,
    cache_path: Path | None = None,
) -> list[str]:
    """The ordered list of concrete providers to try for `model`, MID-REVIEW failover order.

    Drops `unpaid` providers up front (never dispatched), then drops cheaply-unavailable
    providers (no key/CLI — calling them just wastes a call), then — ONLY for a bare logical
    alias / default-head request, never for an explicit concrete provider pin — moves the
    cached last-working provider to the FRONT. Never returns empty: if every alternate is
    filtered out, the requested spelling is kept so the seat fails with its real reason (not
    silently)."""
    resolved_overrides = overrides if overrides is not None else _CONFIG_OVERRIDES
    chain = _base_chain(model, resolved_overrides)
    expanded = _expand_alias(model)
    # Unpaid providers are NEVER dispatched. If EVERY provider is unpaid, fall back to the
    # requested spelling ALONE (fails once with its unpaid reason) — do NOT resurrect the
    # whole disabled chain and iterate misleading failover attempts over dead providers.
    paid = [c for c in chain if not unpaid(c)] or [expanded]
    reachable = [c for c in paid if available(c)] or paid
    if _requested_is_default_selection(model, expanded, resolved_overrides):
        cached = _cached_provider(model, cache_path)
        if cached and cached in reachable:
            reachable = [cached, *[c for c in reachable if c != cached]]
    # de-dupe preserving order (a cached==head or overrides could double an entry)
    seen: set[str] = set()
    ordered = [c for c in reachable if not (c in seen or seen.add(c))]
    return ordered


def any_provider_available(
    model: str,
    *,
    available: Callable[[str], bool],
    unpaid: Callable[[str], bool],
    overrides: dict[str, list[str]] | None = None,
) -> bool:
    """A plain liveness probe for `model` that agrees with what `provider_chain` will
    actually dispatch: True iff `model` itself OR any later provider in its failover chain
    is reachable AND not paywalled.

    WHY: a caller that just needs "is this seat usable right now" (the pool guard) must not
    call raw `available(model)` on the requested spelling alone — that marks a seat DOWN
    when its head provider lacks a key/CLI even though a live failover alternate exists
    (codex P2 on review-cli#157: no ZAI_API_KEY but authenticated oc:zai). Unlike
    `provider_chain`, this never falls back to "keep everything so the seat can fail loud" —
    it is a pure query, not a dispatch plan.

    `_base_chain` NEVER returns empty (it always starts with the expanded requested
    spelling, even for a single-provider model with no chain entry), so `any(...)` over it
    can't spuriously read False just because a custom `-m` model has no multi-provider
    chain — see test_any_provider_available_for_a_single_provider_no_chain_model."""
    chain = _base_chain(
        model, overrides if overrides is not None else _CONFIG_OVERRIDES
    )
    return any(not unpaid(c) and available(c) for c in chain)


# --- last-working-provider cache --------------------------------------------------------
def _cache_file(cache_path: Path | None) -> Path:
    if cache_path is not None:
        return cache_path
    env = os.environ.get(_CACHE_ENV)
    if env:
        return Path(env)
    return Path.home() / ".cache" / "review-cli" / "last-provider.json"


def _load_cache(cache_path: Path | None) -> dict[str, str]:
    path = _cache_file(cache_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _cached_provider(model: str, cache_path: Path | None) -> str | None:
    return _load_cache(cache_path).get(logical_key(model))


def _write_cache(cache: dict[str, str], cache_path: Path | None) -> None:
    """Atomically persist the cache (temp file + rename); best-effort — a read-only cache
    dir must never break a review."""
    path = _cache_file(cache_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".last-provider.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(cache, fh)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except OSError:
        pass  # cache is an optimization; never fail a review on a cache write


def remember_working_provider(
    model: str, provider: str, *, cache_path: Path | None = None
) -> None:
    """Record `provider` as the last-working provider for `model`'s logical key (tried first
    next run). No-op when it already IS the cached value, to avoid needless writes."""
    key = logical_key(model)
    with _CACHE_LOCK:
        cache = _load_cache(cache_path)
        if cache.get(key) == provider:
            return
        cache[key] = provider
        _write_cache(cache, cache_path)


def forget_working_provider(model: str, *, cache_path: Path | None = None) -> None:
    """Drop the cached last-working provider for `model` (rotate out on failure)."""
    key = logical_key(model)
    with _CACHE_LOCK:
        cache = _load_cache(cache_path)
        if key in cache:
            del cache[key]
            _write_cache(cache, cache_path)
