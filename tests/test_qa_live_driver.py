#!/usr/bin/env python3
"""review qa — the Tier-2 LIVE bot driver + suite runner (the REAL MTProto path, #82).

These tests exercise the REAL translation logic of the live bot driver and its suite runner with
NO creds, NO telethon, NO Telegram, NO network — by injecting an in-memory fake at the transport
boundary:

  * ``LiveBotDriver`` (reviewlib/qa/live_tier.py) drives a fake Telethon client (``client_factory``)
    so the driver's OWN logic is under test: connect verifies authorization + resolves the chat +
    records the high-water id; ``expect`` polls for the NEXT inbound reply (min-id past the
    high-water, skipping our own outbound and chat history) and times out to ``None``; ``send``
    delivers as the caller; ``tap`` clicks an inline button by label or index.
  * ``run_live_bot_suite`` (reviewlib/qa/live_bot_runner.py) drives a SCRIPTED fake driver so the
    runner's case sequencing + classification is under test: Send/Expect → PASS/FAIL, Expect-silent,
    the Tap flow (pre-tap card + post-tap reply concatenated), a non-runnable BLOCKED case, and a
    connect failure → a controlled BLOCKED transcript labelled the live backend.

The fakes ARE the Telegram boundary (the thing we don't own); the logic under test is ours.
Runnable standalone (``python3 tests/test_qa_live_driver.py``) or under pytest.
"""
from __future__ import annotations

import asyncio
import sys
import types
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib.qa import live_bot_runner as runner  # noqa: E402
from reviewlib.qa.live_tier import LiveBotDriver, LiveTierUnavailable  # noqa: E402


# =====================================================================================
# A fake Telethon client + message — the transport boundary the driver speaks to.
# =====================================================================================
class _FakeMsg:
    """A minimal stand-in for a Telethon Message: an id, an out/in flag, text, and an async
    ``click`` that records its call (so a tap assertion can read what button was tapped)."""

    def __init__(self, *, id: int, out: bool = False, text: str = "", action: object = None):
        self.id = id
        self.out = out
        self.text = text
        self.message = text
        self.action = action  # truthy on a Telegram SERVICE message (join/pin/…)
        self.clicks: list[dict] = []

    async def click(self, i=None, text=None, data=None):
        self.clicks.append({"i": i, "text": text, "data": data})
        return ("clicked", i, text)


class _FakeClient:
    """An in-memory fake Telethon client. ``get_messages(min_id=…)`` is exclusive on ``min_id``
    (id > min_id) and newest-first, matching Telethon; ``send_message`` makes an OUT message visible
    (so the driver must skip it); ``deliver`` simulates a bot reply landing AFTER connect."""

    def __init__(self, *, authorized: bool = True, history: list[_FakeMsg] | None = None,
                 entity: object = "ENTITY", entity_error: Exception | None = None,
                 connect_hang: bool = False):
        self._authorized = authorized
        self._visible: list[_FakeMsg] = list(history or [])
        self._entity = entity
        self._entity_error = entity_error
        self._connect_hang = connect_hang
        self._id = max((m.id for m in self._visible), default=0)
        self.connected = False
        self.disconnected = False
        self.sent: list[dict] = []
        self.entity_arg: object = None
        self.last_get: dict | None = None
        self.deleted: list[int] | None = None
        self.hang_get = False  # when True, get_messages never returns (a mid-poll transport stall)
        self.get_delay_s = 0.0  # a HEALTHY-but-slow fetch: each get_messages takes this long, then returns

    async def connect(self):
        if self._connect_hang:
            await asyncio.sleep(10)  # never completes within a test's tiny connect timeout
        self.connected = True

    async def is_user_authorized(self):
        return self._authorized

    async def get_entity(self, chat):
        self.entity_arg = chat
        if self._entity_error is not None:
            raise self._entity_error
        return self._entity

    async def get_messages(self, entity, min_id: int = 0, limit: int | None = None):
        if self.hang_get:
            await asyncio.sleep(10)  # a stalled fetch — bounded by the driver's op timeout, not this
        elif self.get_delay_s:
            await asyncio.sleep(self.get_delay_s)  # healthy but not instant (a real round-trip)
        self.last_get = {"entity": entity, "min_id": min_id, "limit": limit}
        pool = [m for m in self._visible if m.id > min_id]
        pool.sort(key=lambda m: m.id, reverse=True)
        return pool[:limit] if limit is not None else pool

    async def send_message(self, entity, text, reply_to=None):
        self._id += 1
        msg = _FakeMsg(id=self._id, out=True, text=text)
        self._visible.append(msg)
        self.sent.append({"entity": entity, "text": text, "reply_to": reply_to})
        return msg

    async def delete_messages(self, entity, ids):
        self.deleted = list(ids)

    async def disconnect(self):
        self.disconnected = True

    # --- test helper: a bot reply (or a service message) lands AFTER connect ---
    def deliver(self, text: str, *, action: object = None) -> _FakeMsg:
        self._id += 1
        msg = _FakeMsg(id=self._id, out=False, text=text, action=action)
        self._visible.append(msg)
        return msg


