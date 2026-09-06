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
fable-5``, priority 1 in ``DEFAULT_BOARD`` AT THAT TIME) and it failed — 1,836 with an
explicit session/usage-limit message ("You've hit your session limit ... resets
HH:MMam/pm"), 714 with the rc=0 administrative "... is currently unavailable" sentinel.
Fable runs through the SAME Claude account/session quota the CLI itself uses (tg-cli's
90%-usage-warning mechanism watches that exact channel), so a known-exhausted Fable
dispatch is not free: it costs wall-clock (a real `claude-p` subprocess spawn +
response) on every single review, and it does nothing useful — the seat cannot be
reached again until the account's own session window resets. (review-cli#fable-seat-
reliability, 2026-08: the failure rate climbed to 97.9-100% and this cache was only
catching a small fraction of it — see that change's own notes for the two-part fix,
a gap in the recording logic below AND Fable's DEFAULT_BOARD priority itself, now
demoted to last.)

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

FIXED (review-cli#188), CONDITIONALLY: ``record_cooldown``/``clear_cooldown``'s
read-modify-write (``_load`` then ``_write``) used to be unlocked across the two calls,
only each individual ``_write`` was atomic. Two threads (a board dispatches its seats in
parallel) recording a cooldown for TWO DIFFERENT models in the same round could race: both
read the store before either wrote, so the second write's snapshot didn't include the
first thread's new entry, silently losing it ("last-writer-wins", not corruption —
``_write``'s ``tempfile.mkstemp`` fix above already closes the SEPARATE same-tmp-path
collision that could corrupt the file or raise). ``_locked()`` now wraps the whole
read-modify-write in an in-process ``threading.Lock`` PLUS (where available) an
``fcntl.flock`` exclusive lock: same-process threads are serialized by the
``threading.Lock`` alone (the flock is only ever attempted while already holding it, so
same-process threads never actually contend the flock against each other — see the
``_locked()`` docstring below); the flock is what extends that serialization ACROSS
separate processes, which the in-process lock alone cannot do (Fable review finding,
round 7: an earlier version of this paragraph overstated the flock as itself
thread-contended, which the implementation makes unreachable — corrected here).

