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

    def to_qa_results(self, *, sut_path: Path) -> str:
        run = len(self.results)
        passed = sum(1 for r in self.results if r.status == PASS)
        failed = sum(1 for r in self.results if r.status == FAIL)
        blocked = sum(1 for r in self.results if r.status == BLOCKED)
        return "\n".join([
            "## QA RESULTS",
            f"SUT: {sut_path}   KIND: bot   BRING-UP: hermetic (fake Telegram)",
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
    every other kind."""
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