def _driver(client: _FakeClient, *, chat: str = "123", session: str = "sess") -> LiveBotDriver:
    return LiveBotDriver(
        api_id="1", api_hash="h", session=session, chat=chat, exit_blocked=8,
        client_factory=lambda _loop: client, poll_interval_s=0.01,
    )


# =====================================================================================
# Driver tests (the fake client is the boundary; the driver's logic is under test).
# =====================================================================================
def test_connect_authorized_resolves_entity_and_high_water():
    """connect: authorized account → connect()+is_user_authorized()+get_entity() are called, the
    chat is resolved, and the high-water id is the newest EXISTING message (so history is ignored)."""
    client = _FakeClient(history=[_FakeMsg(id=10, text="old"), _FakeMsg(id=42, text="newest")])
    drv = _driver(client)
    drv.connect()
    try:
        assert client.connected
        assert client.entity_arg == 123  # numeric chat id coerced to int
        assert drv._last_seen_id == 42  # high-water = newest existing message
    finally:
        drv.disconnect()
    assert client.disconnected


def test_connect_unauthorized_blocks():
    """connect: a connected-but-unauthorized session (expired/revoked) → a controlled
    LiveTierUnavailable naming the session, carrying the boot-failed exit class. Never a fake pass."""
    client = _FakeClient(authorized=False)
    drv = _driver(client)
    try:
        drv.connect()
        raise AssertionError("connect should have raised on an unauthorized session")
    except LiveTierUnavailable as exc:
        assert exc.exit_code == 8
        assert "authorized" in str(exc).lower()


def test_connect_resolves_username_chat_as_string():
    """A non-numeric TG_TEST_CHAT_ID (an @username) is passed to get_entity as the raw string —
    Telethon resolves it — rather than crashing on an int() coercion."""
    client = _FakeClient()
    drv = _driver(client, chat="@my_test_bot")
    drv.connect()
    try:
        assert client.entity_arg == "@my_test_bot"
    finally:
        drv.disconnect()


def test_send_delivers_text_to_chat():
    """send: delivers the text to the resolved chat as the caller and returns the sent message."""
    client = _FakeClient()
    drv = _driver(client)
    drv.connect()
    try:
        msg = drv.send("/start")
        assert client.sent[-1]["text"] == "/start"
        assert client.sent[-1]["entity"] == "ENTITY"
        assert msg.out is True
    finally:
        drv.disconnect()


def test_expect_returns_first_inbound_reply_skipping_outbound_and_history():
    """expect: after a send, returns the bot's NEXT inbound reply — skipping our own outbound (the
    send) AND the pre-connect history (the high-water mark). Proves the min-id / out-filter logic."""
    client = _FakeClient(history=[_FakeMsg(id=100, text="ancient history")])
    drv = _driver(client)
    drv.connect()
    try:
        drv.send("/start")           # an OUT message lands (must be skipped)
        client.deliver("welcome!")   # the bot's reply lands AFTER connect
        reply = drv.expect(lambda m: True, timeout=1.0)
        assert reply is not None
        assert reply.text == "welcome!"
        assert reply.out is False
        # the poll asked for messages strictly newer than the high-water (history excluded)
        assert client.last_get["min_id"] >= 100
    finally:
        drv.disconnect()


