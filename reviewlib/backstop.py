"""Internal run backstop — the ONLY time bound on a `review` run.

`review` is meant to have NO external timeout: agents must not wrap it in a short
shell `timeout`, because the panel/brainstorm modes are multi-round and only emit
their synthesis at the very end (see stats.py / the SKILL.md advertising). Removing
the external cap, though, means a genuinely wedged run (a backend that hangs past
its own per-call deadline in a way the per-call kill can't reach, a pathological
loop) could in principle run forever. So the run carries ONE internal, last-resort
time bound instead: a watchdog that force-terminates the process if the WHOLE run
exceeds a hard ceiling.

Design:
  * The ceiling is INTERNAL and capped at <= 4h (`MAX_BACKSTOP_SECONDS`). Nothing can
    set it higher — that is the contract the SKILL.md/README advertise ("no external
    timeout; internal <=4h backstop"). `$REVIEW_BACKSTOP_SECONDS` may only LOWER it
    (tests, or a user who wants a tighter last-resort), never raise it past 4h.
  * It is a backstop, not a normal deadline: a healthy run finishes in MINUTES, far
    under the ceiling, and the watchdog is cancelled cleanly on exit. It only ever
    fires for a truly stuck run.
  * On fire it KILL-FIRST reaps the registered backend subprocesses (SIGKILL straight
    to each one's OWN session group — never the CLI's or the caller's group, which
    `review` often shares with its parent), prints a loud actionable line to stderr,
    and `os._exit`s with code 124 — the conventional "timed out" code, mirroring shell
    `timeout`. A deadman timer guarantees that `os._exit` even if the stderr announce
    blocks on a full pipe. `os._exit` is deliberate: a wedged run may be stuck inside a
    C call or a non-daemon thread that a normal `sys.exit`/raise could not unwind. See
    `_fire` for the exact ordering and why nothing on that path may block the exit.
"""

from __future__ import annotations

import os
import sys
import threading
from contextlib import contextmanager
from typing import Iterator

# The hard ceiling. The internal backstop can be LOWERED below this (tests, a tighter
# user preference) but NEVER raised above it — 4h is the advertised maximum.
MAX_BACKSTOP_SECONDS = 4 * 60 * 60  # 14400

# Exit code on a backstop fire — matches shell `timeout`'s 124 so callers/CI that
# special-case a timeout keep working.
BACKSTOP_EXIT_CODE = 124


def backstop_seconds() -> int:
    """The effective internal backstop, in seconds. Always 1..MAX_BACKSTOP_SECONDS.

    Defaults to the 4h ceiling. `$REVIEW_BACKSTOP_SECONDS` may only LOWER it: a value
    above the ceiling is clamped down to 4h (the cap can't be raised), a non-positive
    or unparseable value is ignored (falls back to the ceiling). The floor is 1s so a
    test can force an almost-immediate fire.
    """
    raw = os.environ.get("REVIEW_BACKSTOP_SECONDS")
    if raw is None:
        return MAX_BACKSTOP_SECONDS
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return MAX_BACKSTOP_SECONDS
    if val <= 0:
        return MAX_BACKSTOP_SECONDS
    return min(val, MAX_BACKSTOP_SECONDS)


# Grace (seconds) the deadman gives the orderly path (reap + announce) before it forces
# the exit unconditionally. Short — its only job is to outlast a normal reap, not to let
# the run continue.
_DEADMAN_GRACE_SECONDS = 3


