"""Parse review-cli's real on-disk artifacts into structured runs/sessions/stats.

review-cli does NOT emit a structured run record today — the only durable trace of a
run is the per-CALL streamed log files (and the brainstorm discussion `.md`) that
`reviewlib.process` / `reviewlib.modes.brainstorm` write into `log_dir()`:

  per-call log : ``{UTCstamp}-{backend}-r{round}.log``
                 line 0 = ``[review-cli] {backend}: {argv0} (args redacted)``
                          with optional `` task=CODE`` suffix
                 body   = streamed stdout, stderr lines prefixed ``[stderr] ``,
                 a timeout adds ``[review-cli] TIMEOUT after {N}s — partial output above]``
  brainstorm   : ``{UTCstamp}-brainstorm.md`` (topic + per-round persona transcript)

So this parser is a READER over those artifacts. It:
  * parses each call log's filename + header + body into a ``CallLog``;
  * groups calls into SESSIONS by time-clustering (review-cli emits no run id, so a
    "session" = a burst of calls separated from the next by > ``gap`` seconds). The
    session id is derived deterministically from the first call's UTC stamp so the
    overseer's feedback / conscious flag / PR links stay pinned across restarts;
  * infers the review MODE from the round shape (r0 = single-shot review/just-ask/
    quorum, r>=1 = brainstorm/quorum rounds; a sibling brainstorm.md confirms it) and
    surfaces models, roles (personas, parsed from the brainstorm md), durations,
    success/fail, errors, and the redacted prompt/argv per call.

Nothing here writes; the only new persistence is in ``store.py``. Anything review-core
does not record yet (real token/cost numbers, an explicit run id) is reported as an
empty/`null` field with a note rather than faked.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import cached_property, lru_cache
from pathlib import Path

# Per-call log filename: 20260613T040611_516399Z-claude-r0.log
_CALL_RE = re.compile(r"^(\d{8}T\d{6})_(\d+)Z-(.+)-r(\d+)\.log$")
# Brainstorm discussion md: 20260613T114552_999796Z-brainstorm.md
_BRAINSTORM_RE = re.compile(r"^(\d{8}T\d{6})_(\d+)Z-brainstorm\.md$")
_HEADER_RE = re.compile(
    r"^\[review-cli\] (?P<backend>.+?): (?P<argv0>.*?) \(args redacted\)(?: task=(?P<task>\S+))?\s*$"
)
_NO_TASK_RE = re.compile(r"^\[review-cli\] TASK\s*$")
_WAITING_RE = re.compile(
    r"^\[review-cli\] .+?: waiting for a concurrency slot \(cap \d+\)\s*$"
)
_TIMEOUT_RE = re.compile(r"^\[review-cli\] TIMEOUT after (?P<secs>\d+)s")
# Explicit status footer written by every log writer (process._run_streamed and
# backends' REST sidecar). This is the authoritative success/failure signal (finding 4).
_EXIT_RE = re.compile(r"^\[review-cli\] EXIT (?P<code>-?\d+)\s*$")
_STDERR_PREFIX = "[stderr] "

# Default gap (seconds) that separates one session-burst from the next. Real logs show
# intra-session gaps of a few-to-~30s and inter-session gaps of 60s+, so 90s is a safe
# default; override via ?gap= on the API for re-clustering.
DEFAULT_SESSION_GAP_SECONDS = 90.0

# Largest plausible wall-time of a single call (review's default per-call timeout). Used
# to cap mtime-as-end-marker so an out-of-band touch can't balloon a session window.
_MAX_CALL_WALL = timedelta(seconds=1200)

# ---- per-model health classification ---------------------------------------------------
# The dashboard's "Models & roles" tab surfaces, per default-preset board model, an ok-rate and the
# dominant failure class so the CTO can see WHY a model is down at a glance. The classes
# below are the real failure modes observed in `~/Library/Logs/review-cli/*.log` (see the
# CHANGELOG / the HYP diagnosis): the per-call EXIT code + stderr + body sentinels each
# map to one of these. `OK` and `EMPTY` are the two EXIT-0 outcomes (real verdict vs no
# content); the rest are hard failures. Order here is the tie-break for "dominant class"
# when two classes are equally frequent (hard-unavailable beats soft).
HEALTH_OK = "ok"
HEALTH_PAYWALL = (
    "paywall"  # body says "currently unavailable" (Fable). EXIT is often 0.
)
HEALTH_AUTH = "auth"  # EXIT 401 / stderr {"error":"bad key"} (z.ai / GLM bad key).
HEALTH_BLOCKED = "blocked"  # EXIT 403 / "error code: 1010" (Cloudflare bot block).
HEALTH_TIMEOUT = "timeout"  # EXIT 124 / "timed out".
HEALTH_EMPTY = (
    "empty"  # EXIT 0 but no real content (output_tokens=0 / framing-only body).
)
HEALTH_ERROR = "error"  # any other non-zero exit not matched above.

# The three "hard-unavailable" classes: a model currently in one of these is problematic
# regardless of its longer-window rate (a fresh paywall/auth/block is down NOW). The order
# here is the DOMINANT-CLASS TIE-BREAK only (when two failure classes are equally frequent,
# the earlier one wins) — it is NOT the same as classify_call's recognition precedence,
# which interleaves TIMEOUT and runs paywall first for the EXIT-0-lies reason. Two different
# orderings, deliberately.
HARD_UNAVAILABLE_CLASSES = (HEALTH_PAYWALL, HEALTH_AUTH, HEALTH_BLOCKED)
# Every non-OK class is a failure for the ok-rate.
FAILURE_CLASSES = (
    HEALTH_PAYWALL,
    HEALTH_AUTH,
    HEALTH_BLOCKED,
    HEALTH_TIMEOUT,
    HEALTH_EMPTY,
    HEALTH_ERROR,
)

# A model is PROBLEMATIC when its fail-rate over the window meets/exceeds this, OR its most
# recent PROBLEMATIC_RECENT_N calls are ALL failures, OR it is currently in a hard-
# unavailable class. One constant, not a literal scattered around (CTO standing rule).
PROBLEMATIC_FAIL_RATE = 0.5
PROBLEMATIC_RECENT_N = 3

# Sentinel the Fable paywall body carries. review-cli's logger collapses interior
# whitespace, so the on-disk body reads `ClaudeFable5iscurrentlyunavailable` — match the
# NORMALIZED (whitespace-stripped, lower-cased) form so both the spaced and the collapsed
# rendering hit. "unavailable" alone is too broad (a real review could mention it), so we
# anchor on the full `currentlyunavailable` phrase.
_PAYWALL_SENTINEL = "currentlyunavailable"
# Cloudflare bot-block body marker (commandcode gateway behind CF).
_CF_BLOCK_MARKER = "error code: 1010"
# z.ai / GLM bad-key stderr marker.
_BAD_KEY_MARKER = '{"error":"bad key"}'
# `claude` backend header argv0 carries NO model id (Fable and Opus share the `claude-p`
# wrapper), so the model is inferred from the body sentinel: a paywall body = Fable, any
# other = Opus. These are the two board claude seats.
_CLAUDE_FABLE_MODEL = "claude:claude-fable-5"
_CLAUDE_OPUS_MODEL = "claude:claude-opus-4-8"
# Backend-name -> board model id for backends with no per-call selector in argv0.
_SINGLE_MODEL_BACKENDS = {"gemini": "gemini"}
# Codex header argv0 is `codex -m <model>` when a suffixed model was requested, otherwise
# it is just the binary path and maps to the bare codex board seat.
_CODEX_MODEL_RE = re.compile(r"(?:^|\s)-m\s+(?P<model>\S+)")
# `commandcode API <model>` / `z.ai API <model>` header argv0 reveals the gateway model.
_API_MODEL_RE = re.compile(r"\bAPI\s+(?P<model>\S+)")
# opencode header argv0 is `opencode -m <provider/model>` (review_opencode passes it as
# header_argv0). The board seat id is `oc:<provider/model>` (config DEFAULT_BOARD, built by
# `_agentic`), so recover the selector and re-prefix it to the board id — otherwise every
# agentic board seat (Kimi/GLM/Qwen/DeepSeek) would collapse to a single `opencode` row and
# show `no_data` on the health view (review-cli#24).
_OPENCODE_MODEL_RE = re.compile(r"-m\s+(?P<model>\S+)")
# Claude API-mode header argv0 is EXACTLY `Anthropic API <model>` (optionally `@ <base>`),
# written by review_claude_api. Anchor on that full prefix — NOT the generic `\bAPI` — so a
# CLI-mode `claude-p` path that happens to contain `API ` (e.g. `/opt/API Tools/claude-p`)
# can't be mis-read as a model and bypass the paywall/Opus fallback.
_CLAUDE_API_MODEL_RE = re.compile(r"^Anthropic API\s+(?P<model>\S+)")
# z.ai's board seat id is `zai:<model>` (config DEFAULT_BOARD), but the log backend/header
# spell it `z.ai`. Normalise the header model to the board prefix so attribution lines up.
# openrouter's backend name AND board prefix are both `openrouter` (seat `openrouter:<slug>`);
# its `openrouter API <slug>` argv0 carries the per-model slug, so mapping it here keeps each
# openrouter model a distinct dashboard row instead of collapsing them into one `openrouter`.
_BACKEND_BOARD_PREFIX = {
    "commandcode": "commandcode",
    "z.ai": "zai",
    "openrouter": "openrouter",
}


def _parse_stamp(date_part: str, micros: str) -> datetime:
    """``20260613T040611`` + ``516399`` -> aware UTC datetime."""
    dt = datetime.strptime(date_part, "%Y%m%dT%H%M%S")
    return dt.replace(microsecond=int(micros[:6].ljust(6, "0")), tzinfo=timezone.utc)


@dataclass
class CallLog:
    """One streamed backend call (one `.log` file)."""

    path: str
    filename: str
    started: datetime
    backend: str  # backend name from the filename (== header backend)
    round: int
    argv0: str  # redacted: just argv[0], no prompt/diff
    body: str  # streamed output (stdout + tagged stderr), header stripped
    stderr_lines: list[str] = field(default_factory=list)
    timed_out: bool = False
    timeout_secs: int | None = None
    size_bytes: int = 0
    mtime: datetime | None = None
    # The explicit return code recorded by the writer as a trailing
    # `[review-cli] EXIT {code}` line. None when the log predates this footer or was
    # truncated before it (killed mid-write). This is the AUTHORITATIVE success signal —
    # the body is NOT grepped for `error:`/`permission denied`, because a review's
    # output legitimately contains those strings while describing the code it reviewed
    # (HYP-742 finding 4: that grep inflated the Errors panel and tanked the success rate).
    exit_code: int | None = None
    task_code: str | None = None

    @property
    def duration_seconds(self) -> float | None:
        """Wall time from start (filename stamp) to last write (mtime).

        review-cli does not record an explicit duration, so this is the best honest
        proxy: file creation -> last flush. None if mtime is unavailable. Uses the SAME
        capped end as ``ended_at`` — an mtime beyond the per-call timeout window (a copied /
        restored / touched log) is untrustworthy, so the Metrics panel must not report a
        multi-day call duration for it (codex P3); it falls back to 0 there.
        """
        if self.mtime is None:
            return None
        d = (self.ended_at - self.started).total_seconds()
        return d if d >= 0 else None

    @property
    def ended_at(self) -> datetime:
        """When this call FINISHED (last write / mtime), falling back to start.

        Session clustering must use this, not ``started``: a single quorum/brainstorm
        invocation fans out long calls, and the moderator/next round can begin >gap
        seconds after a slow call STARTED but right after it ENDED. Comparing the next
        call's start against the prior call's END keeps the whole invocation in one
        session.

        mtime is only trustworthy as an END marker when it sits within the per-call
        timeout window of the start: a file that was copied/touched/restored long after
        the run (or fixtures that set the filename stamp to the past but mtime to "now")
        would otherwise balloon the session. So we cap the end at start + the max per-call
        timeout (1200s, the review default). Beyond that we treat mtime as untrustworthy
        and fall back to the start stamp.
        """
        if self.mtime is None or self.mtime < self.started:
            return self.started
        if (self.mtime - self.started) > _MAX_CALL_WALL:
            return self.started
        return self.mtime

    @property
    def completed(self) -> bool:
        """Did this call FINISH (so its success/failure is known)?

        Every writer stamps a terminal `EXIT {code}` footer (and a timeout also gets the
        marker). A log with NEITHER an exit code NOR a timeout marker is NOT finished — it
        is either a long call still streaming, or a call whose writer died before the footer
        (e.g. a Popen / E2BIG failure). Such a log must NOT be counted as a success (codex
        P2); it is `running`/unknown. (A truncated old log with a body error marker is still
        surfaced as an error by has_error — only the clean, footerless case is `running`.)
        """
        return (
            self.exit_code is not None
            or self.timed_out
            or bool(self.stderr_lines)
            or _looks_like_error(self.body)
        )

    @property
    def has_error(self) -> bool:
        """Did this call FAIL?

        Success/failure is decided by the EXPLICIT return code (the `EXIT {code}` footer),
        never by grepping the body for `error:` — a review's own output legitimately
        contains those strings (HYP-742 finding 4). When the explicit code exists it is
        authoritative: rc 0 = success (even if the prose mentions errors), rc != 0 = fail.

        A timeout is always a failure (the writer records it as EXIT 124, but a log
        truncated before the footer can still carry the TIMEOUT marker — honor it either
        way). Only when NO explicit code was recorded do we fall back to the legacy
        heuristic (stderr present / error-marker grep) so pre-footer logs still surface.
        A clean footerless log is neither an error NOR a success — it is `running` (see
        `completed`); has_error stays False there but stats must not bucket it as OK.
        """
        if self.timed_out:
            return True
        if self.exit_code is not None:
            return self.exit_code != 0
        # Legacy fallback: log predates the EXIT footer (or was truncated before it).
        return bool(self.stderr_lines) or _looks_like_error(self.body)

    @property
    def error_summary(self) -> str | None:
        if not self.has_error:
            return None
        if self.timed_out:
            return f"TIMEOUT after {self.timeout_secs}s"
        if self.exit_code is not None and self.exit_code != 0:
            # Prefer a concrete stderr line; fall back to the explicit exit code so the
            # Errors panel always has a reason even when stderr is empty.
            if self.stderr_lines:
                return self.stderr_lines[0].strip()[:300]
            return f"exit code {self.exit_code}"
        if self.stderr_lines:
            return self.stderr_lines[0].strip()[:300]
        if _looks_like_error(self.body):
            first = next((ln for ln in self.body.splitlines() if ln.strip()), "")
            return first.strip()[:300]
        return None

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "started": self.started.isoformat(),
            "backend": self.backend,
            # The resolved MODEL id (gateway model split out of the shared commandcode/z.ai
            # backends, Fable-vs-Opus split) so the detail view's call chip wears the right
            # brand logo/label (e.g. "Qwen", not the generic "gateway" backend name).
            "model": model_id_for_call(self),
            "round": self.round,
            "argv0": self.argv0,
            "duration_seconds": self.duration_seconds,
            "timed_out": self.timed_out,
            "timeout_secs": self.timeout_secs,
            "exit_code": self.exit_code,
            "task_code": self.task_code,
            "completed": self.completed,
            "has_error": self.has_error,
            "error_summary": self.error_summary,
            "size_bytes": self.size_bytes,
            "body": self.body,
            "stderr_lines": self.stderr_lines,
        }


@dataclass
class BrainstormLog:
    path: str
    filename: str
    started: datetime
    topic: str
    panel: list[str]
    moderator: str | None
    rounds: list[dict]  # [{round, personas: [{name, model, text}]}]
    body: str
    task_code: str | None = None
    mtime: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "started": self.started.isoformat(),
            "topic": self.topic,
            "panel": self.panel,
            "moderator": self.moderator,
            "task_code": self.task_code,
            "rounds": self.rounds,
            "body": self.body,
        }


_ERROR_MARKERS = (
    "error:",
    "may not exist or you may not have access",
    "tool_approval_blocked",
    "traceback (most recent call last)",
    "command not found",
    "permission denied",
)


def _looks_like_error(text: str) -> bool:
    """LEGACY heuristic, used ONLY for logs that have no explicit `EXIT {code}` footer.

    A substring grep over the body is unreliable — review output legitimately contains
    `error:` / `permission denied` while describing the code under review (HYP-742
    finding 4). The authoritative signal is the recorded return code; this fallback only
    applies to pre-footer / truncated logs where no return code was captured."""
    low = text.lower()
    return any(m in low for m in _ERROR_MARKERS)


def parse_call_log(path: Path) -> CallLog | None:
    """Parse one ``*-r{n}.log`` file. Returns None if the name doesn't match."""
    m = _CALL_RE.match(path.name)
    if not m:
        return None
    started = _parse_stamp(m.group(1), m.group(2))
    backend = m.group(3)
    round_no = int(m.group(4))
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raw = ""
    lines = raw.splitlines()
    argv0 = ""
    body_lines: list[str] = []
    stderr_lines: list[str] = []
    timed_out = False
    timeout_secs: int | None = None
    exit_code: int | None = None
    task_code: str | None = None
    header_seen = False
    pre_header = True
    expect_legacy_empty_task_marker = False
    # The status footer is ALWAYS the final non-empty line the logger writes. Consume it
    # ONLY in that position — a model's review output can legitimately QUOTE an exact
    # `[review-cli] EXIT 1` line mid-body (e.g. while reviewing review-cli's own logs);
    # treating that as the status would mis-flag the call and strip real content (codex
    # P2). So we find the index of the trailing footer (if any) and skip just that line.
    exit_line_idx: int | None = None
    for j in range(len(lines) - 1, -1, -1):
        if not lines[j].strip():
            continue  # ignore trailing blank lines
        em = _EXIT_RE.match(lines[j])
        if em:
            exit_code = int(em.group("code"))
            exit_line_idx = j
        break  # the last non-empty line is decisive either way
    # The TIMEOUT marker must be tied to a real timeout. The writers ALWAYS emit it
    # together with the timeout return code 124 (process.py / write_sidecar_log), so a
    # marker is genuine only when the authoritative exit code is 124 — or absent (a legacy
    # footer-less log, where we fall back to position). A successful `EXIT 0` review whose
    # output merely QUOTES `[review-cli] TIMEOUT after Ns` (e.g. while reviewing review-cli's
    # own logs) must NOT be flagged as a timeout — that would corrupt the dashboard's
    # timeout/error metrics (codex P2). Even within an honored exit code, recognise the
    # marker only in the position the logger writes it: the last non-empty line before the
    # footer (or file end for a legacy log), never an arbitrary quoted line earlier in the body.
    timeout_line_idx: int | None = None
    if exit_code == 124 or exit_code is None:
        scan_start = (
            (exit_line_idx - 1) if exit_line_idx is not None else (len(lines) - 1)
        )
        for j in range(scan_start, -1, -1):
            if not lines[j].strip():
                continue  # skip blank lines between body and footer
            if _TIMEOUT_RE.match(lines[j]):
                timeout_line_idx = j
            break  # only the trailing-most non-body line is decisive
    for i, line in enumerate(lines):
        if i == exit_line_idx:
            continue  # the authoritative trailing status footer — kept out of the body
        if pre_header and not header_seen:
            hm = _HEADER_RE.match(line)
            if hm:
                # filename backend wins (header backend can be the same), but keep argv0.
                argv0 = hm.group("argv0")
                task_code = hm.group("task")
                header_seen = True
                pre_header = False
                expect_legacy_empty_task_marker = True
                continue
            if _WAITING_RE.match(line):
                continue
            pre_header = False
        if expect_legacy_empty_task_marker:
            expect_legacy_empty_task_marker = False
            if _NO_TASK_RE.match(line):
                continue
        if i == timeout_line_idx:
            tm = _TIMEOUT_RE.match(line)
            timed_out = True
            timeout_secs = int(tm.group("secs"))
            continue  # the authoritative timeout marker — kept out of the body
        if line.startswith(_STDERR_PREFIX):
            stderr_lines.append(line[len(_STDERR_PREFIX) :])
        body_lines.append(line)
    try:
        st = path.stat()
        size = st.st_size
        mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
    except OSError:
        size = len(raw.encode("utf-8"))
        mtime = None
    return CallLog(
        path=str(path),
        filename=path.name,
        started=started,
        backend=backend,
        round=round_no,
        argv0=argv0,
        body="\n".join(body_lines).strip(),
        stderr_lines=stderr_lines,
        timed_out=timed_out,
        timeout_secs=timeout_secs,
        exit_code=exit_code,
        task_code=task_code,
        size_bytes=size,
        mtime=mtime,
    )