def test_expect_honors_predicate():
    """expect: a predicate that rejects the first reply keeps waiting for the next matching one;
    earlier non-matching inbound messages are consumed (the high-water advances past them)."""
    client = _FakeClient()
    drv = _driver(client)
    drv.connect()
    try:
        drv.send("/q")
        client.deliver("typing...")
        client.deliver("the answer is 42")
        reply = drv.expect(lambda m: "answer" in (m.text or ""), timeout=1.0)
        assert reply is not None and "42" in reply.text
    finally:
        drv.disconnect()


def test_expect_skips_service_messages():
    """expect: a Telegram SERVICE message (a join/pin — ``out=False`` but ``.action`` set, empty
    text) is NOT mistaken for a bot reply; it is skipped and the real reply is returned. Guards the
    false-FAIL the review flagged (a service message in a supergroup matching an empty 'reply')."""
    client = _FakeClient()
    drv = _driver(client)
    drv.connect()
    try:
        drv.send("/start")
        client.deliver("", action="chat_joined")  # a service message
        client.deliver("welcome!")                # the real bot reply
        reply = drv.expect(lambda m: True, timeout=1.0)
        assert reply is not None and reply.text == "welcome!"
    finally:
        drv.disconnect()


def test_send_tracks_ids_and_disconnect_deletes_them():
    """send records the ids of OUR messages; disconnect deletes them (spec §7.3 cleanup) so a
    dedicated test chat doesn't accumulate run-over-run."""
    client = _FakeClient()
    drv = _driver(client)
    drv.connect()
    drv.send("/one")
    drv.send("/two")
    drv.disconnect()
    assert client.deleted == [1, 2], client.deleted  # both our sends were deleted on teardown


def test_send_rebases_high_water_so_multimessage_reply_does_not_bleed():
    """A bot that answers one Send with MULTIPLE messages (a greeting AND a menu card): the case's
    expect matches the FIRST and leaves the second behind. The leftover must NOT bleed into the NEXT
    case — send() re-bases the high-water to the chat's current newest BEFORE sending, opening a
    fresh reply window per Send:. Regression for the cross-case bleed the review flagged."""
    client = _FakeClient()
    drv = _driver(client)
    drv.connect()
    try:
        drv.send("/start")
        client.deliver("Welcome!")
        client.deliver("Choose an option")  # the second message — only the first is matched below
        first = drv.expect(lambda _m: True, timeout=1.0)
        assert first is not None and first.text == "Welcome!"
        # Next case: the leftover "Choose an option" must be skipped (high-water re-based on send),
        # so expect returns THIS case's own reply, not the prior case's straggler.
        drv.send("/help")
        client.deliver("Here is help")
        second = drv.expect(lambda _m: True, timeout=1.0)
        assert second is not None and second.text == "Here is help", second.text
    finally:
        drv.disconnect()


def test_expect_times_out_to_none():
    """expect: no inbound reply within the timeout → None (the classifier reports the honest miss),
    never a hang. The outbound send is consumed but never matched."""
    client = _FakeClient()
    drv = _driver(client)
    drv.connect()
    try:
        drv.send("/silent")
        reply = drv.expect(lambda m: True, timeout=0.05)
        assert reply is None
    finally:
        drv.disconnect()


