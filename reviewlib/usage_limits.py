"""Best-effort cross-provider usage-limit awareness for board/panel composition.

review-cli itself has no visibility into a provider's account-level rate/usage
window at DISPATCH time -- it only ever learns a seat is exhausted AFTER a call
fails (see reviewlib.retry / reviewlib.seat_cooldown, both reactive). tg-cli's
tg-ctl daemon, however, already tracks Claude's and Codex's own self-reported
usage percentage (parsed from each harness's status-line/hook payloads) and
persists the latest sample per (agent, limitName) to
``~/.config/tg-cli/tg-ctl.<chat_id>.usage-latest.json`` for its own 50/70/90%
warning flow. This module reads that SAME file as a best-effort, READ-ONLY signal
for board/panel composition -- "how close is this model's underlying account to
its rate limit right now" -- so a near-exhausted seat can be excluded BEFORE
dispatch instead of only failing reactively mid-run.

Coverage is PARTIAL by construction: only the agent families tg-ctl itself
tracks (currently ``claude`` and ``codex``) have real percentage data. Every
other provider (zai/glm, gemini, commandcode-routed opencode seats, openrouter,
...) has no comparable local signal yet -- ``usage_percent_for_model`` returns
None for those, which callers MUST treat as "not excluded" (fail OPEN, never
fail closed on missing data). Tracked as a known gap in review-cli#205.

Fails open throughout: a missing/corrupt/stale usage file, an unmapped model, or
any parse error all resolve to None (never excluded), so a review can never be
blocked or degraded by this module being wrong, absent, or out of date.
"""

from __future__ import annotations

import glob
import json
import os
import time
from pathlib import Path

# A sample older than this is not trusted as "current" -- tg-ctl only writes a
# fresh sample when the harness itself reports one (on hook fire / limit check),
# so a stale file (the user hasn't run that harness in a while) must not
# silently exclude a model based on ancient data. Overridable for tests via
# $REVIEW_USAGE_MAX_AGE_SECONDS.
DEFAULT_MAX_AGE_SECONDS = 24 * 60 * 60  # 24h
_ENV_MAX_AGE = "REVIEW_USAGE_MAX_AGE_SECONDS"
_ENV_FILE = "REVIEW_USAGE_LIMITS_FILE"
_ENV_DIR = "REVIEW_USAGE_LIMITS_DIR"

# The rate/usage-WINDOW limit names this module cares about. "context window" is
# a per-call SIZE limit, not an account usage budget that runs out over time, so
# it is deliberately excluded from the near-limit check.
_RATE_LIMIT_NAMES = frozenset({"5-hour", "weekly"})

# The single named threshold this whole feature uses by default (board
# composition, flat-panel padding, and this module's own helpers all import
# this ONE constant rather than each hardcoding "70.0" separately).
DEFAULT_LIMIT_THRESHOLD = 70.0

# Model-string prefix -> the tg-ctl `agent` family sharing its account quota.
# Extend this map as tg-ctl learns to track more agent families (see the module
# docstring's "coverage is PARTIAL" note). `codex:gpt-...` seats (e.g. SOL_SEAT
# in config.py) route through the SAME codex account as the bare `"codex"`
# seat, so they share the prefix mapping too.
_AGENT_FAMILY_PREFIXES: tuple[tuple[str, str], ...] = (
    ("claude:", "claude"),
    ("codex:", "codex"),
)
# Bare (non-prefixed) model ids routed through a specific agent's own account.
_AGENT_FAMILY_EXACT: dict[str, str] = {"codex": "codex"}


def _max_age_seconds() -> float:
    raw = os.environ.get(_ENV_MAX_AGE)
    if not raw or not raw.strip():
        return DEFAULT_MAX_AGE_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_MAX_AGE_SECONDS
    return value if value > 0 else DEFAULT_MAX_AGE_SECONDS


def _usage_file_paths() -> list[Path]:
    override = os.environ.get(_ENV_FILE)
    if override:
        return [Path(override).expanduser()]
    # $REVIEW_USAGE_LIMITS_DIR overrides the DIRECTORY the glob searches (tests
    # drive the multi-file merge path against a throwaway dir); the real
    # default is tg-cli's own config directory.
    base = os.environ.get(_ENV_DIR)
    directory = Path(base).expanduser() if base else Path.home() / ".config" / "tg-cli"
    pattern = str(directory / "tg-ctl.*.usage-latest.json")
    return [Path(p) for p in glob.glob(pattern)]