Fable review finding, round 5: the guarantee above is real but bounded, not absolute — say
so plainly rather than implying "never" like the paragraph above reads in isolation. If
EITHER lock (the in-process ``threading.Lock`` OR the flock) is still held by a peer past
``_LOCK_TOTAL_DEADLINE_SECONDS`` (a stalled thread, a paused process, a hung network
filesystem — the module's own motivating scenario), the caller degrades to progressively
weaker locking and, in the worst case, proceeds FULLY UNLOCKED — the original lost-update
race returns for that one call. This is a deliberate trade (never block the caller
indefinitely; see the ``_locked()`` docstring below) inherited from, and mirroring,
``reviewlib.specweb.store.SpecStore._guard``'s established pattern: fcntl import is guarded
(Windows has none — degrades to the in-process lock only), and any lock-acquisition
``OSError`` (an odd filesystem without working advisory locks) degrades the SAME way rather
than aborting the write entirely — this cache must never regress from "best-effort,
occasionally racy" to "silently stops recording cooldowns", and "occasionally racy under
sustained contention or a stuck filesystem" is exactly the residual risk this trade keeps.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import tempfile
import threading
import time
from pathlib import Path

try:
    import fcntl  # POSIX advisory file locking (cross-process)
except ImportError:  # pragma: no cover - Windows has no fcntl
    fcntl = None  # type: ignore[assignment]

# Default cooldown window once a chronic-unavailable signal is seen. Conservative: long
# enough to skip several would-be-wasted dispatches in a normal burst of review runs, short
# enough that a seat back within its quota isn't skipped for long. Overridable via
# $REVIEW_SEAT_COOLDOWN_SECONDS (tests force it tiny); <= 0 disables the cooldown entirely
# (every dispatch goes through, the pre-fix behaviour).
DEFAULT_COOLDOWN_SECONDS = 600.0  # 10 minutes
_ENV_TTL = "REVIEW_SEAT_COOLDOWN_SECONDS"

# review-cli#221: a seat that keeps failing across MANY separate `review` invocations
# (each its own process) was only ever getting the flat 10-minute cooldown above — real
# review runs are minutes apart, so by the time the next invocation checked, the window
# had already lapsed and the same known-bad seat got re-dispatched, wasting another full
# round (confirmed live: HYP-1295 re-selected `oc:zai/glm-5.2` at 09:22/09:55/10:07/10:25,
# each gap > 10 minutes). This escalates the TTL with each consecutive failure so a
# seat that is CHRONICALLY bad (not just unlucky once) gets pushed out of the active
# pool for long enough that `run_board_with_failover`'s existing reserve-promotion
# naturally substitutes a different model instead of re-trying the same one.
# fail_count -> TTL seconds: 1st failure keeps the original 10-minute default, then
# climbs (3x, then 4x, then 4x) to 30min, 2h, 8h(cap) — round human values, not a
# constant multiplier; a maintainer changing this should edit the tuple, not infer a
# formula from it.
_ESCALATION_SCHEDULE = (600.0, 1800.0, 7200.0, 28800.0)  # 10min, 30min, 2h, 8h (cap)
# A failure this long after the previous one is treated as a FRESH problem (seat looked
# recovered in between), not a continuation — fail_count resets to 1 rather than keep
# climbing forever for an occasionally-flaky-but-mostly-fine seat.
_ESCALATION_RESET_SECONDS = 24 * 3600.0  # 24 hours

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


def _env_ttl_override() -> float | None:
    """`$REVIEW_SEAT_COOLDOWN_SECONDS`'s value if the var is actually SET (whitespace-only
    counts as unset), else `None`. An override that's present but non-numeric/non-finite
    still counts as "set" (to the safe `DEFAULT_COOLDOWN_SECONDS`) rather than falling
    through to escalation — a user who exported a malformed value gets the same flat
    default they'd have gotten pre-escalation, not a surprise multiplier. See the
    non-finite handling note on the caller side (this used to live here; unchanged
    logic, just split out so callers can tell "explicitly overridden" apart from "use
    the normal default/escalation path")."""
    raw = os.environ.get(_ENV_TTL)
    if raw is None or not raw.strip():
        return None
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_COOLDOWN_SECONDS
    return value if math.isfinite(value) else DEFAULT_COOLDOWN_SECONDS


def _ttl_seconds() -> float:
    """The configured cooldown window, read at CALL time so an env override applies.
    See `_env_ttl_override` for the non-finite/malformed handling; this just supplies
    `DEFAULT_COOLDOWN_SECONDS` when nothing is set."""
    override = _env_ttl_override()
    return override if override is not None else DEFAULT_COOLDOWN_SECONDS


# Serializes same-process threads unconditionally (works even where fcntl is unavailable,
# e.g. Windows) — the flock below adds the cross-process guarantee on top where possible.
_LOCK = threading.Lock()

# Total bounded budget for acquiring the in-process `_LOCK` (and, via the flock's own
# smaller sub-budget below, an upper bound on the combined wait) — Opus/GLM review
# findings, round 2. Round 1 bounded only the flock retry and left `with _LOCK:` a plain
# unbounded acquire — but the exact hang this module exists to avoid (a peer stalled
# *inside* the critical section, e.g. a hung `os.replace` on a stuck network filesystem)
# blocks on `_LOCK`, not the flock, so that left the identical bug one layer up. A thread
# that can't get `_LOCK` in time degrades to fully unlocked (matching this module's
# pre-fix, already-accepted best-effort contract) instead of blocking indefinitely.
#
# Fable review finding (final round): an earlier version of this comment claimed sharing
# ONE deadline between `_LOCK` and the flock retry ALSO fixed an "N x deadline convoy"
# for in-process threads queued behind a contended flock — it did not: the thread holding
# `_LOCK` would still spin on the flock for up to the full deadline, so every OTHER
# thread queued on `_LOCK.acquire(timeout=deadline)` timed out at roughly the SAME
# moment and all degraded to fully unlocked SIMULTANEOUSLY, racing each other — the exact
# in-process lost-update this module exists to prevent. See `_FLOCK_SUB_BUDGET_SECONDS`
# below for the actual fix: the flock retry gets its own, much smaller budget, so it
# releases `_LOCK` back to waiting threads quickly instead of holding it hostage for the
# cross-process half's sake.
_LOCK_TOTAL_DEADLINE_SECONDS = 2.0
_FLOCK_RETRY_INTERVAL_SECONDS = 0.02

# Fable review finding, final round: sharing ONE deadline between `_LOCK` acquisition
# and the flock retry (the comment above) does NOT actually prevent the convoy it
# claims to — a thread that wins `_LOCK` first still spins on the flock for up to the
# FULL shared deadline while holding it, so every OTHER same-process thread queued on
# `_LOCK.acquire(timeout=deadline)` times out at roughly the SAME moment and all
# degrade to fully unlocked SIMULTANEOUSLY, racing each other — the exact in-process
# lost-update this module exists to prevent, now triggered by ordinary brief
# cross-process contention rather than a genuinely stalled peer. The flock retry gets
# its OWN, much smaller sub-budget instead: give up on the CROSS-process half quickly
# and degrade to `_LOCK`-only (still real, still serializes this process's threads)
# rather than holding `_LOCK` hostage for the flock's sake. Deliberately much smaller
# than `_LOCK_TOTAL_DEADLINE_SECONDS` — long enough to absorb a normal brief flock hold
# (another process finishing its own write), short enough that `_LOCK` waiters are
# never blocked more than this by someone else's cross-process contention.
_FLOCK_SUB_BUDGET_SECONDS = 0.15


@contextlib.contextmanager
def _locked(path: Path):
    """Exclusive advisory lock serializing the read-modify-write pair in
    `record_cooldown`/`clear_cooldown` (review-cli#188). The flock is only ever attempted
    AFTER acquiring the in-process `_LOCK` below, so within one process the flock itself is
    never actually contended between threads — `_LOCK` alone already serializes them
    (Fable review finding, round 7: an earlier version of this docstring claimed distinct
    threads contend the flock "just like separate processes would," which this ordering
    makes unreachable — corrected here). What the flock's per-open-file-description scoping
    (not per-process) DOES buy is the cross-process half: a SEPARATE `review` invocation
    (its own process, its own `_LOCK`) opens its own fd to the SAME sidecar file and
    contends on that, which is the one case an in-process `threading.Lock` could never
    cover on its own. Locks a dedicated `.<name>.lock` file rather than `path` itself,
    since `path` may not exist yet on a first-ever cooldown and `_write`'s `os.replace`
    swaps the underlying inode out from under any fd opened on `path` directly.

    Mirrors `reviewlib.specweb.store.SpecStore._guard`, with a stronger bound: the WHOLE
    operation (in-process `_LOCK` + cross-process flock, combined) is capped at
    `_LOCK_TOTAL_DEADLINE_SECONDS`. On platforms without `fcntl` (Windows), when the file
    lock fails for any `OSError` reason (an odd filesystem without working advisory locks),
    when a peer holds the FLOCK past the shared deadline, or when a peer holds `_LOCK`
    ITSELF past the shared deadline (a same-process thread stalled inside its own critical
    section — Opus/GLM review finding, round 2: this case was still an unbounded wait after
    round 1's fix, which only bounded the flock half), this degrades progressively — first
    to `_LOCK`-only, then to fully unlocked — instead of blocking indefinitely. Lock
    ACQUISITION (both `_LOCK` and the flock) is bounded by this deadline; the critical
    section's own I/O (`_load`/`_write`, including `os.replace`) is not and cannot be —
    Fable review finding, round 3: a genuinely hung filesystem still stalls the write
    itself, the deadline only guarantees we don't ALSO wait indefinitely just to start.
    A failure to lock must never abort the write, only narrow the guarantee back toward
    this module's pre-fix, already-accepted best-effort contract.

    Fable review finding, final round: the flock retry does NOT spin for the full
    shared `deadline` — it has its own much smaller `_FLOCK_SUB_BUDGET_SECONDS`, so a
    thread holding `_LOCK` while contending for the flock releases `_LOCK` back to
    other same-process threads quickly on flock contention (degrading itself to
    `_LOCK`-only) instead of holding `_LOCK` hostage for the cross-process half's sake.
    Without this, EVERY same-process thread queued on `_LOCK.acquire(timeout=deadline)`
    would time out at roughly the same moment as the first thread's flock spin and all
    degrade to fully unlocked SIMULTANEOUSLY, racing each other — the exact in-process
    lost-update this module exists to prevent, reappearing under ordinary brief
    cross-process contention rather than only a genuinely stalled peer. A thread that
    times out on `_LOCK` itself (waiting for ANOTHER same-process thread, not a
    cross-process peer) still never attempts even a single non-blocking flock — by the
    time `_LOCK` itself is contended for the full deadline, the holder is almost
    certainly stalled deep in the critical section (a hung `_write`/`os.replace`), not
    merely retrying its own flock, so a flock probe here would rarely help and isn't
    worth the extra syscall on the already-slow path."""
    deadline = time.monotonic() + _LOCK_TOTAL_DEADLINE_SECONDS
    got_lock = _LOCK.acquire(timeout=max(0.0, deadline - time.monotonic()))
    try:
        if not got_lock or fcntl is None:
            yield
            return
        lock_path = path.parent / f".{path.name}.lock"
        fd = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            # Its OWN, smaller sub-budget (see `_FLOCK_SUB_BUDGET_SECONDS`'s comment) —
            # NOT the full shared `deadline` — so a contended flock releases `_LOCK`
            # back to other in-process threads quickly instead of holding it hostage.
            flock_deadline = min(deadline, time.monotonic() + _FLOCK_SUB_BUDGET_SECONDS)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= flock_deadline:
                        # Fable review finding, round 3 (comment corrected, round 6): null
                        # out `fd` BEFORE attempting the close, not after. Without this
                        # ordering, a raising `os.close` (an exotic deferred-error
                        # filesystem) would propagate out of this `except BlockingIOError`
                        # into the OUTER `except OSError` below with `fd` STILL non-None —
                        # closing the SAME descriptor number again there, risking an EBADF
                        # on an unrelated fd a concurrent thread has since reused. Nulling
                        # `fd` first and swallowing the close error LOCALLY (immediately
                        # below) is what actually prevents that — control never reaches the
                        # outer handler for this branch at all.
                        _fd_to_close, fd = fd, None
                        try:
                            os.close(_fd_to_close)
                        except OSError:
                            pass
                        break
                    time.sleep(_FLOCK_RETRY_INTERVAL_SECONDS)
        except OSError:
            # k3 review finding, round 4: this bare `os.close(fd)` was the same defect
            # class the deadline-expiry branch above was already fixed for (round 3) — if
            # THIS close itself raises (the same deferred-error-filesystem scenario), the
            # exception would escape `_locked` before `yield`, get swallowed by
            # `record_cooldown`'s own `except Exception`, and silently skip the write —
            # contradicting the "a failure to lock must never abort the write" contract.
            if fd is not None:
                _fd_to_close, fd = fd, None
                try:
                    os.close(_fd_to_close)
                except OSError:
                    pass
        try:
            yield
        finally:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                try:
                    os.close(fd)
                except OSError:
                    pass
    finally:
        if got_lock:
            _LOCK.release()


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
    propagate — only a genuinely unexpected serialization/IO failure is swallowed.

    review-cli#221: when the caller does NOT pass an explicit `ttl_seconds` (the normal
    dispatch-time path — tests that force a tiny window still get exactly what they
    ask for, unaffected), the TTL escalates with consecutive failures per
    `_ESCALATION_SCHEDULE` instead of always using the flat default — see that
    constant's comment for why. `fail_count` is read from any still-persisted PRIOR
    entry for this model (even one whose cooldown has since expired — expiry only
    means dispatch is allowed again, not that the failure history is forgotten) as
    long as it isn't stale past `_ESCALATION_RESET_SECONDS`."""
    now = now if now is not None else time.time()
    try:
        # Cheap disable checks FIRST, before touching the store OR EVEN ACQUIRING THE
        # LOCK — restores the pre-escalation fast path (codex/GLM review finding),
        # tightened by a later review round: the disable path must not enter
        # `_locked()` either, since lock acquisition itself does unbounded
        # `mkdir`/`os.open` on the sidecar file — a caller using `ttl_seconds=0` or
        # `$REVIEW_SEAT_COOLDOWN_SECONDS<=0` specifically to un-stick a seat RIGHT NOW
        # (the documented hatch on `active_cooldown`'s docstring) must not itself be
        # blocked by the very filesystem hang this module exists to route around. An
        # earlier version of this diff nested BOTH disable checks inside `with
        # _locked(path):`, which regressed exactly this guarantee — every no-op call
        # still paid for lock acquisition even though nothing was ever going to be
        # written.
        escalate = False
        if ttl_seconds is not None:
            if ttl_seconds <= 0:
                return
            ttl = ttl_seconds
            fail_count = 1  # explicit ttl: no escalation, see docstring
        else:
            env_override = _env_ttl_override()
            if env_override is not None:
                if env_override <= 0:
                    return
                ttl = env_override
                fail_count = 1  # explicit env override: no escalation, see docstring
            else:
                # Escalation: only reached when NEITHER the caller's explicit
                # `ttl_seconds` NOR an explicit `$REVIEW_SEAT_COOLDOWN_SECONDS` is
                # present — both are a human (or a test) asking for a SPECIFIC window,
                # honored exactly, same as pre-escalation behavior. `ttl`/`fail_count`
                # are computed below, once the lock is held and the store is actually
                # being loaded for the write (round-2 review finding: an earlier
                # version of this diff loaded the store twice per failure on this
                # exact path).
                ttl = None
                fail_count = None
                escalate = True

        path = cooldown_path()
        with _locked(path):
            if escalate:
                data = _load(path)
                prior = data.get(model)
                fail_count = 1
                if isinstance(prior, dict):
                    prior_recorded_at = prior.get("recorded_at")
                    prior_count = prior.get("fail_count")
                    if (
                        # `type(x) in (int, float)`, not `isinstance(x, (int, float))`
                        # — round-6 review finding (Opus): the sibling `prior_count`
                        # guard below already rejects a bare JSON bool via `type(x) is
                        # int` (isinstance(True, int) is True in Python); this guard
                        # used isinstance and so silently accepted a corrupt
                        # `"recorded_at": true`. In practice `now - True` almost always
                        # exceeds the reset window anyway, but the two guards in the
                        # same hardening block should reject the same corrupt shapes
                        # for the same reason, not by accident of magnitude.
                        type(prior_recorded_at) in (int, float)
                        and math.isfinite(prior_recorded_at)
                        and 0 <= (now - prior_recorded_at) <= _ESCALATION_RESET_SECONDS
                        # `type(x) is int`, not `isinstance` — a corrupt store's bare
                        # JSON `true`/`false` is a `bool`, and `isinstance(True, int)`
                        # is True in Python (round-3 review finding), which would
                        # silently escalate off a boolean instead of treating it as
                        # the invalid value it is.
                        and type(prior_count) is int
                        and prior_count >= 1
                    ):
                        fail_count = prior_count + 1
                ttl = _ESCALATION_SCHEDULE[
                    min(fail_count - 1, len(_ESCALATION_SCHEDULE) - 1)
                ]
            else:
                data = _load(path)
            data[model] = {
                "until": now + ttl,
                "reason": reason[:_REASON_MAX_LEN],
                "recorded_at": now,
                "fail_count": fail_count,
            }
            _write(path, data)
    except Exception:  # noqa: BLE001 — best-effort cache, see docstring above
        pass