def test_expect_bounds_a_hung_get_messages():
    """expect: a single get_messages that STALLS mid-poll (an unreachable Telegram) is bounded by the
    op timeout → a controlled TimeoutError (the suite runner turns it into a BLOCKED), NOT a silent
    hang past the reply window on Telethon's own unbounded retries. Regression for the review finding
    that only the BETWEEN-poll deadline was checked, never the fetch itself. op_timeout (0.05s) is far
    below the 5s reply window, so a bounded fetch fires fast — a hang would instead block ~10s."""
    client = _FakeClient()
    drv = LiveBotDriver(
        api_id="1", api_hash="h", session="s", chat="1", exit_blocked=8,
        client_factory=lambda _loop: client, poll_interval_s=0.01, op_timeout_s=0.05,
    )
    drv.connect()  # connect + send do their own fetches — flip the stall on ONLY before expect
    try:
        drv.send("/start")  # send's high-water re-base fetch must complete (stall still off)
        client.hang_get = True  # now the next poll (expect's fetch) stalls
        try:
            drv.expect(lambda _m: True, timeout=5.0)
            raise AssertionError("expect should have surfaced a bounded TimeoutError, not hung")
        except (asyncio.TimeoutError, TimeoutError):
            pass  # the per-fetch op-timeout fired well inside the 5s window — bounded, as intended
    finally:
        client.hang_get = False
        drv.disconnect()


def test_expect_slow_fetch_near_deadline_returns_none_not_blocked():
    """expect: a HEALTHY-but-slow get_messages that merely runs past the tiny time-left at the final
    poll must yield a clean None (the honest no-reply → FAIL / Expect-silent PASS), NEVER a spurious
    transport BLOCKED. The per-fetch cap is the FIXED op ceiling (5s here), well above the 0.03s
    fetch, so every poll completes; only the deadline ends the wait. Regression for the determinism
    flaw where capping the fetch at the shrinking `remaining` cut a normal round-trip into a
    TimeoutError near the deadline."""
    client = _FakeClient()
    client.get_delay_s = 0.03  # healthy round-trip, but longer than the time-left at the last poll
    drv = LiveBotDriver(
        api_id="1", api_hash="h", session="s", chat="1", exit_blocked=8,
        client_factory=lambda _loop: client, poll_interval_s=0.0, op_timeout_s=5.0,
    )
    drv.connect()
    try:
        drv.send("/silent")
        reply = drv.expect(lambda _m: True, timeout=0.05)  # no reply delivered → honest miss
        assert reply is None  # a clean None, not a TimeoutError-driven BLOCKED
    finally:
        drv.disconnect()


def test_tap_clicks_button_by_label_then_by_index():
    """tap: a string button is clicked by LABEL (text=), an int button by INDEX (i=) — the faithful
    callback-query path for q-buttons / plan-approval."""
    client = _FakeClient()
    drv = _driver(client)
    drv.connect()
    try:
        card = _FakeMsg(id=999, text="choose")
        drv.tap(card, "Help")
        drv.tap(card, 2)
        assert card.clicks[0] == {"i": None, "text": "Help", "data": None}
        assert card.clicks[1] == {"i": 2, "text": None, "data": None}
    finally:
        drv.disconnect()


def test_driver_op_before_connect_blocks():
    """A driver op before connect() (no loop) is a controlled LiveTierUnavailable, not an
    AttributeError on a None loop."""
    drv = _driver(_FakeClient())
    try:
        drv.send("x")
        raise AssertionError("send before connect should have raised")
    except LiveTierUnavailable as exc:
        assert exc.exit_code == 8


def test_connect_chat_unresolvable_blocks():
    """connect: get_entity raising (the test chat can't be resolved — wrong id / not a member) is a
    controlled LiveTierUnavailable naming the chat, not a raw traceback. Covers the generic
    connect-failure branch distinct from the authorized check."""
    client = _FakeClient(entity_error=ValueError("No user has CHANNEL"))
    drv = _driver(client)
    try:
        drv.connect()
        raise AssertionError("connect should have raised when the chat can't be resolved")
    except LiveTierUnavailable as exc:
        assert exc.exit_code == 8
        assert "chat" in str(exc).lower()
    assert client.disconnected  # the failed connect still tears the session down