def load_snapshot() -> list[dict]:
    """Every usage sample from EVERY tg-ctl usage-latest.json this host has,
    merged. Multiple chat ids each get their own file; a single "freshest file
    wins" pick would silently drop a whole agent family's data if that chat's
    file happens to be older (e.g. a `claude`-only daemon writes often while a
    separate chat's `codex` samples sit in a less-recently-touched file). Each
    sample already carries its own `sampledAt` and staleness is enforced
    per-sample by the caller, so merging everything is strictly safe and more
    complete than picking one file. Never raises -- any failure reads as "no
    samples", matching this module's fail-open contract.

    Call this ONCE per board/panel composition and reuse the result (pass it
    as `usage_percent_for_model`'s `samples=`) instead of letting every seat
    re-glob + re-read + re-parse the same files independently."""
    merged: list[dict] = []
    try:
        for path in _usage_file_paths():
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            samples = data.get("samples") if isinstance(data, dict) else None
            if isinstance(samples, list):
                merged.extend(samples)
    except Exception:  # noqa: BLE001 -- best-effort, read-only signal
        return []
    return merged


def _agent_family(model: str) -> str | None:
    if model in _AGENT_FAMILY_EXACT:
        return _AGENT_FAMILY_EXACT[model]
    for prefix, family in _AGENT_FAMILY_PREFIXES:
        if model.startswith(prefix):
            return family
    return None


def usage_percent_for_model(
    model: str,
    *,
    samples: list[dict] | None = None,
    now: float | None = None,
) -> float | None:
    """The highest current rate-limit-window usage percent (0-100) tg-ctl has
    recorded for `model`'s underlying account, or None when unknown (unmapped
    provider, no matching sample, or the freshest matching sample is older than
    the trust window). Callers MUST treat None as "not excluded" -- never as 0
    or 100.

    `samples`, when given, is used INSTEAD of reading the live file -- the unit
    tests drive this directly so the algorithm has zero filesystem/env
    dependency. A caller composing a board/panel should load ONE snapshot via
    `load_snapshot()` and pass it here for every seat, rather than letting
    each call re-read the files.

    Two independent staleness checks, per sample (Fable/k3 review finding,
    review-cli#205 round 4):
      * `sampledAt` within the `_max_age_seconds()` trust window (as before);
      * `resetAt`, when present, must be in the FUTURE -- a sample whose own
        rate-limit window has already reset (e.g. a 6-hour-old "5-hour"
        reading) is not "still true" regardless of the 24h trust window.
    Within ONE (agent, limitName) key, only the FRESHEST (highest `sampledAt`)
    surviving sample counts -- `load_snapshot()` MERGES multiple tg-ctl chat
    files, so more than one sample can exist for the same key; taking the
    highest PERCENT across those (instead of the most recent) would let a
    stale high reading outvote a fresh low one for up to the whole trust
    window. The final percent is the max across the (at most one per
    limitName) freshest survivors -- still the more restrictive of the
    5-hour/weekly windows, just never a stale duplicate of either."""
    family = _agent_family(model)
    if family is None:
        return None
    live_samples = samples if samples is not None else load_snapshot()
    if not live_samples:
        return None
    now_ms = (now if now is not None else time.time()) * 1000
    cutoff_ms = now_ms - _max_age_seconds() * 1000
    freshest_by_limit: dict[str, dict] = {}
    for sample in live_samples:
        if not isinstance(sample, dict):
            continue
        limit_name = sample.get("limitName")
        if sample.get("agent") != family or limit_name not in _RATE_LIMIT_NAMES:
            continue
        sampled_at = sample.get("sampledAt")
        if not isinstance(sampled_at, (int, float)):
            continue
        current = freshest_by_limit.get(limit_name)
        if current is None or sampled_at > current["sampledAt"]:
            freshest_by_limit[limit_name] = sample

    best: float | None = None
    for sample in freshest_by_limit.values():
        sampled_at = sample["sampledAt"]
        if sampled_at < cutoff_ms:
            continue
        reset_at = sample.get("resetAt")
        if isinstance(reset_at, (int, float)) and reset_at < now_ms:
            continue  # this sample's own window has already reset
        percent = sample.get("percent")
        if not isinstance(percent, (int, float)):
            continue
        if best is None or percent > best:
            best = float(percent)
    return best
