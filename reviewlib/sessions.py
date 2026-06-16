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
    <blank>
    panel=<csv> moderator=<a>>...>  rounds>=<N> max=<M>
    <blank>
    # Round 1
    #### <model>
    <body...>
    #### <model>
    <body...>
    <blank>
    ## Moderator (round 1)
    <body...>
    DECISION: STOP|CONTINUE
    ... (more rounds) ...
    # Final synthesis        <- present ONLY on a completed run
    <body...>

A session is COMPLETED iff a `# Final synthesis` block exists, else INTERRUPTED. The
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
    therefore spoof-resistant: a `# Round N` heading only starts a round when N is the
    next SEQUENTIAL number; `## Moderator (round N)` only counts for the active round;
    and `# Final synthesis` only completes a session when it appears inside the current
    round's moderator block (where the real synthesis is written) — never mid-persona.
    A persona echoing `# Final synthesis` or `# Round 9` stays body text. (Residual: a
    model that reproduces the FULL `#### model` + `## Moderator (round N)` + DECISION +
    `# Final synthesis` sequence verbatim and in order could still confuse parsing;
    that is far-fetched and the discussion log is a human-readable artifact we do not
    reshape to defend against it.)
"""
from __future__ import annotations

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
)


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

    # Walk the body, splitting on the top-level headings. The round heading in the LOG is
    # a single `#` (`# Round N`); the moderator/synthesis use `## `/`# Final synthesis`.
    rounds: list[RoundBlock] = []
    cur_round_no: int | None = None
    cur_body: list[str] = []
    cur_mod_stop: bool | None = None
    in_moderator = False

    # Exact full-line matches (`^...$`): a structural heading is the WHOLE line. The
    # round number must be SEQUENTIAL to count as structural — this is the key defense
    # against a model spoofing the log structure (codex HIGH): a persona body that
    # contains `# Round 9` or `## Moderator (round 9)` mid-stream does NOT match the
    # expected-next number, so it stays body text instead of starting a phantom round.
    # `# Final synthesis` / `## Moderator` are only honoured once we are INSIDE a round
    # (a body has started) and the moderator only after the round's persona body — a
    # leading-`#` line in the header region or before any round is never structural.
    round_hdr = re.compile(r"^# Round (\d+)\s*$")
    mod_hdr = re.compile(r"^## Moderator \(round (\d+)\)\s*$")
    expected_round = 1  # the next round number that may legitimately start

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

    for line in lines:
        rm = round_hdr.match(line)
        # Only a SEQUENTIAL round heading starts a new round; an out-of-sequence
        # `# Round N` (a model echoing the marker) is treated as plain body text.
        if rm and int(rm.group(1)) == expected_round:
            _flush_round()
            cur_round_no = int(rm.group(1))
            expected_round += 1
            cur_body = []
            cur_mod_stop = None
            in_moderator = False
            continue
        # `# Final synthesis` marks completion — but ONLY when we are inside the current
        # round's MODERATOR block (`in_moderator`). The real synthesis is always written
        # right after the final round's `## Moderator (...)` block, never inside a persona
        # body. Requiring `in_moderator` is the defense against a persona echoing
        # `# Final synthesis` in its output (codex HIGH): mid-body the flag is plain text,
        # so it cannot falsely complete-and-truncate the session.
        if line.rstrip() == "# Final synthesis" and in_moderator:
            _flush_round()
            completed = True
            break
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