def active_cooldown(model: str, *, now: float | None = None) -> dict | None:
    """``{"reason", "until", "remaining_seconds", "fail_count"}`` if ``model`` is
    currently cooling down, else ``None``. Never raises — a corrupt/missing store
    reads as "no cooldown",
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
    fail_count = entry.get("fail_count")
    # `type(x) is int`, not `isinstance` — see the identical round-3 review finding
    # noted on record_cooldown's matching guard: `isinstance(True, int)` is True in
    # Python, so a corrupt store's bare JSON `true` would otherwise read as fail_count
    # 1 (harmless here) but `false` would read as fail_count 0 -> clamped to 1 anyway,
    # EXCEPT the display value itself must never render as a bare `True`/`False`.
    if type(fail_count) is not int or fail_count < 1:
        fail_count = 1
    return {
        "reason": reason,
        "until": until,
        "remaining_seconds": until - now,
        "fail_count": fail_count,
    }


def clear_cooldown(model: str) -> None:
    """Remove any recorded cooldown (and its `fail_count` escalation history) for
    ``model``. Best-effort. Called on the real success path (`backends.py`'s two
    `record_cooldown` call sites, on a genuine `returncode == 0` result) so a seat
    that recovers doesn't stay escalated by the 24h failure-only reset window alone
    (review-cli#221 round-2 review finding: without this, a seat failing more often
    than once per 24h — even with many successes in between — ratchets to the 8h cap
    and stays there, since only FAILURES ever touched the store). Also used directly
    by tests.

    Opus review finding: widened from `except OSError` to `except Exception` for the
    same reason as `record_cooldown` above — `_write` can re-raise a non-`OSError`
    `BaseException`, and only a genuine fatal signal should propagate through a
    best-effort cache write.

    Round-3 review finding (Opus), UPDATED post-#188: the write below is now wrapped
    in `_locked()` (same as `record_cooldown`), so a concurrent write here and a
    `record_cooldown` call (any model — they share one store file) are properly
    serialized within `_locked()`'s bounded deadline, not merely "unlocked and
    accepted." The residual risk is narrower than the pre-#188 note this paragraph
    used to describe: only `_locked()`'s own documented degrade path (sustained
    contention past the deadline; see its docstring) still admits a race, not every
    concurrent call. Round-4 review finding (Opus): under that residual degrade, a
    lost/racy WRITE (not just a lost clear) can still change the escalation window's
    MAGNITUDE, not just its presence — two writes racing past the degrade point, each
    reading the same stale `fail_count`, could leave the schedule stuck at 10 minutes
    instead of climbing toward 30min/2h/8h, or (less likely) jump unexpectedly. Still
    within the module's accepted best-effort contract (a lost/wrong window costs extra
    dispatches, never a crash) — just a narrower window for it than pre-#188.

    Round-4 review finding (k3): deliberately does NOT mirror `record_cooldown`'s
    disable-fast-path (`_ttl_seconds() <= 0` early return) despite an earlier round-3
    version of this docstring proposing exactly that — a real incident shape it would
    have broken: an operator escalated to the 8h cap sets
    `REVIEW_SEAT_COOLDOWN_SECONDS=0` specifically to un-stick the seat RIGHT NOW (the
    documented hatch on `active_cooldown`'s own docstring), the real dispatch runs and
    succeeds, but a disable-gated `clear_cooldown` would bail before ever deleting the
    stale escalated entry — so unsetting the var afterward would leave the seat
    "cooling down" for up to 8 more hours despite the just-observed success. Disabling
    *recording* must not also disable *clearing an observed recovery*. The `if model in
    data` guard right below already skips the WRITE in the overwhelmingly common case
    where there's nothing to clear — the extra cost of NOT fast-pathing is one file
    read per genuine success, negligible next to the dispatch's own subprocess/LLM
    round-trip cost."""
    try:
        path = cooldown_path()
        # Fast pre-check WITHOUT the lock — a DIFFERENT guarantee than record_cooldown's
        # disable checks above. Unlike those (pure env/argument tests, genuinely zero
        # I/O), this still calls `_load(path)` — an open/read/parse on the same
        # filesystem, equally unbounded on a hung disk. It buys ONLY avoidance of lock
        # ACQUISITION overhead in the overwhelmingly common no-op case (nothing to
        # clear), not hung-filesystem protection — do not group this with the true
        # zero-I/O disable hatches. Re-checked under the lock below since this read is
        # racy by construction (TOCTOU) — a concurrent write landing in the gap is just
        # an ordering the caller could have observed anyway, same best-effort contract
        # as the rest of this module.
        if model not in _load(path):
            return
        with _locked(path):
            data = _load(path)
            if model in data:
                del data[model]
                _write(path, data)
    except Exception:  # noqa: BLE001 — best-effort cache, see docstring above
        pass