_BS_HEADER_RE = re.compile(r"panel=(?P<panel>[^ ]*) moderator=(?P<mod>[^ ]*)")
_BS_TASK_RE = re.compile(r"\btask=(?P<task>\S+)")
_BS_PERSONA_RE = re.compile(r"^#### (?P<name>.+?) \((?P<model>[^)]+)\)\s*$")
# The EXACT dashboard section markers the brainstorm WRITER emits after a round's persona
# blocks (modes/brainstorm.py): `## Moderator (round N)` and `# Final synthesis` (each on
# its own line). Reaching one ends the current persona's capture. The match is exact —
# anchored to end-of-line — so a persona's OWN Markdown headings, even ones that merely
# START with these words (`## Moderator notes`, `# Final synthesis plan`, `## Risks`,
# `### Plan`), do NOT terminate capture and drop the rest of that model's transcript
# (codex P3). (`# Round N` is matched separately above; `#### …` personas never match here.)
_BS_SECTION_RE = re.compile(r"^(?:## Moderator \(round \d+\)|# Final synthesis)\s*$")


def parse_brainstorm_log(path: Path) -> BrainstormLog | None:
    m = _BRAINSTORM_RE.match(path.name)
    if not m:
        return None
    started = _parse_stamp(m.group(1), m.group(2))
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raw = ""
    topic = ""
    panel: list[str] = []
    moderator: str | None = None
    task_code: str | None = None
    rounds: list[dict] = []
    cur_round: dict | None = None
    cur_persona: dict | None = None
    # True while inside a moderator / final-synthesis CONTROL section (between such a
    # marker and the next `# Round N` / EOF). No personas are captured there — the
    # moderator's own text can contain a `#### Name (model)` heading that must NOT be
    # parsed as a new persona of the previous round (codex P2).
    in_control = False
    for line in raw.splitlines():
        if line.startswith("# Brainstorm:"):
            topic = line[len("# Brainstorm:") :].strip()
            continue
        hm = _BS_HEADER_RE.search(line)
        if hm and not panel:
            panel = [p for p in hm.group("panel").split(",") if p]
            moderator = hm.group("mod") or None
            tm = _BS_TASK_RE.search(line)
            if tm:
                task_code = tm.group("task")
            continue
        rm = re.match(r"^# Round (\d+)\s*$", line)
        if rm:
            cur_round = {"round": int(rm.group(1)), "personas": []}
            rounds.append(cur_round)
            cur_persona = None
            in_control = False  # a new round ends any control section
            continue
        # The moderator summary (`## Moderator (round N)`) and the final synthesis
        # (`# Final synthesis`) are written AFTER a round's persona blocks. Entering one
        # ends the current persona's capture AND opens a control section: until the next
        # `# Round N` / EOF, no `####` heading is treated as a persona, so the moderator's
        # / synthesis's own text (which can contain a `#### Name (model)` line) is never
        # misattributed as an extra persona of the previous round (codex P2).
        if _BS_SECTION_RE.match(line):
            cur_persona = None
            in_control = True
            continue
        pm = _BS_PERSONA_RE.match(line)
        # A real persona heading is `#### {name} ({model})` where {model} is one of the
        # panel backends (see modes/brainstorm.py: label=f"{persona} ({model})"). Model
        # OUTPUT itself can contain `#### (a) ... (`SOME_MAP`...)` lines, so we ONLY accept
        # a heading whose parenthesized tail is a known panel model — otherwise it's body
        # text and belongs to the current persona's transcript. Inside a control section we
        # accept no personas at all.
        if (
            pm
            and cur_round is not None
            and not in_control
            and (not panel or pm.group("model") in panel)
        ):
            cur_persona = {
                "name": pm.group("name"),
                "model": pm.group("model"),
                "text": "",
            }
            cur_round["personas"].append(cur_persona)
            continue
        if cur_persona is not None and not in_control:
            cur_persona["text"] += line + "\n"
    for rnd in rounds:
        for p in rnd["personas"]:
            p["text"] = p["text"].strip()
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        mtime = None
    return BrainstormLog(
        path=str(path),
        filename=path.name,
        started=started,
        topic=topic,
        panel=panel,
        moderator=moderator,
        task_code=task_code,
        rounds=rounds,
        body=raw,
        mtime=mtime,
    )


