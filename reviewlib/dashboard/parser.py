"""Parse review-cli's real on-disk artifacts into structured runs/sessions/stats.

review-cli does NOT emit a structured run record today — the only durable trace of a
run is the per-CALL streamed log files (and the brainstorm discussion `.md`) that
`reviewlib.process` / `reviewlib.modes.brainstorm` write into `log_dir()`:

  per-call log : ``{UTCstamp}-{backend}-r{round}.log``
                 line 0 = ``[review-cli] {backend}: {argv0} (args redacted)``
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

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Per-call log filename: 20260613T040611_516399Z-claude-r0.log
_CALL_RE = re.compile(r"^(\d{8}T\d{6})_(\d+)Z-(.+)-r(\d+)\.log$")
# Brainstorm discussion md: 20260613T114552_999796Z-brainstorm.md
_BRAINSTORM_RE = re.compile(r"^(\d{8}T\d{6})_(\d+)Z-brainstorm\.md$")
_HEADER_RE = re.compile(r"^\[review-cli\] (?P<backend>.+?): (?P<argv0>.*?) \(args redacted\)\s*$")
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
# The dashboard's "Models & roles" tab surfaces, per board model, an ok-rate and the
# dominant failure class so the CTO can see WHY a model is down at a glance. The classes
# below are the real failure modes observed in `~/Library/Logs/review-cli/*.log` (see the
# CHANGELOG / the HYP diagnosis): the per-call EXIT code + stderr + body sentinels each
# map to one of these. `OK` and `EMPTY` are the two EXIT-0 outcomes (real verdict vs no
# content); the rest are hard failures. Order here is the tie-break for "dominant class"
# when two classes are equally frequent (hard-unavailable beats soft).
HEALTH_OK = "ok"
HEALTH_PAYWALL = "paywall"  # body says "currently unavailable" (Fable). EXIT is often 0.
HEALTH_AUTH = "auth"  # EXIT 401 / stderr {"error":"bad key"} (z.ai / GLM bad key).
HEALTH_BLOCKED = "blocked"  # EXIT 403 / "error code: 1010" (Cloudflare bot block).
HEALTH_TIMEOUT = "timeout"  # EXIT 124 / "timed out".
HEALTH_EMPTY = "empty"  # EXIT 0 but no real content (output_tokens=0 / framing-only body).
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
# Backend-name -> board model id for the single-model backends (the agentic codex CLI and
# the gemini REST route each map to exactly one board seat).
_SINGLE_MODEL_BACKENDS = {"codex": "codex", "gemini": "gemini"}
# `commandcode API <model>` / `z.ai API <model>` header argv0 reveals the gateway model.
_API_MODEL_RE = re.compile(r"\bAPI\s+(?P<model>\S+)")
# z.ai's board seat id is `zai:<model>` (config DEFAULT_BOARD), but the log backend/header
# spell it `z.ai`. Normalise the header model to the board prefix so attribution lines up.
_BACKEND_BOARD_PREFIX = {"commandcode": "commandcode", "z.ai": "zai"}


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
        return self.exit_code is not None or self.timed_out or bool(self.stderr_lines) or _looks_like_error(self.body)

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
            "round": self.round,
            "argv0": self.argv0,
            "duration_seconds": self.duration_seconds,
            "timed_out": self.timed_out,
            "timeout_secs": self.timeout_secs,
            "exit_code": self.exit_code,
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
    mtime: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "started": self.started.isoformat(),
            "topic": self.topic,
            "panel": self.panel,
            "moderator": self.moderator,
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
        scan_start = (exit_line_idx - 1) if exit_line_idx is not None else (len(lines) - 1)
        for j in range(scan_start, -1, -1):
            if not lines[j].strip():
                continue  # skip blank lines between body and footer
            if _TIMEOUT_RE.match(lines[j]):
                timeout_line_idx = j
            break  # only the trailing-most non-body line is decisive
    for i, line in enumerate(lines):
        if i == exit_line_idx:
            continue  # the authoritative trailing status footer — kept out of the body
        if i == 0:
            hm = _HEADER_RE.match(line)
            if hm:
                # filename backend wins (header backend can be the same), but keep argv0.
                argv0 = hm.group("argv0")
                continue
        if i == timeout_line_idx:
            tm = _TIMEOUT_RE.match(line)
            timed_out = True
            timeout_secs = int(tm.group("secs"))
            continue  # the authoritative timeout marker — kept out of the body
        if line.startswith(_STDERR_PREFIX):
            stderr_lines.append(line[len(_STDERR_PREFIX):])
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
        size_bytes=size,
        mtime=mtime,
    )


_BS_HEADER_RE = re.compile(r"panel=(?P<panel>[^ ]*) moderator=(?P<mod>[^ ]*)")
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
            topic = line[len("# Brainstorm:"):].strip()
            continue
        hm = _BS_HEADER_RE.search(line)
        if hm and not panel:
            panel = [p for p in hm.group("panel").split(",") if p]
            moderator = hm.group("mod") or None
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
        if pm and cur_round is not None and not in_control and (not panel or pm.group("model") in panel):
            cur_persona = {"name": pm.group("name"), "model": pm.group("model"), "text": ""}
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
        return list(dict.fromkeys(
            inv for c in self.calls if (inv := (c.argv0 or "").strip())
        ))

    @property
    def has_error(self) -> bool:
        return any(c.has_error for c in self.calls)

    @property
    def running(self) -> bool:
        """True if the session has no errors but at least one call has not FINISHED
        (a footerless in-flight / aborted call). The UI shows these as running/unknown
        rather than a green OK badge (codex P2)."""
        return not self.has_error and any(not c.completed for c in self.calls)

    @property
    def errors(self) -> list[dict]:
        out = []
        for c in self.calls:
            if c.has_error:
                out.append({"backend": c.backend, "round": c.round, "summary": c.error_summary, "filename": c.filename})
        return out

    def to_summary(self) -> dict:
        return {
            "session_id": self.session_id,
            "started": self.started.isoformat(),
            "ended": self.ended.isoformat(),
            "duration_seconds": self.duration_seconds,
            "mode": self.mode,
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
                entry = seen.setdefault(key, {"role": p["name"], "models": [], "count": 0})
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
        if cur is None or (call.started - cur.ended).total_seconds() > gap_seconds:
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


def load_sessions(log_dir_path: Path, gap_seconds: float = DEFAULT_SESSION_GAP_SECONDS) -> list[Session]:
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


def compute_stats(sessions: list[Session]) -> dict:
    """Aggregate counts/metrics across sessions for the Stats/Metrics/Models panels."""
    by_mode: dict[str, int] = {}
    by_model: dict[str, int] = {}
    by_role: dict[str, int] = {}
    by_day: dict[str, int] = {}
    durations: list[float] = []
    call_total = 0
    error_calls = 0
    timeout_calls = 0
    ok_calls = 0
    running_calls = 0
    for s in sessions:
        by_mode[s.mode] = by_mode.get(s.mode, 0) + 1
        by_day[s.started.date().isoformat()] = by_day.get(s.started.date().isoformat(), 0) + 1
        for m in s.models:
            by_model[m] = by_model.get(m, 0) + 1
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

    return {
        "session_count": len(sessions),
        "call_count": call_total,
        "ok_calls": ok_calls,
        "error_calls": error_calls,
        "timeout_calls": timeout_calls,
        "running_calls": running_calls,
        # success_rate is over COMPLETED calls only (ok + error) — an in-flight / aborted
        # footerless call has no known outcome and must not drag the rate either way.
        "success_rate": round(ok_calls / (ok_calls + error_calls), 4) if (ok_calls + error_calls) else None,
        "by_mode": by_mode,
        "by_model": by_model,
        "by_role": by_role,
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
    }


def _normalize_body(text: str) -> str:
    """Lower-case + strip ALL whitespace, so a sentinel survives the logger's whitespace
    collapsing. The Fable paywall body lands on disk as `ClaudeFable5iscurrentlyunavailable`
    (interior spaces gone), so a spaced `currently unavailable` match would miss it; we
    compare against the fully de-spaced form instead."""
    return re.sub(r"\s+", "", text).lower()


def _body_has_real_content(call: "CallLog") -> bool:
    """Did this EXIT-0 call return a real verdict, or is it empty/framing-only?

    A call is EMPTY when it exited cleanly but produced no usable review: an explicit
    `output_tokens=0` usage line, or a body that — once the `[review-cli] …` framing and
    stderr/usage lines are dropped — has no remaining prose. (The body has already had the
    header + EXIT/timeout footer stripped by the parser; what remains can still be a usage
    line or pure whitespace.)"""
    # Explicit zero-output usage line is the strongest empty signal.
    if re.search(r"\boutput_tokens=0\b", call.body):
        return False
    for raw in call.body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("[review-cli]"):
            continue  # framing line
        if line.startswith("[reasoning_content"):
            continue  # reasoning-only, no final answer
        if re.fullmatch(r"(prompt_tokens=\d+\s*)?(output_tokens=\d+\s*)?", line):
            continue  # a bare usage line is not content
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
    if _PAYWALL_SENTINEL in _normalize_body(call.body):
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
      * `claude`: the wrapper argv0 is identical for Fable and Opus, so the model is inferred
        from the body — a paywall body = Fable, anything else = the Opus seat.
      * `codex` / `gemini`: one model each; the backend name IS the board id.
      * anything else: fall back to the backend name."""
    backend = call.backend
    if backend == "claude":
        if _PAYWALL_SENTINEL in _normalize_body(call.body):
            return _CLAUDE_FABLE_MODEL
        return _CLAUDE_OPUS_MODEL
    if backend in _SINGLE_MODEL_BACKENDS:
        return _SINGLE_MODEL_BACKENDS[backend]
    prefix = _BACKEND_BOARD_PREFIX.get(backend)
    if prefix is not None:
        m = _API_MODEL_RE.search(call.argv0)
        if m:
            return f"{prefix}:{m.group('model')}"
    return backend


