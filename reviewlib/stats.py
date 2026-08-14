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
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Schema version for the JSONL records. Bump if the record shape changes
# incompatibly; readers tolerate unknown/missing fields and skip junk lines.
#
# v3 adds "passed": bool — the run's VERDICT (did it come back clean), keyed off
# the mode handler's own exit code (0 = every seat produced a usable verdict /
# board not degraded, nonzero = failure/degraded — see `_run_mode_with_stats` in
# cli.py). A record with no "passed" key has verdict UNKNOWN — either it predates
# v3, or it is a CURRENT record from a mode with no verdict to thread (e.g. `qa`,
# report-only by design). Readers must treat "no key" as unknown either way, never
# crash, and — for the quorum gate specifically — fail-closed (unknown counts as
# not-passed; see quorum_check).
#
# v4 adds "prompt_tokens"/"output_tokens": int — aggregate REAL token usage summed
# across every backend call in the run (panel.py's call tally, fed from
# `ReviewResult.prompt_tokens`/`output_tokens`, which backends.py sets ONLY at the
# REST call sites that parse a provider's own usage payload — gemini/OpenAI-shape
# z.ai|commandcode|openrouter/anthropic). A CLI/agentic backend (codex, opencode,
# omp, claude in CLI mode) never sets these, so it contributes 0, same as any error
# path that never got a successful usage payload. 0 therefore means "no REST usage
# data for this run" — NOT necessarily "zero tokens spent" — and a record with no
# "prompt_tokens"/"output_tokens" key at all predates v4; both cases must be read
# the same way (unknown/absent), never treated as a confirmed zero-token run.
STATS_VERSION = 4
_TASK_CODE_RE = re.compile(r"^[^\s\x00-\x1f\x7f]{1,120}$")


