"""Resumable brainstorm SESSIONS — list + resume the on-disk discussion logs.

WHAT THIS IS
------------
`review brainstorm` already persists every run as a round-by-round *discussion log*
under `process.log_dir()` (`<UTC-stamp>-brainstorm.md`), written line-buffered as each
round/decision lands so a crash / kill / timeout never loses the discussion-so-far.
That log is the persistence substrate for this feature — there is no separate session
store. This module:

  * SCANS `log_dir()` for `*-brainstorm.md` and parses each (even an INTERRUPTED one:
    crashed mid-round, missing the Final synthesis, or with empty rounds where a model
    returned "(no output)") into a `Session` record;
  * derives a stable short SESSION ID from the log's filename stamp;
  * lets `review sessions -s <id>` RESUME a session — the parsed transcript + the saved
    topic / panel / moderator are handed back to `mode_brainstorm` which continues the
    round loop from `completed_round + 1` and produces the final synthesis.

LOG FORMAT (the contract this parser reads — see modes/brainstorm.py `_disc`)
----------------------------------------------------------------------------
    # Brainstorm: <topic>
    <!-- review:session <nonce> -->          <- per-run nonce, declared once
    <blank>
    panel=<csv> moderator=<a>>...>  rounds>=<N> max=<M>
    <blank>
    # Round 1
    <!-- review:round 1 nonce=<nonce> -->    <- structural sentinel (machine marker)
    #### <model>
    <body...>
    #### <model>
    <body...>
    <blank>
    ## Moderator (round 1)
    <body...>
    DECISION: STOP|CONTINUE
    ... (more rounds) ...
    # Final synthesis                        <- present ONLY on a completed run
    <!-- review:final nonce=<nonce> -->
    <body...>

A session is COMPLETED iff a nonce-valid `# Final synthesis` block exists, else INTERRUPTED.
(Pre-sentinel LEGACY logs lack the nonce lines; the parser falls back to a sequential-number
heuristic for them — see the INVARIANTS note below.) The
parser is deliberately lenient: a half-written final round, a missing moderator line,
or an empty `#### model` body (a backend that returned nothing) must not raise — listing
and resume both have to survive a crashed log.

INVARIANTS
----------
  * Read-only: listing never mutates a log, so it is concurrent-safe against a live
    brainstorm still appending to one (a torn final line just parses as a shorter
    transcript — the next resume re-runs that round).
  * `transcript_blocks` are rebuilt in the EXACT shape the brainstorm loop's `shared`
    join expects (`## Round N\n#### model\nbody ...`), so a resumed run feeds prior
    rounds to the personas identically to a fresh run — the round header in the *log*
    is a single `#`, but the loop's in-memory block is `##`, so we normalize on parse.
  * The discussion log interleaves STRUCTURE (round/moderator/synthesis headings) with
    raw MODEL OUTPUT, and a model can echo those headings in its body. The parser is
    therefore spoof-resistant via a per-run NONCE: the WRITER mints an unguessable token,
    declares it ONCE at the trusted header position (line index 1, right after the
    `# Brainstorm:` topic — `<!-- review:session <nonce> -->`), and stamps every structural
    sentinel with it (`<!-- review:round N nonce=<nonce> -->`, `<!-- review:final
    nonce=<nonce> -->`). The parser keys structure on a NONCE-VALID sentinel: a `# Round N`
    heading starts a round only when the next line is a round sentinel carrying the trusted
    nonce, and `# Final synthesis` completes only when the next line is the nonce'd final
    sentinel. The personas never see the nonce, so a model echoing `# Round 2` (even a
    SEQUENTIAL one — the case a sequence-only check missed) or reproducing the fixed marker
    text cannot stamp the nonce, and stays body text. The nonce is trusted ONLY from line 1
    (a model can author neither line 0 nor line 1 of the writer-owned header), so a forged
    `<!-- review:session ... -->` anywhere in a body is ignored. LEGACY logs predating the
    sentinel carry no nonce, so the parser falls back to the older heuristic (sequential round
    number + synthesis honoured only inside the active moderator block) — they stay
    listable/resumable, only with the weaker legacy guard; a legacy log RESUMED by the new
    writer stays fully legacy (the writer mints no mid-file nonce).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .process import log_dir

# The discussion-log filename: `<stamp>-brainstorm.md`, stamp == the UTC strftime
# `%Y%m%dT%H%M%S_%fZ` the writer uses (e.g. 20260616T013310_280992Z).
_LOG_SUFFIX = "-brainstorm.md"
_STAMP_RE = re.compile(r"^(\d{8}T\d{6})(?:_(\d{1,6}))?Z?$")


@dataclass
class RoundBlock:
    """One parsed round: its number + the verbatim `#### model\\nbody` text (already in
    the shape the brainstorm loop joins as `shared`). `moderator_stop` is True when this
    round's moderator emitted `DECISION: STOP` (None if there was no moderator line)."""

    number: int
    text: str
    moderator_stop: bool | None = None


