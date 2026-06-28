"""The deterministic bot Tier-1 DRIVER: parse a prose ``## Case:`` suite into send/expect
steps, boot the SUT bot against the fake Telegram, run each case, and emit the machine-parsed
``## QA RESULTS`` contract the executor's parser reads (spec §7.3 / §8).

This is the bot-kind counterpart to the un-caged executor: same OUTPUT contract
(``parse_qa_results`` reads it unchanged), but a fully deterministic, no-model run. A case is
"inject this inbound update -> assert the bot's outbound reply", which the fake Telegram makes
mechanical. The driver therefore:

  1. boots the bot pointed at the fake via ``TG_API_BASE`` (``bot_harness.boot_bot``);
  2. runs the POSITIVE capability probe (``bot_harness.probe_reachable``) so a never-reached
     fake fails LOUD with the ``TG_API_BASE`` pointer instead of false-passing on zero sends;
  3. for each ``## Case:`` block: injects its ``Send:`` update, waits for the bot's outbound,
     and matches the captured text against the case's ``Expect:`` assertions;
  4. tallies PASS / FAIL / BLOCKED and writes the ``## QA RESULTS`` block (verdict FAIL on any
     failed case, BLOCKED if the bot could not be brought up at all).

THE CASE GRAMMAR (a thin, validated superset of the free-form ``## Case:`` markdown). Inside a
case block, three directive lines drive the deterministic run:

    ## Case: /start greets the user
    Send: /start
    Expect: welcome
    Expect-no: error

  * ``Send:`` — the inbound text to inject (a leading ``/`` is a command, as a human would
    type). ``Send-callback:`` injects a button tap (callback_data) instead.
  * ``Expect:`` — a substring (case-insensitive) the bot's outbound reply MUST contain. Multiple
    ``Expect:`` lines all must match.
  * ``Expect-no:`` — a substring the reply must NOT contain.
  * ``Expect-silent`` (a bare directive) — the bot must send NOTHING for this case.

A case with no ``Send:`` is BLOCKED (the deterministic driver has nothing to inject) rather
than silently skipped — the prose-only cases are for the un-caged tester, not this hermetic
path. A suite mixing the two is fine: the driver runs the structured cases and BLOCKS the rest
with a clear reason, so the report is honest about what the hermetic run did and didn't cover.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import bot_harness as bh
from .config import BotConfig

# The case heading — kept identical to modes/qa.py's _CASE_HEADING_RE (same thing counted),
# imported there so the two never drift.
from ..modes.qa import _CASE_HEADING_RE

# Directive lines inside a case block. Case-insensitive; value is everything after the colon.
_SEND_RE = re.compile(r"^\s*send\s*:\s*(.+?)\s*$", re.IGNORECASE)
_SEND_CALLBACK_RE = re.compile(r"^\s*send-callback\s*:\s*(.+?)\s*$", re.IGNORECASE)
_EXPECT_RE = re.compile(r"^\s*expect\s*:\s*(.+?)\s*$", re.IGNORECASE)
_EXPECT_NO_RE = re.compile(r"^\s*expect-no\s*:\s*(.+?)\s*$", re.IGNORECASE)
_EXPECT_SILENT_RE = re.compile(r"^\s*expect-silent\s*$", re.IGNORECASE)

# AGENT-SIDE directive lines (a bridge bot driven by the agent's hook client; see bot_harness's
# agent-side seam). Checked BEFORE the inbound directives where prefixes overlap.
_ASK_QUESTION_RE = re.compile(r"^\s*ask-question\s*:\s*(.+?)\s*$", re.IGNORECASE)
_ASK_PERMISSION_RE = re.compile(r"^\s*ask-permission\s*:\s*(.+?)\s*$", re.IGNORECASE)
_EXPECT_CARD_RE = re.compile(r"^\s*expect-card\s*:\s*(.+?)\s*$", re.IGNORECASE)
_TAP_RE = re.compile(r"^\s*tap\s*:\s*(.+?)\s*$", re.IGNORECASE)
_EXPECT_ANSWER_RE = re.compile(r"^\s*expect-answer\s*:\s*(.+?)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class BotCase:
    """One parsed deterministic case. ``title`` labels it in the report. ``send`` is the inbound
    text (or ``None`` for a callback-only / un-driveable case); ``send_callback`` is a button
    tap. ``expect`` / ``expect_no`` are the substring assertions; ``expect_silent`` requires no
    reply. ``runnable`` is False for a prose-only case (no Send/Send-callback) — those BLOCK."""

    title: str
    send: str | None = None
    send_callback: str | None = None
    expect: tuple[str, ...] = ()
    expect_no: tuple[str, ...] = ()
    expect_silent: bool = False

    @property
    def runnable(self) -> bool:
        return self.send is not None or self.send_callback is not None


@dataclass(frozen=True)
class CaseResult:
    """The verdict for one driven case. ``status`` is PASS / FAIL / BLOCKED; ``detail`` is the
    human reason (the proof line in the report); ``severity`` is the finding tag for a FAIL
    (P1 by default — a wrong/absent reply is a real bug)."""

    title: str
    status: str
    detail: str
    severity: str | None = None


PASS = "PASS"
FAIL = "FAIL"
BLOCKED = "BLOCKED"


# --- parsing the prose suite into deterministic cases --------------------------------
def parse_bot_cases(suite_text: str) -> list[BotCase]:
    """Split a suite's concatenated markdown into ``BotCase`` objects, one per ``## Case:``
    block. Each block's directive lines (``Send:`` / ``Expect:`` / …) become the case's fields;
    a block with no ``Send`` / ``Send-callback`` parses to a non-runnable (prose-only) case the
    driver BLOCKS. Order is preserved so ``--max-cases`` and the report read left-to-right."""
    blocks = _split_into_case_blocks(suite_text)
    return [_parse_one_case(title, body) for title, body in blocks]


def _split_into_case_blocks(text: str) -> list[tuple[str, str]]:
    """Cut ``text`` at each ``## Case:`` heading into (title, body) pairs. The title is the
    heading's trailing text; the body is everything up to the next ``## Case:`` (or EOF)."""
    matches = list(_CASE_HEADING_RE.finditer(text))
    blocks: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        # End-of-heading-line: search from m.END(), not m.start(). The heading regex's leading
        # ``^\s*`` can swallow the newline BEFORE ``## Case:`` into the match, so a find from
        # m.start() would land on that PRECEDING newline and leave the title empty (-> the
        # case-N fallback). From m.end() (just past the colon) the next newline is the real
        # end of the heading line, and the title is the text between.
        heading_line_end = text.find("\n", m.end())
        if heading_line_end == -1:
            heading_line_end = len(text)
        title = text[m.end():heading_line_end].strip() or f"case-{i + 1}"
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[heading_line_end + 1:body_end]
        blocks.append((title, body))
    return blocks


