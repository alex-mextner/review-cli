"""The bot Tier-2 LIVE suite runner: drive a real test Telegram account (via ``LiveBotDriver``,
MTProto/Telethon) through a prose ``## Case:`` suite and emit the SAME ``## QA RESULTS`` contract
the hermetic Tier-1 path produces (spec §7.3 Tier 2, #82).

WHERE THIS SITS. Tier-1 (``bot_driver.run_hermetic_bot_test``) boots the SUT bot against a fake
Telegram and asserts on captured outbound calls — deterministic, no creds. Tier-2 swaps that fake
for the REAL Telegram: the bot-under-test is an already-running bot reachable in a DEDICATED test
chat, and a DEDICATED test USER account (the ``LiveBotDriver``) plays the human — sending,
tapping inline buttons, and waiting for the bot's real replies. The output contract is identical
(``parse_qa_results`` reads it unchanged), so the live path slots into the existing
verdict→exit mapping with no new parser.

THE LIVE CASE GRAMMAR (a faithful subset of the hermetic grammar plus a real ``Tap:``):

    ## Case: /start greets, then tapping Help shows usage
    Send: /start
    Expect: welcome
    Tap: Help
    Expect: usage
    Expect-no: error

  * ``Send:``        — the text to deliver to the bot AS the human (a leading ``/`` is a command).
  * ``Expect:``      — a substring the bot's reply MUST contain (case-insensitive; all must match).
  * ``Expect-no:``   — a substring the reply must NOT contain.
  * ``Expect-silent``— the bot must send NOTHING for this case.
  * ``Tap: <label>`` — tap the inline button with that label on the bot's reply (a real callback
    query); the message AFTER the tap is the one ``Expect:`` then asserts.

A case with no ``Send:`` is BLOCKED (the live driver has nothing to initiate) — honest about
what the live run did and didn't cover, exactly like the hermetic path.

KNOWN SIMPLIFICATION (v1). ``expect`` matches the bot's NEXT inbound reply message. A ``Tap:``
asserts ``Expect:`` across the pre-tap card, the tap's callback ANSWER (the toast/alert Telegram
shows), AND a post-tap reply message — concatenated. The gap that remains: a bot that responds to
a tap by EDITING the card IN PLACE (no new message, no alert) produces no inbound reply for
``expect`` to see, so an ``Expect:`` asserting only the edited text would FAIL even though the bot
worked. Capturing in-place edits (re-fetching the card by id) and multi-message concatenation can
be layered on the same seam later without changing the contract.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .bot_driver import (
    BLOCKED,
    FAIL,
    PASS,
    BotRunResult,
    CaseResult,
    _EXPECT_NO_RE,
    _EXPECT_RE,
    _EXPECT_SILENT_RE,
    _SEND_RE,
    _TAP_RE,
    _split_into_case_blocks,
)
from .live_tier import LiveTierUnavailable, live_driver_for

# The report's BRING-UP label (vs the hermetic path's "hermetic (fake Telegram)") so a live
# transcript is never mislabelled as a hermetic run. Public so the dispatch's gate-not-ok BLOCKED
# report reuses the SAME literal (one source of truth — no drift).
LIVE_BRING_UP = "live (real Telegram, MTProto)"

# How long a live case waits for the bot's reply / confirms silence. Real bots are slower than a
# fake, so the default reply window is generous; both are overridable for tests / a snappy bot.
def _env_float(name: str, default: float) -> float:
    """A float from env ``name``, falling back to ``default`` on a missing OR malformed value — so a
    typo (``REVIEW_QA_BOT_LIVE_TIMEOUT_S=15s``) is a clean default at import, not a ValueError that
    crashes the whole gate-ok dispatch path."""
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


_LIVE_REPLY_TIMEOUT_S = _env_float("REVIEW_QA_BOT_LIVE_TIMEOUT_S", 15.0)
_LIVE_SILENT_TIMEOUT_S = _env_float("REVIEW_QA_BOT_LIVE_SILENT_S", 5.0)


@dataclass(frozen=True)
class LiveBotCase:
    """One parsed live case. ``send`` initiates (``None`` → a non-runnable, BLOCKED case);
    ``tap`` is an inline-button label to tap on the reply; ``expect``/``expect_no`` are the
    substring assertions on the (post-tap) reply; ``expect_silent`` requires no reply at all."""

    title: str
    send: str | None = None
    tap: str | None = None
    expect: tuple[str, ...] = ()
    expect_no: tuple[str, ...] = ()
    expect_silent: bool = False

    @property
    def runnable(self) -> bool:
        return self.send is not None


# --- parsing -------------------------------------------------------------------------------
def parse_live_bot_cases(suite_text: str) -> list[LiveBotCase]:
    """Split a suite into ``LiveBotCase`` objects, one per ``## Case:`` block (reusing the
    hermetic block splitter + directive regexes so the two grammars never drift)."""
    return [_parse_one(title, body) for title, body in _split_into_case_blocks(suite_text)]


def _parse_one(title: str, body: str) -> LiveBotCase:
    send: str | None = None
    tap: str | None = None
    expect: list[str] = []
    expect_no: list[str] = []
    expect_silent = False
    for line in body.splitlines():
        if (m := _SEND_RE.match(line)) and send is None:
            send = m.group(1)
        elif (m := _TAP_RE.match(line)) and tap is None:
            tap = m.group(1)
        elif m := _EXPECT_NO_RE.match(line):  # before Expect (prefix overlap)
            expect_no.append(m.group(1))
        elif m := _EXPECT_RE.match(line):
            expect.append(m.group(1))
        elif _EXPECT_SILENT_RE.match(line):
            expect_silent = True
    return LiveBotCase(
        title=title, send=send, tap=tap,
        expect=tuple(expect), expect_no=tuple(expect_no), expect_silent=expect_silent,
    )


# --- driving -------------------------------------------------------------------------------
def run_live_bot_suite(
    *,
    suite_text: str,
    sut_path: Path,
    exit_blocked: int,
    driver: Any | None = None,
    reply_timeout_s: float = _LIVE_REPLY_TIMEOUT_S,
    silent_timeout_s: float = _LIVE_SILENT_TIMEOUT_S,
) -> str:
    """Connect the live driver, drive every case against the real bot, and return the
    ``## QA RESULTS`` transcript. A connect failure (telethon absent, not authorized, chat
    unresolved — a ``LiveTierUnavailable``) is a controlled BLOCKED, never a traceback. The driver
    is ALWAYS disconnected (try/finally). ``driver`` is injectable so a unit test drives a scripted
    fake; production builds the real MTProto driver via ``live_driver_for``."""
    cases = parse_live_bot_cases(suite_text)
    bot = driver if driver is not None else live_driver_for("bot", exit_blocked=exit_blocked)
    try:
        try:
            bot.connect()
        except LiveTierUnavailable as exc:
            return BotRunResult(blocked_reason=str(exc)).to_qa_results(
                sut_path=sut_path, bring_up=LIVE_BRING_UP)
        results = [
            _drive_one_isolated(
                c, bot, reply_timeout_s=reply_timeout_s, silent_timeout_s=silent_timeout_s)
            for c in cases
        ]
        return BotRunResult(results=results).to_qa_results(
            sut_path=sut_path, bring_up=LIVE_BRING_UP)
    finally:
        # Always tear the session down — incl. a driver whose connect() partially initialized
        # before raising. disconnect() is idempotent on both the real and a fake driver.
        bot.disconnect()


def _drive_one_isolated(
    case: LiveBotCase, bot: Any, *, reply_timeout_s: float, silent_timeout_s: float,
) -> CaseResult:
    """Drive one case, converting ANY transport/driver error (a FloodWaitError, an RPCError, a
    dropped session raising LiveTierUnavailable mid-run) into a BLOCKED result for THIS case rather
    than a raw traceback that aborts the whole suite and discards the cases already run. Per-case
    isolation preserves partial progress and honors the 'never a traceback' contract."""
    try:
        return _drive_one(
            case, bot, reply_timeout_s=reply_timeout_s, silent_timeout_s=silent_timeout_s)
    except Exception as exc:  # noqa: BLE001 — a live op can raise many transport errors
        return CaseResult(
            case.title, BLOCKED,
            f"the live run hit a driver/transport error driving this case "
            f"({type(exc).__name__}: {exc}) — the bot may be flood-limited or the session dropped.")


def _drive_one(
    case: LiveBotCase, bot: Any, *, reply_timeout_s: float, silent_timeout_s: float,
) -> CaseResult:
    """Drive one live case: send → wait for the bot's reply → (optionally tap a button on it and
    wait for the post-tap reply) → classify the COMBINED reply text against the case's assertions.

    A ``Tap:`` case asserts ``Expect:`` against BOTH replies concatenated (the pre-tap card AND the
    post-tap answer), mirroring the hermetic path's multi-message concatenation — so a suite can
    assert the card label and the tapped result in one case."""
    if not case.runnable:
        return CaseResult(
            title=case.title, status=BLOCKED,
            detail="no Send: directive — a prose-only case the live driver cannot initiate; "
                   "author a Send:/Expect: pair (optionally a Tap:) to drive it.",
        )
    bot.send(case.send)
    if case.expect_silent:
        stray = bot.expect(_ANY_REPLY, silent_timeout_s)
        if stray is not None:
            return CaseResult(case.title, FAIL,
                              f"expected NO reply, but the bot sent: {_text_of(stray)!r}", "P1")
        return CaseResult(case.title, PASS, "bot stayed silent as expected")
    texts: list[str] = []
    reply = bot.expect(_ANY_REPLY, reply_timeout_s)
    if reply is not None:
        texts.append(_text_of(reply))
    if case.tap is not None:
        if reply is None:
            return CaseResult(
                case.title, FAIL,
                f"Tap: {case.tap!r} — the bot sent no reply to carry the inline button to tap.",
                "P1")
        # The tap RESULT carries the bot's callback answer (the toast/alert text Telegram shows on
        # a button tap) — fold it into the asserted text so an alert-only response (no new message,
        # no edit) is still matchable. A bot that EDITS the card in place (no new message, no alert)
        # is the documented gap below.
        answer_text = _callback_answer_text(bot.tap(reply, case.tap))
        if answer_text:
            texts.append(answer_text)
        post = bot.expect(_ANY_REPLY, reply_timeout_s)
        if post is not None:
            texts.append(_text_of(post))
    return _classify(case, texts)


def _classify(case: LiveBotCase, texts: list[str]) -> CaseResult:
    """Match the bot's reply text(s) against the case's ``Expect:``/``Expect-no:`` substrings. The
    reply texts are concatenated (a ``Send:`` reply, plus a post-``Tap:`` reply) and matched
    collectively — every ``Expect:`` must be present, no ``Expect-no:`` may be."""
    if not texts:
        return CaseResult(
            case.title, FAIL,
            "the bot sent NO reply (expected one matching: "
            f"{', '.join(case.expect) or '(any reply)'})", "P1")
    combined = " || ".join(texts)
    missing = [needle for needle in case.expect if needle.lower() not in combined.lower()]
    forbidden = [needle for needle in case.expect_no if needle.lower() in combined.lower()]
    if missing or forbidden:
        return CaseResult(case.title, FAIL, _mismatch_detail(combined, missing, forbidden), "P1")
    return CaseResult(case.title, PASS, f"reply matched all expectations (captured: {combined!r})")


def _mismatch_detail(text: str, missing: list[str], forbidden: list[str]) -> str:
    parts = []
    if missing:
        parts.append(f"reply is missing expected substring(s) {missing!r}")
    if forbidden:
        parts.append(f"reply contains forbidden substring(s) {forbidden!r}")
    return f"{'; '.join(parts)} — proof: captured reply was {text!r}"


def _text_of(message: Any) -> str:
    """A Telegram message's text — Telethon exposes ``.text`` (and ``.message`` for the raw body);
    a media-only message has neither, which reads as an empty string (it matches no substring)."""
    return getattr(message, "text", None) or getattr(message, "message", "") or ""


def _callback_answer_text(answer: Any) -> str:
    """The toast/alert text the bot returned for an inline-button tap. Telethon's
    ``message.click()`` returns a ``BotCallbackAnswer`` whose ``.message`` is that text (empty when
    the bot answered silently or only edited the card). ``None`` / no ``.message`` → empty."""
    return getattr(answer, "message", None) or ""


def _ANY_REPLY(_message: Any) -> bool:
    """The default ``expect`` predicate: accept the bot's next inbound reply (the driver already
    filters out our own outbound), letting the classifier assert on its text."""
    return True