@dataclass
class Session:
    """A time-clustered burst of calls = one logical review-cli invocation (best-effort,
    since review-cli emits no run id)."""

    session_id: str  # deterministic, derived from the first call's UTC stamp
    started: datetime
    ended: datetime
    calls: list[CallLog] = field(default_factory=list)
    brainstorm: BrainstormLog | None = None

    @property
    def task_code(self) -> str | None:
        # Best-effort session clustering can only derive task identity from child artifacts:
        # the brainstorm discussion header is authoritative when present; otherwise use the
        # first call log carrying task metadata. A real invocation writes one task code to all
        # calls, so mixed codes indicate manual/corrupt logs rather than a supported shape.
        if self.brainstorm is not None and self.brainstorm.task_code:
            return self.brainstorm.task_code
        for call in self.calls:
            if call.task_code:
                return call.task_code
        return None

    @property
    def models(self) -> list[str]:
        seen: list[str] = []
        # For a brainstorm, the `*-brainstorm.md` `panel=` line is the SOLE authoritative
        # model list: it records the user's exact selection — including aliases / suffixed
        # ids like `codex:gpt-5` or `zai` that the per-call log FILENAMES lose to the
        # resolved backend name (`codex`, `z.ai`). Use ONLY it (when present), so attribution
        # is STABLE whether or not the per-call logs have aged out, and the same session is
        # NOT counted under both the requested model AND its resolved backend (codex P2 —
        # appending the resolved call backends double-counted while logs existed and then
        # dropped them once logs aged out). Non-brainstorm sessions use the call backends.
        if self.brainstorm is not None and self.brainstorm.panel:
            for model in self.brainstorm.panel:
                if model not in seen:
                    seen.append(model)
            return seen
        for c in self.calls:
            if c.backend not in seen:
                seen.append(c.backend)
        return seen

    @property
    def mode(self) -> str:
        """Infer mode from the call/round shape.

        review-cli does not stamp the mode into the log, so this is inferred:
          * a sibling brainstorm.md  -> 'brainstorm'
          * any call with round >= 1 -> 'brainstorm' (rounds are a brainstorm signal)
          * a single call at round 0 -> 'just-ask/review' (indistinguishable from the
            log alone — both are one r0 shot; we label it 'review')
          * multiple r0 calls         -> 'panel' (review/quorum fan-out)
        This is surfaced honestly as an INFERRED mode in the UI.
        """
        if self.brainstorm is not None:
            return "brainstorm"
        if any(c.round >= 1 for c in self.calls):
            return "brainstorm"
        if len(self.calls) > 1:
            return "panel"
        return "review"

    @property
    def duration_seconds(self) -> float:
        return max(0.0, (self.ended - self.started).total_seconds())

    @property
    def invocations(self) -> list[str]:
        """The distinct recorded invocation lines (``argv0``) for this session, in order.

        The prompt/diff is redacted from the logs, so ``argv0`` (the backend command/endpoint,
        e.g. ``z.ai API glm-5.2``) is the only durable "what was run" a non-brainstorm session
        has; surfacing it lets the Prompts panel / panel rows show the invocation instead of a
        bare "redacted" note. Order-preserving de-dup (one entry per distinct seat)."""
        return list(
            dict.fromkeys(inv for c in self.calls if (inv := (c.argv0 or "").strip()))
        )

    @property
    def has_error(self) -> bool:
        return any(c.has_error for c in self.calls)

    @property
    def running(self) -> bool:
        """True if the session has no errors but at least one call has not FINISHED
        (a footerless in-flight / aborted call). The UI shows these as running/unknown
        rather than a green OK badge (codex P2)."""
        return not self.has_error and any(not c.completed for c in self.calls)

    @cached_property
    def errors(self) -> list[dict]:
        """Failed calls, each enriched with the failure CLASS, the resolved model id, the
        planned FALLBACK seat (the next board seat by priority — what the failover pool would
        promote), and a per-error RECOVERY assessment so the Errors tab can show what happened
        next instead of a dead end.

        CACHED per Session instance (cached_property): `to_summary()` exposes this for EVERY
        session on the runs-list endpoint, and `to_detail()` reads it again, so the O(calls^2)
        recovery scan runs once per session, not per access (glm review finding 4). A Session is
        rebuilt on each parse, so the cache never goes stale against changed logs.

        Recovery is read off the session's OWN calls (review-cli records no explicit retry
        link): a `recovered` error is one where SOME later call in the same session — a retry of
        the same seat or a different seat — returned a clean OK verdict, so the run still
        produced a result. An error with no clean call after it is `unrecovered` and surfaces a
        suggested next action (the fallback seat) + the manual-control affordance in the UI."""
        # A panel fans out its seats at nearly the same instant for the SAME request, so a
        # same-round sibling that returns cleanly recovers the run even if the failed seat's
        # timestamp lands a moment later. Sort by start only to give "at/after the failure" a
        # well-defined meaning; the same-round clause below (not timestamps) is what credits a
        # concurrent panel sibling, so strict end-time overlap math isn't needed.
        ordered = sorted(self.calls, key=lambda c: c.started)
        out = []
        for i, c in enumerate(ordered):
            if not c.has_error:
                continue
            model_id = model_id_for_call(c)
            cls = classify_call(c) if c.completed else HEALTH_ERROR
            # Recovered iff SOME OTHER clean-OK call either ran in the SAME round (a parallel
            # panel sibling answering the same request — its success means the run produced a
            # verdict despite this seat failing) OR started at/after this failure (a later
            # retry / next-round seat). It is NOT recovered by an EARLIER round's success, so a
            # round-1 OK never masks a round-3 failure (glm review finding 5: the old
            # session-wide `any_ok` overclaimed recovery across rounds).
            recovered = any(
                other is not c
                and _call_is_clean_ok(other)
                and (other.round == c.round or other.started >= c.started)
                for other in ordered
            )
            out.append(
                {
                    "backend": c.backend,
                    "round": c.round,
                    "summary": c.error_summary,
                    "filename": c.filename,
                    "model": model_id,
                    "started": c.started.isoformat(),
                    "health_class": cls,
                    # The next board seat by priority — what the failover pool promotes when this
                    # seat is down. None when this seat is off-board or already the lowest priority.
                    "fallback": _fallback_seat_for(model_id),
                    # Did the run still produce a usable verdict at/after this failure?
                    #   recovered   — a clean OK call ran concurrently-or-after this failed seat.
                    #   unrecovered — no clean OK call did; this run needs attention (manual control).
                    "recovery": "recovered" if recovered else "unrecovered",
                }
            )
        return out

    def to_summary(self) -> dict:
        return {
            "session_id": self.session_id,
            "started": self.started.isoformat(),
            "ended": self.ended.isoformat(),
            "duration_seconds": self.duration_seconds,
            "mode": self.mode,
            "task_code": self.task_code,
            "models": self.models,
            "call_count": len(self.calls),
            "error_count": len(self.errors),
            "has_error": self.has_error,
            "running": self.running,
            "has_brainstorm": self.brainstorm is not None,
            "topic": self.brainstorm.topic if self.brainstorm else None,
            # The recorded invocation(s) — the closest thing to a "prompt" a non-brainstorm
            # run leaves behind (the prompt itself is redacted). Lets the Prompts panel /
            # panel rows show the invoked command instead of "redacted, argv only".
            "invocations": self.invocations,
            # The enriched per-error list (model / failure class / recovery / planned fallback)
            # so the Errors tab can drill down + show recovery without a per-session detail fetch.
            "errors": self.errors,
        }

    def to_detail(self) -> dict:
        d = self.to_summary()
        d["calls"] = [c.to_dict() for c in self.calls]
        d["errors"] = self.errors
        d["brainstorm"] = self.brainstorm.to_dict() if self.brainstorm else None
        d["roles"] = self.roles()
        return d

    def roles(self) -> list[dict]:
        """Personas/roles used in this session (only brainstorm logs carry them)."""
        if not self.brainstorm:
            return []
        seen: dict[str, dict] = {}
        for rnd in self.brainstorm.rounds:
            for p in rnd["personas"]:
                key = p["name"]
                entry = seen.setdefault(
                    key, {"role": p["name"], "models": [], "count": 0}
                )
                entry["count"] += 1
                if p["model"] not in entry["models"]:
                    entry["models"].append(p["model"])
        return list(seen.values())