def _parse_one_case(title: str, body: str) -> BotCase:
    send: str | None = None
    send_callback: str | None = None
    expect: list[str] = []
    expect_no: list[str] = []
    expect_silent = False
    for line in body.splitlines():
        if (m := _SEND_RE.match(line)) and send is None:
            send = m.group(1)
        elif (m := _SEND_CALLBACK_RE.match(line)) and send_callback is None:
            send_callback = m.group(1)
        elif m := _EXPECT_NO_RE.match(line):  # check Expect-no BEFORE Expect (prefix overlap)
            expect_no.append(m.group(1))
        elif m := _EXPECT_RE.match(line):
            expect.append(m.group(1))
        elif _EXPECT_SILENT_RE.match(line):
            expect_silent = True
    return BotCase(
        title=title, send=send, send_callback=send_callback,
        expect=tuple(expect), expect_no=tuple(expect_no), expect_silent=expect_silent,
    )


# --- driving the parsed cases against the fake ---------------------------------------
@dataclass
class BotRunResult:
    """The outcome of a full hermetic bot run: the per-case results + the rolled-up verdict.
    ``to_qa_results`` renders the ``## QA RESULTS`` block the executor's ``parse_qa_results``
    reads, so the bot path and the un-caged path produce the SAME machine-parsed contract."""

    results: list[CaseResult] = field(default_factory=list)
    blocked_reason: str | None = None  # set when the bot itself couldn't be brought up

    @property
    def verdict(self) -> str:
        """The rolled-up verdict. A FAIL anywhere fails the run; otherwise ANY blocked case (an
        authored case the hermetic driver could NOT exercise — e.g. a prose-only case with no
        Send: directive) makes the run BLOCKED, NOT PASS. A mix of passing + blocked cases must
        never report VERDICT: PASS while the report says "N blocked" — an unexercised authored
        case is not a green run (review finding). PASS only when EVERY case ran and passed."""
        if self.blocked_reason is not None:
            return BLOCKED
        if not self.results:
            return BLOCKED
        if any(r.status == FAIL for r in self.results):
            return FAIL
        if any(r.status == BLOCKED for r in self.results):
            return BLOCKED
        return PASS

    def to_qa_results(self, *, sut_path: Path, bring_up: str = "hermetic (fake Telegram)") -> str:
        run = len(self.results)
        passed = sum(1 for r in self.results if r.status == PASS)
        failed = sum(1 for r in self.results if r.status == FAIL)
        blocked = sum(1 for r in self.results if r.status == BLOCKED)
        return "\n".join([
            "## QA RESULTS",
            f"SUT: {sut_path}   KIND: bot   BRING-UP: {bring_up}",
            f"CASES: {run} run, {passed} passed, {failed} failed, {blocked} blocked",
            "",
            "### FINDINGS",
            *self._finding_lines(),
            "",
            "### BLOCKED",
            *self._blocked_lines(),
            "",
            f"VERDICT: {self.verdict}",
        ]) + "\n"

    def _finding_lines(self) -> list[str]:
        lines = [
            f"- [{r.severity or 'P1'}] {r.title} — {r.detail}"
            for r in self.results if r.status == FAIL
        ]
        if self.blocked_reason is not None:
            lines.insert(0, f"- [P0] hermetic bring-up — {self.blocked_reason}")
        return lines or ["no findings"]

    def _blocked_lines(self) -> list[str]:
        lines = [f"- {r.title} — {r.detail}" for r in self.results if r.status == BLOCKED]
        return lines or ["none"]


def run_bot_suite(
    *,
    cases: list[BotCase],
    fake: bh.FakeTelegram,
    skip_probe: bool = False,
    exit_boot_failed: int,
) -> BotRunResult:
    """Drive every ``runnable`` case against the already-booted bot + fake, classify, and
    return the rolled-up result. The POSITIVE capability probe runs FIRST (unless
    ``skip_probe``): a fake the bot never reaches yields a BLOCKED run with the ``TG_API_BASE``
    pointer, so 'zero sends' can't be mistaken for a clean pass. Assumes the bot was already
    booted by the caller (so boot failure is handled before this is reached); a probe failure
    here means the bot booted but its sender isn't wired to the fake.

    NOTE: ``run_hermetic_bot_test`` drives the probe ITSELF (so it can distinguish a CRASHED bot
    from an unwired one); this entry point keeps its own probe for a direct caller (and the
    ``skip_probe`` path), and is the seam the unit tests exercise."""
    if not skip_probe and not bh.probe_reachable(fake):
        return BotRunResult(blocked_reason=_UNWIRED_REASON)
    results = [_drive_one_case(c, fake) for c in cases]
    return BotRunResult(results=results)