def _board_models() -> list[dict]:
    """The canonical board model list (id/role/display) the health view covers. Imported
    lazily so the parser stays import-light and free of a config dependency at module load."""
    from ..config import DEFAULT_BOARD

    return [{"model": b.model, "role": b.role, "display": b.display} for b in DEFAULT_BOARD]


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
        hard = HARD_UNAVAILABLE_CLASSES.index(cls) if cls in HARD_UNAVAILABLE_CLASSES else len(HARD_UNAVAILABLE_CLASSES)
        return (-n, hard, cls)

    return sorted(counts.items(), key=_rank)[0][0]


def _model_is_problematic(ok_rate: float | None, classes_newest_first: list[str], current_class: str | None) -> bool:
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
    if len(recent) >= PROBLEMATIC_RECENT_N and all(c in FAILURE_CLASSES for c in recent):
        return True
    return False


def compute_model_health(sessions: list[Session]) -> dict:
    """Per-model health rollup for the dashboard's Models & roles tab.

    Walks every call across the (already window-bounded) sessions, attributes it to a
    MODEL id (commandcode/z.ai gateway model, Fable-vs-Opus split, etc.), classifies it, and
    rolls up per model: total/ok/fail counts, ok-rate, the dominant failure class, the most
    recent class, and a `problematic` flag. Board models with NO calls in the window are
    still listed (status `no_data`) so the view covers the whole board; any non-board model
    that appears in the logs is appended too. Returns
    `{"models": [...], "problematic_count": N}`; `problematic_count` is over BOARD models
    (the tab badge), matching the spec."""
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
        models_out.append({
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
        })
    return {"models": models_out, "problematic_count": problematic_count}