def _session_id_for(stamp: datetime) -> str:
    return "sess-" + stamp.strftime("%Y%m%dT%H%M%S_%f")


def cluster_sessions(
    calls: list[CallLog],
    brainstorms: list[BrainstormLog],
    gap_seconds: float = DEFAULT_SESSION_GAP_SECONDS,
) -> list[Session]:
    """Group calls into sessions by time-gap, attaching brainstorm md by proximity."""
    ordered = sorted(calls, key=lambda c: c.started)
    sessions: list[Session] = []
    cur: Session | None = None
    for call in ordered:
        # Gap is measured from the prior call's END (last write), not its start, so a
        # single invocation whose individual call runs longer than `gap` is not split
        # into multiple sessions. `cur.ended` therefore tracks the max call end-time.
        same_task = cur is not None and (cur.task_code or "") == (call.task_code or "")
        if (
            cur is None
            or not same_task
            or (call.started - cur.ended).total_seconds() > gap_seconds
        ):
            cur = Session(
                session_id=_session_id_for(call.started),
                started=call.started,
                ended=call.ended_at,
                calls=[call],
            )
            sessions.append(cur)
        else:
            cur.calls.append(call)
            if call.ended_at > cur.ended:
                cur.ended = call.ended_at
    # Attach each brainstorm.md to the session whose window contains/precedes it.
    win = timedelta(seconds=gap_seconds)
    for bs in sorted(brainstorms, key=lambda b: b.started):
        best: Session | None = None
        for s in sessions:
            if s.started - win <= bs.started <= s.ended + win:
                best = s
        # A brainstorm's session id is ALWAYS derived from the brainstorm stamp, in BOTH
        # the attached and the orphan path. Otherwise annotations would move: while the
        # per-call `.log` files exist the session id is the first call's stamp, but once
        # they age out (the `.md` outlives them) the orphan path would recreate the run
        # under `sess-<brainstorm-stamp>` and the overseer's feedback/conscious/links —
        # keyed by the old id — would vanish. Pinning to the brainstorm stamp keeps the
        # id stable across log cleanup. (`mode_brainstorm` writes the `.md` first, so its
        # stamp is a stable, deterministic anchor for the whole run.)
        bs_id = _session_id_for(bs.started)
        if best is not None:
            best.brainstorm = bs
            best.session_id = bs_id
        else:
            # Orphan brainstorm (its per-call logs aged out): make a session of its own.
            sessions.append(
                Session(
                    session_id=bs_id,
                    started=bs.started,
                    ended=bs.mtime or bs.started,
                    calls=[],
                    brainstorm=bs,
                )
            )
    sessions.sort(key=lambda s: s.started, reverse=True)
    return sessions