def test_connect_times_out_when_transport_hangs():
    """connect: an unreachable Telegram (a connect that never completes) is bounded by the driver's
    connect timeout → a controlled LiveTierUnavailable, never an unbounded hang."""
    client = _FakeClient(connect_hang=True)
    drv = LiveBotDriver(
        api_id="1", api_hash="h", session="s", chat="1", exit_blocked=8,
        client_factory=lambda _loop: client, poll_interval_s=0.01, connect_timeout_s=0.05,
    )
    try:
        drv.connect()
        raise AssertionError("connect should have timed out")
    except LiveTierUnavailable as exc:
        assert exc.exit_code == 8


# --- the REAL _build_client path (the client_factory bypasses it; cover it with a fake telethon) --
@contextmanager
def _fake_telethon(record: dict):
    """Install a fake ``telethon`` + ``telethon.sessions`` so the driver's REAL ``_build_client``
    runs — constructing the client through the actual ``TelegramClient(StringSession(...), api_id,
    api_hash)`` call (no ``client_factory`` shortcut). The fake records what it was built with so a
    test can assert the construction (esp. that NO ``loop=`` kwarg is passed — Telethon 1.x removed
    it). Restored on exit."""
    class _FakeStringSession:
        def __init__(self, s):
            record["session"] = s

    class _FakeTelegramClient:
        def __init__(self, session, api_id, api_hash, **kwargs):
            record["api_id"] = api_id
            record["api_hash"] = api_hash
            record["kwargs"] = kwargs
            self._session = session

        async def connect(self):
            record["connected"] = True

        async def is_user_authorized(self):
            return True

        async def get_entity(self, chat):
            return ("ent", chat)

        async def get_messages(self, entity, min_id=0, limit=None):
            return []

        async def delete_messages(self, entity, ids):
            record["deleted"] = list(ids)

        async def disconnect(self):
            record["disconnected"] = True

    tele = types.ModuleType("telethon")
    tele.TelegramClient = _FakeTelegramClient
    sessions = types.ModuleType("telethon.sessions")
    sessions.StringSession = _FakeStringSession
    saved = {k: sys.modules.get(k) for k in ("telethon", "telethon.sessions")}
    sys.modules["telethon"] = tele
    sys.modules["telethon.sessions"] = sessions
    try:
        yield record
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def test_real_build_client_constructs_without_loop_kwarg():
    """The REAL build path (no client_factory): ``_build_client`` coerces TG_TEST_API_ID to int,
    wraps the session in a StringSession, and constructs TelegramClient with NO ``loop=`` kwarg
    (Telethon 1.x removed it — passing one would TypeError on a live run). Regression guard for the
    review finding that no test covered the real Telethon construction."""
    record: dict = {}
    with _fake_telethon(record):
        drv = LiveBotDriver(
            api_id="4242", api_hash="abc", session="SESSDATA", chat="55", exit_blocked=8,
            poll_interval_s=0.01,
        )
        drv.connect()
        drv.disconnect()
    assert record["api_id"] == 4242 and isinstance(record["api_id"], int)
    assert record["api_hash"] == "abc"
    assert record["session"] == "SESSDATA"
    assert record["kwargs"] == {}, "TelegramClient must be built with NO loop= kwarg (Telethon 1.x)"
    assert record.get("connected") and record.get("disconnected")


def test_real_build_client_non_numeric_api_id_blocks():
    """A non-numeric TG_TEST_API_ID is a controlled LiveTierUnavailable naming API_ID — not the
    misleading generic 'could not resolve the chat' message (review finding #6)."""
    record: dict = {}
    with _fake_telethon(record):
        drv = LiveBotDriver(
            api_id="not-a-number", api_hash="h", session="s", chat="1", exit_blocked=8,
            poll_interval_s=0.01,
        )
        try:
            drv.connect()
            raise AssertionError("a non-numeric api id should have blocked")
        except LiveTierUnavailable as exc:
            assert exc.exit_code == 8
            assert "TG_TEST_API_ID" in str(exc)


