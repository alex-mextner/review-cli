"""Per-model behavior hints, versioned so they can be updated as models evolve.

Why this exists
----------------
review-cli's process-level timeout (``process.idle_timeout_seconds``) treats every
model identically: it tolerates up to 20 minutes of silence before reaping a stalled
subprocess, because *some* backends legitimately think for a long time without emitting
any output (see that module's docstring). But "legitimately silent for a while" and
"stuck/dead" look identical from the outside for the first several minutes — the only
honest distinguishing signal is model-specific track record: some models are known to
produce a real verdict after a long *quiet* stretch, others going silent for more than a
couple of minutes has, in practice, meant a wedged process (Alex's request 2026-08-19/20:
"если модель думает вслух ... и молчит скажем 5 минут то жёсткое прерывание").

This module is the single place that knowledge lives, so a future model-behavior change
(a provider ships a new "thinking" mode, a model that used to stream tokens stops doing
so) is a one-line data update here rather than a scattered set of magic numbers. Entries
are deliberately data, not code — reviewers/maintainers should be able to update a
threshold without touching any control-flow logic.

KNOWN SCOPE GAP (review-cli#239, found during this module's own review gate): the
current ``true_silence_timeout_seconds`` check only covers the window BEFORE the
first byte of output ever arrives — once ANY output shows up, the check permanently
disarms and the ordinary (much longer) idle timeout governs for the rest of the call.
Re-reading Alex's quoted request above more literally, "если модель думает вслух"
("if a model thinks aloud") describes a model that DOES normally produce visible
output going silent — i.e. a MID-call silence, not a from-spawn one. That case is not
yet covered here; review-cli#239 tracks clarifying and closing this gap.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Default grace period (seconds) a model gets with ZERO output before it is treated as
# stuck rather than "silently thinking" — Alex's explicit ask: ~5 minutes. This is
# DELIBERATELY much tighter than process._DEFAULT_IDLE_TIMEOUT (20 minutes): the 20-minute
# floor governs a model that has ALREADY produced at least one byte and then gone idle
# (a real, in-progress call going quiet between tool calls/tokens); this threshold governs
# the narrower, earlier window before the FIRST byte ever arrives, where "nothing at all
# yet" is a much stronger stuck signal than "quiet for a bit after starting."
_DEFAULT_TRUE_SILENCE_SECONDS = 5 * 60

# NOTE on the cooldown DURATION (Alex's "~30 minutes" ask): review-cli's cooldown TTL
# is deliberately owned entirely by seat_cooldown's own escalation schedule (10min→
# 30min→2h→8h, review-cli#230) — a true-silence trip records a cooldown via the SAME
# un-parameterized `record_cooldown(model, reason)` call every other cooldown-worthy
# failure uses, so repeat offenses escalate correctly instead of being pinned at one
# fixed window forever (codex review finding, round 1: passing an explicit per-model
# TTL here made `record_cooldown` treat every occurrence as fail_count=1, silently
# disabling escalation). This module intentionally has NO cooldown-duration function.


@dataclass(frozen=True)
class ModelBehavior:
    """What review-cli currently believes about one model's output behavior.

    ``updated``/``note`` exist so a human editing ``_REGISTRY`` below always leaves a
    trail of WHY a threshold is what it is and WHEN it was last confirmed accurate —
    model behavior drifts across provider releases, and an unexplained number is
    indistinguishable from a stale one."""

    silent_thinker: bool
    true_silence_seconds: int
    updated: str
    note: str


_DEFAULT_BEHAVIOR = ModelBehavior(
    silent_thinker=False,
    true_silence_seconds=_DEFAULT_TRUE_SILENCE_SECONDS,
    updated="2026-08-20",
    note="unlisted model — conservative default (5min true-silence cutoff)",
)

# Per-model overrides. Keyed by the SAME model identifier string review-cli uses
# elsewhere (board seat / cooldown-store key, e.g. "oc:zai/glm-5.2", "claude:claude-
# fable-5") so a lookup never needs a second normalization step. Intentionally starts
# empty of specific overrides beyond the mechanism itself — this repo's own code
# currently has no VERIFIED per-model silent-thinking data (the "5min" figure that
# review-cli#153/#159/#179's zai/glm watchdog folklore referenced was never actually
# implemented at this layer). This mechanism is currently wired into review_opencode
# only (review-cli#235 tracks extending it to the other 5 backend call sites). Add an
# entry here backed by real observed evidence (a linked issue/incident), not a guess.
_REGISTRY: dict[str, ModelBehavior] = {}


def true_silence_timeout_seconds(model: str) -> int | None:
    """Seconds of ZERO output tolerated for ``model`` before treating it as stuck
    rather than silently thinking. None disables the check entirely — passes straight
    through to ``process._run_streamed``'s own ``true_silence_timeout=None`` contract.

    Precedence, highest first: (1) $REVIEW_TRUE_SILENCE_SECONDS, a blanket
    process-wide override for every model — any value <= 0 disables the check
    entirely (mirrors seat_cooldown's own $REVIEW_SEAT_COOLDOWN_SECONDS<=0 convention,
    the feature this one directly feeds; codex + Fable review finding, round 4: an
    earlier version only treated EXACTLY 0 as disable, silently re-enabling the check
    at the default for any negative value despite the help text already promising
    "<= 0 disables" — this is the coarsest, most-recently-expressed operator intent,
    so it wins even over a per-model entry); (2) a per-model entry in ``_REGISTRY``;
    (3) ``_DEFAULT_BEHAVIOR`` for any unlisted model."""
    raw = os.environ.get("REVIEW_TRUE_SILENCE_SECONDS")
    if raw is not None and raw.strip():
        try:
            value = int(raw)
        except ValueError:
            value = None
        if value is not None:
            if value <= 0:
                return None  # explicit disable
            return value
    # codex review finding, review-cli#243 round 10: a registry (or _DEFAULT_BEHAVIOR)
    # entry with true_silence_seconds <= 0 must ALSO mean "disabled", the same as the
    # env override above -- an un-normalized 0/negative value is `is not None`, so it
    # would ARM _run_streamed's check with a near-zero budget, causing an INSTANT
    # false reap (rc 125) plus an escalating cooldown bench of a perfectly healthy
    # seat on its very first poll (~0.5s after spawn). Latent today (_REGISTRY ships
    # empty, _DEFAULT_BEHAVIOR is a positive constant), but a future maintainer adding
    # an entry — the help text explicitly documents "<= 0 disables" as the general
    # convention this module shares with seat_cooldown's own env var — would otherwise
    # hit exactly this trap with no guard catching it.
    seconds = _REGISTRY.get(model, _DEFAULT_BEHAVIOR).true_silence_seconds
    return seconds if seconds > 0 else None


def is_known_silent_thinker(model: str) -> bool:
    """True if ``model`` is a KNOWN, verified silent-thinker (produces no output for
    extended stretches while still eventually returning a real verdict).

    NOT currently consumed by any control flow (codex review finding, round 1: an
    earlier version of this docstring overclaimed "surfaced in diagnostics" — it
    wasn't). This is deliberately still exposed as documented, versioned per-model
    DATA — Alex's request explicitly asked to "хранить и актуализировать эту
    информацию" (store and keep this information up to date) as its own requirement,
    independent of which feature reads it first; review-cli#236 (auto-summarize a
    timed-out seat) is the most likely first real consumer. Even once wired up, a
    known silent thinker should still get its OWN registry-configured
    true_silence_seconds (generous, not unbounded) rather than having the check
    disabled outright for it — an unbounded allowance would make a genuinely wedged
    call for that model indistinguishable from a slow-but-alive one, forever."""
    return _REGISTRY.get(model, _DEFAULT_BEHAVIOR).silent_thinker
