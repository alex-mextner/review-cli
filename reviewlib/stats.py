"""Per-run statistics + a startup ETA, so agents never short-timeout the tool.

review-cli runs are multi-model / multi-round and take MINUTES. Agents that wrap
the command in a short shell `timeout` kill the run before it can finish (most
painfully a brainstorm, which only produces its synthesis at the very end). To
make the expected duration *visible* up front, every run:

  1. appends a structured stat record to a JSONL store when it finishes, and
  2. prints a one-line ETA to stderr at dispatch, computed from past runs of the
     same shape.

Why a NEW store and not the dashboard's session reconstruction
--------------------------------------------------------------
The dashboard (``reviewlib.dashboard.parser``) reconstructs "sessions" by
time-clustering the per-CALL ``*.log`` files. That reader cannot, by its own
admission, recover (mode, pool_size, real wall-clock) cleanly:

  * MODE is *inferred* from the round shape — a plain ``review`` and a
    ``--just-ask`` are both a single r0 call and indistinguishable; a multi-model
    review and a ``--quorum`` both look like a "panel". The ETA must key on the
    EXACT mode (a brainstorm of 4 is nothing like a plain review of 4), so an
    inferred mode is not good enough.
  * POOL SIZE is "distinct backends seen", which a brainstorm (same backend in
    several persona slots) undercounts.
  * DURATION is a proxy (filename stamp -> file mtime), which the parser itself
    caps and warns can be ballooned by an out-of-band touch.

So this module records the GROUND TRUTH the run already knows — the real mode,
the real pool size (models actually dispatched), and the real wall-clock from a
monotonic clock — into its own append-only JSONL. The dashboard's per-call logs
are untouched and keep serving their richer drill-down; this store serves the ETA.

Privacy: the store holds model NAMES only — never prompts, diffs, or keys. It is
created 0600 (same posture as the per-call logs, which can hold secrets) even
though it shouldn't carry any.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Schema version for the JSONL records. Bump if the record shape changes
# incompatibly; readers tolerate unknown/missing fields and skip junk lines.
STATS_VERSION = 1


def stats_path() -> Path:
    """Append-only JSONL store of run records.

    Honors ``$REVIEW_STATS_FILE`` (tests / opt-relocation); otherwise lives next
    to the other review-cli config under ``~/.config/review-cli/run-stats.jsonl``.
    The parent dir is created 0700-ish by the OS default; the file itself is
    forced 0600 on first write.
    """
    override = os.environ.get("REVIEW_STATS_FILE")
    if override:
        p = Path(override).expanduser()
    else:
        p = Path.home() / ".config" / "review-cli" / "run-stats.jsonl"
    return p


def fmt_duration(seconds: float) -> str:
    """Compact human duration: ``6m12s``, ``47s``, ``1h03m``. Always >= ``0s``."""
    total = int(round(max(0.0, seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def record_run(
    *,
    mode: str,
    models: list[str],
    duration_seconds: float,
    ok_count: int,
    fail_count: int,
    started: datetime | None = None,
) -> bool:
    """Append one run record to the JSONL store. Best-effort: never raises.

    ``models`` is the list ACTUALLY dispatched (so ``pool_size`` reflects reality,
    not what was requested but skipped). ``duration_seconds`` must be the REAL
    wall-clock the caller timed with a monotonic clock. Returns True on a
    successful append, False if anything went wrong (unwritable dir, etc.) — the
    run must never fail because stats couldn't be persisted.
    """
    record = {
        "v": STATS_VERSION,
        "ts": (started or datetime.now(timezone.utc)).isoformat(),
        "mode": mode,
        "pool_size": len(models),
        "models": list(models),
        "duration_seconds": round(float(duration_seconds), 3),
        "ok_count": int(ok_count),
        "fail_count": int(fail_count),
    }
    try:
        p = stats_path()  # may raise RuntimeError on an unexpandable ~user / no HOME
        p.parent.mkdir(parents=True, exist_ok=True)
        # O_APPEND keeps concurrent runs from clobbering each other (each writes a
        # whole line). 0600 because we mirror the per-call-log privacy posture.
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            # O_CREAT's 0600 only applies when WE create the file; a run-stats.jsonl that
            # already exists (or a $REVIEW_STATS_FILE the user pre-created) could carry
            # broader perms and keep them forever. fchmod on every write so the 0600
            # privacy guarantee holds for pre-existing files too.
            os.fchmod(fd, 0o600)
            os.write(fd, (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except Exception:  # noqa: BLE001 — stats are best-effort; never abort a finished run
        # Called from a finally in the CLI, so a stats-only failure (unwritable dir,
        # an unexpandable $REVIEW_STATS_FILE that makes stats_path() raise, …) must
        # NEVER turn an otherwise-completed review into a crash.
        return False


def _load_records() -> list[dict]:
    """Read every well-formed JSONL record. Skips junk lines; never raises."""
    out: list[dict] = []
    try:
        raw = stats_path().read_text(encoding="utf-8")  # stats_path() may raise RuntimeError
    except Exception:  # noqa: BLE001 — unreadable/unexpandable store -> no history, never crash
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and "duration_seconds" in rec:
            out.append(rec)
    return out


def estimate_eta(mode: str, pool_size: int) -> dict | None:
    """Average past wall-clock for a run of this shape.

    Keyed PRIMARILY on (mode, pool_size) — a brainstorm of 4 is nothing like a
    plain review of 4 — then falls back to pool_size alone (any mode), then to
    None when there is no usable history at all. Returns a dict
    ``{"avg_seconds", "samples", "basis"}`` where ``basis`` is ``"mode+pool"`` or
    ``"pool"``; None means "no history". Never raises — an unreadable store yields
    None and the caller prints the no-history line.
    """
    records = _load_records()
    if not records:
        return None

    def _avg(matching: list[dict]) -> float | None:
        durs = [
            float(r["duration_seconds"])
            for r in matching
            if isinstance(r.get("duration_seconds"), (int, float)) and r["duration_seconds"] >= 0
        ]
        return (sum(durs) / len(durs)) if durs else None

    exact = [r for r in records if r.get("mode") == mode and r.get("pool_size") == pool_size]
    avg = _avg(exact)
    if avg is not None:
        return {"avg_seconds": avg, "samples": len(exact), "basis": "mode+pool"}

    by_pool = [r for r in records if r.get("pool_size") == pool_size]
    avg = _avg(by_pool)
    if avg is not None:
        return {"avg_seconds": avg, "samples": len(by_pool), "basis": "pool"}

    return None


def eta_line(mode: str, pool_size: int) -> str:
    """One concise stderr line shown at dispatch. Always returns a string.

    With history: ``[review] pool=4 (brainstorm) — typically ~6m12s based on 12
    past runs of this size; do NOT timeout.`` Without history (or an unreadable
    store): a no-data line that still warns about the multi-round / minutes-long
    nature, so the agent never short-timeouts a first-of-its-kind run either.
    """
    try:
        eta = estimate_eta(mode, pool_size)
    except Exception:  # noqa: BLE001 — stats must never block a run
        eta = None
    if eta is None:
        return (
            f"[review] pool={pool_size} ({mode}) — no history yet for this size; "
            "this is multi-model / multi-round, expect MINUTES. Do NOT timeout."
        )
    avg = fmt_duration(eta["avg_seconds"])
    n = eta["samples"]
    plural = "run" if n == 1 else "runs"
    if eta["basis"] == "mode+pool":
        basis = f"based on {n} past {plural} of this size"
    else:
        basis = f"based on {n} past {plural} of pool={pool_size} (any mode)"
    return f"[review] pool={pool_size} ({mode}) — typically ~{avg} {basis}; do NOT timeout."


def announce_eta(mode: str, pool_size: int, stream=None) -> None:
    """Print the ETA line to stderr (or ``stream``) at dispatch. Never raises."""
    try:
        print(eta_line(mode, pool_size), file=stream or sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 — never let the announcement abort a run
        pass