def _drive_one_case(case: BotCase, fake: bh.FakeTelegram) -> CaseResult:
    """Inject the case's update, wait for the bot's reply, and classify against its Expects."""
    if not case.runnable:
        return CaseResult(
            title=case.title, status=BLOCKED,
            detail="no Send:/Send-callback: directive — a prose-only case the hermetic driver "
                   "cannot inject; author a Send:/Expect: pair to run it deterministically.",
        )
    since = time.monotonic()
    _inject_case(case, fake)
    if case.expect_silent:
        # A silent case has no "satisfied early" state — it must wait the (short) confirm window
        # to PROVE no reply arrived. Any captured call fails it.
        calls = fake.wait_until_satisfied(
            since, predicate=lambda c: bool(c), timeout=bh._SILENT_CONFIRM_TIMEOUT_S)
        return _classify(case, calls)
    # A case that EXPECTS a reply returns the MOMENT its expectations are met — so a multi-message
    # reply whose expected text is in a LATER message keeps the window open until it lands, rather
    # than false-failing on a 50ms grace (review finding). On timeout it returns whatever arrived
    # and the classifier reports the honest miss.
    calls = fake.wait_until_satisfied(
        since, predicate=lambda c: _expectations_met(case, c),
        timeout=bh._CASE_RESPONSE_TIMEOUT_S)
    return _classify(case, calls)


def _inject_case(case: BotCase, fake: bh.FakeTelegram) -> None:
    update_id = _next_update_id()
    if case.send is not None:
        fake.inject(bh.make_text_update(update_id, case.send))
    elif case.send_callback is not None:
        fake.inject(bh.make_callback_update(update_id, case.send_callback))


def _reply_text(calls: list[bh.OutboundCall]) -> str:
    """The captured outbound texts CONCATENATED — a multi-message reply (a bot that splits a long
    answer across sends) is matched collectively against the case's substring expectations."""
    return " || ".join(c.text for c in calls if c.text)


def _expectations_met(case: BotCase, calls: list[bh.OutboundCall]) -> bool:
    """Whether ``calls`` ALREADY satisfy a NON-silent case (used as the wait predicate so the
    driver returns the moment the reply matches, even if it arrives across several messages).
    Requires at least one reply, every ``Expect:`` substring present, and no ``Expect-no:``
    substring present. (Silent cases don't use this — they wait the window to prove silence.)"""
    if not calls:
        return False
    text = _reply_text(calls).lower()
    if any(needle.lower() not in text for needle in case.expect):
        return False
    return not any(needle.lower() in text for needle in case.expect_no)


def _classify(case: BotCase, calls: list[bh.OutboundCall]) -> CaseResult:
    """Match the captured outbound calls against the case's assertions.

    Order of checks: a case that ``Expect-silent`` must have NO outbound (any reply FAILs it);
    a case with ``Expect:``/``Expect-no:`` must have at least one reply whose CONCATENATED text
    satisfies every substring rule."""
    reply_text = _reply_text(calls)
    if case.expect_silent:
        if calls:
            return CaseResult(case.title, FAIL,
                              f"expected NO reply, but the bot sent: {reply_text!r}", "P1")
        return CaseResult(case.title, PASS, "bot stayed silent as expected")
    if not calls:
        return CaseResult(case.title, FAIL,
                          "the bot sent NO reply (expected one matching: "
                          f"{', '.join(case.expect) or '(any reply)'})", "P1")
    missing = [needle for needle in case.expect if needle.lower() not in reply_text.lower()]
    present_forbidden = [needle for needle in case.expect_no if needle.lower() in reply_text.lower()]
    if missing or present_forbidden:
        return CaseResult(case.title, FAIL, _mismatch_detail(reply_text, missing, present_forbidden), "P1")
    return CaseResult(case.title, PASS,
                      f"reply matched all expectations (captured: {reply_text!r})")


def _mismatch_detail(reply_text: str, missing: list[str], forbidden: list[str]) -> str:
    parts = []
    if missing:
        parts.append(f"reply is missing expected substring(s) {missing!r}")
    if forbidden:
        parts.append(f"reply contains forbidden substring(s) {forbidden!r}")
    return f"{'; '.join(parts)} — proof: captured reply was {reply_text!r}"


# A per-process monotonic update-id counter for injected CASE updates. It starts ABOVE the
# probe's update id (``bot_harness._PROBE_UPDATE_ID``): a long-poller acks updates by sending
# ``offset = last_update_id + 1``, so once it has seen the probe its offset is past the probe
# id and the fake will only DELIVER higher ids. A case update with a LOWER id would be filtered
# out as already-acked and never reach the bot (it would wait out the whole response window in
# silence). So every case id must exceed the probe id — start the counter just above it.
_UPDATE_ID = bh._PROBE_UPDATE_ID + 1000


def _next_update_id() -> int:
    global _UPDATE_ID
    _UPDATE_ID += 1
    return _UPDATE_ID


