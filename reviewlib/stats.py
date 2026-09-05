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
from .config import REVIEW_ROLES

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
    roles: list[str] | None = None,
) -> bool:
    """Append one run record to the JSONL store. Best-effort: never raises.

    ``models`` is the list ACTUALLY dispatched (so ``pool_size`` reflects reality,
    not what was requested but skipped). ``duration_seconds`` must be the REAL
    wall-clock the caller timed with a monotonic clock. Returns True on a
    successful append, False if anything went wrong (unwritable dir, etc.) — the
    run must never fail because stats couldn't be persisted.

    ``roles`` (review-cli#221) is the board ROLE each USABLE seat that produced a
    verdict was reviewing under, for callers that carry per-seat role info
    (currently: the review-diff/visual board dispatch, whose seats each carry a
    ``REVIEW_ROLES`` key — see ``panel.FailoverOutcome.usable_roles``). It IS
    index-aligned with ``models`` for every current producer (``usable_models``/
    ``usable_roles`` are appended in the same loop iteration in
    ``panel.run_board_with_failover``; the CLI's explicit-board branches build
    both from the same seat list in the same order — see ``cli._ran_models``/
    ``_ran_roles``), and ``quorum_check``'s monoculture guard
    (``_models_behind_role_coverage``) relies on that alignment to credit only a
    model whose OWN role is valid, not merely one sharing a record with a
    valid-role seat. ``record_run`` itself DOES enforce this at write time (see
    the ``len(roles) == len(models)`` check below — a mismatched-length ``roles``
    is dropped, not partially written), so every record THIS module writes is
    aligned; the ``zip``-truncates-gracefully fallback in
    ``_models_behind_role_coverage`` matters only for pre-existing/externally-
    written records this function's own guard predates, which can still supply a
    mismatched pair (``zip`` truncates to the shorter list, under- never over-
    crediting — the fail-closed direction) — it just won't get full role credit.
    ``None``
    (the default) means "this mode/caller has no role concept" and the key is
    omitted entirely — same "unknown, not written as an empty list" convention as
    ``passed``/``repo_id`` — so a ``--min-roles`` gate can tell "never recorded a
    role" apart from "recorded zero roles". Modes with no board (quorum/just-ask/
    brainstorm/qa) never pass this.

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
    if roles is not None:
        # Round-7 review finding (Fable): `_distinct_roles` (the ENFORCING counter
        # behind --min-roles) has no reference to `models` at all — it just iterates
        # `roles` independently, unlike the advisory `_models_behind_role_coverage`,
        # which zips the two and so fails closed (under- never over-credits) on a
        # length mismatch. That makes the enforcement path fail OPEN if a future/
        # buggy/adversarial producer ever wrote more roles than models (e.g.
        # `models: ["codex"], roles: ["architect", "security"]`), inflating role
        # coverage from a single seat — backwards for a self-merge-authority gate.
        # No CURRENT producer can write mismatched lengths (`panel.
        # FailoverOutcome.usable_models`/`usable_roles` are appended in lockstep;
        # `_ran_models`/`_ran_roles` build both from the same seat list in the same
        # order), so this is defense-in-depth at the one write chokepoint, not a
        # fix for a reachable bug — best-effort/never-raise like every other
        # validation in this function: silently omit `roles` (not partially write
        # it) rather than persist a shape whose length lies about which seats it
        # actually describes.
        if len(roles) == len(models):
            record["roles"] = list(roles)
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


def _record_ts_str(record: dict) -> str:
    """The raw ``ts`` sort key for `iterations_for_task` -- NOT related to
    `parse_iso_ts` below despite the similar name (GLM review finding on this
    feature's own PR: the two used to be named `_parse_ts`/`parse_iso_ts`, a
    near-miss for a call-site mix-up since one returns the raw string and the
    other a parsed `datetime | None`). This one exists only to give
    `list.sort(key=...)` a lexicographically-sortable string (ISO-8601
    timestamps sort correctly as plain strings); it never parses anything."""
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
    records.sort(key=_record_ts_str)
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
    """Classify one iteration against the CURRENT check context.

    Despite the name of its main caller (``_sort_passed_iterations_into_buckets``),
    this function itself never reads ``item.get("passed")`` — it purely compares
    recorded repo/diff identity, so it is safe to reuse on iterations of ANY
    verdict (see ``_iterations_excluding_mismatched``, which does exactly that for
    the bare-check zero-role fallback's "has any role data" check).

    ``current_files`` is a pre-built ``frozenset`` (hoisted ONCE by each caller
    via ``_current_files_frozenset``, not rebuilt per iteration — a caller's loop
    can run once per PASSED iteration (``_sort_passed_iterations_into_buckets``)
    or once per iteration of ANY verdict (``_iterations_excluding_mismatched``),
    either of which, for the exact "years of history" pollution shape this
    feature targets, can be thousands; review finding on this feature's own PR).

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


def _current_files_frozenset(diff_files: list[str] | None) -> frozenset[str]:
    """Shared ``diff_files -> frozenset`` construction for diff-identity
    classification (``frozenset(diff_files or ())``) — used by every caller of
    `_classify_iteration_identity` so independent call sites can never diverge on
    how a ``None``/absent ``diff_files`` is treated (Fable review finding: two
    call sites each inlining this expression separately risked silent drift if
    one were ever edited without the other)."""
    return frozenset(diff_files or ())


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
    current_files = _current_files_frozenset(diff_files)
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


def _distinct_roles(items: list[dict]) -> list[str]:
    """Sorted, deduped board-role list across a set of run-stats iterations
    (review-cli#221 --min-roles). Mirrors ``_distinct_models``, reading the
    ``roles`` key instead of ``models`` — with one deliberate difference: only
    roles that are a real ``REVIEW_ROLES`` key count.

    An iteration with no recorded ``roles`` key (a mode with no role concept, or
    history that predates this field) contributes nothing — it neither helps nor
    hurts a ``--min-roles`` count, same as a pre-v4 record and ``repo_id`` in the
    diff-identity check.

    Codex review finding: a config ``board:`` entry may carry an UNKNOWN role
    string (``config._normalize_board_reviewer`` keeps it but degrades the seat
    to the generic prompt — no distinct lens — and logs a warning; see that
    function's docstring). Counting an unknown/typo'd role string here would let
    N seats that all reviewed under the SAME generic prompt satisfy a
    ``--min-roles N`` self-merge-authority gate as if they were N genuinely
    distinct facets — exactly the "distinct lenses" contract this gate exists to
    enforce. Filtering to ``REVIEW_ROLES`` keeps the count honest: only a role
    that actually carries its own lens text can ever count as covered."""
    roles: list[str] = []
    for item in items:
        for role in item.get("roles") or []:
            if isinstance(role, str) and role in REVIEW_ROLES and role not in roles:
                roles.append(role)
    roles.sort()
    return roles


def _has_any_role_data(items: list[dict]) -> bool:
    """True iff at least one item recorded ANY role data (a non-empty ``roles``
    list) -- used by `quorum_check`'s bare-check zero-role fallback (Alex's
    review-cli#246 follow-up, see the module-level constant `_DEFAULT_QUORUM_FLOOR`
    usage below) to tell "genuinely never recorded a role" apart from "recorded
    some roles, just not enough to meet the floor". Only the former falls back to
    the old model-counting default -- the latter must fail on its own merits via
    role-counting, exactly like PR #246 left it.

    Deliberately checks for a non-empty ``roles`` LIST, not whether it contains a
    valid ``REVIEW_ROLES`` name (unlike `_distinct_roles`, which is stricter
    because it decides what counts TOWARD the floor). An unrecognized/typo'd role
    string is still evidence the task WAS reviewed through a role-recording mode
    (the normal ``review diff`` board), which is the only fact this check needs --
    whether that role earns credit is a separate question `_distinct_roles`
    already answers correctly. Truthiness (not merely ``is not None``) is
    deliberate too (round-review finding, Opus/Fable): `record_run` can in
    principle persist ``"roles": []`` for a caller that supplies matching empty
    ``models``/``roles`` lists (its own length-match guard permits ``0 == 0``) --
    though no CURRENT producer's own truthy-result guard (see cli.py's
    `_run_mode_with_stats`) ever exercises that path in practice, an empty list
    carries no actual role information and must not count as "role data present"
    either way; treating it as sufficient would re-create the exact
    guaranteed-fail-with-no-escape this feature exists to close."""
    return any(item.get("roles") for item in items)


# The exact instant role-tracking became POSSIBLE: PR #246 (commit
# 7a8b64f2eb00178acddbbc325a9c1cf8c53b0dc8), merged 2026-08-21T10:52:25Z, is the
# commit that added `record_run`'s `roles` param and the "roles" key to the JSONL
# record shape in the first place -- no record written before this instant could
# carry role data no matter which review mode produced it, because the code to
# write one didn't exist yet. This is a real git-log fact, not a guess: `git log
# -S'"roles"' -- reviewlib/stats.py` shows the "roles" key was introduced in that
# one commit, nothing earlier -- cross-checked (Opus round-review finding on
# this feature's own PR, worried an EARLIER PR could have written role data
# first) two ways: (1) `git log --all --oneline --reverse -- reviewlib/stats.py`
# lists every commit that ever touched this file, in order, and 7a8b64f is the
# only one before this PR's own that shows up in the `-S'"roles"'` search
# above; (2) "review-cli#221", referenced throughout this module's comments as
# where the role concept originated, is a GitHub ISSUE (closed, a *different*
# topic -- gate tolerance for a flaky model), not a merged PR that could have
# landed code -- the actual "roles" implementation landed entirely in #246.
#
# Used by `_all_iterations_predate_role_tracking` (review-cli#252 follow-up,
# Alex's explicit request) to tell a GENUINELY pre-role-tracking task -- role data
# literally could not have existed at the time it was reviewed -- apart from a
# task that simply never CHOSE a role-recording mode despite having the option.
# See `quorum_check`'s "Bare-check zero-role fallback" docstring section for why
# this distinction matters: only the former may silently reuse the old
# model-counting default; the latter must not, or the role-based gate becomes
# permanently optional for anyone who reviews via quorum/just-ask/brainstorm/qa
# instead of `review diff`.
#
# KNOWN LIMITATION (Fable round-review finding on this feature's own PR): this
# is a WALL-CLOCK cutoff keyed on when PR #246 merged in THIS repository's own
# history, not a per-record marker of which code version actually wrote it. A
# hypothetical separate deployment of review-cli that upgrades past commit
# 7a8b64f some time after 2026-08-21T10:52:25Z would have PRE-#246-code-written
# history carrying POST-cutoff timestamps -- misclassified as case (b) even
# though it's genuinely case (a). This fails CLOSED (denies the free pass
# rather than granting one it shouldn't), so it's an availability annoyance for
# that scenario, not a correctness hole -- and review-cli today has exactly one
# real deployment (this repo, upgraded via `git pull`/worktree, not a
# separately-versioned distributed package), so the scenario is currently
# theoretical. A more robust discriminator would be a per-record schema/format-
# version marker written by `record_run` itself (there is no such field today
# -- checked via `git log -S` before choosing this design, per Alex's explicit
# instruction to verify rather than invent one) rather than comparing against
# an upstream merge instant; worth revisiting if review-cli ever ships as a
# distributed package with staggered upgrades.
#
# The constant below is verified, not eyeballed (Fable round-review finding on
# this feature's own PR: confirm it matches the commit's OWN timestamp, since a
# GitHub "merged at" can in principle differ from author/committer date after a
# rebase). `git show -s --format='%aI%n%cI' 7a8b64f` gives AuthorDate =
# CommitDate = 2026-08-21T13:52:25+03:00, which converts to
# 2026-08-21T10:52:25Z -- exactly `gh pr view 246 --json mergedAt` and exactly
# the literal below. `7a8b64f` has a single parent (not a 2-parent merge
# commit), so its own timestamp is what actually landed on `main`.
_ROLE_TRACKING_CUTOFF = datetime(2026, 8, 21, 10, 52, 25, tzinfo=timezone.utc)


def parse_iso_ts(ts: object) -> datetime | None:
    """Parse an ISO-8601 timestamp, or ``None`` if `ts` isn't a non-empty string or
    fails to parse. Normalizes a trailing ``Z`` (Zulu/UTC shorthand) to ``+00:00``
    first — ``datetime.fromisoformat`` only accepts ``Z`` directly from Python 3.11
    onward, but this project declares ``requires-python = ">=3.9"``.

    GLM review finding on this feature's own PR: this exact fix (strip-trailing-Z,
    then `fromisoformat`) used to be copied independently at THREE call sites across
    two modules, and two of them had already drifted apart (`cli.py`'s
    `_parse_task_record_started` used `.replace("Z", "+00:00")` — rewrites ANY `Z`
    anywhere in the string, not just a trailing one — while the other two used the
    suffix-strip form here). One shared helper, one fix surface.

    Deliberately does NOT default a naive (tz-less) result to UTC or otherwise
    interpret the parsed value — that's a caller decision (some callers reject naive
    timestamps outright as unverifiable, others assume UTC for a field this module's
    own writer always emits tz-aware), so every caller still inspects `.tzinfo`
    itself before trusting the result."""
    if not isinstance(ts, str) or not ts:
        return None
    normalized = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _iteration_predates_role_tracking(item: dict) -> bool:
    """True iff ``item``'s own ``ts`` is a parseable, timezone-aware timestamp
    strictly before ``_ROLE_TRACKING_CUTOFF`` (PR #246's merge instant).

    Fail-closed on anything that can't be verified: a missing, malformed, or
    naive (no UTC offset) ``ts`` does NOT count as "provably old" -- an
    unparseable/ambiguous date proves nothing either way, so it can never help
    grant the legacy fallback via `_all_iterations_predate_role_tracking` below.
    Every producer writes ``ts`` as ``datetime.now(timezone.utc).isoformat()``
    (see `record_run`'s single caller, `cli.py`'s `_run_mode_with_stats`), which
    always parses back timezone-aware -- and this is not merely the CURRENT
    behavior: `git log -S'datetime.now(timezone.utc)).isoformat()' --
    reviewlib/stats.py` shows this exact line has been unchanged since
    `record_run`'s original introduction (commit 4f3bb98), the entire history of
    this store. Fable round-review finding on this feature's own PR asked this
    to be verified (not merely assumed) precisely because case (a)'s whole
    population is historical -- confirmed, so the naive/malformed/missing
    branch below exists only for a hypothetical externally-written or corrupted
    record, never a real gap in this repo's own history.

    "Provably"/"genuinely" old here means "recorded, via this store's existing
    self-reported ``ts`` field, as predating the cutoff" -- the SAME trust model
    every other field in this JSONL store already relies on (`record_run`'s
    ``started`` param, and therefore ``ts``, is caller-supplied; a write-access
    caller could in principle backdate one, exactly as this repo's own test
    suite deliberately does via `started=LEGACY_TS`). This is not new attack
    surface for the self-merge-authority gate -- everything the store records
    (models, verdicts, roles) already carries this same level of trust -- so
    "provably" is used in the sense of "verified against the record's own
    claim," not "cryptographically unforgeable" (Fable round-review finding on
    this feature's own PR).

    Normalizes a trailing ``Z`` (Zulu/UTC shorthand) to ``+00:00`` before
    parsing -- ``datetime.fromisoformat`` only accepts ``Z`` directly from
    Python 3.11 onward, but this project declares ``requires-python = ">=3.9"``.
    Without this, a genuinely pre-cutoff record using ``Z`` (a valid,
    unambiguous UTC marker -- unlike an offsetless naive string, which stays
    fail-closed below) would raise on 3.9/3.10 and get wrongly denied case (a)'s
    fallback. Delegates that normalization to `parse_iso_ts` (shared with
    `_resolve_stat_since` and `_parse_task_record_started` in `cli.py`, which used to
    each carry their own independent copy of this exact fix -- GLM review finding on
    this feature's own PR) because `record_run` itself never emits ``Z``
    (`datetime.isoformat()` on a UTC-aware value always writes ``+00:00``), so this
    guards only against externally-written or historical records in an unverified
    format, not anything this module's own writer produces (codex round-review
    finding on this feature's own PR).
    """
    parsed = parse_iso_ts(item.get("ts"))
    if parsed is None or parsed.tzinfo is None:
        return False
    return parsed < _ROLE_TRACKING_CUTOFF


def _all_iterations_predate_role_tracking(items: list[dict]) -> bool:
    """True iff EVERY item in ``items`` provably predates role-tracking (case (a)
    in `quorum_check`'s "Bare-check zero-role fallback" docstring section) --
    i.e. this task's entire in-scope history could not possibly carry role data,
    so the old model-counting fallback is the best this gate can ever do for it.

    An empty ``items`` returns False, not True -- this function's one caller
    (`quorum_check`'s bare-check zero-role fallback) only ever calls it on an
    already-nonempty ``role_data_population``; "no history at all" is a
    different, pre-existing fail-closed case (`_finalize_quorum_result`'s
    zero-iterations branch), not "all history is old"."""
    return bool(items) and all(_iteration_predates_role_tracking(i) for i in items)


def _iterations_excluding_mismatched(
    iterations: list[dict], repo_id: str | None, diff_files: list[str] | None
) -> list[dict]:
    """All ``iterations`` (of ANY verdict -- passed, failed, or unverdicted) except
    those diff-identity-MISMATCHED against the current check context.

    Used ONLY by the bare-check zero-role fallback's "has this task ever recorded
    role data" check (`_has_any_role_data`) -- deliberately a DIFFERENT population
    than `gate_iterations` (which is PASSED-only). A role-tagged iteration that
    FAILED, or was never verdicted, is still real evidence the task went through a
    role-recording review mode; excluding it from the "any role data" check (round-
    review finding, Opus/Fable, independently) would let the fallback fire for a
    task that genuinely has role-recording history, just none of it happened to
    pass -- contradicting this feature's own documented "even ONE role-tagged
    iteration is unaffected" guarantee and, in the worst case, silently letting
    model-counting decide a self-merge-authority gate that role coverage was
    supposed to govern.

    Mismatched iterations ARE still excluded, though (unlike the "include every
    verdict" relaxation above) -- for the OPPOSITE reason `gate_iterations`
    excludes them: a role-tagged iteration recorded against a genuinely DIFFERENT
    repo/diff (the exact task-code-reuse/collision pollution
    `_sort_passed_iterations_into_buckets` exists to guard against) has nothing to
    do with THIS check's task, and letting it force the CURRENT, unrelated check
    into role-mode (where it would then see zero roles and fail) would silently
    reopen that same cross-repo pollution bug class through a new angle -- the
    counting MODE, not the iteration count itself.

    When ``repo_id`` is None (no check context given -- a direct library call that
    omits it, exactly as elsewhere in this module), no filtering is applied: every
    iteration is returned as-is, matching the "verification is opt-in" convention
    used throughout diff-identity binding.

    Its caller passes only the NON-passed remainder of a task's iterations, not
    the full list -- the PASSED-and-non-mismatched subset is exactly
    ``gate_iterations`` (already computed by `_sort_passed_iterations_into_buckets`
    moments earlier with identical inputs), so re-classifying it here would be
    pure duplicate work on the exact "thousands of iterations" history shape this
    module's docstrings call out as performance-relevant (GLM/Fable review
    finding, raised across two rounds)."""
    if repo_id is None:
        return list(iterations)
    current_files = _current_files_frozenset(diff_files)
    return [
        item
        for item in iterations
        if _classify_iteration_identity(item, repo_id, current_files)[0] != "mismatched"
    ]


def _models_behind_role_coverage(items: list[dict]) -> list[str]:
    """Distinct models that actually EARNED a VALID (``REVIEW_ROLES``) role — the
    population the ``--min-roles`` monoculture guard in ``quorum_check``'s
    suggestion must check diversity against.

    Round-3 review finding (Fable): the guard originally checked
    ``len(models) >= 2`` across EVERY passed iteration, including role-LESS ones
    (quorum/just-ask/brainstorm). That let a role-less iteration on a second model
    satisfy "2 distinct models" while 100% of the counted ROLE coverage still came
    from a single other model — exactly the monoculture shape the guard exists to
    block, just laundered through an unrelated iteration.

    Round-4 review finding (Codex), sharper still: scoping to "this ITERATION
    contributed >=1 valid role" is not enough EITHER — a single multi-seat record
    (e.g. one `review diff` call recording every board seat's ``models``/``roles``
    together) can mix a valid-role seat with an unknown-role seat, and crediting
    every model in that record (not just the one that earned the valid role) lets
    the unknown-role seat's model launder in as "diverse" too. ``models``/``roles``
    ARE index-aligned per seat for every current producer (`panel.
    FailoverOutcome.usable_models`/`usable_roles`, and the CLI's `_ran_models`/
    `_ran_roles` explicit-board branches both iterate the SAME seat list in the
    SAME order — see their docstrings), so ``zip`` pairs each model with the SAME
    seat's own role and credits ONLY a model whose OWN role is valid — never a
    model that merely shares a record with a valid-role seat. A record from a
    hypothetical future producer that doesn't honor this alignment just
    under-credits (``zip`` stops at the shorter list) rather than over-crediting —
    the fail-closed direction for a self-merge-authority gate."""
    models: list[str] = []
    for item in items:
        item_models = item.get("models") or []
        item_roles = item.get("roles") or []
        for model, role in zip(item_models, item_roles):
            if (
                isinstance(model, str)
                and isinstance(role, str)
                and role in REVIEW_ROLES
                and model not in models
            ):
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
    mutates ``result`` in place.

    Deliberately does NOT special-case the review-cli#252 follow-up's
    ``role_tracking_gap`` (case (b), "not genuinely old" -- see `quorum_check`'s
    docstring): that message is surfaced as its own additive ``result`` key and a
    printed note/hint line, the same treatment ``quorum_mode_fallback`` itself
    already gets, rather than promoted into this ``error`` slot -- promoting it
    would silently move the primary NOT-met line from stdout to stderr for the
    ordinary "task genuinely doesn't have enough role-tracked reviews yet" shape
    (no different from an ordinary "2/3 distinct roles" shortfall, which also
    never becomes an "error" here), churning stream expectations no other
    shortfall-only denial disturbs."""
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


_DEFAULT_QUORUM_FLOOR = 3  # the "at least 3 distinct opinions" floor -- shared by
# --min-models' historical default AND --min-roles' new default below (review-cli#246).


def quorum_check(
    task_code: str,
    *,
    min_iter: int,
    min_models: int | None = None,
    min_roles: int | None = None,
    repo_id: str | None = None,
    diff_files: list[str] | None = None,
) -> dict:
    """Compute the quorum verdict for one task: N PASSED iterations across M distinct
    models (self-merge-authority gate; CTO decision tg#7306 #1) and/or across M distinct
    BOARD ROLES (review-cli#221) — the role floor governs whenever ``min_roles`` is
    either explicitly given OR defaulted-in (review-cli#246, see below); see the
    explicit-vs-default semantics below for exactly when each floor governs.

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

    ``min_roles`` (optional, review-cli#221) switches the SECOND half of the gate from
    counting distinct model-name strings to counting distinct board ROLES covered by
    ``gate_iterations`` — a role filled by the board's shortage-resilience duplicate-
    model pad (a model reused onto an otherwise-empty role slot, see
    ``config.select_pool_with_reuse`` / PR #207) counts once per role there, exactly
    like any other role, instead of collapsing into the model string ``_distinct_models``
    already counted. Only a role that is an actual ``REVIEW_ROLES`` key counts (an
    unknown/typo'd role string is kept for its own diagnostics but earns no lens
    credit). Iterations from a mode with no role concept (quorum / just-ask /
    brainstorm / qa — only the review-diff/visual board dispatch records per-seat
    roles today) contribute no roles, same fail-closed-by-omission treatment as an
    unverdicted iteration.

    ``min_models`` and ``min_roles`` semantics (review-cli#246, fixing a review-cli#221
    round-3 finding — see below): both parameters default to ``None``, which means
    "the caller did not ask for this floor" — NOT "the caller asked for a 0/low value".
    This distinction is what lets the function tell an explicit request apart from a
    default, the same way the CLI's own argparse layer already could (see
    ``reviewlib.cli``'s ``--check`` handling) but couldn't PASS DOWN before this fix,
    since a plain ``int`` default here was indistinguishable from an explicit int of
    the same value.

      * Only ``min_models`` explicitly given (``min_roles`` omitted): reproduces the
        exact pre-#221 PASS/FAIL semantics — a strict distinct-model-name count
        decides ``passed`` (no ``roles``/``distinct_roles_passed``/``min_roles`` keys
        in the result). The result SHAPE gains one new key here (review-cli#246's
        ``min_models_advisory``, see below) — the gate's decision logic is unchanged,
        the payload is not byte-identical.
      * Only ``min_roles`` explicitly given (``min_models`` omitted): reproduces the
        pre-#246 ``--min-roles`` pass/fail behavior — distinct BOARD ROLES decide
        ``passed``. ``distinct_models_passed``/``models`` are still computed/reported
        for visibility (no ``min_models`` key itself, since no floor was requested).
      * BOTH explicitly given: an explicitly requested floor is now ALWAYS enforced —
        ``passed`` requires ``min_iter`` AND the model floor AND the role floor to all
        be met (Alex's policy: "if a model limit is explicitly set, it must work").
        This closes the round-3 finding below.
      * NEITHER given (the true default — no flags at all): the default switches to a
        ROLE-based check at ``_DEFAULT_QUORUM_FLOOR`` (the SAME numeric value
        ``--min-models`` used to default to), not a model-count check — Alex's
        direction that role-based counting is the new default everywhere, with no
        default model-count floor; an explicit model-count request is still always
        honored per the bullet above. FALLBACK (Alex's review-cli#246 follow-up,
        NARROWED in review-cli#252 — see "Bare-check zero-role fallback" and
        "Genuinely-old vs. merely-mode-avoiding" below): this role-based default
        only applies when the task has AT LEAST ONE role-tagged iteration
        ANYWHERE in its non-mismatched history (any verdict — passed, failed, or
        unverdicted); a task with genuinely ZERO role-tagged iterations, ALL of
        it recorded as predating role-tracking, falls back to the OLD
        model-counting default instead of a guaranteed hard fail. A task with
        zero role-tagged iterations that is NOT provably old (case (b) below)
        does NOT get that fallback — it stays role-based and fails, with a
        `role_tracking_gap` explanation instead.

    Bare-check zero-role fallback (review-cli#246 follow-up; Alex's explicit
    request after shipping the role-based default above): a task whose ENTIRE
    review history predates role-tracking (pre-#246), or whose history comes only
    from ``review quorum``/``review just-ask``/``review brainstorm``/``review qa``
    (modes that never record roles at all — see `record_run`'s ``roles`` param
    docstring), has ZERO role-tagged iterations. Treating that identically to
    "recorded some roles, just short of the floor" is a hard, undeserved fail even
    when the task genuinely has 3+ distinct MODELS in its history that would have
    satisfied the pre-#246 model-counting default. So: in the bare-check default
    path ONLY (neither ``min_models`` nor ``min_roles`` explicitly given), whenever
    the store is readable AND the task has at least one recorded iteration of ANY
    kind (same ``store_error is None and iterations`` guard the sibling
    ``min_models_advisory``/``min_roles_suggestion`` already use — a task with NO
    history at all keeps the pre-existing role-based fail-closed shape; "never
    reviewed" and "reviewed, but never through a role-recording mode" are
    different problems), the task's history is checked for role data — every
    recorded iteration REGARDLESS OF VERDICT (passed, failed, or unverdicted), but
    EXCLUDING any diff-identity-MISMATCHED iteration (a role-tagged review of a
    genuinely different repo/diff under a reused task code must not force THIS
    check into role-mode — see ``_iterations_excluding_mismatched``). This is
    deliberately a WIDER population than ``gate_iterations`` (PASSED-only): a
    role-tagged iteration that FAILED is still real evidence the task went through
    a role-recording review mode, and must not be invisible to this check just
    because it didn't pass. The fallback fires ONLY when this non-mismatched
    population is itself non-empty, carries no role data, AND every iteration in
    it is recorded as predating role-tracking (case (a) — see "Genuinely-old vs.
    merely-mode-avoiding" below for the case (b) exception, where at least one
    iteration is NOT provably old and the fallback must NOT fire). A task whose
    ENTIRE history is diff-identity-mismatched (e.g. a task-code collision with a
    genuinely different repo) is treated the same as "no effective history for
    this check" and keeps the pre-existing role-based fail-closed shape, rather
    than swapping to a model-counting shape with a message that would falsely
    claim no role data exists at all. When it DOES fire, the default silently
    swaps from role-counting to the old model-counting check at the same numeric
    floor — ``result`` ends up with a ``min_models``/``distinct_models_passed``
    shape (no ``min_roles``/``roles`` keys) exactly as if ``--min-models`` had been
    given explicitly, PLUS a ``quorum_mode_fallback`` key explaining why AND a
    ``min_models_source`` key set to ``"fallback"`` (vs. ``"explicit"`` for a
    genuine ``--min-models`` request) — ``"min_models" in result`` alone is NOT a
    reliable "was this explicit" signal anymore; ``min_models_source`` is the
    positive discriminator (``quorum_mode_fallback``'s presence/absence works too,
    but is a secondary/derived signal).
    A task with even ONE role-tagged iteration of ANY verdict in its
    non-mismatched history — regardless of whether the role floor is actually
    met — never falls back: it stays role-based and must pass or fail on that
    basis alone, same as PR #246 left it. This fallback decision is INDEPENDENT
    of ``min_models_explicit``/``min_roles_explicit`` (both stay False in the
    result) and never fires when either floor is explicitly given.

    Genuinely-old vs. merely-mode-avoiding (review-cli#252 follow-up; Alex's
    explicit request after PR #252 shipped the fallback above with no such
    distinction): "zero role-tagged iterations" was being treated as one single
    case, but it actually covers two very different situations, and only one of
    them deserves the free pass above:

      * Case (a), genuinely old — EVERY iteration in ``role_data_population``
        provably predates ``_ROLE_TRACKING_CUTOFF`` (PR #246's merge instant, the
        exact moment a ``roles`` key first became possible to write at all — see
        `_all_iterations_predate_role_tracking`). Nothing more can ever be done
        about this specific history; falling back to model-counting is correct
        and permanent. This is the ONLY case that reaches the behavior described
        above (``quorum_mode_fallback`` fires, ``min_models``/``min_roles`` swap).
      * Case (b), not genuinely old — at least one iteration in
        ``role_data_population`` ran AFTER role-tracking became possible, yet it
        used a role-blind mode (``quorum``/``just-ask``/``brainstorm``/``qa``)
        instead of the role-tracking ``review diff``. Silently granting the same
        model-counting free pass here would let ANY task dodge the role-based
        gate forever, permanently, just by always choosing quorum/brainstorm over
        diff mode — not a one-time migration accommodation for old history, an
        indefinite escape hatch. So case (b) does NOT swap counting mode:
        ``min_models``/``min_roles`` are left exactly as the bare-check default
        set them (role-based, at ``_DEFAULT_QUORUM_FLOOR``), which fails on its
        own merits (``roles`` is empty, same "N/M distinct roles" ratio a task
        with genuinely insufficient — but present — role coverage would print).
        What's ADDED is ``result["role_tracking_gap"]``, a positive discriminator
        (same convention as ``quorum_mode_fallback``) naming exactly what's
        missing and how to fix it — "run ``review diff --task CODE`` instead of
        quorum/just-ask/brainstorm" — printed as an extra note/hint line
        alongside the ratio, not a replacement for it: this is deliberately the
        SAME stdout/stderr treatment as an ordinary role-count shortfall (never
        promoted to ``result["error"]``), so it doesn't quietly move where an
        otherwise-identical "not enough role-tracked reviews yet" denial prints
        just because this specific variant has a name for why.

      Design note on WHERE this lives: a ``--check`` invocation is a fast,
      side-effect-free status query (typically called from an automated ship
      pipeline) — auto-triggering a brand-new multi-model review run as a side
      effect of asking "is the bar met" would violate that contract and could
      turn a status check into a multi-minute, backend-call-spending operation
      the caller never asked for. So this function only ever produces a precise
      diagnosis; actually running the missing role-tracked review is left to the
      caller (a human, or ``gh ship``'s own retry loop) once the
      ``role_tracking_gap`` message tells them what to run.

    FIXED (review-cli#246; was a KNOWN, DELIBERATE trade-off per a Fable round-3 review
    finding on review-cli#221): a caller passing BOTH floors explicitly — e.g.
    ``min_models=5, min_roles=1`` — used to silently let ``min_roles`` govern alone,
    so a task with only 1 distinct model could get ``passed: True`` even though the
    caller explicitly asked for 5. The AND logic above closes that: an explicit
    ``min_models`` can no longer be silently outvoted by ``min_roles`` (or the reverse).

    Whenever ``min_models`` is explicitly given AND there is real history to evaluate
    it against (the store is readable AND at least one iteration is recorded for the
    task — met, not-met, and a diff-identity-mismatch denial all qualify), the result
    also carries a non-blocking ``min_models_advisory`` key (review-cli#246) — Alex's
    policy allows enforcing an explicit model floor while still nudging the caller
    that role-based coverage is usually enough on its own; the advisory never affects
    ``passed``. It is NOT added to any of the fail-closed shapes below that carry NO
    real history (invalid task code, unreadable store, zero recorded iterations, or a
    floor-validation failure) — matching the pre-existing ``min_roles_suggestion``
    convention exactly (same ``store_error is None and iterations`` guard).

    Fail-closed (independent of the above): an invalid task code, an unreadable/missing
    store, or zero recorded iterations for the code all yield ``passed: False`` plus an
    ``"error"`` key explaining why — the caller must never treat "no data" as "quorum
    met". A ``min_iter`` floor below 1, or an explicitly given ``min_models``/
    ``min_roles`` below 1, is also rejected the same way — 0 would trivially satisfy a
    floor via ``0 >= 0`` even for a task with zero passed iterations, undermining the
    whole point of this gate. Validated HERE (not only in the CLI wrapper) so a direct
    library caller can't bypass the floor either.
    """
    # Captured BEFORE any default substitution below. Historically `min_models is
    # not None` alone was a safe "was this explicit" check for the rest of the
    # function, because min_models was NEVER reassigned -- only min_roles was. The
    # bare-check zero-role fallback (below) now CAN reassign min_models too (once
    # real history is loaded, a genuinely role-data-free task swaps it in), so both
    # flags must be captured here, before either variable is ever touched.
    min_models_explicit = min_models is not None
    min_roles_explicit = min_roles is not None
    bare_check_default = not min_models_explicit and not min_roles_explicit
    if bare_check_default:
        # The new default (Alex, review-cli#246): role-based coverage, not a distinct-
        # model-name count -- switches counting MODE while preserving the original "at
        # least 3 distinct opinions" intent via the SAME numeric floor --min-models
        # used to default to. TENTATIVE: this is swapped back to the old model-
        # counting default below, once real history is loaded, if the task turns out
        # to have zero role-tagged iterations (Alex's review-cli#246 follow-up -- see
        # this function's docstring "Bare-check zero-role fallback" section, and the
        # `_has_any_role_data` check right after `gate_iterations` is computed).
        min_roles = _DEFAULT_QUORUM_FLOOR

    def _rejected(error: str) -> dict:
        result = {
            "task_code": task_code,
            "passed_iterations": 0,
            "total_iterations": 0,
            "distinct_models_passed": 0,
            "models": [],
            "min_iter": min_iter,
            "passed": False,
            "error": error,
        }
        if min_models is not None:
            result["min_models"] = min_models
            # Round-review finding (Opus/Fable, independently): `min_models_source`
            # must accompany `min_models` on EVERY shape that carries it, including
            # this early-reject path. Derived from `min_models_explicit` (Fable
            # review finding on an earlier revision) rather than hardcoded
            # `"explicit"` -- both `_rejected()` call sites DO currently run before
            # the fallback logic below, so hardcoding would be correct today, but
            # deriving it makes that an enforced structural fact instead of an
            # assumption a future edit to this function could silently invalidate.
            result["min_models_source"] = (
                "explicit" if min_models_explicit else "fallback"
            )
        if min_roles is not None:
            result["distinct_roles_passed"] = 0
            result["roles"] = []
            result["min_roles"] = min_roles
        return result

    if (
        min_iter < 1
        or (min_models is not None and min_models < 1)
        or (min_roles is not None and min_roles < 1)
    ):
        # Opus round-3 review finding: only name a floor in the message when it was
        # actually part of the input -- otherwise a bare min_iter violation prints a
        # confusing "min_models=None min_roles=None" (the same fix the CLI layer
        # already applies to its own copy of this validation; mirrored here so a
        # direct library caller sees the identical, non-confusing message).
        clauses = [f"min_iter={min_iter}"]
        if min_models is not None:
            clauses.append(f"min_models={min_models}")
        if min_roles_explicit:
            # Opus review finding: must check the PRE-substitution flag, not
            # `min_roles is not None` -- by this point the "neither given"
            # default substitution above may already have set min_roles=3, and
            # naming it here would falsely claim the caller typed --min-roles
            # when they didn't (defeating the whole point of this "only name
            # what was actually part of the input" guard).
            clauses.append(f"min_roles={min_roles}")
        return _rejected(
            "min_iter must be >= 1, and min_models/min_roles (when explicitly given) "
            f"must also be >= 1 (got {' '.join(clauses)})"
        )
    try:
        clean = normalize_task_code(task_code)
    except ValueError as exc:
        return _rejected(f"invalid task code: {exc}")

    store_error = _store_unreadable_error()
    iterations = iterations_for_task(clean) if clean else []
    # Fail-closed: a record with no "passed" key (pre-v3, verdict unknown) is NOT
    # passed -- `is True` (not truthy) so it never accidentally matches None/missing.
    passed_iterations = [item for item in iterations if item.get("passed") is True]
    verified, mismatched, unverifiable = _sort_passed_iterations_into_buckets(
        passed_iterations, repo_id, diff_files
    )
    # Gate-worthy = verified (identity checked out) + unverifiable (no identity to
    # check, so unchanged from pre-v4 behavior) -- mismatched iterations are excluded.
    gate_iterations = verified + unverifiable

    # Bare-check zero-role fallback (Alex's review-cli#246 follow-up -- see this
    # function's docstring "Bare-check zero-role fallback" section). Only the true
    # bare-default path is eligible (an explicit --min-models/--min-roles is never
    # second-guessed here).
    #
    # `role_data_population` (ALL verdicts, not just passed -- only diff-identity-
    # MISMATCHED iterations are dropped) is deliberately NOT `gate_iterations`
    # (PASSED-only): a task with even one role-tagged iteration of ANY verdict --
    # passed, failed, or unverdicted -- in that population must stay role-based
    # and fail on its own merits (not fall back just because the role floor isn't
    # met). Round-review finding (Opus/Fable, independently): checking
    # `gate_iterations` alone missed a role-tagged iteration that FAILED (or a
    # role-less current-repo run alongside an unrelated-repo role-tagged one) --
    # letting the fallback fire even though the task genuinely went through
    # role-recording review, just not successfully. See
    # `_iterations_excluding_mismatched`'s own docstring for why mismatched
    # iterations are still excluded (the opposite concern: a role-tagged
    # iteration for a genuinely DIFFERENT repo/diff must not force THIS check into
    # role-mode -- that would reopen the exact cross-repo pollution bug class
    # diff-identity binding exists to close, just via the counting mode instead of
    # the iteration count).
    #
    # `store_error is None and role_data_population` deliberately EXCLUDES two
    # denials from the swap, keeping BOTH at the PRE-existing role-based shape
    # (`min_roles`=3, `roles=[]`) exactly as before this fix:
    #   1. "No history at all" (unreadable store, or a readable store with zero
    #      records for this task code) -- `role_data_population` is trivially
    #      empty whenever `iterations` is. "Never reviewed" and "reviewed, but
    #      never through a role-recording mode" are different problems with
    #      different fixes; blurring them would muddy `_finalize_quorum_result`'s
    #      already-clear "no recorded review iterations for X" error with an
    #      unrelated role/model mode note that has nothing real to explain.
    #   2. "Every recorded iteration is diff-identity-MISMATCHED" (round-review
    #      finding, Opus/Fable, independently -- a SECOND, sharper round after the
    #      first fix above): checking `_has_any_role_data` against a population
    #      that RAW `iterations` is non-empty but is filtered down to nothing
    #      would fire the fallback -- and its message would falsely claim "no
    #      role-tagged review history recorded" for a task whose ENTIRE history is
    #      genuinely role-tagged, just for an unrelated repo/diff (a task-code
    #      reuse/collision, exactly the shape diff-identity binding exists to
    #      catch). Checking `role_data_population` (POST-filter) directly for
    #      non-emptiness, instead of raw `iterations`, closes this: an
    #      all-mismatched task is treated the same as "no effective history for
    #      this check" and keeps the role-based fail-closed shape, rather than
    #      silently swapping to a misleadingly-worded model-counting shape.
    #
    # `role_data_population` is only ever computed when `bare_check_default and
    # store_error is None` -- an explicit `--min-models`/`--min-roles` caller (or
    # any denial with an unreadable store) pays ZERO extra work for a fallback
    # that could never apply to them: `role_data_population` was previously
    # computed OUTSIDE any conditional at all (an earlier revision's mistake, not
    # a Python `and`-short-circuit issue -- `and` already short-circuits fine),
    # so moving the computation inside this branch is what makes the skip real.
    #
    # `gate_iterations` (PASSED, non-mismatched -- already computed above by
    # `_sort_passed_iterations_into_buckets`) is reused directly here rather than
    # re-classifying those same items via `_iterations_excluding_mismatched`
    # again (GLM/Fable review finding, raised across two rounds: on the exact
    # "thousands of iterations" history shape this module's docstrings call out,
    # re-running `_classify_iteration_identity` -- itself an O(files) set
    # comparison per item -- over the ENTIRE history a second time roughly
    # doubles the dominant per-iteration cost of a bare `--check`). Only the
    # NON-passed remainder (failed/unverdicted -- never seen by the bucket sort,
    # which is passed-only) is freshly classified.
    #
    # `_has_any_role_data(gate_iterations)` is checked FIRST, before touching the
    # non-passed remainder at all (GLM/Fable review finding, a THIRD round,
    # independently converging on the identical fix): if the task's PASSED,
    # non-mismatched history already carries a role, the fallback can never fire
    # regardless of what the non-passed remainder holds -- role_data_population
    # would be non-empty AND have role data either way. This lets the single most
    # common bare-check shape (a task with real, PASSING role-tagged history --
    # exactly the steady state once review-cli#246 is adopted) skip classifying
    # the non-passed remainder entirely, rather than always paying for it only to
    # discover the answer was already decided.
    quorum_mode_fallback: str | None = None
    # GLM review finding on this feature's own PR: the removed `elif
    # bare_check_default and store_error is None: role_data_population =
    # gate_iterations` branch was dead -- reachable only when
    # `_has_any_role_data(gate_iterations)` is True (the negation of the branch
    # below), and its sole consumer (`if role_data_population and not
    # _has_any_role_data(role_data_population):`) always evaluates False in that
    # case. It also forced a second, redundant `_has_any_role_data` scan over
    # `gate_iterations` on the steady-state path this module repeatedly notes is
    # perf-sensitive (thousands-of-iterations shape). Computed once here instead.
    if (
        bare_check_default
        and store_error is None
        and not _has_any_role_data(gate_iterations)
    ):
        non_passed = [item for item in iterations if item.get("passed") is not True]
        role_data_population = gate_iterations + _iterations_excluding_mismatched(
            non_passed, repo_id, diff_files
        )
    else:
        role_data_population = []
    role_tracking_gap: str | None = None
    if role_data_population and not _has_any_role_data(role_data_population):
        # "against the current repo/diff" only makes sense when a check context
        # was actually given (Opus review finding: a direct library caller that
        # omits repo_id has no such context, and the unqualified message reads
        # oddly claiming a comparison that never happened).
        scope = " against the current repo/diff" if repo_id is not None else ""
        # GLM/Opus round-review finding on this feature's own PR: this
        # re-parses each item's `ts` a second time in the case-(b) branch
        # below (which rebuilds an equivalent list). Deliberately NOT
        # deduplicated into a single pass -- both reviewers independently
        # assessed the actual cost as negligible (a `--check` status query,
        # not a hot loop; single-digit ms even for a task with thousands of
        # iterations), and folding the two together would orphan
        # `_all_iterations_predate_role_tracking` as a production call site
        # (leaving it reachable only from its own direct unit test) purely to
        # chase a micro-optimization neither reviewer treated as a real
        # concern. Kept as two clearly-named, independently-testable steps.
        if _all_iterations_predate_role_tracking(role_data_population):
            # Case (a), genuinely old (review-cli#252 follow-up docstring section
            # above) -- every iteration in scope ran before a "roles" key could
            # exist at all. Nothing more can ever be done about THIS history, so
            # fall back to the pre-#246 model-counting default, exactly as PR #252
            # originally shipped this fallback.
            min_roles = None
            min_models = _DEFAULT_QUORUM_FLOOR
            # Fable round-review finding on this feature's own PR: the
            # "excluding mismatched" qualifier below must be conditioned on
            # `repo_id is not None`, same as `scope` just above -- mismatch
            # exclusion is a no-op when no check context was given (see
            # `_iterations_excluding_mismatched`'s own docstring), so an
            # unconditional claim here would assert a repo/diff comparison
            # that never ran for a direct library caller -- exactly the
            # imprecision `scope` itself exists to avoid.
            in_scope_note = (
                " -- excluding any recorded for a different repo/diff --"
                if repo_id is not None
                else ""
            )
            quorum_mode_fallback = (
                f"no role-tagged review history recorded for {clean}{scope}; fell "
                "back to the pre-review-cli#246 model-counting default "
                f"(min_models={_DEFAULT_QUORUM_FLOOR}) since there is no role data "
                "to count against the new role-based default (every in-scope "
                f"iteration{in_scope_note} predates role-tracking)"
            )
        else:
            # Case (b), NOT genuinely old -- at least one iteration is NOT
            # provably pre-role-tracking (either it genuinely ran after
            # role-tracking became possible, or its timestamp simply can't be
            # verified as older -- see `_iteration_predates_role_tracking`'s own
            # fail-closed docstring), yet it carries no role data (either a
            # role-blind mode -- quorum/just-ask/brainstorm/qa never record roles
            # at all -- or a role-less `review diff` run with no board/role config
            # in play). Alex's explicit instruction: this must NOT get case (a)'s
            # free pass -- that would let a task dodge the role-based gate
            # forever, permanently, just by always choosing quorum/brainstorm
            # over a role-tracking board. `min_roles`/`min_models` are left
            # exactly as the bare-check default already set them (role-based, at
            # `_DEFAULT_QUORUM_FLOOR`) so this fails on its own merits below
            # (`roles` ends up empty) -- only the MESSAGE changes, naming the
            # specific gap and the fix, instead of a bare "0/3 roles" ratio that
            # gives no hint the caller could still fix this by reviewing
            # differently.
            #
            # Independent round-review finding (Opus/Codex/Fable, all three,
            # converging on the identical bug): the message must be built from
            # ONLY the iterations that actually caused case (b) -- the ones NOT
            # provably predating the cutoff -- not from the full
            # `role_data_population`, which can legitimately MIX legacy and
            # non-legacy iterations (exactly the shape
            # test_quorum_check_bare_default_post_cutoff_quorum_mode_blocks_
            # fallback_review_cli_252_followup constructs: one backdated `review`
            # iteration + one current `quorum` iteration). Counting/naming the
            # whole population overstates the count and can name a LEGACY mode
            # (e.g. a genuinely pre-cutoff `review` iteration) as if it were part
            # of the gap, which is simply false.
            not_legacy = [
                item
                for item in role_data_population
                if not _iteration_predates_role_tracking(item)
            ]
            modes_used = sorted(
                {
                    m
                    for item in not_legacy
                    if isinstance(m := item.get("mode"), str) and m
                }
            )
            # `mode="review"` is the run-stats identity for the `review diff`
            # SUBCOMMAND (see reviewlib/modes/review.py's `ModeSpec` -- the verb
            # moved to `diff`, the stats key stayed "review" for stability), so
            # rendering it as `` `review review` `` would be flatly wrong (that
            # stuttering form was removed) and, per Fable's review finding on
            # this feature's own PR, wrongly implies a role-less DIFF run is one
            # of the role-blind PANEL modes it's listed alongside. Map it to the
            # real invocation instead (`visual`'s own `stats_mode`/`subcommand`
            # already match, so `f"review {m}"` renders it correctly as-is).
            display_modes = [
                "review diff" if m == "review" else f"review {m}" for m in modes_used
            ]
            modes_str = (
                ", ".join(f"`{m}`" for m in display_modes) or "an unverified mode"
            )
            # Derived from `_ROLE_TRACKING_CUTOFF` itself (Fable round-review
            # finding on this feature's own PR), not a separate hardcoded string
            # -- a future correction to the constant must not leave this message
            # quoting a stale date.
            cutoff_date = _ROLE_TRACKING_CUTOFF.date().isoformat()
            # Opus round-review finding on this feature's own PR: telling a
            # caller who already ran `review diff` (a role-LESS one, mode=
            # "review" with no board configured) to "run `review diff`" reads
            # as self-referential/unhelpful -- their actual fix is configuring
            # a board, not repeating the same command. Lead with that fix
            # specifically when a role-less BOARD-CAPABLE run is part of the
            # gap.
            #
            # Codex round-review finding, a further round: `visual` (see
            # reviewlib/modes/visual.py's `ModeSpec`) has the EXACT SAME
            # board-vs-flat duality as `review` -- `cli.py`'s dispatch gates
            # the board path on `mode.name not in ("review", "visual")`, so a
            # role-less `review visual` run (no board configured) is just as
            # real a shape as a role-less `review diff` run, and must get the
            # SAME "configure a board" remediation, not be misclassified as a
            # role-blind PANEL mode (quorum/just-ask/brainstorm/qa, which can
            # NEVER record roles regardless of board config -- a materially
            # different, board-config-proof situation).
            #
            # Second-round Opus finding: `not_legacy` can mix a role-less
            # board-capable iteration WITH a role-blind panel iteration (e.g.
            # quorum) -- when BOTH are present, mention both fixes rather than
            # only the board-config one, which would otherwise silently ignore
            # the quorum/just-ask iterations (a board fix alone doesn't help
            # those; they never record roles regardless of board config).
            board_capable_modes = ("review", "visual")
            board_capable_used = sorted(
                m for m in modes_used if m in board_capable_modes
            )
            has_panel = any(m not in board_capable_modes for m in modes_used)
            # GLM round-review finding on this feature's own PR: a board-capable
            # record with `ok_count == 0 and fail_count > 0` means every configured
            # seat FAILED that run (dead API keys, broken auth, ...) -- NOT that no
            # board was ever configured. `_run_mode_with_stats` (cli.py) omits the
            # `roles` key whenever the failover outcome's `usable_roles` comes back
            # empty, which happens for BOTH "no board configured" and "board
            # configured, every seat unusable" -- the two are indistinguishable from
            # the record alone except via this exact signal. Telling an operator
            # whose board IS configured (but whose seats are all broken) to
            # "configure a reviewer board" sends them chasing a config problem that
            # doesn't exist instead of restoring seat health -- the concrete failure
            # scenario this check exists to catch.
            board_seats_failed = any(
                item.get("mode") in board_capable_modes
                and item.get("ok_count") == 0
                and (item.get("fail_count") or 0) > 0
                for item in not_legacy
            )
            # Codex round-review finding: don't render `` `review visual
            # --task CODE` `` as a copy-pasteable "run this again" command --
            # `review visual` REQUIRES an IMAGE positional argument the gate
            # has no way to know (cli.py rejects a bare `review visual
            # --task CODE` with "the following arguments are required:
            # IMAGE"), so printing it as if it were runnable as-is would hand
            # the caller a broken command. Describe the fix instead of
            # dictating an exact invocation for `visual`.
            rerun_hint = (
                f"`review diff --task {clean}`"
                if board_capable_used == ["review"]
                else (
                    f"`review visual IMAGE --task {clean}` (IMAGE is the "
                    "screenshot you're reviewing)"
                    if board_capable_used == ["visual"]
                    else (
                        f"`review diff --task {clean}` (or `review visual "
                        f"IMAGE --task {clean}` for screenshot review)"
                    )
                )
            )
            board_fix = (
                (
                    "restore the reviewer board's seats to health (see `review "
                    "--show-board` for per-seat availability -- dead API keys, "
                    "missing CLI auth, or an unpaid provider are the usual "
                    f"causes) and re-run {rerun_hint} -- every configured seat "
                    "failed on this run, which records no per-seat roles"
                )
                if board_seats_failed
                else (
                    "configure a reviewer board for this repo (see `review "
                    "--show-board` / config.yaml's `board:` entry) and re-run "
                    f"{rerun_hint} through it -- a role-less run with no board "
                    "config records no per-seat roles"
                )
            )
            # Opus round-review finding: when NO mode could be identified at
            # all (`modes_used` empty -- a corrupted/externally-written
            # record with a missing/blank `mode`), don't assert a confident
            # claim about WHICH modes are involved -- we genuinely don't
            # know, so the specific "quorum/just-ask/brainstorm/qa never
            # record roles" parenthetical would be an unverified guess dressed
            # up as fact.
            diff_fix = (
                f"run `review diff --task {clean}` through a role-tracking "
                "board (quorum/just-ask/brainstorm/qa never record roles at "
                "all)"
                if modes_used
                else f"run `review diff --task {clean}` through a role-tracking board"
            )
            if board_capable_used and has_panel:
                # Deliberately generic ("the other" rather than naming
                # quorum/just-ask/brainstorm/qa specifically) -- Fable
                # round-review finding: a future/unrecognized mode is also
                # `not in board_capable_modes` and would be misdescribed by a
                # hardcoded panel-mode list here, the same imprecision the
                # `board_capable_modes` tuple exists to avoid elsewhere.
                fix_it = (
                    f"{board_fix}; the other iteration(s) need a "
                    "role-tracking `review diff` run instead regardless of "
                    "board config (quorum/just-ask/brainstorm/qa never "
                    "record roles at all)"
                )
            elif board_capable_used:
                fix_it = board_fix
            else:
                fix_it = diff_fix
            role_tracking_gap = (
                f"{clean} has {len(not_legacy)} review iteration(s) not confirmed "
                f"to predate role-tracking (review-cli#246, merged {cutoff_date}) "
                f"-- either recorded after it became available, or with a timestamp "
                f"that can't be verified as older{scope} -- yet none carry role "
                f"data: they ran under {modes_str} without recording per-seat "
                "roles. This is NOT eligible for the legacy model-counting "
                "fallback (that only covers history verifiably predating "
                f"role-tracking entirely): {fix_it} to record real role "
                f"coverage -- {min_roles} distinct role(s) are required."
            )

    models = _distinct_models(gate_iterations)
    roles = _distinct_roles(gate_iterations) if min_roles is not None else None

    # review-cli#246: each floor gates independently when given, vacuously satisfied
    # when not -- an explicitly requested floor can never be silently outvoted by the
    # other one anymore (this is the fix for the round-3 finding in the docstring).
    iter_ok = len(gate_iterations) >= min_iter
    models_ok = min_models is None or len(models) >= min_models
    roles_ok = min_roles is None or (roles is not None and len(roles) >= min_roles)
    gate_met = iter_ok and models_ok and roles_ok

    result = {
        "task_code": clean,
        "passed_iterations": len(gate_iterations),
        "total_iterations": len(iterations),
        "distinct_models_passed": len(models),
        "models": models,
        "min_iter": min_iter,
        "passed": gate_met,
    }
    if min_models is not None:
        result["min_models"] = min_models
        # Round-review finding (Fable, two independent rounds): `"min_models" in
        # result` used to be a reliable "was --min-models explicit" signal, but the
        # fallback above breaks that -- both an explicit request and a fallback now
        # produce the identical `min_models`/`distinct_models_passed` shape, and
        # telling them apart required a caller to check the ABSENCE of
        # `quorum_mode_fallback` (a negative/derived signal, easy to get wrong).
        # This key is a direct, POSITIVE discriminator instead -- always present
        # whenever `min_models` is, naming exactly which of the two put it there.
        result["min_models_source"] = (
            "fallback" if quorum_mode_fallback is not None else "explicit"
        )
    if quorum_mode_fallback is not None:
        # Alex's review-cli#246 follow-up: distinguishes THIS silent bare-check
        # swap-to-model-counting from a genuine explicit `--min-models` request --
        # both end up with the identical `min_models`/`distinct_models_passed`
        # result shape, so without this key a caller can't tell "you asked for
        # model-counting" apart from "you asked for nothing and there was no role
        # data to give you the new default". Present ONLY when the fallback
        # actually fired (never on an explicit-flag path).
        result["quorum_mode_fallback"] = quorum_mode_fallback
    if role_tracking_gap is not None:
        # review-cli#252 follow-up: the positive, machine-readable discriminator
        # for case (b) above (NOT genuinely old -- see `quorum_check`'s docstring)
        # -- a caller like `gh ship` parsing --json can key on this key's presence
        # directly, the same way `quorum_mode_fallback` is its own positive
        # signal. Deliberately never promoted to `result["error"]` -- printed as
        # an additive note/hint line alongside the ordinary role-count ratio (see
        # `_finalize_quorum_result`'s docstring for why).
        result["role_tracking_gap"] = role_tracking_gap
    if roles is not None:
        result["distinct_roles_passed"] = len(roles)
        result["roles"] = roles
        result["min_roles"] = min_roles
    if min_models_explicit and store_error is None and iterations:
        # review-cli#246: non-blocking advisory -- Alex's policy explicitly enforces
        # a caller-requested model floor (models_ok above), but role-based coverage is
        # generally sufficient on its own, so surface the option without failing the
        # gate over it. Fires whenever min_models was EXPLICITLY given AND the gate
        # was actually evaluated against real history, regardless of whether it
        # happened to govern/pass/fail. `store_error is None` (k3/Opus round-1
        # finding) excludes the unreadable-store denial; `iterations` (k3/Opus
        # round-2 finding, same shape as the sibling `min_roles_suggestion` guard
        # immediately below) additionally excludes the zero-recorded-iterations
        # denial -- both `_finalize_quorum_result` turns into an "error" result
        # AFTER this point runs, so neither is itself a short-circuit; a nudge
        # saying "this floor may not be needed" on a task with NO review history
        # at all (of any kind) would contradict its own fail-closed error.
        #
        # Gated on `min_models_explicit` (the PRE-substitution flag), NOT on
        # `min_models is not None` -- the latter is also true for the bare-check
        # zero-role fallback above, and this advisory's own text ("an explicit
        # min_models floor is set... role-based coverage is generally sufficient")
        # would be actively WRONG there: nothing was explicit, and role-based
        # coverage is exactly what's UNAVAILABLE (that's why the fallback fired).
        result["min_models_advisory"] = (
            "an explicit min_models floor is set; role-based coverage (min_roles) is "
            "generally sufficient and this explicit floor may not be needed unless "
            "you have a specific reason to require distinct models"
        )
    # review-cli#221: when --min-models governs the gate (min_roles not given) and the
    # bar isn't met because model diversity itself is short, point at --min-roles as an
    # alternative counting mode — a duplicated-model role-fill (PR #207) still counts
    # toward it even though `_distinct_models` collapsed it to one string here.
    #
    # Round-1 review finding (Opus/Codex/Fable, independently): the ORIGINAL version of
    # this block only checked "models fell short", which suggests a flag that CANNOT
    # help in real cases — a task whose history has no recorded roles at all (predates
    # this field, or every iteration came from quorum/just-ask/brainstorm/qa) is
    # GUARANTEED to still fail under --min-roles too, just with less diagnostic detail;
    # so is a task that's ALSO short on `min_iter` (switching counting mode doesn't add
    # iterations). Compute the roles `--min-roles {min_models}` would actually see and
    # only suggest it when it would truly flip the verdict to passed — never a hint that
    # sends the caller straight into a second denial.
    #
    # Round-2 review finding (Fable): requiring at least 2 distinct models keeps the
    # hint scoped to its own stated premise — genuine partial model diversity being
    # under-credited by `_distinct_models` (the PR #207 "2 real models + 1 role-fill"
    # shape this feature exists for) — instead of ALSO recommending --min-roles for a
    # full monoculture (every passed iteration on the SAME single model, just under
    # different roles), which would let one model self-authorize a merge that a
    # self-merge-authority gate exists specifically to prevent. --min-roles ITSELF still
    # allows that shape if a caller explicitly opts into it (that's the feature's own
    # documented semantics — see this function's docstring); only the ADVISORY hint here
    # is more conservative than the gate it points at.
    #
    # Round-3 review finding (Fable): the guard must count distinct models among the
    # ROLE-BEARING iterations specifically (`_models_behind_role_coverage`), NOT every
    # passed iteration — the original `len(models) >= 2` counted a role-LESS iteration
    # on a second model as "diversity" even when 100% of the counted role coverage
    # still traced back to a single other model, laundering the exact monoculture the
    # guard exists to block through an unrelated iteration.
    # Gated on `min_models_explicit` (the PRE-substitution flag), NOT on
    # `min_models is not None` -- the bare-check zero-role fallback (above) can
    # ALSO leave `min_roles is None and min_models is not None` true (it swaps
    # min_roles back to None and sets min_models itself), and suggesting "switch
    # to --min-roles" would be nonsensical there: the fallback fired BECAUSE
    # `gate_iterations` has zero role-tagged items, so `potential_roles` below is
    # guaranteed empty and the inner `len(potential_roles) >= min_models` check
    # would never fire anyway -- but gating explicitly here keeps that a
    # documented invariant this block relies on by DESIGN, not a coincidence of
    # `_has_any_role_data`/`_distinct_roles` happening to agree today.
    if (
        min_roles is None
        and min_models_explicit
        and not gate_met
        and store_error is None
        and iterations
    ):
        potential_roles = _distinct_roles(gate_iterations)
        role_bearing_models = _models_behind_role_coverage(gate_iterations)
        if (
            len(role_bearing_models) >= 2
            and len(models) < min_models
            and len(gate_iterations) >= min_iter
            and len(potential_roles) >= min_models
        ):
            result["min_roles_suggestion"] = (
                f"{min_models} distinct models required, only {len(models)} found. If "
                "review passes duplicated a model onto multiple roles (see PR #207), "
                f"replace --min-models {min_models} with --min-roles {min_models} "
                "(review-cli#246: keeping both now requires BOTH to pass), which "
                f"covers {len(potential_roles)} distinct roles rather than unique "
                "model names."
            )
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