def _fire(seconds: int, stream) -> None:
    """Watchdog body: GUARANTEE a hard 124 exit, reaping backend children on the way out.

    `os._exit` (not `sys.exit`/raise) is deliberate: a wedged run may be stuck inside a
    C call or a non-daemon thread that a normal unwind could not interrupt — `os._exit`
    is the unconditional guarantee that the run cannot survive this.

    NOTHING here may be allowed to BLOCK the exit. In particular the announcement writes
    to stderr, which can be a PIPE a non-draining parent has let fill — a flushed `print`
    on a full pipe blocks forever (codex P2). So before any potentially-blocking work we
    arm a DEADMAN: a daemon timer that force-exits 124 after a short grace no matter what.
    Then, in order: reap the registered backend subprocesses, announce (best-effort), and
    `os._exit`. If the reap or the announce blocks, the deadman still terminates the
    process — the backstop's guarantee holds.

    It does NOT signal its own / the caller's process group. `review` is frequently
    invoked as a CHILD that shares a process group with its caller (an agent's shell, a
    CI step), so a `killpg(getpgrp())` would take the CALLER down with it. It reaps ONLY
    the registered backend subprocesses (`process.kill_live_children`): each runs in its
    OWN session (`start_new_session=True`), so killing those groups bounds the actual
    work without touching the caller's group.
    """
    # Deadman FIRST: a daemon timer that hard-exits even if everything below wedges
    # (e.g. the announce blocks on a full stderr pipe, or a reap hangs). os._exit in a
    # timer thread terminates the whole process.
    deadman = threading.Timer(
        _DEADMAN_GRACE_SECONDS, os._exit, args=(BACKSTOP_EXIT_CODE,)
    )
    deadman.daemon = True
    deadman.start()
    # Reap live backend subprocesses (their own session groups) so a wedged run's backends
    # don't get orphaned. Bounded internally; the deadman covers a pathological hang.
    try:
        from .process import kill_live_children

        kill_live_children()
    except Exception:  # noqa: BLE001 — best-effort; the deadman + os._exit are the guarantee
        pass
    # Reap any pending qa SUT env (hook/compose) BEFORE the announce. os._exit below BYPASSES
    # atexit, so without this a wedged qa run leaks the daemonized SUT env the backstop's
    # subprocess-group SIGKILL cannot reach (codex P2). Order matters: RESOURCE SAFETY (don't
    # leak the env) ranks ABOVE observability (printing the line). The announce can BLOCK on a
    # full, undrained stderr pipe — and a full pipe is one of the wedge scenarios — so if it
    # ran first and blocked, the deadman would force-exit mid-print and this sweep would never
    # run, leaking the very env it exists to reap. Sweeping first guarantees the env is reaped
    # whenever the sweep itself completes within the deadman grace. Costs of this call: it is
    # idempotent, never raises, and a no-op when nothing is pending — but it is NOT free for a
    # plain review run that never imported qa.env, where it triggers a one-time module import at
    # the worst moment (the deadman bounds even that). It is also NOT instantaneous: each
    # teardown is a synchronous `down` spawn bounded by its own timeout, so a long sweep can be
    # cut off mid-flight by the deadman (acceptable last-resort; a re-run's atexit hook or a
    # manual `down` reaps the remainder).
    try:
        from .qa.env import sweep_pending_teardowns

        sweep_pending_teardowns()
    except Exception:  # noqa: BLE001 — best-effort; the deadman + os._exit are the guarantee
        pass
    # If THIS process is a detached `--detach` job's child (review-cli#160 companion;
    # `$REVIEW_JOB_ID` is only set by `_spawn_detached_job`'s child env), record its
    # terminal status BEFORE the `os._exit` below — that exit bypasses every `finally`/
    # `atexit`, including `cli.main`'s own job-finalization wrapper, so without this a
    # backstop-killed detached job would never get its OWN terminal status/exit code
    # recorded; `review status`/`wait` would only ever see it reconciled to the generic
    # "unknown-terminated" once the pid disappears, losing the specific 124 (codex
    # review). Best-effort like every other step here: a job record we can't write must
    # never block the guaranteed exit.
    try:
        import os as _os
        import time as _time

        job_id = _os.environ.get("REVIEW_JOB_ID")
        if job_id:
            from . import jobs

            jobs.write_job(
                job_id,
                status="failed",
                exit_code=BACKSTOP_EXIT_CODE,
                finished_at=_time.time(),
            )
    except Exception:  # noqa: BLE001 — best-effort; the deadman + os._exit are the guarantee
        pass
    # Announce LAST among the fallible steps: a full undrained stderr pipe could block this
    # print, but children + the qa env are already reaped and the deadman guarantees the exit.
    try:
        print(
            f"[review] INTERNAL BACKSTOP fired after {seconds}s "
            f"(hard ceiling {MAX_BACKSTOP_SECONDS}s / 4h) — the run was force-terminated. "
            "This is a last-resort bound, not a normal timeout; a healthy run finishes "
            "in minutes. If you see this, a backend wedged past its per-call deadline.",
            file=stream,
            flush=True,
        )
    except Exception:  # noqa: BLE001 — never let the announcement block the exit
        pass
    os._exit(BACKSTOP_EXIT_CODE)


@contextmanager
def run_backstop(seconds: int | None = None, *, stream=None) -> Iterator[None]:
    """Arm the internal run backstop for the duration of the `with` block.

    `seconds` defaults to `backstop_seconds()` (the clamped 4h-or-less ceiling). A
    daemon `threading.Timer` fires `_fire` if the block has not exited by then; on a
    normal exit the timer is cancelled so it never fires. Daemon so it can never keep
    the process alive on its own. Re-entrant-safe enough for the single top-level
    `main()` use it is built for.
    """
    secs = (
        backstop_seconds()
        if seconds is None
        else max(1, min(int(seconds), MAX_BACKSTOP_SECONDS))
    )
    out = stream if stream is not None else sys.stderr
    timer = threading.Timer(secs, _fire, args=(secs, out))
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()