def load_sessions(
    log_dir_path: Path, gap_seconds: float = DEFAULT_SESSION_GAP_SECONDS
) -> list[Session]:
    """Read every artifact in ``log_dir_path`` and return clustered sessions, newest first."""
    calls: list[CallLog] = []
    brainstorms: list[BrainstormLog] = []
    if not log_dir_path.exists():
        return []
    for entry in sorted(log_dir_path.iterdir()):
        if not entry.is_file():
            continue
        if entry.suffix == ".log":
            c = parse_call_log(entry)
            if c:
                calls.append(c)
        elif entry.suffix == ".md":
            b = parse_brainstorm_log(entry)
            if b:
                brainstorms.append(b)
    return cluster_sessions(calls, brainstorms, gap_seconds)


# ---- request-time session cache --------------------------------------------------------
# WHY: every dashboard page load fires `/api/runs` AND `/api/stats` together (the front-end's
# `loadAll()` Promise.all), and each handler called the PURE `load_sessions` above — a full
# read + parse of EVERY artifact in `log_dir()`. On a real, long-lived install that dir grows
# to tens of thousands of files / hundreds of MB; one parse is ~6s of CPU. Two-plus full
# re-parses per page load saturated the single (threaded) server, the `/api/runs` fetch blew
# past the browser/Tailscale-proxy timeout, never resolved, and the panel stayed stuck on
# "Loading…" — i.e. an EMPTY dashboard. The fix is a short-lived memo of the parsed sessions,
# invalidated by a CHEAP directory signature so live activity is never hidden behind a stale
# cache. `load_sessions` itself stays PURE (no caching) so tests keep deterministic parses.
_CacheKey = tuple[str, float]  # (resolved dir str, gap)
_Signature = tuple[int, float]  # (entry-count, max-mtime), from `_dir_signature`