# =====================================================================================
# Suite-runner tests (a scripted fake driver is the boundary; the runner's logic is under test).
# =====================================================================================
class _ScriptDriver:
    """A scripted stand-in for the live driver: ``expect`` returns queued replies in order (then
    None); ``send``/``tap`` are recorded; ``connect`` optionally raises a scripted error."""

    def __init__(self, replies: list[object] | None = None,
                 connect_error: Exception | None = None, tap_answer: str | None = None,
                 fail_send_on: int | None = None):
        self._replies = list(replies or [])
        self._connect_error = connect_error
        self._tap_answer = tap_answer
        self._fail_send_on = fail_send_on  # raise on the Nth send (1-based) to simulate a flood/RPC error
        self.connected = False
        self.disconnected = False
        self.sent: list[str] = []
        self.taps: list[tuple] = []

    def connect(self):
        if self._connect_error is not None:
            raise self._connect_error
        self.connected = True

    def send(self, text, *, reply_to=None):
        self.sent.append(text)
        if self._fail_send_on is not None and len(self.sent) == self._fail_send_on:
            raise RuntimeError("FloodWaitError: wait of 30 seconds required")

    def expect(self, predicate, timeout):
        return self._replies.pop(0) if self._replies else None

    def tap(self, message, button):
        self.taps.append((message, button))
        # Mirror Telethon: message.click() returns a callback answer carrying the toast/alert text.
        return _CallbackAnswer(self._tap_answer) if self._tap_answer is not None else None

    def disconnect(self):
        self.disconnected = True


class _Reply:
    def __init__(self, text: str):
        self.text = text
        self.message = text


class _CallbackAnswer:
    """A stand-in for Telethon's BotCallbackAnswer: ``.message`` is the toast/alert text."""

    def __init__(self, message: str):
        self.message = message


def _run(suite: str, driver: _ScriptDriver) -> str:
    return runner.run_live_bot_suite(
        suite_text=suite, sut_path=Path("/sut/bot"), exit_blocked=8, driver=driver,
        reply_timeout_s=0.01, silent_timeout_s=0.01,
    )


def test_runner_send_expect_pass():
    drv = _ScriptDriver(replies=[_Reply("welcome aboard")])
    out = _run("## Case: greet\nSend: /start\nExpect: welcome\n", drv)
    assert "VERDICT: PASS" in out
    assert "1 run, 1 passed" in out
    assert drv.sent == ["/start"]
    assert drv.connected and drv.disconnected
    # honest backend label — a live run is NEVER mislabelled as the hermetic fake
    assert "BRING-UP: live (real Telegram, MTProto)" in out


def test_runner_expect_mismatch_fails_with_proof():
    drv = _ScriptDriver(replies=[_Reply("internal error 500")])
    out = _run("## Case: greet\nSend: /start\nExpect: welcome\nExpect-no: error\n", drv)
    assert "VERDICT: FAIL" in out
    assert "missing expected substring" in out
    assert "forbidden substring" in out  # 'error' present


def test_runner_no_reply_fails():
    drv = _ScriptDriver(replies=[None])
    out = _run("## Case: greet\nSend: /start\nExpect: welcome\n", drv)
    assert "VERDICT: FAIL" in out
    assert "NO reply" in out


def test_runner_expect_silent_pass_and_fail():
    ok = _ScriptDriver(replies=[None])
    out_ok = _run("## Case: quiet\nSend: /noop\nExpect-silent\n", ok)
    # A PASS case emits no FINDINGS line (only FAILs do), so assert the rolled-up verdict + tally.
    assert "VERDICT: PASS" in out_ok and "1 run, 1 passed" in out_ok

    bad = _ScriptDriver(replies=[_Reply("surprise!")])
    out_bad = _run("## Case: quiet\nSend: /noop\nExpect-silent\n", bad)
    assert "VERDICT: FAIL" in out_bad
    assert "expected NO reply" in out_bad


def test_runner_tap_flow_concatenates_pre_and_post_tap():
    """A Tap case: send → reply (the card) → tap a button → post-tap reply; Expect: matches across
    BOTH replies concatenated, and the tap is recorded against the card message."""
    card = _Reply("choose an option")
    drv = _ScriptDriver(replies=[card, _Reply("here is the usage")])
    out = _run(
        "## Case: menu\nSend: /menu\nExpect: choose\nTap: Help\nExpect: usage\n", drv)
    assert "VERDICT: PASS" in out
    assert drv.taps == [(card, "Help")]