# --- the top-level hermetic orchestrator (the qa handler's entrypoint) ----------------
def run_hermetic_bot_test(
    *,
    suite_text: str,
    bot_config: BotConfig,
    cwd: Path,
    sut_path: Path,
    exit_boot_failed: int,
) -> str:
    """Run the full hermetic bot Tier-1 flow and return the ``## QA RESULTS`` transcript the
    qa handler feeds to ``parse_qa_results`` / ``verdict_to_exit_code`` (the SAME contract the
    un-caged executor emits, so the bot path slots into the existing verdict->exit mapping).

    Steps: start the fake Telegram, boot the SUT bot pointed at it via ``TG_API_BASE``, run
    the positive capability probe, drive every structured case, and ALWAYS tear the bot +
    fake down (try/finally) — a hermetic run leaks nothing. A boot failure or an unreached
    fake yields a BLOCKED transcript (the handler maps BLOCKED to ``exit_boot_failed``), never
    a traceback.

    ``cwd`` is where the bot command runs (the isolated worktree / the SUT path under
    --in-place); ``sut_path`` only labels the report. The deterministic driver needs no
    worktree isolation of its own — it spawns the bot the config names and reaps it — but the
    qa handler still runs it inside the worktree so the bot sees the committed tree, matching
    every other kind.

    AGENT-SIDE ROUTING. A ``sut.bot`` block with an ``ask_command`` (a bridge bot like tg-ctl)
    cannot be driven inbound — the loop is agent-emits-question -> card -> tap -> answer. Such a
    run is routed to ``run_agent_side_bot_test``; the inbound flow below is for ``Send:``/
    ``Expect:`` bots."""
    if bot_config.is_agent_side:
        return run_agent_side_bot_test(
            suite_text=suite_text, bot_config=bot_config, cwd=cwd, sut_path=sut_path,
            exit_boot_failed=exit_boot_failed,
        )
    cases = parse_bot_cases(suite_text)
    fake = bh.FakeTelegram()
    fake.start()
    bot: bh.BotProcess | None = None
    try:
        try:
            bot = bh.boot_bot(
                command=list(bot_config.command), cwd=cwd, api_base=fake.base_url(),
                extra_env=bot_config.env, exit_boot_failed=exit_boot_failed,
            )
        except bh.BotHarnessError as exc:
            return BotRunResult(blocked_reason=str(exc)).to_qa_results(sut_path=sut_path)
        result = _run_or_block_on_dead_bot(
            cases=cases, fake=fake, bot=bot, skip_probe=bot_config.skip_probe,
            exit_boot_failed=exit_boot_failed,
        )
        return result.to_qa_results(sut_path=sut_path)
    finally:
        # GUARANTEED teardown on every exit path — reap the bot tree first (so it stops polling
        # the fake), then stop the fake. Both are idempotent and never raise.
        if bot is not None:
            bot.reap()
        fake.stop()


def _run_or_block_on_dead_bot(
    *, cases: list[BotCase], fake: bh.FakeTelegram, bot: bh.BotProcess,
    skip_probe: bool, exit_boot_failed: int,
) -> BotRunResult:
    """Run the suite, distinguishing a CRASHED bot from an UNWIRED one.

    A bot that crashes on startup (bad token handling, missing dep) exits before it ever polls;
    the probe's silence would otherwise be misattributed to an unwired TG_API_BASE. The crash
    can land at ANY time relative to our check (a fresh fork may not have finished exiting when
    boot_bot returns), so we don't rely on a single ``poll()`` snapshot: we run the probe, and
    on a probe FAILURE re-check ``poll()`` — a dead process means it CRASHED (report its output),
    a live one that never sent is the genuine UNWIRED case the probe reports. ``skip_probe`` bot
    only gets the up-front liveness check (no probe to distinguish on)."""
    crash = _crash_block(bot)
    if crash is not None:
        return crash
    if skip_probe:
        return run_bot_suite(cases=cases, fake=fake, skip_probe=True, exit_boot_failed=exit_boot_failed)
    if not bh.probe_reachable(fake):
        # The probe found no outbound. Was it because the bot DIED mid-probe (crash) or because
        # it is alive but unwired? Re-check now that the probe window has elapsed.
        crash = _crash_block(bot)
        if crash is not None:
            return crash
        return BotRunResult(blocked_reason=_UNWIRED_REASON)
    # Probe passed — skip the driver's own probe (already proven reachable).
    results = [_drive_one_case(c, fake) for c in cases]
    return BotRunResult(results=results)


def _crash_block(bot: bh.BotProcess) -> BotRunResult | None:
    """A BLOCKED result if the bot process has EXITED (crashed before/while being driven), else
    ``None``. The output tail is the proof so a boot crash is diagnosable."""
    rc = bot.proc.poll()
    if rc is None:
        return None
    out = _drain_bot_output(bot)
    return BotRunResult(blocked_reason=(
        f"the bot process exited (code {rc}) before it could be driven — it crashed on "
        f"startup rather than long-polling the fake Telegram. Output tail:\n{out}"
    ))


_UNWIRED_REASON = (
    "the bot booted but made NO outbound call against the fake Telegram within the probe "
    "window — its sender is almost certainly NOT honoring TG_API_BASE (it likely hardcodes "
    "api.telegram.org). A hermetic run cannot capture sends until the sender reads TG_API_BASE. "
    "(Set sut.bot.skip_probe: true only if the bot legitimately never sends on /start.)"
)


def _drain_bot_output(bot: bh.BotProcess, *, limit: int = 2000) -> str:
    """The crashed bot's captured stdout tail for the BLOCKED proof. Reads the bounded buffer
    the BotProcess drain thread fills continuously from boot — NOT the pipe — so it never blocks
    even when a forked child still holds the pipe open (review finding). Never raises."""
    return bot.output_tail(limit=limit)


