"""Capability-aware seat resolution from the shared model manifest (rig-cli#8 consumer side).

Why this exists
---------------
The ecosystem keeps ONE source of truth for "what model is each provider currently pinned
to, and what can it do" in `agent-tools/lib/contracts/models.yaml`: per-model `capabilities`
tags (`vision` / `reasoning` / `code`), a `roles:` map (symbolic lens -> concrete id), and
`aliases:`. The daily-noon currency checker bumps that manifest; rig provisions the cron.
The remaining consumer-side residual of rig-cli#8 is: let review-cli's board RESOLVE a seat
by CAPABILITY or ROLE from that manifest, instead of every model id being hardcoded.

This module is the resolver. It is deliberately ADDITIVE and OPT-IN:
  * the hardcoded `DEFAULT_BOARD` and `-m <model>` paths are untouched — they never call here;
  * a caller that WANTS a capability-resolved seat (e.g. "give me a vision-capable model for
    the image-review need") asks this module, which reads the manifest's tags;
  * when the manifest can't be located (review-cli ships standalone with only a `pyyaml`
    dep; the manifest lives in the separate agent-tools repo, which may not be on a given
    host), every function degrades to "no resolution" — `None` / an empty list — and the
    board falls back to its hardcoded seats. NEVER a crash, NEVER a hard dependency on
    agent-tools being checked out.

Provider mapping
----------------
The manifest's `provider` tokens are the BOARD's provider names, not review-cli's seat
prefixes: `anthropic` -> `claude:`, `openai` -> `codex` (the agentic codex route GPT-5.5
runs on), `gemini` -> `gemini`, `commandcode` -> `commandcode:`, `zai` -> `zai:`. A concrete
manifest id is turned into a runnable review-cli seat string via `_seat_for()` so the
resolved value drops straight into the same backend dispatch the board already uses.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

# The closed capability vocabulary the manifest uses (mirrors models.schema.json). Resolution
# accepts only these — an unknown capability resolves to nothing rather than guessing.
KNOWN_CAPABILITIES = frozenset({"vision", "reasoning", "code"})

# Env override: point review-cli straight at a manifest file (highest priority). Used by tests
# and by a host that keeps the manifest somewhere non-standard.
MANIFEST_ENV = "REVIEW_MODELS_MANIFEST"

# Manifest provider token (models.yaml `provider:`) -> the review-cli SEAT prefix builder.
# A `None` value means "the provider IS the bare seat name" (gemini -> `gemini`, no id tail).
# `openai` maps to the agentic `codex` route (GPT-5.5 runs there), matching DEFAULT_BOARD's
# priority-5 Codex seat — NOT the diff-only `commandcode:gpt-5.5` HTTP route.
_PROVIDER_SEAT = {
    "anthropic": "claude:{id}",
    "openai": "codex",
    "gemini": "gemini",
    "commandcode": "commandcode:{id}",
    "zai": "zai:{id}",
    # `fireworks` is DELIBERATELY ABSENT: it is a known-dead route (review-cli#25 denylist —
    # the suspended `glide` account), so a capability scan must never even MINT a fireworks
    # seat. An unmapped provider resolves to `None` (skipped), which is exactly right here —
    # safer than minting an `oc:fireworks/...` seat and relying on a downstream guard to drop it.
}

# NOTE: `openai` and `gemini` resolve to the PROVIDER-LEVEL bare seat (`codex` / `gemini`), not
# a model-pinned id — those two review-cli routes pick their concrete model themselves (the
# agentic codex CLI runs GPT-5.5; the gemini REST backend its configured model), so the seat is
# the route name, not `route:<id>`. Resolution is therefore provider-level for those two and
# model-level for the rest; `models_with_capability` dedupes so two openai entries can't yield a
# duplicate `codex` seat.


def _candidate_manifest_paths() -> list[Path]:
    """Ordered locations to look for `models.yaml`, strongest first.

    1. `$REVIEW_MODELS_MANIFEST` (explicit override — a file path);
    2. `$AGENT_TOOLS_DIR/lib/contracts/models.yaml` (if the ecosystem points us at its root);
    3. a small set of conventional checkout locations relative to $HOME.

    Only EXISTING files are returned, so a caller can take the first hit. The list is built
    fresh each call (it reads the env), but `load_manifest()` caches the PARSED result."""
    candidates: list[Path] = []
    env_file = os.environ.get(MANIFEST_ENV)
    if env_file:
        candidates.append(Path(env_file).expanduser())
    agent_tools = os.environ.get("AGENT_TOOLS_DIR")
    if agent_tools:
        candidates.append(
            Path(agent_tools).expanduser() / "lib" / "contracts" / "models.yaml"
        )
    # `Path.home()` raises (RuntimeError, or KeyError via pwd.getpwuid) when no home is
    # resolvable — a real case in containers run under an arbitrary uid with no $HOME. Guard it
    # so the conventional-path probe degrades to "no candidates" instead of crashing a review;
    # the env override above still works (it's appended before this block). NEVER let the home
    # probe break the "a broken/absent manifest degrades, never crashes" invariant.
    try:
        home = Path.home()
    except Exception:
        home = None
    if home is not None:
        for rel in (
            "xp/agent-tools/lib/contracts/models.yaml",
            "work/agent-tools/lib/contracts/models.yaml",
            "agent-tools/lib/contracts/models.yaml",
            ".config/agent-tools/lib/contracts/models.yaml",
        ):
            candidates.append(home / rel)
    # `is_file()` can itself raise on a pathological path (e.g. an OSError); treat any failure as
    # "not a usable candidate" so path probing never propagates an error.
    usable: list[Path] = []
    for p in candidates:
        try:
            if p.is_file():
                usable.append(p)
        except OSError:
            continue
    return usable


def manifest_path() -> Path | None:
    """The first existing manifest file, or `None` when none is reachable on this host."""
    found = _candidate_manifest_paths()
    return found[0] if found else None


@lru_cache(maxsize=1)
def _load_manifest_cached(path_str: str) -> dict:
    """Parse one manifest file. Keyed on the resolved path string so the cache is keyed on
    WHICH file, not on the env at first call. Returns {} on any read/parse failure — a broken
    manifest must degrade to 'no resolution', never crash a review.

    INVARIANT: the returned dict is the SHARED cached parse — callers (and everything reached
    via `load_manifest`) treat it as READ-ONLY. Every consumer in this module only reads it; do
    not mutate the result, or you corrupt the cache for the whole process.

    The cache is keyed on the path string only, NOT on the file's mtime — review is a short-
    lived CLI, so re-reading a file mid-process never matters. If this module is ever reused in
    a long-lived daemon, a daily-noon manifest bump would NOT be picked up without clearing the
    cache; re-key on `(path, mtime)` then."""
    try:
        import yaml

        data = yaml.safe_load(Path(path_str).read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_manifest() -> dict:
    """The parsed manifest as a dict, or `{}` when no manifest is reachable / it's unparseable.

    Resolves the path fresh (so a test that sets `$REVIEW_MODELS_MANIFEST` is honoured), then
    returns the cached parse for that path. An absent manifest is a normal state on a host
    without agent-tools checked out — callers treat `{}` as 'fall back to the hardcoded
    board'."""
    path = manifest_path()
    if path is None:
        return {}
    return _load_manifest_cached(str(path))