def test_runner_tap_callback_answer_is_asserted():
    """A tap whose bot answers with a toast/alert (no NEW message — e.g. it edits the card) still
    matches: the callback answer text is folded into the asserted text. Guards the review finding
    that an alert-only / edit response would false-FAIL."""
    card = _Reply("settings menu")
    drv = _ScriptDriver(replies=[card, None], tap_answer="saved!")  # post-tap reply is None
    out = _run(
        "## Case: save\nSend: /settings\nExpect: settings\nTap: Save\nExpect: saved\n", drv)
    assert "VERDICT: PASS" in out, out
    assert drv.taps == [(card, "Save")]


def test_runner_tap_without_card_fails():
    drv = _ScriptDriver(replies=[None])
    out = _run("## Case: menu\nSend: /menu\nTap: Help\nExpect: usage\n", drv)
    assert "VERDICT: FAIL" in out
    assert "no reply to carry the inline button" in out
    assert drv.taps == []


def test_runner_isolates_per_case_driver_error():
    """A transport/driver error mid-run (a flood wait, a dropped session) is contained to ITS case
    as a BLOCKED result — the cases already run keep their verdicts, and the run never aborts with a
    raw traceback. Guards the review finding that an unhandled exception lost prior results."""
    drv = _ScriptDriver(replies=[_Reply("hi there")], fail_send_on=2)
    out = _run(
        "## Case: one\nSend: /a\nExpect: hi\n## Case: two\nSend: /b\nExpect: never\n", drv)
    assert "2 run, 1 passed, 0 failed, 1 blocked" in out, out
    assert "VERDICT: BLOCKED" in out  # a blocked case rolls the run up to BLOCKED, not PASS
    assert "driver/transport error" in out
    assert drv.disconnected  # teardown still ran


def test_runner_non_runnable_case_blocked():
    """A case with no Send: cannot be initiated live → BLOCKED (honest about uncovered cases),
    not a silent skip or a false pass."""
    drv = _ScriptDriver(replies=[])
    out = _run("## Case: proseonly\nExpect: something\n", drv)
    assert "VERDICT: BLOCKED" in out
    assert "no Send:" in out


def test_runner_connect_failure_is_blocked_transcript():
    """A connect failure (telethon absent / not authorized — a LiveTierUnavailable) → a controlled
    BLOCKED transcript carrying the reason and the live backend label, never a traceback. The suite
    is not driven (no sends)."""
    err = LiveTierUnavailable("telethon is not installed: pip install telethon", exit_code=8)
    drv = _ScriptDriver(replies=[_Reply("never reached")], connect_error=err)
    out = _run("## Case: greet\nSend: /start\nExpect: welcome\n", drv)
    assert "VERDICT: BLOCKED" in out
    assert "telethon" in out.lower()
    assert "BRING-UP: live (real Telegram, MTProto)" in out
    assert drv.sent == []


def test_parse_live_cases_grammar():
    """The live grammar parses Send/Expect/Expect-no/Expect-silent/Tap across multiple cases."""
    suite = (
        "## Case: one\nSend: /a\nExpect: x\nExpect-no: y\nTap: Go\n"
        "## Case: two\nSend: /b\nExpect-silent\n"
        "## Case: three\nExpect: orphan\n"
    )
    cases = runner.parse_live_bot_cases(suite)
    assert [c.title for c in cases] == ["one", "two", "three"]
    assert cases[0].send == "/a" and cases[0].tap == "Go"
    assert cases[0].expect == ("x",) and cases[0].expect_no == ("y",)
    assert cases[1].expect_silent and cases[1].send == "/b"
    assert cases[2].send is None and not cases[2].runnable


if __name__ == "__main__":
    failures = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"ok   {_name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {_name}: {exc}")
    sys.exit(1 if failures else 0)