# =====================================================================================
# AGENT-SIDE tier: a bridge bot (tg-ctl) driven by the agent's hook client.
#
# The loop the inbound path cannot reach: the AGENT emits a question via a hook client
# (`ask_command`, e.g. `tg-ctl ask`) that reads a payload on stdin; the daemon forwards ONE
# inline-button CARD to (the fake) Telegram; the user TAPS; the answer flows back to the hook
# client's STDOUT. The driver below scripts that loop deterministically against the hermetic
# FakeTelegram: emit -> assert card count -> inject tap -> read the answer. Its regression value
# is the #98 class — a re-fire of an already-answered question must post NO second card and
# replay the stored answer (a duplicate card, or a lost tap/replay, is the bug).
# =====================================================================================

# A hermetic bot token when the SUT declares none. The numeric head IS the bot id tg-ctl keys
# its socket/registration/pid files on, so it must be all-digits. Never a real token.
_DEFAULT_HERMETIC_TOKEN = "7654321:hermetic-qa-token"
# A brief settle after the expected card count is reached, to catch a LATE extra card (the #98
# duplicate could land a beat after the first) before asserting the count.
_CARD_SETTLE_S = 0.3


@dataclass(frozen=True)
class AgentCase:
    """One parsed AGENT-SIDE case. ``payload`` is the raw hook-payload JSON written to the hook
    client's stdin (``None`` for a non-runnable, prose-only block). ``kind`` labels it
    question/permission for the report. ``expect_card`` is how many NEW inline-button cards this
    emit must produce (1 by default; 0 is the #98 re-fire — an answered re-ask posts none).
    ``tap`` is the button label to tap (``None`` = don't tap, e.g. a re-fire that replays).
    ``expect_answer`` are substrings the answer reaching the agent must contain."""

    title: str
    kind: str = "question"
    payload: str | None = None
    expect_card: int = 1
    tap: str | None = None
    expect_answer: tuple[str, ...] = ()

    @property
    def runnable(self) -> bool:
        return self.payload is not None


# --- parsing the prose suite into agent-side cases -----------------------------------
def suite_has_agent_directives(suite_text: str) -> bool:
    """Whether any case block carries an ``Ask-question:``/``Ask-permission:`` directive — the
    signal that this suite drives the AGENT-SIDE loop rather than the inbound ``Send:`` path."""
    for _title, body in _split_into_case_blocks(suite_text):
        for line in body.splitlines():
            if _ASK_QUESTION_RE.match(line) or _ASK_PERMISSION_RE.match(line):
                return True
    return False


def parse_agent_cases(suite_text: str) -> list[AgentCase]:
    """Split the suite into ``AgentCase`` objects, one per ``## Case:`` block. A block with no
    ``Ask-*`` directive parses to a non-runnable case the driver BLOCKS (an inbound-style case
    mixed into an agent-side suite — honest about what the agent-side run did not cover)."""
    return [_parse_one_agent_case(title, body)
            for title, body in _split_into_case_blocks(suite_text)]


def _parse_one_agent_case(title: str, body: str) -> AgentCase:
    kind = "question"
    payload: str | None = None
    expect_card: int | None = None
    tap: str | None = None
    expect_answer: list[str] = []
    for line in body.splitlines():
        if (m := _ASK_QUESTION_RE.match(line)) and payload is None:
            payload, kind = m.group(1), "question"
        elif (m := _ASK_PERMISSION_RE.match(line)) and payload is None:
            payload, kind = m.group(1), "permission"
        elif (m := _EXPECT_CARD_RE.match(line)) and expect_card is None:
            expect_card = _parse_card_count(m.group(1))
        elif (m := _TAP_RE.match(line)) and tap is None:
            tap = m.group(1)
        elif m := _EXPECT_ANSWER_RE.match(line):
            expect_answer.append(m.group(1))
    return AgentCase(
        title=title, kind=kind, payload=payload,
        expect_card=1 if expect_card is None else expect_card,
        tap=tap, expect_answer=tuple(expect_answer),
    )


def _parse_card_count(raw: str) -> int:
    """``Expect-card:`` value as a non-negative int; a malformed value defaults to 1 (the common
    one-card case) rather than crashing the parse."""
    try:
        return max(0, int(raw.strip()))
    except (TypeError, ValueError):
        return 1


# --- the agent-side run context + workspace ------------------------------------------
@dataclass
class _AgentRunCtx:
    """Everything ``_drive_agent_case`` needs: the fake (cards + tap injection), the templated
    hook-client argv + its cwd + env, the tap sender id, and the live ask handles to reap."""

    fake: bh.FakeTelegram
    ask_command: list[str]
    cwd: Path
    env: dict[str, str]
    sender_id: int
    handles: list[bh.AskHandle]
    exit_boot_failed: int = 1  # default so a new call-site can't break; real runs pass the QA class


@dataclass
class _AgentWorkspace:
    """The run's throwaway dirs: ``root`` (also ``HOME``), ``config_dir`` (the bot's config /
    socket / registration dir), and ``project`` (the cwd the daemon + hook client run in, the cwd
    a registration is keyed on)."""

    root: Path
    config_dir: Path
    project: Path

    @property
    def home(self) -> Path:
        return self.root

    @classmethod
    def create(cls) -> _AgentWorkspace:
        import tempfile

        # NOT resolve()d: a bridge bot binds an AF_UNIX socket under config_dir, and that path has
        # a hard ~104-char limit on macOS. resolve() rewrites the macOS tempdir /var -> /private/var
        # (+8 chars) and blows the limit. So keep the short /var path; a bot that keys a tap on the
        # registration cwd must realpath BOTH sides itself (tg-ctl does; the fixture does too), which
        # is the correct way to bridge the /var vs /private/var (macOS) symlink — not lengthening
        # the socket path. The prefix is kept short for the same AF_UNIX budget.
        root = Path(tempfile.mkdtemp(prefix="rvqa-"))
        config_dir = root / "config"
        config_dir.mkdir()
        project = root / "project"
        project.mkdir()
        return cls(root=root, config_dir=config_dir, project=project)

    def cleanup(self) -> None:
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)

    def resolve(self, rel_or_abs: str) -> Path:
        p = Path(rel_or_abs)
        return p if p.is_absolute() else self.root / p


