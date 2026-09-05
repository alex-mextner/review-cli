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

Privacy: the store holds model NAMES, plus (since v4 — see "Diff-identity
binding" below) a normalized REPO IDENTIFIER (a remote URL with credentials
stripped, or a local absolute path when there is no remote) and the list of FILE
PATHS touched by the reviewed diff. It never holds prompts, diff BODIES, or keys.
It is created 0600 (same posture as the per-call logs, which can hold secrets)
even though it shouldn't carry any.

Diff-identity binding (task-code quorum-pollution fix)
--------------------------------------------------------
The self-merge-authority quorum gate (`quorum_check` / `review task CODE
--check`) counts PASSED iterations keyed purely by TASK CODE STRING. Three real
incidents in one session (2026-08-11) showed that string alone is not enough:
task-code reuse across unrelated repos/PRs/typos let one diff's real reviews
silently count toward a completely different diff's quorum — a wrong-repo
review, a deliberate task-code swap between two PRs sharing a parent ticket, and
years of unrelated cross-repo history piling up under one shared code. In all
three the reviewed content had nothing to do with the diff being merged.

So every new record carries `repo_id` (which repo the review ran in) and
`diff_files` (which files the reviewed diff touched). `quorum_check` can then be
handed the CURRENT repo/diff context and flag/exclude iterations that don't
match — see quorum_check's own docstring for the exact matching rules. This is
INTENTIONALLY NOT a diff content-hash equality check: the whole point of
multiple review iterations is that the code changes between them (findings get
fixed, then re-reviewed), so the diff TEXT is expected to differ round to
round — only the REPO and the FILE SET are expected to stay stable across a
task's legitimate iterations. `diff_sha256` is recorded too (an exact hash of
the reviewed diff text) purely as an additional diagnostic signal for
`--detail`/debugging, not as part of the mismatch-detection gate.

Threat-model boundary: this store is a local, self-reported JSONL file — nothing
stops a caller with write access from appending a FRESH record with a fabricated
`repo_id`/`diff_files` that happen to match the current check context (the test
suite does exactly this, deliberately, to simulate history without a real
`review diff` run). This fix closes the "wrong string still matches real but
unrelated history" bug class the 3 incidents actually were — task-code reuse,
typos, and shared-parent-ticket confusion — NOT a cryptographic guarantee against
a fully malicious agent minting a convincing fake record from scratch. Don't
oversell it as the latter in downstream docs/messaging.

Old records (pre-v4) simply lack `repo_id`/`diff_files`/`diff_sha256` — readers
must treat that as "identity unknown, can't verify" (still counted, per the
gate's existing backward-compat contract for missing fields) rather than
crashing or auto-failing them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import seat_cooldown as _seat_cooldown

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
# v4 adds "repo_id" (normalized repo identity) and "diff_files" (sorted list of
# file paths the reviewed diff touched), plus "diff_sha256" (an exact hash of the
# reviewed diff text, diagnostic-only). See the module docstring's "Diff-identity
# binding" section. A record with none of these predates v4 (or ran in a context
# with no resolvable repo/diff) — readers must treat that as identity UNKNOWN,
# not as a mismatch.
STATS_VERSION = 4
_TASK_CODE_RE = re.compile(r"^[^\s\x00-\x1f\x7f]{1,120}$")
# `diff --git a/<path> b/<path>` header. Paths containing spaces/special chars are
# C-quoted by git (`"a/weird\tname"`) and NOT unescaped here — extraction degrades
# to a best-effort miss for those rare paths rather than a wrong path, which is
# fine for this module's purpose (file-SET overlap, not an exact manifest).
_DIFF_GIT_HEADER_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.MULTILINE)
# `git@host:org/repo(.git)` SCP-style SSH remote syntax.
_SCP_STYLE_REMOTE_RE = re.compile(r"^[^@/]+@([^:/]+):(.+)$")
# Cap on `quorum_check`'s `mismatch_details` list (the COUNT in
# `excluded_mismatched_iterations` is always the true total, uncapped) -- a
# task with thousands of polluted iterations (the HYP-858 shape this feature
# targets) must not balloon `--check --json` into a multi-MB payload.
_MISMATCH_DETAILS_CAP = 50


def diff_content_hash(diff_text: str) -> str:
    """Sha256 hex digest of the exact reviewed diff text (diagnostic-only; NOT part
    of the mismatch-detection gate — see the module docstring)."""
    return hashlib.sha256(
        diff_text.encode("utf-8", errors="surrogateescape")
    ).hexdigest()


