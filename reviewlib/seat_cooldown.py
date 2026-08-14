"""Cross-invocation cooldown cache for a CHRONICALLY unavailable claude seat (Fable).

Why this exists
----------------
Each ``review`` invocation is a fresh process, so review-cli's existing in-seat retry
(``reviewlib.retry``) and its rc=0 "unavailable" sentinel detection (``reviewlib.panel``)
only ever see ONE process's worth of history — they correctly avoid retrying a
chronically-unavailable seat WITHIN a run, but every NEW run pays for one full dispatch
attempt before discovering the seat is still down.

The token-burn investigation (2026-08) found this is not theoretical: of 6,383 recorded
review runs over ~2 months, 4,322 (67.7%) dispatched the Fable seat (``claude:claude-
fable-5``, priority 1 in ``DEFAULT_BOARD``) and it failed — 1,836 with an explicit
session/usage-limit message ("You've hit your session limit ... resets HH:MMam/pm"), 714
with the rc=0 administrative "... is currently unavailable" sentinel. Fable runs through
the SAME Claude account/session quota the CLI itself uses (tg-cli's 90%-usage-warning
mechanism watches that exact channel), so a known-exhausted Fable dispatch is not free:
it costs wall-clock (a real `claude-p` subprocess spawn + response) on every single
review, and it does nothing useful — the seat cannot be reached again until the account's
own session window resets.

This module adds a small, best-effort, PERSISTENT (cross-process) cooldown: once a
dispatch comes back with one of the two recognised CHRONIC signals, the seat is marked
"cooling down" for a bounded window; a later invocation within that window skips the real
dispatch entirely and returns a synthetic result shaped EXACTLY like the existing rc=0
"is currently unavailable" sentinel (see ``reviewlib.backends._UNAVAILABLE_MARKERS`` —
the canonical source; ``reviewlib.panel`` re-exports it) — so
every downstream consumer (failover, in-seat retry, the dashboard's HEALTH_PAYWALL
classification) recognises it via the SAME code path already exercised for a live
paywall response, with zero new integration surface.

Deliberately narrow: only the two CHRONIC signals recognised elsewhere in review-cli
(the administrative sentinel body, and a session-limit/usage-credits notice) start a
cooldown. An auth failure, a bad model, or any other seat-fatal class is NOT cached here
— those can be a transient misconfiguration a human fixes moments later, and silently
skipping a "real" retry after a key rotation would hide the fix. Chronic-only means a
false-cache costs at most one cooldown window of extra skips, never a wrong-forever verdict.

KNOWN LIMITATION (codex review finding, not fixed here): the store is keyed by ``model``
only, checked BEFORE ``REVIEW_CLAUDE_MODE``/CLI-vs-API transport selection. A session-
limit recorded from the CLI transport (subscription quota) therefore also skips that
model's separate, key-billed API transport for the same window — even though switching
transport is a legitimate, immediate fix a human could make (unlike the auth-failure case
above, which this module already protects). Impact is bounded: the synthetic sentinel
names the cached reason + expiry, and ``$REVIEW_SEAT_COOLDOWN_SECONDS=0`` un-sticks it
immediately. A real fix would key the store by ``(model, transport)`` — tracked as
review-cli#187, out of scope for this change.

KNOWN LIMITATION (Fable review finding, deliberately not fixed here): ``record_cooldown``/
``clear_cooldown``'s read-modify-write (``_load`` then ``_write``) is NOT itself locked
across the two calls, only each individual ``_write`` is atomic. Two threads (a board
dispatches its seats in parallel) recording a cooldown for TWO DIFFERENT models in the
same round can race: both read the store before either writes, so the second write's
snapshot doesn't include the first thread's new entry, and that first entry is lost
("last-writer-wins", not corruption — ``_write``'s ``tempfile.mkstemp`` fix above already
closes the SEPARATE same-tmp-path collision that could corrupt the file or raise). Bounded
impact, matching this module's "best-effort" contract throughout: a lost cooldown record
costs at most one extra real dispatch on the next run for THAT model, never a crash or a
wrong-forever verdict. A real fix needs a file lock (e.g. ``fcntl.flock``) around the whole
read-modify-write — tracked as review-cli#188, out of scope here as a lower-severity,
already-bounded race.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from pathlib import Path

# Default cooldown window once a chronic-unavailable signal is seen. Conservative: long
# enough to skip several would-be-wasted dispatches in a normal burst of review runs, short
# enough that a seat back within its quota isn't skipped for long. Overridable via
# $REVIEW_SEAT_COOLDOWN_SECONDS (tests force it tiny); <= 0 disables the cooldown entirely
# (every dispatch goes through, the pre-fix behaviour).
DEFAULT_COOLDOWN_SECONDS = 600.0  # 10 minutes
_ENV_TTL = "REVIEW_SEAT_COOLDOWN_SECONDS"

# How much of the triggering reason text to persist. Enough to show a human "why", short
# enough that the cooldown file never carries a meaningful fragment of a reviewed diff.
_REASON_MAX_LEN = 200


def cooldown_path() -> Path:
    """Where the cooldown store lives. Honors $REVIEW_SEAT_COOLDOWN_FILE (tests /
    opt-relocation); otherwise ``~/.config/review-cli/seat-cooldown.json``, matching
    ``reviewlib.stats.stats_path``'s convention for small local state under that dir."""
    override = os.environ.get("REVIEW_SEAT_COOLDOWN_FILE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "review-cli" / "seat-cooldown.json"


def _ttl_seconds() -> float:
    """The configured cooldown window, read at CALL time so an env override applies. A
    missing/blank/non-numeric value falls back to the default — and so does a
    non-finite one (`nan`/`inf`/`-inf`): `float("nan") <= 0` and `float("inf") <= 0`
    are BOTH False, so those values silently pass every `<= 0` "disabled" check, persist
    into the stored `until`, and later crash `int(cooldown["remaining_seconds"])` in
    backends._cooldown_skip_result (`nan` -> ValueError, `inf` -> OverflowError); `inf`
    also creates an effectively permanent cooldown. Reject them explicitly rather than
    let a malformed env var wedge a seat or crash a review (codex review finding)."""
    raw = os.environ.get(_ENV_TTL)
    if raw is None or not raw.strip():
        return DEFAULT_COOLDOWN_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_COOLDOWN_SECONDS
    return value if math.isfinite(value) else DEFAULT_COOLDOWN_SECONDS


def _load(path: Path) -> dict:
    """Read the JSON store. Never raises: a missing/corrupt/non-dict file means "no
    cooldowns recorded", not a crash — this cache must never be able to break a review.

    codex review finding: `Path.read_text(encoding="utf-8")` can raise
    `UnicodeDecodeError` on a non-UTF-8 file (e.g. hand-edited or corrupted by a
    concurrent writer) — that is a `ValueError` subclass, not an `OSError`, so the
    original `except OSError` here let it propagate through every claude dispatch
    instead of failing open like every other corruption class this function handles."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, data: dict) -> None:
    """Atomic same-dir write (temp + os.replace) so a concurrent reader never sees a
    half-written file. Best-effort: any OS error is swallowed by the caller.

    Opus/codex review finding: the previous temp name (``f"{path.name}.tmp{os.getpid()}"``)
    was unique per-PROCESS, not per-CALL — a board dispatches its seats in parallel
    THREADS within one process, so two claude-provider seats recording a cooldown in the
    same round raced on the IDENTICAL tmp path (interleaved writes -> possibly corrupt
    JSON; the second ``os.replace`` on an already-moved tmp raised ``FileNotFoundError``,
    silently dropping that cooldown). The fixed-name path was also symlink-followable:
    ``Path.write_text`` opens without ``O_EXCL``, so a pre-existing symlink at the tmp
    path would have been followed and its target overwritten. ``tempfile.mkstemp``
    (matching ``backends._atomic_write_text``'s identical pattern) is per-CALL unique
    and opens with ``O_EXCL`` — no collision, no symlink-follow — and its temp file is
    cleaned up on any failure instead of leaking into ``~/.config/review-cli/``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, separators=(",", ":")))
        try:
            os.chmod(tmp, 0o600)  # mirrors stats.py's privacy posture for local state
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def record_cooldown(
    model: str,
    reason: str,
    *,
    now: float | None = None,
    ttl_seconds: float | None = None,
) -> None:
    """Mark ``model`` as chronically unavailable until ``now + ttl``. Best-effort: never
    raises — a cooldown we failed to persist just means the next run pays for one more
    real dispatch, never a broken review.

    Opus review finding: `_write` re-raises ANY `BaseException` after its temp-file
    cleanup (not just `OSError`) — this call runs on the hot path immediately after a
    genuine `review_claude` dispatch, so a caught-too-narrow exception here would
    propagate up and take down the review, contradicting the "never raises" contract
    this docstring (and the module's) promises. `Exception` (not `BaseException`) is
    deliberate: a real fatal signal (`KeyboardInterrupt`, `SystemExit`) must still
    propagate — only a genuinely unexpected serialization/IO failure is swallowed."""
    ttl = ttl_seconds if ttl_seconds is not None else _ttl_seconds()
    if ttl <= 0:
        return
    now = now if now is not None else time.time()
    try:
        path = cooldown_path()
        data = _load(path)
        data[model] = {
            "until": now + ttl,
            "reason": reason[:_REASON_MAX_LEN],
            "recorded_at": now,
        }
        _write(path, data)
    except Exception:  # noqa: BLE001 — best-effort cache, see docstring above
        pass