# --- template substitution (the seed/env/argv knobs reference the run's allocated paths) ---
def _substitute(text: str, variables: dict[str, str]) -> str:
    """Replace ``{key}`` tokens with their values. A plain token replace (NOT ``str.format``) so a
    seed body that is JSON — full of literal ``{`` / ``}`` — is left untouched except the known
    tokens."""
    for key, val in variables.items():
        text = text.replace("{" + key + "}", val)
    return text


def _template_vars(
    ws: _AgentWorkspace, *, owner: int, sender: int, bot_id: str, api_base: str, sut_dir: Path,
) -> dict[str, str]:
    return {
        "workdir": str(ws.root),
        "config_dir": str(ws.config_dir),
        # The REALPATH of the project dir: a process started with cwd=project reports its realpath
        # via os.getcwd()/process.cwd() (macOS /var -> /private/var), and a bridge bot like tg-ctl
        # matches a registration's cwd LITERALLY against that — so the seeded cwd must already be
        # canonical or the tap routing drops as "not-registered". The socket path stays under the
        # SHORT (unresolved) {config_dir} to respect the AF_UNIX length limit.
        "cwd": os.path.realpath(str(ws.project)),
        "home": str(ws.home),
        "owner_id": str(owner),
        "sender_id": str(sender),
        "bot_id": bot_id,
        "api_base": api_base,
        # The SUT's own directory (the worktree / --in-place path). The daemon + hook client RUN
        # in the throwaway ``project`` dir (so a registration's cwd is stable), so the command must
        # reference the SUT's binary/script by absolute path — ``{sut_dir}/tg-ctl`` etc.
        "sut_dir": str(sut_dir),
    }


def _resolve_token(env: dict[str, str]) -> str:
    tok = (env.get("TG_BOT_TOKEN") or "").strip()
    return tok or _DEFAULT_HERMETIC_TOKEN


def _bot_id_from_token(token: str) -> str:
    head = token.split(":", 1)[0]
    return head if head.isdigit() else ""


def _write_seed(
    seeds, ws: _AgentWorkspace, variables: dict[str, str], *, exit_boot_failed: int,
) -> None:
    """Write each pre-boot seed file (templated path + content) before the daemon starts.

    CONTAINMENT (review finding). A seed ``path`` comes from the reviewed repo's ``qa.yaml``; a
    stray absolute path or a ``../`` segment would escape the run's throwaway workdir and write an
    arbitrary file — breaking the 'hermetic, nothing leaks' guarantee (not an escalation — the
    harness already runs ``command``/``ask_command`` from the same qa.yaml — but a real correctness
    boundary). Every resolved target must land INSIDE ``ws.root``; one that escapes is a BLOCKED
    config error (``BotHarnessError``), not a silent out-of-tree write."""
    root_real = os.path.realpath(str(ws.root))
    for seed in seeds:
        target = ws.resolve(_substitute(seed.path, variables))
        # realpath the PARENT (the target itself doesn't exist yet) and on BOTH sides so the macOS
        # /var -> /private/var symlink can't read as an escape. A ``..`` that climbs out of root
        # resolves to a parent whose commonpath with root_real is no longer root_real.
        parent_real = os.path.realpath(str(target.parent))
        if os.path.commonpath([root_real, parent_real]) != root_real:
            raise bh.BotHarnessError(
                f"sut.bot.seed path {seed.path!r} resolves OUTSIDE the run's throwaway workdir "
                f"({target}) — a seed precondition must stay inside the hermetic run dir. Use a "
                "relative path (or a {config_dir}/{workdir} token), with no '..' escape or absolute "
                "path that leaves the run dir.",
                exit_code=exit_boot_failed,  # mapped to a BLOCKED transcript by the caller
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_substitute(seed.content, variables), encoding="utf-8")