def _seat_for(model_id: str, provider: str) -> str | None:
    """Turn a concrete manifest `(id, provider)` into a runnable review-cli seat string, or
    `None` when the provider has no known review-cli route. The board's own availability /
    dead-provider guards still apply downstream; this only builds the id."""
    template = _PROVIDER_SEAT.get(provider)
    if template is None:
        return None
    return template.format(id=model_id) if "{id}" in template else template


def _models(manifest: dict) -> list[dict]:
    raw = manifest.get("models")
    return [m for m in raw if isinstance(m, dict)] if isinstance(raw, list) else []


def _entry_by_id(manifest: dict, model_id: str) -> dict | None:
    for entry in _models(manifest):
        if entry.get("id") == model_id:
            return entry
    return None


def role_names(manifest: dict | None = None) -> frozenset[str]:
    """The set of symbolic role names the manifest's `roles:` map defines (architect/reasoning/
    vision/code/fast), or an empty set when no manifest is reachable. Used to give a 'did you
    mean role:<x>?' hint when a bare `capability:` value collides with a role name."""
    manifest = load_manifest() if manifest is None else manifest
    roles = manifest.get("roles")
    if not isinstance(roles, dict):
        return frozenset()
    # Filter to STRING keys: an externally-edited manifest could carry a non-string YAML key
    # (e.g. `null`/a number/a date), and a consumer doing `{r.lower() for r in role_names()}`
    # would crash on it — violating the module's "a broken manifest degrades, never crashes"
    # invariant. Drop non-string keys here so every consumer gets a clean set of names.
    return frozenset(k for k in roles if isinstance(k, str))


def resolve_role(role: str, manifest: dict | None = None) -> str | None:
    """Resolve a symbolic ROLE (`architect`/`reasoning`/`vision`/`code`/`fast`) to a runnable
    review-cli seat string via the manifest's `roles:` map, or `None` when the manifest is
    absent / the role is unknown / its target provider has no review-cli route.

    The manifest guarantees a role points at a concrete `id` in `models:`; we look that id up
    to read its `provider`, then build the seat. Backward-compatible: a caller that doesn't
    ask for a role never touches this, and a missing manifest yields `None` so the board uses
    its hardcoded seats."""
    manifest = load_manifest() if manifest is None else manifest
    roles = manifest.get("roles")
    if not isinstance(roles, dict):
        return None
    target_id = roles.get(role)
    if not isinstance(target_id, str):
        return None
    entry = _entry_by_id(manifest, target_id)
    if entry is None:
        return None
    provider = entry.get("provider")
    if not isinstance(provider, str):
        return None
    return _seat_for(target_id, provider)


def models_with_capability(capability: str, manifest: dict | None = None) -> list[str]:
    """Every runnable review-cli seat whose manifest entry carries `capability`, in manifest
    (priority) order. An unknown capability, an absent manifest, or no matching entry yields
    `[]`. Entries on a provider with no review-cli route are skipped (not an error)."""
    if capability not in KNOWN_CAPABILITIES:
        return []
    manifest = load_manifest() if manifest is None else manifest
    seats: list[str] = []
    seen: set[str] = set()
    for entry in _models(manifest):
        caps = entry.get("capabilities")
        if not isinstance(caps, list) or capability not in caps:
            continue
        model_id = entry.get("id")
        provider = entry.get("provider")
        if not isinstance(model_id, str) or not isinstance(provider, str):
            continue
        seat = _seat_for(model_id, provider)
        # Dedupe while preserving priority order: two provider-level entries (e.g. two openai
        # models) both map to the bare `codex` seat, which must appear once, not twice.
        if seat is not None and seat not in seen:
            seen.add(seat)
            seats.append(seat)
    return seats


def resolve_capability(capability: str, manifest: dict | None = None) -> str | None:
    """The single strongest (first in manifest priority order) runnable seat carrying
    `capability`, or `None` when none is reachable. The convenience one-shot over
    `models_with_capability` for "give me A vision/reasoning/code-capable seat"."""
    seats = models_with_capability(capability, manifest)
    return seats[0] if seats else None