class _SingleFlightCache:
    """A signature-keyed memo that COLLAPSES concurrent cold misses for the same key to ONE
    in-flight producer call (cache-stampede / thundering-herd prevention).

    WHY this and not a plain `dict` + lock: the dashboard runs on a ThreadingHTTPServer with
    unbounded threads. A cold cache (server just started) OR a moving dir signature (a live
    `review` run streaming its `.log` keeps `_dir_signature` shifting) made EVERY concurrent
    request MISS and launch its OWN full ~30s parse of the 434MB / 35k-file log dir. Observed
    live: the server pegged at 103% CPU with a dozen concurrent 30s parses, every request timed
    out, and the dashboard stayed empty under realistic conditions (active reviews + several
    tabs each firing runs+stats+SSE). A memo alone is insufficient — the concurrent cold misses
    must collapse to one parse, which is what this provides.

    Contract: when a producer for a key is already running, other callers asking for the SAME key
    WAIT for that in-flight result instead of starting a duplicate producer. A waiter accepts
    whatever that one in-flight cycle produced even if the dir signature has since moved again —
    staleness of a single parse-cycle is fine, and it guarantees forward progress (no re-parse
    loop while a writer hammers the dir). `load_sessions` / `compute_stats` themselves stay pure.

    Thread-safety / deadlock-freedom: one `threading.Condition` (and its single underlying lock)
    guards `_cache` and `_in_flight`. The lock is held ONLY for the cheap bookkeeping + the
    `wait`/`notify`; the multi-second `producer()` runs with the lock RELEASED. `Condition.wait`
    atomically releases the lock while blocked and re-acquires on wake, so a waiter never holds
    the lock across the producer. There is exactly one lock, taken in one order, so no lock-order
    cycle exists. Used independently per cache instance (sessions, stats) with no nesting between
    them — neither calls into the other while holding its lock.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        # key -> (signature, value); the freshest memoised result per key.
        self._cache: dict[_CacheKey, tuple[_Signature, object]] = {}
        # key -> the signature the currently-running producer is computing FOR. Presence marks
        # the key as in-flight; absence means no producer is running for it.
        self._in_flight: dict[_CacheKey, _Signature] = {}

    def _fresh(self, key: _CacheKey, signature: _Signature) -> tuple[bool, object]:
        """Return (hit, value) if `_cache[key]` matches `signature`, else (False, None)."""
        entry = self._cache.get(key)
        if entry is not None and entry[0] == signature:
            return True, entry[1]
        return False, None

    def get_or_compute(self, key: _CacheKey, signature: _Signature, producer):
        """Return the memoised value for `key` at `signature`, computing it via `producer()` (a
        zero-arg callable) at most ONCE across all concurrent callers for that key.

        Fast warm path: a fresh entry is returned without ever touching the producer. Cold path:
        the first caller marks the key in-flight and runs `producer()` with the lock released;
        concurrent callers for the same key wait and then return that producer's result.
        """
        with self._cond:
            while True:
                hit, value = self._fresh(key, signature)
                if hit:
                    return value
                if key in self._in_flight:
                    # Another thread is producing this key. Wait for it, then take whatever it
                    # produced for this cycle (re-check below also catches the now-cached value).
                    self._cond.wait()
                    cached = self._cache.get(key)
                    if cached is not None:
                        return cached[1]
                    # Producer left no entry (it raised) — loop to re-evaluate / take our turn.
                    continue
                # We are the producer for this key: claim it and break out to run the producer
                # with the lock released.
                self._in_flight[key] = signature
                break
        try:
            value = producer()
        except BaseException:
            # On a producer exception, clear in-flight + wake waiters so a failed producer can't
            # wedge the key forever (a waiter then re-takes the producer role), and re-raise.
            with self._cond:
                self._in_flight.pop(key, None)
                self._cond.notify_all()
            raise
        # Publish the result, clear in-flight, and wake every waiter — they read the fresh entry.
        with self._cond:
            self._cache[key] = (signature, value)
            self._in_flight.pop(key, None)
            self._cond.notify_all()
        return value

    def clear(self) -> None:
        """Drop all memoised entries. In-flight producers are left to finish (clearing the memo
        mid-flight cannot deadlock: producers run lock-free and only re-take the lock to store +
        notify). Does NOT block on in-flight work."""
        with self._cond:
            self._cache.clear()


# One single-flight memo per derived view. Key = (resolved dir str, gap). Bounded implicitly:
# one entry per (dir, gap) the server actually serves — a handful in practice (default gap +
# any ?gap=). The stats memo holds the PURE `compute_stats` aggregate (which the `/api/stats`
# handler re-ran per request — ~8.5s, fanning into `compute_model_health` — on a warm session
# cache). Both invalidate on the SAME dir signature, so stats stay coherent with their sessions.
_SESSIONS_CACHE = _SingleFlightCache()
_STATS_CACHE = _SingleFlightCache()


def _dir_signature(log_dir_path: Path) -> tuple[int, float]:
    """A cheap (entry-count, max-mtime) fingerprint of the log dir — STAT-ONLY, no file reads.

    Computing this must be orders of magnitude cheaper than parsing (which opens + reads every
    file), so a single `os.scandir` pass using the stat already cached on each `DirEntry` is the
    whole budget. A new/grown/removed/touched artifact moves either the count or the max mtime,
    so the signature changes exactly when a re-parse is warranted (e.g. a live review streaming
    its `.log`). Missing dir -> (0, 0.0), a stable signature distinct from any populated dir."""
    count = 0
    max_mtime = 0.0
    try:
        with os.scandir(log_dir_path) as it:
            for entry in it:
                count += 1
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    # A racing unlink between scandir and stat: skip it; the next request's
                    # changed count/mtime will re-sync. Don't let one vanished file crash parse.
                    continue
                if mtime > max_mtime:
                    max_mtime = mtime
    except (FileNotFoundError, NotADirectoryError):
        return (0, 0.0)
    return (count, max_mtime)


def load_sessions_cached(
    log_dir_path: Path, gap_seconds: float = DEFAULT_SESSION_GAP_SECONDS
) -> list[Session]:
    """Cached wrapper over `load_sessions`, keyed on (resolved dir, gap) + a stat-only dir
    signature. Returns the memoised parse when the dir is unchanged since the last call; re-parses
    (and refreshes the cache) when the signature moves. Thread-safe AND single-flight: the
    dashboard's ThreadingHTTPServer serves many threads, and concurrent cold misses for the same
    key collapse to ONE parse (see `_SingleFlightCache`) instead of N parallel ~30s parses that
    saturated CPU and timed every request out. The multi-second `load_sessions` runs OUTSIDE the
    lock. Use this from request handlers; use the pure `load_sessions` where deterministic
    re-parsing is required (tests)."""
    key = (str(log_dir_path.resolve()), gap_seconds)
    signature = _dir_signature(log_dir_path)
    return _SESSIONS_CACHE.get_or_compute(
        key, signature, lambda: load_sessions(log_dir_path, gap_seconds=gap_seconds)
    )


def compute_stats_cached(
    sessions: list[Session],
    log_dir_path: Path,
    gap_seconds: float = DEFAULT_SESSION_GAP_SECONDS,
) -> dict:
    """Cached wrapper over `compute_stats`, keyed identically to `load_sessions_cached`:
    (resolved dir, gap) + the cheap stat-only dir signature. Returns the memoised aggregate when
    the dir is unchanged, re-aggregating only when the signature moves — so stats invalidate
    exactly when the sessions they summarise do. Thread-safe AND single-flight: concurrent cold
    misses for the same key collapse to ONE aggregation (see `_SingleFlightCache`), not N parallel
    multi-second computes; the aggregation runs OUTSIDE the lock. `sessions` must be the SAME
    parse returned by `load_sessions_cached` for this (dir, gap) — they share the signature
    contract.

    The `/api/stats` handler layers per-request annotation counts (conscious_count /
    feedback_count) on top of the result. To keep the cached aggregate pristine no matter how a
    caller treats the return value, this hands back a SHALLOW COPY each call — top-level keys
    only, microseconds, nowhere near the multi-second aggregation — so adding/overwriting a
    top-level key can never accumulate into the shared cache. Nested values are shared (the
    handler only adds new scalar keys, never mutates nested ones)."""
    key = (str(log_dir_path.resolve()), gap_seconds)
    signature = _dir_signature(log_dir_path)
    stats = _STATS_CACHE.get_or_compute(key, signature, lambda: compute_stats(sessions))
    return dict(stats)


def invalidate_sessions_cache() -> None:
    """Drop all memoised sessions AND their derived stats. Clearing both keeps the two caches
    coherent (a stale stats aggregate must never outlive the sessions it summarised). For tests
    and for a future explicit-refresh affordance. Each cache takes its OWN lock to clear (no
    shared lock between them), so this can never deadlock against an in-flight producer — clearing
    only drops memoised entries; in-flight producers finish and re-publish lock-free."""
    _SESSIONS_CACHE.clear()
    _STATS_CACHE.clear()


def compute_stats(sessions: list[Session]) -> dict:
    """Aggregate counts/metrics across sessions for the Stats/Metrics/Models panels."""
    by_mode: dict[str, int] = {}
    by_model: dict[str, int] = {}
    by_role: dict[str, int] = {}
    by_task: dict[str, int] = {}
    task_groups: dict[str, dict] = {}
    by_day: dict[str, int] = {}
    durations: list[float] = []
    call_total = 0
    error_calls = 0
    timeout_calls = 0
    ok_calls = 0
    running_calls = 0
    for s in sessions:
        by_mode[s.mode] = by_mode.get(s.mode, 0) + 1
        by_day[s.started.date().isoformat()] = (
            by_day.get(s.started.date().isoformat(), 0) + 1
        )
        if s.task_code:
            by_task[s.task_code] = by_task.get(s.task_code, 0) + 1
            group = task_groups.setdefault(
                s.task_code,
                {
                    "task_code": s.task_code,
                    "iterations": 0,
                    "models": [],
                    "modes": set(),
                    "session_ids": [],
                    "last_started": None,
                },
            )
            group["iterations"] += 1
            group["session_ids"].append(s.session_id)
            if (
                group["last_started"] is None
                or s.started.isoformat() > group["last_started"]
            ):
                group["last_started"] = s.started.isoformat()
            group["modes"].add(s.mode)
        for m in s.models:
            by_model[m] = by_model.get(m, 0) + 1
            if s.task_code and m not in task_groups[s.task_code]["models"]:
                task_groups[s.task_code]["models"].append(m)
        for role in s.roles():
            by_role[role["role"]] = by_role.get(role["role"], 0) + role["count"]
        for c in s.calls:
            call_total += 1
            d = c.duration_seconds
            if d is not None:
                durations.append(d)
            if c.timed_out:
                timeout_calls += 1
            if not c.completed:
                # A footerless, error-free log = a call still streaming or whose writer died
                # before the footer. It is neither success nor failure — don't inflate the
                # OK count (codex P2); track it separately so success_rate stays honest.
                running_calls += 1
            elif c.has_error:
                error_calls += 1
            else:
                ok_calls += 1
    durations.sort()

    def _pct(p: float) -> float | None:
        if not durations:
            return None
        idx = min(len(durations) - 1, int(p * len(durations)))
        return round(durations[idx], 3)

    tasks = []
    for group in task_groups.values():
        item = dict(group)
        item["modes"] = sorted(item["modes"])
        tasks.append(item)
    tasks.sort(key=lambda item: item.get("last_started") or "", reverse=True)

    return {
        "session_count": len(sessions),
        "call_count": call_total,
        "ok_calls": ok_calls,
        "error_calls": error_calls,
        "timeout_calls": timeout_calls,
        "running_calls": running_calls,
        # success_rate is over COMPLETED calls only (ok + error) — an in-flight / aborted
        # footerless call has no known outcome and must not drag the rate either way.
        "success_rate": round(ok_calls / (ok_calls + error_calls), 4)
        if (ok_calls + error_calls)
        else None,
        "by_mode": by_mode,
        "by_model": by_model,
        "by_role": by_role,
        "by_task": by_task,
        "tasks": tasks,
        "by_day": dict(sorted(by_day.items())),
        "duration_seconds": {
            "count": len(durations),
            "min": round(durations[0], 3) if durations else None,
            "p50": _pct(0.5),
            "p90": _pct(0.9),
            "max": round(durations[-1], 3) if durations else None,
        },
        # review-core does not record token usage or $ cost in the logs today; these are
        # surfaced as nulls with a note rather than faked. See the Metrics panel.
        "tokens_recorded": False,
        "cost_recorded": False,
        # Per-model health (ok-rate / dominant failure class / problematic flag) for the
        # Models & roles tab, plus the count of problematic BOARD models for the tab badge.
        "model_health": (_mh := compute_model_health(sessions))["models"],
        "problematic_count": _mh["problematic_count"],
        # The priority-ordered board (id/display/role/priority) so the UI can show the failover
        # order and compute a seat's planned fallback without re-deriving it client-side.
        "board": _board_models(),
    }


def _normalize_body(text: str) -> str:
    """Lower-case + strip ALL whitespace, so a sentinel survives the logger's whitespace
    collapsing. The Fable paywall body lands on disk as `ClaudeFable5iscurrentlyunavailable`
    (interior spaces gone), so a spaced `currently unavailable` match would miss it; we
    compare against the fully de-spaced form instead."""
    return re.sub(r"\s+", "", text).lower()


# `_has_paywall_sentinel`'s cheap first check: `currently`/`unavailable` as two adjacent
# WORDS with only whitespace between them (0+, so the fully-collapsed `currentlyunavailable`
# rendering still matches) — a `re.search` for this is a literal-text scan with NO new
# string built, unlike `_normalize_body`'s `re.sub(r"\s+", "", text)`, which allocates and
# rewrites the ENTIRE body. `review-cli#186`'s token-burn investigation profiled
# `classify_call` against this project's own real log_dir() (~7,000 calls/~760MB of body
# text for a 7-day window) and found that `re.sub` — run on the FULL body of every call,
# unconditionally, as `classify_call`'s very first check — was 20+ of a ~50 second report.
# A first attempt at this prefilter searched for the single word "current" (no `unavailable`
# adjacency) and barely moved the needle: these ARE code-review logs, so ~37% of real calls'
# bodies contain "current" somewhere as ordinary prose/code (`current_user`, "the current
# implementation", ...) — worse, that ~37% skews toward the LARGEST bodies (a call quoting a
# huge diff is more likely to contain the word than a short one), so the prefilter let
# through almost exactly the expensive tail it existed to filter out. The two-word phrase
# is far more specific: 33 of 6,981 real calls (0.5%) in the same window.
_PAYWALL_PREFILTER_RE = re.compile(r"currently\s*unavailable", re.IGNORECASE)


def _has_paywall_sentinel(text: str) -> bool:
    """`_PAYWALL_SENTINEL in _normalize_body(text)`, byte-for-byte — this is a perf
    fast path, NEVER a truncation. `text` is always scanned in full on the slow path; the
    fast path only decides whether that scan needs to run at all.

    review-cli#186: an earlier version of this fast path CAPPED the body (classified a
    truncated copy) on the assumption that a paywall/blocked/auth sentinel is always an
    early, short administrative rejection — plausible for `claude`/Fable's immediate
    reject, but FALSE on this project's own real `codex` logs: a codex call can stream a
    long transcript (a large quoted diff, real analysis) and only THEN hit a session
    sentinel at the very end, past any reasonable byte cap. Verified: capping at 20,000
    bytes silently reclassified 7 genuinely-paywalled codex calls out of a real 7-day
    window as `error`/`ok` — a report that's supposed to be the honest source of truth
    for exactly this kind of failure pattern would have quietly lied. A substring
    PRE-FILTER has no such risk: `_PAYWALL_SENTINEL` ("currentlyunavailable") can only
    survive whitespace-collapse from a raw body where "currently" and "unavailable"
    already appear as adjacent words (0+ whitespace between them, per
    `_PAYWALL_PREFILTER_RE`) — the collapsing this project's loggers perform is strictly
    INTER-word (see `_normalize_body`'s docstring), never splitting a word's own letters
    apart — so for any body a real model/logger produces, this pre-filter changes
    NOTHING about which calls classify as paywall (see the HONEST LIMITATION note below
    for the narrow, unrealistic case where that stops holding); it only skips the
    `re.sub` allocation for the (vast majority) case where the phrase isn't present
    anywhere in the body at all. Covered against the exact whitespace-
    collapse fixtures (spaced + fully-collapsed) in
    `tests/test_dashboard.py::test_paywall_sentinel_prefilter_matches_normalize_body`,
    and separately verified equal to the pre-fix (unfiltered) classification on this
    project's own real 7-day log_dir() window during development.

    HONEST LIMITATION (Opus review finding, round 3 — confirmed by direct reproduction,
    not merely theoretical as an earlier round assumed): the equivalence above holds
    for any body a real model/logger actually produces, but is NOT a mathematical
    guarantee of the code itself. `_normalize_body` strips ALL whitespace, including
    INTRA-word (`re.sub(r"\s+", "", text)` makes no inter/intra distinction — "loggers
    only collapse inter-word whitespace" is an assumption about the INPUT text, not
    something this function enforces), while `_PAYWALL_PREFILTER_RE` requires
    "currently"/"unavailable" as literal contiguous tokens. A body containing unusual
    intra-word whitespace (e.g. "curre ntly unavailable" — not a shape any real
    provider response or this project's own loggers produce, but not something this
    code can rule out either) makes the two diverge: the reference check reads it as a
    paywall sentinel, this fast path does not. Bounded impact, matching this module's
    other accepted-limitation notes: at most one call misclassified as `error`/`ok`
    instead of `HEALTH_PAYWALL`, never a crash or systemic drift — pinned explicitly
    (not silently) by
    `test_paywall_sentinel_prefilter_intra_word_split_diverges_from_reference` in
    `tests/test_dashboard.py`, so a reader sees the real boundary instead of trusting
    an unverified "changes nothing" claim."""
    if not _PAYWALL_PREFILTER_RE.search(text):
        return False
    return _PAYWALL_SENTINEL in _normalize_body(text)


def _body_has_real_content(call: "CallLog") -> bool:
    """Did this EXIT-0 call return a real verdict, or is it empty/framing-only?

    A call is EMPTY when it exited cleanly but produced no usable review: a body that —
    once the `[review-cli] …` framing and stderr/usage lines are dropped — has no remaining
    prose. (The body has already had the header + EXIT/timeout footer stripped by the
    parser; what remains can still be a usage line or pure whitespace.)

    A bare `output_tokens=0` usage line is NOT, on its own, an empty signal: a REST backend
    that returns REAL review text but omits usage metadata still appends a zero fallback
    usage line (`input_tokens=0 output_tokens=0`). The verdict above that line is real, so
    presence of actual prose decides — we only treat the usage line itself as non-content,
    not the whole call (codex P2)."""
    for raw in call.body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("[review-cli]"):
            continue  # framing line
        if line.startswith("[reasoning_content"):
            continue  # reasoning-only, no final answer
        # A bare usage line is not content. Match both spellings review-cli emits: the
        # OpenAI-shaped `prompt_tokens=` (z.ai / commandcode) and the Anthropic-shaped
        # `input_tokens=` (the claude REST backend), in either order, regardless of value.
        if re.fullmatch(
            r"((prompt|input)_tokens=\d+\s*)?(output_tokens=\d+\s*)?", line
        ):
            continue
        return True
    return False


def classify_call(call: "CallLog") -> str:
    """Bucket one finished call into a health class (see HEALTH_* constants).

    Precedence is by how hard/actionable the failure is: timeout and the three
    hard-unavailable classes (paywall/auth/blocked) are recognised first, because their
    sentinels are unambiguous and they explain a model being down NOW. EMPTY vs OK is the
    EXIT-0 split (no content vs a real verdict). A non-zero exit with no recognised sentinel
    falls through to the generic ERROR class so it still counts against the ok-rate."""
    # Paywall: the body sentinel is authoritative even when EXIT is 0 (Fable returns 0 with
    # an "unavailable" body — the EXIT code lies, the body tells the truth). This is the only
    # check that must run on the EXIT-0 happy path, so it leads.
    if _has_paywall_sentinel(call.body):
        return HEALTH_PAYWALL
    if call.timed_out or call.exit_code == 124:
        return HEALTH_TIMEOUT
    # The CF bot-block / bad-key markers can land in stderr OR the body; build the haystack
    # once, only for the error-bearing calls that need it (the healthy majority skips this).
    blob = ("\n".join(call.stderr_lines) + "\n" + call.body).lower()
    # Blocked (Cloudflare): EXIT 403 or the CF marker.
    if call.exit_code == 403 or _CF_BLOCK_MARKER in blob:
        return HEALTH_BLOCKED
    # Auth: EXIT 401 or the bad-key marker.
    if call.exit_code == 401 or _BAD_KEY_MARKER in blob:
        return HEALTH_AUTH
    if call.has_error:
        return HEALTH_ERROR
    # EXIT 0 from here: real verdict vs empty/framing-only.
    if not _body_has_real_content(call):
        return HEALTH_EMPTY
    return HEALTH_OK


def model_id_for_call(call: "CallLog") -> str:
    """Resolve the MODEL-level id for a call (Kimi vs Qwen vs DeepSeek all share the
    `commandcode` backend, so the filename/backend alone is too coarse).

    Source of truth, in order:
      * `commandcode` / `z.ai`: the header argv0 carries `… API <model>` — map to the board
        prefix (`commandcode:<model>` / `zai:<model>`). A bare probe (`commandcode` with no
        `API …`) has no model, so it stays the backend name.
      * `claude`: in API mode (`REVIEW_CLAUDE_MODE=api`, or no `claude-p`) the sidecar argv0
        carries the EXACT model as `Anthropic API <model>` — read that first and map it to
        the board id (`claude:<model>`). Only the CLI wrapper (`claude-p`) hides the model
        (its argv0 is identical for Fable and Opus); there the model is inferred from the
        body — a paywall body = Fable, anything else = the Opus seat.
      * `codex`: a suffixed run records `codex -m <model>` in argv0, so recover
        `codex:<model>`; otherwise the bare codex CLI maps to `codex`.
      * `gemini`: one model, so the backend name IS the board id.
      * anything else: fall back to the backend name."""
    backend = call.backend
    if backend == "claude":
        # API mode names the exact model in argv0 (`Anthropic API <model>` [@ <base>]); use
        # it before defaulting to Opus, so an API-mode Fable (no paywall body) isn't
        # mis-attributed to the Opus seat (codex P2). Anchored on the full `Anthropic API `
        # prefix so a CLI-mode `claude-p` path containing `API ` isn't mistaken for a model.
        m = _CLAUDE_API_MODEL_RE.match(call.argv0)
        if m:
            return f"claude:{m.group('model')}"
        if _has_paywall_sentinel(call.body):
            return _CLAUDE_FABLE_MODEL
        return _CLAUDE_OPUS_MODEL
    if backend == "codex":
        m = _CODEX_MODEL_RE.search(call.argv0)
        if m:
            return f"codex:{m.group('model')}"
        return backend
    if backend in _SINGLE_MODEL_BACKENDS:
        return _SINGLE_MODEL_BACKENDS[backend]
    if backend == "opencode":
        # `opencode -m <provider/model>` -> board id `oc:<provider/model>` (the `_agentic`
        # board seat). A bare opencode call with no `-m` (shouldn't happen for board seats)
        # stays the backend name so it doesn't mis-attribute to a real seat.
        m = _OPENCODE_MODEL_RE.search(call.argv0)
        if m:
            return f"oc:{m.group('model')}"
        return backend
    if backend == "omp":
        # `omp -m <provider/model>` -> board id `omp:<provider/model>` (review_omp passes
        # it as header_argv0) — mirrors the opencode -> `oc:` mapping above, so every omp
        # seat gets its own health row instead of collapsing to one `omp` row (the
        # review-cli#24 class of bug). A bare omp call with no `-m` stays the backend
        # name so it doesn't mis-attribute to a real seat.
        m = _OPENCODE_MODEL_RE.search(call.argv0)
        if m:
            return f"omp:{m.group('model')}"
        return backend
    prefix = _BACKEND_BOARD_PREFIX.get(backend)
    if prefix is not None:
        m = _API_MODEL_RE.search(call.argv0)
        if m:
            return f"{prefix}:{m.group('model')}"
    return backend


def _board_models() -> list[dict]:
    """The canonical built-in model list (id/role/display) the health view covers. Imported
    lazily so the parser stays import-light and free of a config dependency at module load.

    Carries the 1-based raw-board PRIORITY so the Models tab and the Errors-tab fallback hint
    cover optional heavy-preset seats (Sol/Kimi) as well as the default preset -- Fable is
    excluded from every preset (paywalled, review-cli#280) but stays in the raw board. This
    returns FRESH dict copies so a caller can never mutate the shared cache (glm review
    finding 7 — the lookup runs once per failed call on the runs-list endpoint, so rebuilding
    the config objects each time was the wasteful part; copying small dicts is cheap)."""
    return [dict(b) for b in _board_models_cached()]


@lru_cache(maxsize=1)
def _board_models_cached() -> tuple[dict, ...]:
    from ..config import DEFAULT_BOARD

    return tuple(
        {"model": b.model, "role": b.role, "display": b.display, "priority": i + 1}
        for i, b in enumerate(DEFAULT_BOARD)
    )


def _call_is_clean_ok(call: "CallLog") -> bool:
    """True when a call FINISHED with a clean OK verdict (used for the recovery assessment).

    A footerless/in-flight call is not 'ok' (its outcome is unknown), an errored call is not,
    and an EXIT-0-but-empty/paywall call is not — only a real OK verdict counts as recovery."""
    return call.completed and not call.has_error and classify_call(call) == HEALTH_OK


def _fallback_seat_for(model_id: str) -> dict | None:
    """The next board seat (by priority) the failover pool would promote when ``model_id`` is
    down, or ``None`` if the model is off-board or already the lowest-priority seat.

    review-cli's pool runs the top-N AVAILABLE seats and backfills a failed seat from the
    next-priority reserve (config.select_pool / panel.run_board_with_failover). So the honest
    'planned fallback' for a failed seat is simply the next board seat after it.

    `model_id` is a `model_id_for_call` result, which shares the built-in board id scheme exactly
    (e.g. `commandcode:moonshotai/Kimi-K2.7-Code`, `zai:glm-5.2`), so the exact match below
    resolves every BOARD seat's fallback; an off-board model (e.g. `opencode`) correctly has
    none. Iterates the CACHED board tuple directly (no per-call rebuild — glm review finding 5)."""
    board = _board_models_cached()
    for idx, seat in enumerate(board):
        if seat["model"] == model_id:
            nxt = board[idx + 1] if idx + 1 < len(board) else None
            if nxt is None:
                return None
            return {
                "model": nxt["model"],
                "display": nxt["display"],
                "role": nxt["role"],
                "priority": nxt["priority"],
            }
    return None


def _dominant_class(classes: list[str]) -> str | None:
    """The most common failure class among a model's calls (hard-unavailable wins ties).

    Only failure classes count toward "dominant" — a model with mostly OK calls but a few
    failures reports the dominant FAILURE so the UI can label why it degrades, not 'ok'."""
    counts: dict[str, int] = {}
    for c in classes:
        if c in FAILURE_CLASSES:
            counts[c] = counts.get(c, 0) + 1
    if not counts:
        return None

    # Sort by frequency desc, then by hard-unavailable precedence, then name for stability.
    def _rank(item: tuple[str, int]) -> tuple[int, int, str]:
        cls, n = item
        hard = (
            HARD_UNAVAILABLE_CLASSES.index(cls)
            if cls in HARD_UNAVAILABLE_CLASSES
            else len(HARD_UNAVAILABLE_CLASSES)
        )
        return (-n, hard, cls)

    return sorted(counts.items(), key=_rank)[0][0]


def _model_is_problematic(
    ok_rate: float | None, classes_newest_first: list[str], current_class: str | None
) -> bool:
    """A model is problematic when ANY of:
      * it is currently in a hard-unavailable class (paywall/auth/blocked) — down NOW;
      * its fail-rate over the window meets/exceeds PROBLEMATIC_FAIL_RATE;
      * its most-recent PROBLEMATIC_RECENT_N calls are ALL failures (a fresh streak that an
        averaged rate would dilute).
    `classes_newest_first` is the per-call class list ordered newest first."""
    if current_class in HARD_UNAVAILABLE_CLASSES:
        return True
    if ok_rate is not None and (1.0 - ok_rate) >= PROBLEMATIC_FAIL_RATE:
        return True
    recent = classes_newest_first[:PROBLEMATIC_RECENT_N]
    if len(recent) >= PROBLEMATIC_RECENT_N and all(
        c in FAILURE_CLASSES for c in recent
    ):
        return True
    return False


def compute_model_health(sessions: list[Session]) -> dict:
    """Per-model health rollup for the dashboard's Models & roles tab.

    Walks every call across the (already window-bounded) sessions, attributes it to a
    MODEL id (commandcode/z.ai gateway model, Fable-vs-Opus split, etc.), classifies it, and
    rolls up per model: total/ok/fail counts, ok-rate, the dominant failure class, the most
    recent class, and a `problematic` flag. Built-in raw-board models with NO calls in the
    window are still listed (status `no_data`) so the view covers optional heavy-preset
    seats (Sol/Kimi) as well as the default preset and the excluded-but-still-raw-board
    Fable seat (paywalled, review-cli#280); any non-board model that appears in
    the logs is appended too. Returns `{"models": [...], "problematic_count": N}`;
    `problematic_count` is over built-in board models (the tab badge)."""
    # Gather calls per model, newest-first (sessions arrive newest-first; within a session
    # we keep call order then reverse so the most-recent call leads).
    per_model_classes: dict[str, list[str]] = {}
    for s in sessions:  # sessions are already sorted newest-first by the loader
        for call in reversed(s.calls):
            mid = model_id_for_call(call)
            per_model_classes.setdefault(mid, []).append(classify_call(call))

    board = _board_models()
    board_ids = {b["model"] for b in board}
    # Board models first (in board/priority order), then any extra model seen in the logs.
    ordered_ids = [b["model"] for b in board]
    for mid in per_model_classes:
        if mid not in board_ids:
            ordered_ids.append(mid)
    board_meta = {b["model"]: b for b in board}

    models_out: list[dict] = []
    problematic_count = 0
    for mid in ordered_ids:
        classes = per_model_classes.get(mid, [])  # newest first
        total = len(classes)
        ok = sum(1 for c in classes if c == HEALTH_OK)
        fail = total - ok
        ok_rate = round(ok / total, 4) if total else None
        current = classes[0] if classes else None
        dominant = _dominant_class(classes)
        meta = board_meta.get(mid)
        on_board = mid in board_ids
        if total == 0:
            problematic = False
            status = "no_data"
        else:
            problematic = _model_is_problematic(ok_rate, classes, current)
            # A problematic model surfaces its dominant FAILURE class as the status; a
            # not-problematic one is OK. `dominant` is guaranteed non-None when problematic
            # (problematic ⇒ >=1 failure ⇒ a dominant failure class exists), but fall back to
            # the generic ERROR class rather than rely on that coupling implicitly.
            status = (dominant or HEALTH_ERROR) if problematic else HEALTH_OK
        if problematic and on_board:
            problematic_count += 1
        models_out.append(
            {
                "model": mid,
                "display": meta["display"] if meta else mid,
                "role": meta["role"] if meta else None,
                "on_board": on_board,
                "calls": total,
                "ok": ok,
                "fail": fail,
                "ok_rate": ok_rate,
                "current_class": current,
                "dominant_class": dominant,
                "status": status,
                "problematic": problematic,
            }
        )
    return {"models": models_out, "problematic_count": problematic_count}