# --- the agent-side orchestrator -----------------------------------------------------
def run_agent_side_bot_test(
    *, suite_text: str, bot_config: BotConfig, cwd: Path, sut_path: Path, exit_boot_failed: int,
) -> str:
    """Drive the AGENT-SIDE loop and return the same ``## QA RESULTS`` transcript the inbound path
    emits. Order: allocate throwaway dirs → write seed preconditions → boot the daemon vs the fake
    → wait-ready → per case: emit the question, assert the card count, inject the tap, read the
    answer off the hook client's stdout. ALWAYS reaps the hook clients + daemon and stops the fake
    (try/finally) — a hermetic run leaks nothing. ``cwd`` (the worktree) is unused for the spawn —
    the daemon/hook client run in the run's own ``project`` dir so a registration's cwd is stable —
    but is kept in the signature to point the command at the SUT via the ``{sut_dir}`` token."""
    if not suite_has_agent_directives(suite_text):
        # ask_command is set (so we routed here), but the suite has NO Ask-question:/Ask-permission:
        # directive — the author configured the AGENT-SIDE tier yet wrote an inbound-style suite.
        # BLOCK with a clear single message instead of booting a daemon to BLOCK every case.
        return BotRunResult(blocked_reason=(
            "sut.bot.ask_command is set (the AGENT-SIDE tier), but the suite has no "
            "Ask-question:/Ask-permission: directive to drive — author an "
            "Ask-question:/Expect-card:/Tap:/Expect-answer: script, or drop ask_command to run the "
            "inbound Send:/Expect: path."
        )).to_qa_results(sut_path=sut_path)
    cases = parse_agent_cases(suite_text)
    ws = _AgentWorkspace.create()
    fake = bh.FakeTelegram()
    fake.start()
    daemon: bh.BotProcess | None = None
    handles: list[bh.AskHandle] = []
    try:
        token = _resolve_token(bot_config.env)
        owner = int(bot_config.owner_id)  # config guarantees non-None for the agent-side tier
        sender = int(bot_config.effective_sender_id)
        variables = _template_vars(
            ws, owner=owner, sender=sender, bot_id=_bot_id_from_token(token),
            api_base=fake.base_url(), sut_dir=cwd,
        )
        env = bh.build_agent_env(
            api_base=fake.base_url(), owner_id=owner, token=token,
            config_dir=ws.config_dir, home=ws.home,
            extra_env={k: _substitute(v, variables) for k, v in bot_config.env.items()},
        )
        try:
            # Seed the daemon's preconditions, then boot it — both can raise a controlled
            # BotHarnessError (a seed that escapes the run dir; a daemon that can't launch), which
            # maps to a BLOCKED transcript rather than a traceback.
            _write_seed(bot_config.seed, ws, variables, exit_boot_failed=exit_boot_failed)
            daemon = bh.boot_agent_daemon(
                command=[_substitute(a, variables) for a in bot_config.command],
                cwd=ws.project, env=env, exit_boot_failed=exit_boot_failed,
            )
        except bh.BotHarnessError as exc:
            return BotRunResult(blocked_reason=str(exc)).to_qa_results(sut_path=sut_path)
        blocked = _wait_daemon_ready(daemon, bot_config, ws, variables)
        if blocked is not None:
            return blocked.to_qa_results(sut_path=sut_path)
        run_ctx = _AgentRunCtx(
            fake=fake, ask_command=[_substitute(a, variables) for a in bot_config.ask_command],
            cwd=ws.project, env=env, sender_id=sender, handles=handles,
            exit_boot_failed=exit_boot_failed,
        )
        results = [_drive_agent_case(c, run_ctx) for c in cases]
        return BotRunResult(results=results).to_qa_results(sut_path=sut_path)
    finally:
        for handle in handles:
            handle.reap()
        if daemon is not None:
            daemon.reap()
        fake.stop()
        ws.cleanup()


def _wait_daemon_ready(
    daemon: bh.BotProcess, bot_config: BotConfig, ws: _AgentWorkspace, variables: dict[str, str],
) -> BotRunResult | None:
    """Gate the run until the daemon is ready, or return a BLOCKED result. With a ``ready_file``
    (e.g. tg-ctl's hook socket) the gate waits for that file to appear (deterministic); without
    one it waits a fixed boot grace. Either way a daemon that EXITED is reported as a crash with
    its output tail, never mistaken for 'still starting'."""
    if bot_config.ready_file:
        target = ws.resolve(_substitute(bot_config.ready_file, variables))
        if not bh.wait_for_file(target, timeout=bh._DAEMON_READY_TIMEOUT_S):
            crash = _agent_crash_block(daemon)
            return crash or BotRunResult(blocked_reason=(
                f"the agent-side daemon never created its readiness file {target} within "
                f"{bh._DAEMON_READY_TIMEOUT_S:.0f}s — it failed to start listening. Output tail:\n"
                f"{daemon.output_tail()}"
            ))
    else:
        time.sleep(bh._DAEMON_BOOT_GRACE_S)
    return _agent_crash_block(daemon)


def _agent_crash_block(daemon: bh.BotProcess) -> BotRunResult | None:
    """A BLOCKED result if the daemon has EXITED (crashed before it could be driven), else None."""
    rc = daemon.proc.poll()
    if rc is None:
        return None
    return BotRunResult(blocked_reason=(
        f"the agent-side daemon exited (code {rc}) before it could be driven — it crashed on "
        f"startup rather than long-polling the fake Telegram. Output tail:\n{daemon.output_tail()}"
    ))


