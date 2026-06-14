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
_STDERR_PREFIX = "[stderr] "

# Default gap (seconds) that separates one session-burst from the next. Real logs show
# intra-session gaps of a few-to-~30s and inter-session gaps of 60s+, so 90s is a safe
# default; override via ?gap= on the API for re-clustering.
DEFAULT_SESSION_GAP_SECONDS = 90.0

# Largest plausible wall-time of a single call (review's default per-call timeout). Used
# to cap mtime-as-end-marker so an out-of-band touch can't balloon a session window.
_MAX_CALL_WALL = timedelta(seconds=1200)


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

    @property
    def duration_seconds(self) -> float | None:
        """Wall time from start (filename stamp) to last write (mtime).

        review-cli does not record an explicit duration, so this is the best honest
        proxy: file creation -> last flush. None if mtime is unavailable.
        """
        if self.mtime is None:
            return None
        d = (self.mtime - self.started).total_seconds()
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
    def has_error(self) -> bool:
        return self.timed_out or bool(self.stderr_lines) or _looks_like_error(self.body)

    @property
    def error_summary(self) -> str | None:
        if self.timed_out:
            return f"TIMEOUT after {self.timeout_secs}s"
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
    for i, line in enumerate(lines):
        if i == 0:
            hm = _HEADER_RE.match(line)
            if hm:
                # filename backend wins (header backend can be the same), but keep argv0.
                argv0 = hm.group("argv0")
                continue
        tm = _TIMEOUT_RE.match(line)
        if tm:
            timed_out = True
            timeout_secs = int(tm.group("secs"))
            continue
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
        size_bytes=size,
        mtime=mtime,
    )


_BS_HEADER_RE = re.compile(r"panel=(?P<panel>[^ ]*) moderator=(?P<mod>[^ ]*)")
_BS_PERSONA_RE = re.compile(r"^#### (?P<name>.+?) \((?P<model>[^)]+)\)\s*$")


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
            continue
        pm = _BS_PERSONA_RE.match(line)
        # A real persona heading is `#### {name} ({model})` where {model} is one of the
        # panel backends (see modes/brainstorm.py: label=f"{persona} ({model})"). Model
        # OUTPUT itself can contain `#### (a) ... (`SOME_MAP`...)` lines, so we ONLY accept
        # a heading whose parenthesized tail is a known panel model — otherwise it's body
        # text and belongs to the current persona's transcript.
        if pm and cur_round is not None and (not panel or pm.group("model") in panel):
            cur_persona = {"name": pm.group("name"), "model": pm.group("model"), "text": ""}
            cur_round["personas"].append(cur_persona)
            continue
        if cur_persona is not None:
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
    def has_error(self) -> bool:
        return any(c.has_error for c in self.calls)

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
            "has_brainstorm": self.brainstorm is not None,
            "topic": self.brainstorm.topic if self.brainstorm else None,
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
            if c.has_error:
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
        "success_rate": round(ok_calls / call_total, 4) if call_total else None,
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
    }