def extract_diff_files(diff_text: str) -> list[str]:
    """Sorted, deduped file paths touched by a unified git diff's ``a/``/``b/`` sides.

    Best-effort: pure regex over ``diff --git a/<path> b/<path>`` headers, no full
    diff parse. Both sides are collected (not just the post-image) so a rename or a
    delete (``b/`` side ``/dev/null``) still credits the pre-image path. Returns []
    for an empty/header-less diff — never raises.
    """
    files: set[str] = set()
    for a_path, b_path in _DIFF_GIT_HEADER_RE.findall(diff_text or ""):
        for p in (a_path, b_path):
            if p and p != "/dev/null":
                files.add(p)
    return sorted(files)


def normalize_repo_remote(url: str) -> str | None:
    """Normalize a git remote URL to a host/org/repo identity string, or None if
    ``url`` is empty/unparseable.

    Strips credentials, protocol, a trailing ``.git``, and trailing slashes so the
    SAME remote reached over https vs ssh, with or without an embedded token,
    normalizes to one identical id — e.g. ``https://x-access-token:ghp_abc@
    github.com/org/repo.git`` and ``git@github.com:org/repo.git`` both become
    ``github.com/org/repo``. Also lowercases the host (DNS hostnames are
    case-insensitive; ``GitHub.com`` and ``github.com`` are the same remote) and
    drops an explicit default SSH port (``:22``) so ``ssh://git@host:22/org/repo``
    matches the portless ``git@host:org/repo`` form (codex/fable review findings on
    this feature's own PR — both were real "same repo, spurious repo_mismatch"
    false-positive gaps in the ORIGINAL cut of this function; a NON-default port is
    intentionally NOT stripped since it plausibly names a different remote). This
    is an identity key for cross-repo mismatch detection, not a URL a caller should
    try to clone from.
    """
    raw = (url or "").strip()
    if not raw:
        return None
    scp = _SCP_STYLE_REMOTE_RE.match(raw)
    if scp:
        host, path = scp.group(1), scp.group(2)
    else:
        # Strip a protocol scheme (https://, git://, ssh://, http://) if present.
        no_scheme = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", raw)
        # Drop embedded credentials (user[:pass]@host/...).
        no_creds = re.sub(r"^[^/@]+@", "", no_scheme)
        parts = no_creds.split("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return None
        host, path = parts
    host = host.strip().rstrip("/").lower()
    host = re.sub(r":22$", "", host)  # default SSH port -> same remote as portless
    path = path.strip().strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    if not host or not path:
        return None
    return f"{host}/{path}"


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
    repo_id: str | None = None,
    diff_files: list[str] | None = None,
    diff_sha256: str | None = None,
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

    ``repo_id``/``diff_files``/``diff_sha256`` are the diff-identity fields (v4 —
    see the module docstring's "Diff-identity binding" section), each omitted from
    the record when ``None`` (same "unknown, not written as null" convention as
    ``passed``/``task_code``). ``diff_files`` MAY be an empty list (a real run with
    no diff, e.g. a diff-less ``just-ask``) — that is recorded as ``[]``, distinct
    from omitting the key entirely.
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
    }
    if clean_task is not None:
        record["task_code"] = clean_task
    if passed is not None:
        record["passed"] = bool(passed)
    if repo_id is not None:
        record["repo_id"] = repo_id
    if diff_files is not None:
        record["diff_files"] = list(diff_files)
    if diff_sha256 is not None:
        record["diff_sha256"] = diff_sha256
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


def _classify_iteration_identity(
    item: dict, repo_id: str, current_files: frozenset[str]
) -> tuple[str, str | None]:
    """Classify one PASSED iteration against the CURRENT check context.

    ``current_files`` is a pre-built ``frozenset`` (hoisted ONCE by the caller,
    not rebuilt per iteration — this loop runs once per PASSED iteration, which
    for the exact "years of history" pollution shape this feature targets can be
    thousands; review finding on this feature's own PR).

    Returns ``(bucket, reason)``:
      * ``"verified"`` — the iteration's recorded repo matches ``repo_id``, and
        either it has no recorded file set, ``current_files`` (the current
        context) is empty, or the two file sets share at least one file.
        ``reason`` is None.
      * ``"mismatched"`` — the recorded repo differs (``reason="repo_mismatch"``),
        or the repo matches but BOTH file sets are non-empty and share NO file at
        all (``reason="diff_mismatch"``) — this is the pattern all three real
        incidents shared: iterations reviewing manifestly different content
        counting toward an unrelated task's quorum.
      * ``"unverifiable"`` — the iteration predates diff-identity recording (no
        ``repo_id`` on the record); can't be verified either way, ``reason`` None.
    """
    item_repo = item.get("repo_id")
    if not isinstance(item_repo, str) or not item_repo:
        return "unverifiable", None
    if item_repo != repo_id:
        return "mismatched", "repo_mismatch"
    item_files = item.get("diff_files")
    if current_files and isinstance(item_files, list) and item_files:
        if current_files.isdisjoint(item_files):
            return "mismatched", "diff_mismatch"
    return "verified", None


def _sort_passed_iterations_into_buckets(
    passed_iterations: list[dict], repo_id: str | None, diff_files: list[str] | None
) -> tuple[list[dict], list[dict], list[dict]]:
    """Split PASSED iterations into (verified, mismatched_detail_dicts, unverifiable).

    When ``repo_id`` is None (the caller supplied no check context — e.g. a direct
    library call with no cwd to resolve), verification is skipped entirely: every
    passed iteration is "verified" as-is, matching this function's pre-diff-identity
    behavior exactly (no new keys, no filtering) for backward compatibility.
    """
    if repo_id is None:
        return list(passed_iterations), [], []
    current_files = frozenset(diff_files or ())
    verified: list[dict] = []
    mismatched: list[dict] = []
    unverifiable: list[dict] = []
    for item in passed_iterations:
        bucket, reason = _classify_iteration_identity(item, repo_id, current_files)
        if bucket == "verified":
            verified.append(item)
        elif bucket == "mismatched":
            # recorded_diff_files is the actual evidence for a "diff_mismatch" (the
            # repo already matched by definition in that case, so recorded_repo_id
            # alone tells an operator nothing about WHY it was excluded — review
            # finding on this feature's own PR).
            mismatched.append(
                {
                    "iteration": item.get("iteration"),
                    "reason": reason,
                    "recorded_repo_id": item.get("repo_id"),
                    "recorded_diff_files": item.get("diff_files"),
                    "ts": item.get("ts"),
                }
            )
        else:
            unverifiable.append(item)
    return verified, mismatched, unverifiable


def _distinct_models(items: list[dict]) -> list[str]:
    """Sorted, deduped model list across a set of run-stats iterations."""
    models: list[str] = []
    for item in items:
        for model in item.get("models") or []:
            if isinstance(model, str) and model not in models:
                models.append(model)
    models.sort()
    return models


def _finalize_quorum_result(
    result: dict,
    *,
    store_error: str | None,
    iterations: list[dict],
    mismatched: list[dict],
    clean: str,
) -> None:
    """Set the terminal ``passed``/``error`` keys on an in-progress quorum result, in
    fail-closed priority order: an unreadable store, then zero history, then (only if
    the bar still isn't met) surfacing that some history was excluded as mismatched --
    mutates ``result`` in place."""
    if store_error is not None:
        result["passed"] = False
        result["error"] = store_error
    elif not iterations:
        result["passed"] = False
        result["error"] = f"no recorded review iterations for {clean}"
    elif mismatched and not result["passed"]:
        # Bar not met AND some history was excluded as mismatched -- surface WHY in
        # the human-facing error too (not just the diagnostic keys), so an operator
        # staring at "0/3 iterations" understands it isn't simply "never reviewed".
        result["error"] = (
            f"{len(mismatched)} recorded iteration(s) for {clean} were excluded: "
            "recorded repo/diff did not match the code currently being checked "
            "(see mismatch_details)"
        )


def quorum_check(
    task_code: str,
    *,
    min_iter: int,
    min_models: int,
    repo_id: str | None = None,
    diff_files: list[str] | None = None,
) -> dict:
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

    ``repo_id``/``diff_files`` (both optional) are the CURRENT check context — "what
    repo/diff are we deciding whether to merge right now" — and enable diff-identity
    verification (v4, see the module docstring): a passed iteration whose OWN recorded
    ``repo_id`` differs, or whose recorded ``diff_files`` shares no file with the
    current ``diff_files``, is EXCLUDED from ``passed_iterations``/``models`` rather
    than silently counted (this is what closes the three real quorum-pollution
    incidents this field exists for). Iterations with no recorded identity (pre-v4,
    or a run with no resolvable repo) are "unverifiable" and still count, preserving
    the old behavior for history that predates this field. When ``repo_id`` is None
    (the caller has no check context to give — direct library callers that omit it,
    exactly as before this parameter existed), NO verification is attempted: no
    mismatch/unverifiable keys, every passed iteration counts. Passing a check context
    is what turns the gate on. (review-cli#221: independently of ``repo_id``, a
    ``stalled_models`` key is added whenever the bar ISN'T met and an attempted model
    is currently cooling down — this is the one exception to "identical to pre-v4
    shape when passing," since it doesn't depend on diff-identity at all.)

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
    verified, mismatched, unverifiable = _sort_passed_iterations_into_buckets(
        passed_iterations, repo_id, diff_files
    )
    # Gate-worthy = verified (identity checked out) + unverifiable (no identity to
    # check, so unchanged from pre-v4 behavior) — mismatched iterations are excluded.
    gate_iterations = verified + unverifiable
    models = _distinct_models(gate_iterations)

    result = {
        "task_code": clean,
        "passed_iterations": len(gate_iterations),
        "total_iterations": len(iterations),
        "distinct_models_passed": len(models),
        "models": models,
        "min_iter": min_iter,
        "min_models": min_models,
        "passed": len(gate_iterations) >= min_iter and len(models) >= min_models,
    }
    # review-cli#221: when the bar isn't met, name the SPECIFIC model(s) that were
    # actually attempted for this task and are CURRENTLY cooling down (an unavailable
    # sentinel or a session-limit/usage-credits notice — the two chronic signals
    # seat_cooldown records; a plain timeout does NOT currently start a cooldown, see
    # seat_cooldown.py's own docstring) — a bare "2/3 models" count leaves an operator
    # guessing which seat is the problem. Sourced from `seat_cooldown` (the same live signal dispatch
    # itself checks), scoped to models this task code actually tried (attempted_models,
    # from ALL recorded iterations, not just passed ones) so this never lists an
    # unrelated seat that simply isn't part of this task's history. Always computed
    # when the bar isn't met (not gated behind `repo_id is not None` like the
    # verification keys above) since it doesn't depend on diff-identity at all.
    if not result["passed"]:
        attempted_models = _distinct_models(iterations)
        stalled = []
        # GLM round-4 review finding: N reads of the same cooldown-store file for N
        # attempted models — negligible today (CLI check command, M is small), but a
        # real N+1 shape. Deferred: low value here per the finding's own assessment;
        # worth a batch `active_cooldowns(models)` API in seat_cooldown if this ever
        # gets called in a loop over many task codes.
        for m in attempted_models:
            cd = _seat_cooldown.active_cooldown(m)
            if cd is not None:
                stalled.append(
                    {
                        "model": m,
                        "reason": cd["reason"],
                        "remaining_seconds": round(cd["remaining_seconds"]),
                        "consecutive_failures": cd["fail_count"],
                    }
                )
        if stalled:
            result["stalled_models"] = stalled
    # Verification-diagnostic keys are added ONLY when a check context was actually
    # supplied — omitted entirely otherwise, so a caller that never opts in sees the
    # exact pre-v4 shape (no new keys) — see this function's own docstring.
    if repo_id is not None:
        result["verified_iterations"] = len(verified)
        result["unverifiable_iterations"] = len(unverifiable)
        # The COUNT is always the true total (this is what the gate math above
        # already used); only the detail LIST is capped below, so a task with
        # thousands of polluted iterations (the exact HYP-858 shape this feature
        # targets) can't balloon --check --json into a multi-MB payload (GLM
        # review finding on this feature's own PR) — the count alone is enough
        # for a machine gate, and a human debugging the exclusion has --detail.
        result["excluded_mismatched_iterations"] = len(mismatched)
        result["mismatch_details"] = mismatched[:_MISMATCH_DETAILS_CAP]
        if len(mismatched) > _MISMATCH_DETAILS_CAP:
            result["mismatch_details_truncated"] = True
    _finalize_quorum_result(
        result,
        store_error=store_error,
        iterations=iterations,
        mismatched=mismatched,
        clean=clean,
    )
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
        for key in ("ok_count", "fail_count"):
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