# --- driving one agent-side case -----------------------------------------------------
def _drive_agent_case(case: AgentCase, ctx: _AgentRunCtx) -> CaseResult:
    """Emit the case's question, assert the NEW-card count, inject the tap, and read the answer off
    the hook client's stdout — classifying PASS/FAIL with a proof line."""
    if not case.runnable:
        return CaseResult(
            title=case.title, status=BLOCKED,
            detail="no Ask-question:/Ask-permission: directive — an inbound-style case in an "
                   "agent-side suite; author an Ask-question:/Expect-card:/Tap:/Expect-answer: "
                   "script to drive it.",
        )
    baseline = len(bh.cards_captured(ctx.fake))
    try:
        handle = bh.emit_question(
            ask_command=ctx.ask_command, cwd=ctx.cwd, env=ctx.env, payload=case.payload or "",
            exit_boot_failed=ctx.exit_boot_failed)
    except bh.BotHarnessError as exc:
        # The hook client could not be SPAWNED (a typo'd/unavailable ask_command). Report a
        # controlled BLOCKED case, not an uncaught traceback that kills the whole run. A missing
        # binary fails EVERY case identically → an all-BLOCKED run, and BotRunResult.verdict rolls
        # that up to VERDICT BLOCKED, which the qa handler maps to the non-zero exit_boot_failed
        # class — so a misconfigured ask_command never passes as a green run. (The exit class is
        # carried on the BotHarnessError too, but the per-case BLOCKED path reports via the verdict,
        # not that code, so the run-level mapping is what makes the exit non-zero.)
        return CaseResult(case.title, BLOCKED, str(exc))
    ctx.handles.append(handle)
    timeout = bh._CARD_TIMEOUT_S if case.expect_card > 0 else bh._NO_CARD_CONFIRM_S
    new_cards = _observe_new_cards(ctx.fake, baseline, want=case.expect_card, timeout=timeout)
    if new_cards != case.expect_card:
        handle.reap()
        return CaseResult(case.title, FAIL, _card_count_detail(case, new_cards), "P1")
    tap_fail = _do_tap(case, ctx)
    if tap_fail is not None:
        handle.reap()
        return tap_fail
    if not _expects_answer(case):
        handle.reap()
        return CaseResult(case.title, PASS,
                          f"posted {new_cards} card(s) as expected (no tap/answer asserted)")
    answer = handle.await_answer(timeout=bh._ANSWER_TIMEOUT_S)
    return _classify_agent_answer(case, answer)


def _observe_new_cards(
    fake: bh.FakeTelegram, baseline: int, *, want: int, timeout: float,
) -> int:
    """How many NEW cards landed (vs ``baseline``) within ``timeout``. For ``want > 0`` it returns
    as soon as ``want`` are seen (plus a brief settle to catch a LATE duplicate — the #98 case);
    for ``want == 0`` (a re-fire that must post nothing) it waits the FULL window so a late
    duplicate can't slip past the assertion.

    NOTE the COUNTER-DIRECTIONAL meaning of the ``want == 0`` window (``_NO_CARD_CONFIRM_S``): it is
    BOTH the time we confirm "no card came" AND the time a buggy DUPLICATE has to appear in to be
    caught (``test_dod_duplicate_card_bug_fails`` wants the #98 dup detected here). Tests shrink it
    via ``REVIEW_QA_BOT_NO_CARD_TIMEOUT_S``; keep it comfortably above a slow/cold-CI duplicate POST
    so a real duplicate isn't missed (a false ``got == 0`` would flip that FAIL-expecting test)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = len(bh.cards_captured(fake)) - baseline
        if want > 0 and got >= want:
            time.sleep(_CARD_SETTLE_S)
            return len(bh.cards_captured(fake)) - baseline
        time.sleep(bh._WAIT_POLL_S)
    return len(bh.cards_captured(fake)) - baseline


def _do_tap(case: AgentCase, ctx: _AgentRunCtx) -> CaseResult | None:
    """Inject the case's tap (if any). Returns a FAIL CaseResult when there is no card to tap or the
    named button is not on it, else ``None`` (no tap requested, or the tap was injected)."""
    if case.tap is None:
        return None
    cards = bh.cards_captured(ctx.fake)
    if not cards:
        # An authored Tap: with no card to tap (e.g. Expect-card: 0 + Tap:) — an honest FAIL, not
        # an IndexError that crashes the whole agent-side run.
        return CaseResult(
            case.title, FAIL,
            f"Tap: {case.tap!r} — there is no card to tap (this question posted no card).", "P1")
    card = cards[-1]  # the newest card — the one this emit produced
    if not bh.tap(ctx.fake, card, case.tap, from_id=ctx.sender_id):
        labels = bh.card_button_labels(card)
        return CaseResult(
            case.title, FAIL,
            f"Tap: {case.tap!r} — the card has no button with that label; available labels: "
            f"{labels!r}", "P1")
    return None


def _expects_answer(case: AgentCase) -> bool:
    """Whether this case should wait for an answer on the hook client's stdout: a tapped card, a
    re-fire (``Expect-card: 0`` → expects a replayed answer), or an explicit ``Expect-answer:``.
    A card-only assertion (posted but never tapped, no answer asserted) does NOT wait — the hook
    client stays blocked and is reaped, which is correct, not a hang failure."""
    return case.tap is not None or case.expect_card == 0 or bool(case.expect_answer)


def _classify_agent_answer(case: AgentCase, answer: str | None) -> CaseResult:
    """Classify the answer the agent received against the case's ``Expect-answer:`` substrings. A
    ``None`` answer means the hook client hung past the timeout — the tap/replay never reached the
    agent (the tap-loss / lost-answer bug), a real FAIL."""
    if answer is None:
        return CaseResult(
            case.title, FAIL,
            "the hook client returned NO answer (it hung past the timeout) — the tap or the "
            "answered-replay never reached the agent (tap-loss / lost answer).", "P1")
    missing = [s for s in case.expect_answer if s.lower() not in answer.lower()]
    if missing:
        return CaseResult(
            case.title, FAIL,
            f"the answer is missing expected substring(s) {missing!r} — proof: the agent received "
            f"{answer!r}", "P1")
    return CaseResult(case.title, PASS,
                      f"card count + answer matched (the agent received: {answer!r})")


def _card_count_detail(case: AgentCase, actual: int) -> str:
    if case.expect_card == 0 and actual > 0:
        return (f"expected NO new card on the re-fire (the answered question must replay, not "
                f"re-post), but {actual} new card(s) were posted — a duplicate, superseded card "
                f"(the #98 class).")
    return (f"expected {case.expect_card} new card(s) for this question, but {actual} were posted "
            f"— proof: the captured card count changed by {actual}.")