def active_cooldown(model: str, *, now: float | None = None) -> dict | None:
    """``{"reason", "until", "remaining_seconds"}`` if ``model`` is currently cooling
    down, else ``None``. Never raises — a corrupt/missing store reads as "no cooldown",
    fail-open toward dispatching (never fail-closed toward silently starving a seat that
    would actually work).

    Re-checks ``$REVIEW_SEAT_COOLDOWN_SECONDS <= 0`` here too (not just in
    ``record_cooldown``): a user who sets it to 0 to un-stick a seat RIGHT NOW must see
    that take effect immediately, even against a cooldown recorded before the change —
    otherwise "<= 0 disables it" would only be true for cooldowns recorded AFTER the
    env was set, not a promise that holds the moment you set it (kimi review finding).

    Also validates a stored ``until`` with ``math.isfinite()`` (codex review finding,
    with a reproduced live crash): ``_ttl_seconds()`` rejects a non-finite *env* value
    before it is ever written, but Python's ``json`` module accepts the non-standard
    ``NaN``/``Infinity``/``-Infinity`` literals on READ too — so a hand-edited or
    otherwise corrupted store file can still carry a non-finite ``until``. Without this
    check ``isinstance(float("nan"), float)`` is True and ``nan <= now`` is False, so a
    NaN `until` would pass through and hand the caller ``remaining_seconds=nan``, which
    then crashes ``int(remaining_seconds)`` in ``backends._cooldown_skip_result``
    (confirmed: raises ``ValueError: cannot convert float NaN to integer``)."""
    if _ttl_seconds() <= 0:
        return None
    now = now if now is not None else time.time()
    try:
        data = _load(cooldown_path())
    except Exception:  # noqa: BLE001 — see docstring: never raises, fail-open
        # Opus review finding, round 3: `cooldown_path()` (called inside this try, via
        # `_load`'s argument) can raise `RuntimeError` — NOT `OSError` — when
        # `Path.home()` can't resolve a home directory. This runs on the hot path in
        # `review_claude`/`review_with_images` before EVERY dispatch, so a too-narrow
        # catch here would take down the review — the exact "never raises" violation
        # `record_cooldown`/`clear_cooldown` were already widened to `Exception` to
        # avoid; this call site was missed.
        return None
    entry = data.get(model)
    if not isinstance(entry, dict):
        return None
    until = entry.get("until")
    if not isinstance(until, (int, float)) or not math.isfinite(until) or until <= now:
        return None
    # codex review finding: `entry.get("reason") or "unknown"` validated only
    # falsy-ness, not TYPE — a corrupt/hand-edited store with `"reason": 1` (JSON
    # accepts any value there) passed straight through as a non-string int. The only
    # consumer, `backends._bounded_cooldown_skip_body`, slices it (`reason[:budget]`),
    # which raises `TypeError` on a non-string — crashing every claude dispatch while
    # that cooldown is active, violating this module's "never raises" / fail-open
    # contract just like the non-finite `until` case right above already guards
    # against. A non-string `reason` reads as "unknown", same as a missing one.
    reason = entry.get("reason")
    if not isinstance(reason, str) or not reason:
        reason = "unknown"
    return {
        "reason": reason,
        "until": until,
        "remaining_seconds": until - now,
    }


def clear_cooldown(model: str) -> None:
    """Remove any recorded cooldown for ``model``. Best-effort; used by tests and
    available for a future ``review`` maintenance command.

    Opus review finding: widened from `except OSError` to `except Exception` for the
    same reason as `record_cooldown` above — `_write` can re-raise a non-`OSError`
    `BaseException`, and only a genuine fatal signal should propagate through a
    best-effort cache write."""
    try:
        path = cooldown_path()
        data = _load(path)
        if model in data:
            del data[model]
            _write(path, data)
    except Exception:  # noqa: BLE001 — best-effort cache, see docstring above
        pass