def normalize_task_code(value: str | None) -> str | None:
    """Return a safe task code for stats/log metadata, or None when absent.

    Task codes are identifiers, not prose: one non-whitespace token, no control
    characters. Preserve case because external trackers may be case-sensitive.
    """
    if value is None:
        return None
    code = str(value).strip()
    if not code:
        return None
    if not _TASK_CODE_RE.match(code):
        raise ValueError(
            "task code must be one non-whitespace token, max 120 characters"
        )
    return code


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
    task_code: str | None = None,
    mode: str,
    models: list[str],
    duration_seconds: float,
    ok_count: int,
    fail_count: int,
    started: datetime | None = None,
    passed: bool | None = None,
    prompt_tokens: int = 0,
    output_tokens: int = 0,
) -> bool:
    """Append one run record to the JSONL store. Best-effort: never raises.

    ``models`` is the list ACTUALLY dispatched (so ``pool_size`` reflects reality,
    not what was requested but skipped). ``duration_seconds`` must be the REAL
    wall-clock the caller timed with a monotonic clock. Returns True on a
    successful append, False if anything went wrong (unwritable dir, etc.) — the
    run must never fail because stats couldn't be persisted.

    ``passed`` is the run's VERDICT — the caller's own success/failure criterion
    (e.g. the mode handler's exit code), NOT ``ok_count``/``fail_count`` (those are
    per-BACKEND-CALL technical tallies, unrelated to whether the review came back
    clean). ``None`` means "caller has no verdict to report" and the field is
    omitted from the record entirely, so a reader can tell "unknown" apart from an
    explicit False. Every pre-v3 record looks like this (the field didn't exist
    yet), but so can a CURRENT v3 record from a mode with no verdict to thread
    (e.g. ``qa``, which is report-only — see the ``mode == "qa"`` branch in
    ``cli.py``'s ``_run_mode_with_stats``): a missing ``passed`` key means "verdict
    unknown", not specifically "written before v3".

    ``prompt_tokens``/``output_tokens`` are aggregate REAL token counts for the run
    (see the v4 STATS_VERSION comment above) — default 0, which means "no REST usage
    data", not a confirmed zero-token run.
    """
    try:
        clean_task = normalize_task_code(task_code)
    except ValueError:
        clean_task = None
    record = {
        "v": STATS_VERSION,
        "ts": (started or datetime.now(timezone.utc)).isoformat(),
        "mode": mode,
        "pool_size": len(models),
        "models": list(models),
        "duration_seconds": round(float(duration_seconds), 3),
        "ok_count": int(ok_count),
        "fail_count": int(fail_count),
        "prompt_tokens": int(prompt_tokens),
        "output_tokens": int(output_tokens),
    }
    if clean_task is not None:
        record["task_code"] = clean_task
    if passed is not None:
        record["passed"] = bool(passed)
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
            os.write(
                fd, (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
            )
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
        raw = stats_path().read_text(
            encoding="utf-8"
        )  # stats_path() may raise RuntimeError
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


def _parse_ts(record: dict) -> str:
    ts = record.get("ts")
    return ts if isinstance(ts, str) else ""


def iterations_for_task(task_code: str) -> list[dict]:
    """Return run records for one task, oldest first, annotated with iteration numbers."""
    try:
        clean = normalize_task_code(task_code)
    except ValueError:
        return []
    if clean is None:
        return []
    records = [r for r in _load_records() if r.get("task_code") == clean]
    records.sort(key=_parse_ts)
    out: list[dict] = []
    for index, record in enumerate(records, start=1):
        item = dict(record)
        item["iteration"] = index
        out.append(item)
    return out


def _store_unreadable_error() -> str | None:
    """Return an error string if the stats store cannot be read, else None.

    Distinguishes "store missing/unreadable" from "store readable but the task has
    zero records" so quorum_check can report a more useful message than a bare
    empty result in both cases. Never raises.
    """
    try:
        stats_path().read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 — reporting only, never abort a check
        return f"stats store unreadable: {exc}"
    return None


def quorum_check(task_code: str, *, min_iter: int, min_models: int) -> dict:
    """Compute the quorum verdict for one task: N PASSED iterations across M distinct
    models (self-merge-authority gate; CTO decision tg#7306 #1).

    Only iterations whose record has ``passed is True`` count toward ``min_iter``, and
    ``min_models`` is the distinct-model count among those SAME passed iterations — a
    model that only ever failed/degraded does not help satisfy model diversity either.
    A record with no ``passed`` key is verdict UNKNOWN (either pre-STATS_VERSION-3
    history, or a current record from a mode with no verdict to thread, e.g. `qa`) and
    is deliberately treated as not-passed: unverdicted iterations — old OR current —
    can never satisfy this gate, only verdict-tagged passed runs can. This is
    fail-closed by design, not a bug — see record_run's ``passed`` param.

    Fail-closed (independent of the above): an invalid task code, an unreadable/missing
    store, or zero recorded iterations for the code all yield ``passed: False`` plus an
    ``"error"`` key explaining why — the caller must never treat "no data" as "quorum
    met". A ``min_iter``/``min_models`` floor below 1 is also rejected the same way — 0
    would trivially satisfy the bar via ``0 >= 0`` even for a task with zero passed
    iterations, undermining the whole point of this gate. Validated HERE (not only in
    the CLI wrapper) so a direct library caller can't bypass the floor either.
    """

    def _rejected(error: str) -> dict:
        return {
            "task_code": task_code,
            "passed_iterations": 0,
            "total_iterations": 0,
            "distinct_models_passed": 0,
            "models": [],
            "min_iter": min_iter,
            "min_models": min_models,
            "passed": False,
            "error": error,
        }

    if min_iter < 1 or min_models < 1:
        return _rejected(
            f"min_iter and min_models must both be >= 1 (got min_iter={min_iter} "
            f"min_models={min_models})"
        )
    try:
        clean = normalize_task_code(task_code)
    except ValueError as exc:
        return _rejected(f"invalid task code: {exc}")

    store_error = _store_unreadable_error()
    iterations = iterations_for_task(clean) if clean else []
    # Fail-closed: a record with no "passed" key (pre-v3, verdict unknown) is NOT
    # passed — `is True` (not truthy) so it never accidentally matches None/missing.
    passed_iterations = [item for item in iterations if item.get("passed") is True]
    models: list[str] = []
    for item in passed_iterations:
        for model in item.get("models") or []:
            if isinstance(model, str) and model not in models:
                models.append(model)
    models.sort()

    result = {
        "task_code": clean,
        "passed_iterations": len(passed_iterations),
        "total_iterations": len(iterations),
        "distinct_models_passed": len(models),
        "models": models,
        "min_iter": min_iter,
        "min_models": min_models,
        "passed": len(passed_iterations) >= min_iter and len(models) >= min_models,
    }
    if store_error is not None:
        result["passed"] = False
        result["error"] = store_error
    elif not iterations:
        result["passed"] = False
        result["error"] = f"no recorded review iterations for {clean}"
    return result


def task_summaries() -> list[dict]:
    """Aggregate run-stats by task code, newest task first."""
    groups: dict[str, dict] = {}
    for record in _load_records():
        code = record.get("task_code")
        if not isinstance(code, str) or not code:
            continue
        group = groups.setdefault(
            code,
            {
                "task_code": code,
                "iterations": 0,
                "models": [],
                "modes": set(),
                "first_ts": None,
                "last_ts": None,
                "duration_seconds": 0.0,
                "ok_count": 0,
                "fail_count": 0,
                "prompt_tokens": 0,
                "output_tokens": 0,
            },
        )
        group["iterations"] += 1
        ts = record.get("ts")
        if isinstance(ts, str):
            if group["first_ts"] is None or ts < group["first_ts"]:
                group["first_ts"] = ts
            if group["last_ts"] is None or ts > group["last_ts"]:
                group["last_ts"] = ts
        mode = record.get("mode")
        if isinstance(mode, str) and mode:
            group["modes"].add(mode)
        for model in record.get("models") or []:
            if isinstance(model, str) and model not in group["models"]:
                group["models"].append(model)
        dur = record.get("duration_seconds")
        if isinstance(dur, (int, float)):
            group["duration_seconds"] += float(dur)
        for key in ("ok_count", "fail_count", "prompt_tokens", "output_tokens"):
            value = record.get(key)
            if isinstance(value, int):
                group[key] += value
    summaries = []
    for group in groups.values():
        item = dict(group)
        item["modes"] = sorted(item["modes"])
        item["duration_seconds"] = round(item["duration_seconds"], 3)
        summaries.append(item)
    summaries.sort(key=lambda item: item.get("last_ts") or "", reverse=True)
    return summaries


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
            if isinstance(r.get("duration_seconds"), (int, float))
            and r["duration_seconds"] >= 0
        ]
        return (sum(durs) / len(durs)) if durs else None

    exact = [
        r for r in records if r.get("mode") == mode and r.get("pool_size") == pool_size
    ]
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