@dataclass
class Session:
    """A parsed brainstorm session — the unit `sessions -a` lists and `-s <id>` resumes."""

    session_id: str
    path: Path
    topic: str
    panel: list[str]
    moderator: str
    min_rounds: int
    max_rounds: int
    rounds: list[RoundBlock] = field(default_factory=list)
    completed: bool = False  # True iff a Final synthesis block exists
    timestamp: datetime | None = None
    task_code: str | None = None

    @property
    def completed_rounds(self) -> int:
        """How many full transcript rounds were captured (the resume start is +1)."""
        return len(self.rounds)

    @property
    def status(self) -> str:
        return "completed" if self.completed else "interrupted"

    @property
    def stopped(self) -> bool:
        """True iff the LAST captured round's moderator emitted `DECISION: STOP` AND the
        round count is at/over the saved minimum — i.e. the brainstorm had reached its
        natural end and only the final synthesis is missing. A resume of such a session
        should SYNTHESIZE, not run more rounds (the moderator already said stop), even
        though no `# Final synthesis` was written (the run crashed between the STOP and
        the synthesis)."""
        if not self.rounds:
            return False
        min_floor = max(self.min_rounds, 1)
        return bool(self.rounds[-1].moderator_stop) and self.completed_rounds >= min_floor

    def transcript_blocks(self) -> list[str]:
        """The prior rounds in the EXACT shape the brainstorm loop keeps in memory
        (`## Round N\\n<#### model body ...>`), so a resumed run feeds them to the
        personas byte-for-byte like a fresh run would have."""
        return [f"## Round {rb.number}\n{rb.text}" for rb in self.rounds]


def _derive_id(path: Path) -> str:
    """A short, stable id from the log filename stamp.

    The filename is `<stamp>-brainstorm.md`; the stamp already encodes the UTC time to
    the microsecond, so it is unique per run and stable across reads. We keep the
    compact `YYYYMMDDTHHMMSS` prefix (drop the `_<micros>Z` tail) as the *display* id —
    short enough to type, and `find_session` accepts any unambiguous prefix so two runs
    in the same second (their micros differ) are still individually addressable by a
    longer prefix. A filename that does not match the stamp pattern falls back to the
    whole stem so it is still listable/resumable (never silently dropped)."""
    stem = path.name[: -len(_LOG_SUFFIX)] if path.name.endswith(_LOG_SUFFIX) else path.stem
    m = _STAMP_RE.match(stem)
    if not m:
        return stem
    return m.group(1)


def _parse_timestamp(path: Path) -> datetime | None:
    stem = path.name[: -len(_LOG_SUFFIX)] if path.name.endswith(_LOG_SUFFIX) else path.stem
    m = _STAMP_RE.match(stem)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


_META_RE = re.compile(
    r"panel=(?P<panel>\S*)\s+moderator=(?P<mod>\S*)\s+rounds>=(?P<min>\d+)\s+max=(?P<max>\d+)"
    r"(?:\s+task=(?P<task>\S+))?"
)

# UNFORGEABLE structural sentinels the WRITER (modes/brainstorm.py) emits on their own line
# at column 0, right after the `# Round N` / `# Final synthesis` heading, each stamped with a
# per-run NONCE declared once in the header (`<!-- review:session <nonce> -->`). The parser
# keys structure on a sentinel whose nonce matches a session nonce DECLARED IN THIS FILE — a
# persona body never sees the nonce, so it cannot forge a round/final delimiter even by
# reproducing the fixed marker text or quoting this diff. SYNC: formats mirror
# `brainstorm._SESSION_SENTINEL` / `_ROUND_SENTINEL` / `_FINAL_SENTINEL` (change together).
_SESSION_SENTINEL_RE = re.compile(r"^<!-- review:session ([0-9a-fA-F]+) -->\s*$")
_ROUND_SENTINEL_RE = re.compile(r"^<!-- review:round (\d+) nonce=([0-9a-fA-F]+) -->\s*$")
_FINAL_SENTINEL_RE = re.compile(r"^<!-- review:final nonce=([0-9a-fA-F]+) -->\s*$")


def parse_log(path: Path) -> Session:
    """Parse one `*-brainstorm.md` discussion log into a `Session`.

    Tolerant by design: a crashed log (no Final synthesis, a half-written last round, a
    `#### model` block with an empty / `(no output)` body) parses without raising. The
    topic / panel / moderator come from the header; the rounds from the `# Round N`
    blocks; `completed` is set iff a `# Final synthesis` heading is present.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    topic = ""
    panel: list[str] = []
    moderator = ""
    min_rounds = 0
    max_rounds = 0
    completed = False
    task_code: str | None = None

    # Header: `# Brainstorm: <topic>` then a `panel=… moderator=… rounds>=N max=M` line.
    for line in lines[:8]:
        if line.startswith("# Brainstorm:"):
            topic = line[len("# Brainstorm:"):].strip()
        else:
            mm = _META_RE.search(line)
            if mm:
                panel = [p for p in mm.group("panel").split(",") if p]
                moderator = mm.group("mod")
                min_rounds = int(mm.group("min"))
                max_rounds = int(mm.group("max"))
                task_code = mm.group("task")

    # The set of session nonces this file declared at its TRUSTED header position. A sentinel-
    # era log declares exactly one (line index 1); a pre-sentinel LEGACY log declares none (so
    # this set is empty and the parser uses the sequential-heuristic fallback throughout). A
    # structural round/final sentinel counts ONLY when its nonce is in this set — which a
    # persona body (never shown the nonce) cannot satisfy.
    #
    # CRITICAL (codex P2): the session-nonce declaration is trusted ONLY from the ONE position
    # a model can NEVER author — line index 1, immediately after the `# Brainstorm:` topic on
    # line 0. The writer owns the file header absolutely; persona/moderator output is always
    # appended far below it. A model body that includes a `<!-- review:session forged -->`
    # line (even right after a forged `# Brainstorm:` / `# Resumed` line in its own text) is
    # NOT at line 1, so it is ignored — it cannot inject a nonce and then self-consistently
    # forge round/final sentinels. (The writer never declares a SECOND session nonce on
    # resume: a sentinel-era log reuses its header nonce; a legacy resume appends NO sentinels
    # at all, staying legacy end-to-end — see modes/brainstorm.py. So one trusted header line
    # is the whole authority.)
    valid_nonces: set[str] = set()
    trusted_session_lines: set[int] = set()  # the single trusted session-sentinel line index
    if len(lines) >= 2 and lines[0].startswith("# Brainstorm:"):
        sm = _SESSION_SENTINEL_RE.match(lines[1])
        if sm:
            valid_nonces.add(sm.group(1))
            trusted_session_lines.add(1)

    def _round_sentinel_ok(ln: str) -> int | None:
        """The round number iff `ln` is a round sentinel carrying a DECLARED nonce, else None."""
        m = _ROUND_SENTINEL_RE.match(ln)
        if m and m.group(2) in valid_nonces:
            return int(m.group(1))
        return None

    def _final_sentinel_ok(ln: str) -> bool:
        m = _FINAL_SENTINEL_RE.match(ln)
        return m is not None and m.group(1) in valid_nonces

    # Walk the body, splitting on the top-level headings. The round heading in the LOG is
    # a single `#` (`# Round N`); the moderator/synthesis use `## `/`# Final synthesis`.
    rounds: list[RoundBlock] = []
    cur_round_no: int | None = None
    cur_body: list[str] = []
    cur_mod_stop: bool | None = None
    in_moderator = False

    round_hdr = re.compile(r"^# Round (\d+)\s*$")
    mod_hdr = re.compile(r"^## Moderator \(round (\d+)\)\s*$")
    expected_round = 1  # the next round number a sentinel-LESS (legacy) heading may start

    # Structure is decided PER HEADING with a RUNNING "nonce regime" flag, not a whole-file
    # mode. `nonce_regime` flips True once the trusted line-1 session sentinel is crossed (it
    # sits in the header, before round 1); from then on a heading is structural ONLY via its
    # nonce'd sentinel. A LEGACY file (no trusted header nonce) never enters the regime, so
    # every heading uses the sequential-number fallback — including the sentinel-LESS rounds a
    # legacy resume appends, which keeps that whole file legacy end-to-end (the writer mints no
    # mid-file nonce — codex P2). For each `# Round N`:
    #   * next line is a round sentinel carrying the trusted nonce -> structural (unforgeable);
    #   * else, while NOT yet in the nonce regime (a legacy file / before the header sentinel)
    #     -> sequential-number fallback (the weaker legacy guard);
    #   * else (inside the nonce regime, heading without its sentinel) -> body text, since a
    #     real writer-emitted heading there always carries its sentinel.
    # `# Final synthesis` completes only when followed by a nonce-valid final sentinel — or, in
    # the legacy region, only inside the active moderator block.
    nonce_regime = False

    def _flush_round() -> None:
        nonlocal cur_round_no, cur_body, cur_mod_stop, in_moderator
        if cur_round_no is None:
            return
        body = "\n".join(cur_body).strip("\n")
        rounds.append(RoundBlock(number=cur_round_no, text=body, moderator_stop=cur_mod_stop))
        cur_round_no = None
        cur_body = []
        cur_mod_stop = None
        in_moderator = False

    for idx, line in enumerate(lines):
        next_line = lines[idx + 1] if idx + 1 < len(lines) else ""
        # Crossing a WRITER-TRUSTED session-sentinel line (header or post-`# Resumed`) enters
        # the strict nonce regime for every heading BELOW it, and is swallowed as metadata. A
        # persona-forged session sentinel (an UNtrusted position) is NOT honoured here — it
        # falls through to body text, so it can neither flip the regime nor be mistaken for
        # structure.
        if idx in trusted_session_lines:
            nonce_regime = True
            continue
        rm = round_hdr.match(line)
        if rm:
            num = int(rm.group(1))
            sentinel_num = _round_sentinel_ok(next_line)
            if sentinel_num is not None:
                is_structural = sentinel_num == num
            elif not nonce_regime:
                is_structural = num == expected_round
            else:
                is_structural = False  # nonce regime, heading without its sentinel -> body
            if is_structural:
                _flush_round()
                cur_round_no = num
                expected_round = num + 1
                cur_body = []
                cur_mod_stop = None
                in_moderator = False
                continue
        # A declared-nonce round sentinel line is structural metadata, never body text —
        # swallow it (the `# Round N` heading above already opened the round). A sentinel with
        # an UNKNOWN nonce (a persona forging the marker text but not the nonce) is NOT
        # swallowed: it falls through to body, preserving it verbatim in the transcript.
        if _round_sentinel_ok(line) is not None:
            continue
        # `# Final synthesis` marks completion. It completes ONLY when the next line is a
        # nonce-valid final sentinel — a persona echoing `# Final synthesis` cannot stamp the
        # nonce. In the legacy region (no nonce regime yet) it falls back to the older guard
        # (inside the active moderator block).
        if line.rstrip() == "# Final synthesis":
            if _final_sentinel_ok(next_line):
                _flush_round()
                completed = True
                break
            if not nonce_regime and in_moderator:
                _flush_round()
                completed = True
                break
        if _final_sentinel_ok(line):
            continue  # swallow the declared-nonce final sentinel itself
        mh = mod_hdr.match(line)
        # The moderator block ends a round's transcript body; honour it only INSIDE a
        # round and only for the CURRENT round number (a body echoing `## Moderator
        # (round 9)` won't match the active round). Capture its STOP/CONTINUE but do NOT
        # fold it into the persona transcript (the loop re-derives the moderator prompt).
        if mh and cur_round_no is not None and int(mh.group(1)) == cur_round_no and not in_moderator:
            in_moderator = True
            cur_mod_stop = False
            continue
        if cur_round_no is None:
            continue  # still in the header region (before the first real round)
        if in_moderator:
            if "DECISION: STOP" in line.upper():
                cur_mod_stop = True
            continue
        cur_body.append(line)

    _flush_round()

    return Session(
        session_id=_derive_id(path),
        path=path,
        topic=topic,
        panel=panel,
        moderator=moderator,
        min_rounds=min_rounds,
        max_rounds=max_rounds,
        rounds=rounds,
        completed=completed,
        timestamp=_parse_timestamp(path),
        task_code=task_code,
    )


def list_sessions(*, include_dead: bool = False, limit: int = 20) -> list[Session]:
    """All brainstorm sessions, newest first.

    By DEFAULT (`include_dead=False`) only COMPLETED sessions (those with a Final
    synthesis) are returned, capped at `limit` — the sensible "recent finished work"
    subset. `include_dead=True` (the `-a/--all` flag) additionally returns INTERRUPTED
    sessions (crashed / killed / timed out — no synthesis) and lifts the cap, so the
    full history including dead runs is visible. Read-only: a live brainstorm still
    appending to its log is safe to list (it parses as a shorter transcript).
    """
    base = log_dir()
    paths = sorted(base.glob(f"*{_LOG_SUFFIX}"), key=lambda p: p.name, reverse=True)
    out: list[Session] = []
    for p in paths:
        try:
            sess = parse_log(p)
        except OSError:
            continue  # unreadable file (perms / race) — skip, never abort the listing
        if not include_dead and not sess.completed:
            continue
        out.append(sess)
        if not include_dead and len(out) >= limit:
            break
    return out


def find_session(session_id: str) -> Session | None:
    """Resolve a session by its id (exact or an unambiguous PREFIX). Scans the full
    history (dead sessions included) so a crashed session is resumable. Returns None for
    an unknown id; raises `AmbiguousSessionError` when a short prefix matches >1 session
    so the caller can ask the user to disambiguate rather than silently resuming the
    wrong one."""
    base = log_dir()
    matches: list[Session] = []
    full_stems: list[str] = []
    for p in sorted(base.glob(f"*{_LOG_SUFFIX}"), key=lambda p: p.name, reverse=True):
        sid = _derive_id(p)
        stem = p.name[: -len(_LOG_SUFFIX)] if p.name.endswith(_LOG_SUFFIX) else p.stem
        # Match the short display id (exact or prefix) OR the full filename stem (so two
        # same-second sessions are still individually addressable by their full stamp).
        if sid == session_id or sid.startswith(session_id) or stem == session_id or stem.startswith(session_id):
            try:
                matches.append(parse_log(p))
                full_stems.append(stem)
            except OSError:
                continue
    if not matches:
        return None
    # An EXACT full-stem match is unambiguous even if the display id collides.
    for m, stem in zip(matches, full_stems):
        if stem == session_id:
            return m
    if len(matches) > 1:
        # Use the full stems as the disambiguating candidates: when two runs share a
        # display id (same second), their stems differ in the microsecond tail, so the
        # message tells the user exactly which fuller id to type.
        raise AmbiguousSessionError(session_id, full_stems)
    return matches[0]


def resume_session(
    sess: Session,
    *,
    models: list[str],
    cwd: Path,
    timeout: int,
    moderators: list[str],
    diff: str = "",
    force: bool = False,
) -> int:
    """Continue a brainstorm session's round loop from `completed_rounds + 1` and produce
    the final synthesis. Topic / panel / moderator come from the SAVED session (the caller
    passes them in resolved); the prior transcript is seeded so the personas see the same
    history a continuous run would have. The continued rounds + the synthesis are APPENDED
    to the original discussion log.

    An ALREADY-COMPLETED session (it has a Final synthesis) raises
    `SessionAlreadyCompleteError` by default — re-running would burn model calls to redo
    finished work. `force=True` re-synthesizes from the saved transcript anyway (the
    documented "re-synthesize a completed session" escape hatch). A session with ZERO
    usable rounds is started from round 1 (there is nothing to continue, so it degrades
    to a fresh run over the saved topic).
    """
    from .modes.brainstorm import brainstorm_pool, mode_brainstorm

    if sess.completed and not force:
        raise SessionAlreadyCompleteError(sess.session_id)

    start_round = sess.completed_rounds + 1
    # The persona rotation advances by `max(3, len(panel))` slots PER completed round (the
    # exact slot count `mode_brainstorm` fills). Reconstruct it from the SAVED panel so a
    # resume continues the rotation where it left off, even if the live `models` differ.
    saved_panel_size = len(sess.panel) or len(models)
    slots_per_round = len(brainstorm_pool([""] * saved_panel_size)) if saved_panel_size else 3
    seed_persona_index = sess.completed_rounds * slots_per_round

    # SYNTHESIZE-ONLY (skip the round loop, go straight to a fresh synthesis over the
    # seed) when the brainstorm had already reached its natural end and only the synthesis
    # is missing:
    #   * a COMPLETED session re-run under --force (it already has a synthesis), OR
    #   * an interrupted session whose last round emitted DECISION: STOP at/after the min
    #     (`sess.stopped`) — the moderator decided to stop but the run crashed before the
    #     synthesis was written; running MORE rounds would contradict that decision.
    # `synthesize_only` is needed because a low saved-cap cannot be honoured by passing a
    # low max (mode_brainstorm re-floors min_rounds to 5).
    synthesize_only = sess.completed or sess.stopped
    # Otherwise honour the saved caps EXACTLY — do NOT inflate max by start_round. An
    # interrupted session that already ran every round (crashed at the cap, just before
    # synthesis) has start_round > max_rounds, so the loop is empty and the run synthesizes
    # straight away (no spurious extra round). mode_brainstorm re-clamps to its min>=5 /
    # max>=min invariants.
    saved_max = max(sess.max_rounds, 1)
    saved_min = max(sess.min_rounds, 1)

    old_task_env = os.environ.get("REVIEW_TASK_CODE")
    if sess.task_code:
        os.environ["REVIEW_TASK_CODE"] = sess.task_code
    try:
        return mode_brainstorm(
            sess.topic,
            models,
            cwd,
            timeout,
            moderators,
            saved_min,
            saved_max,
            diff=diff,
            synthesize_only=synthesize_only,
            seed_transcript=sess.transcript_blocks(),
            seed_persona_index=seed_persona_index,
            start_round=start_round,
            resume_log=sess.path,
        )
    finally:
        if sess.task_code:
            if old_task_env is None:
                os.environ.pop("REVIEW_TASK_CODE", None)
            else:
                os.environ["REVIEW_TASK_CODE"] = old_task_env


class AmbiguousSessionError(Exception):
    """A short session-id prefix matched more than one session."""

    def __init__(self, prefix: str, candidates: list[str]) -> None:
        self.prefix = prefix
        self.candidates = candidates
        super().__init__(
            f"session id '{prefix}' is ambiguous: matches {', '.join(candidates)}"
        )


class SessionAlreadyCompleteError(Exception):
    """A resume was requested on a session that already has a Final synthesis. Resume
    `force=True` overrides (re-synthesize); the CLI surfaces this as a friendly message
    pointing at `--force`."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(
            f"session '{session_id}' already completed (has a final synthesis); "
            "pass --force to re-synthesize"
        )
