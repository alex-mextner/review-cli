"""The bot Tier-1 HERMETIC harness: a fake Telegram Bot API server + a deterministic
suite driver that injects synthetic inbound updates and asserts the SUT bot's outbound
calls — no real token, no live network (spec docs/specs/review-qa.md §7.3, Tier 1).

WHY A SEPARATE, DETERMINISTIC PATH (not the un-caged executor). The web/ext/backend
testers need an un-caged write/exec agent to drive a live system. A Tier-1 hermetic bot
test does NOT: "send this update -> expect this outbound message" is a fully mechanical
assertion the moment the bot polls a fake Bot-API server. So bot Tier-1 runs as
DETERMINISTIC Python — it boots the SUT bot pointed at the fake via ``TG_API_BASE``,
POSTs synthetic ``getUpdates`` payloads, captures the bot's ``sendMessage`` calls, and
classifies each prose ``## Case:`` block by matching the captured text. This keeps the
hermetic path off the blast-radius of an un-caged agent entirely (the agent cage is a
real boundary the spec went to lengths to keep — bot Tier-1 simply doesn't need to remove
it), and makes the run reproducible in normal CI with zero model spend.

THE FAKE TELEGRAM SERVER (``FakeTelegram``). A stdlib ``http.server`` that speaks the Bot
API the SUT's poller talks to: ``getUpdates`` returns the INJECTED updates (a human typing)
and honors Telegram's real OFFSET/ACK contract — an update is RETAINED and re-deliverable
until the poller acks it by calling ``getUpdates`` with ``offset > update_id`` (it is NOT
popped on read), so a poller that never advances its offset cannot accidentally pass;
``sendMessage`` / ``sendPhoto`` / ``sendDocument`` / ``answerCallbackQuery`` /
``editMessageText`` CAPTURE the bot's outbound calls and return a plausible ``ok: true``
result; ``getMe`` / ``deleteWebhook`` / ``setMyCommands`` are benign stubs so a bot's
startup handshake succeeds. The SUT bot reaches it via ``TG_API_BASE`` — ``tg-ctl`` already
honors that env (verified ``tg-ctl:2169``), as do telegraf / grammy / python-telegram-bot /
aiogram via their API-root option. The update shapes follow the real Bot API
(``update_id`` / ``message`` / ``callback_query``) so a real poller accepts them unchanged.

THE POSITIVE CAPABILITY PROBE (load-bearing — closes the spec footgun). Zero captured
sends is ambiguous: it looks identical whether the bot is correctly silent OR its sender
hardcodes ``api.telegram.org`` and never reached the fake at all (the un-patched-sender
gap, spec §7.3). So the driver makes the bot demonstrably reachable BEFORE any case: it
injects a probe update and waits for ANY outbound call within the boot window. A SUT that
never calls the fake fails the health gate with the precise ``TG_API_BASE`` pointer, rather
than silently passing on zero sends. (A bot that legitimately never sends on the probe can
declare ``sut.bot.skip_probe: true``; the default is to require it.)

SAFETY (fail-closed, even hermetic). The fake binds ``127.0.0.1`` only — it never accepts a
remote connection, and ``TG_API_BASE`` is overwritten to the fake so a bot that honors it can
only reach the fake. The remaining risk is a bot that IGNORES ``TG_API_BASE`` and hardcodes
``api.telegram.org`` — we cannot stop it from reaching the host, but we CAN refuse to run when a
real-looking ``TG_CHAT_ID`` is in the environment (the value that would target a real human), so
a misconfigured run fails closed before it can leak. All chat ids used in injected updates are
synthetic test ids in a reserved negative range.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

# How much of the bot's stdout tail to retain in memory for a BLOCKED-report proof. The drain
# thread keeps the OS pipe empty (so a chatty bot never blocks); we hold only the last few KiB,
# which is plenty for a crash/traceback tail.
_OUTPUT_TAIL_BYTES = 16384

# A reserved synthetic chat id for injected updates. Negative (a Telegram group-style id) and
# fixed so a suite author can address it, but unmistakably a TEST id — never a real chat.
TEST_CHAT_ID = -1009999000001
# The synthetic human "from" user that authors every injected update.
TEST_FROM_USER = {"id": 424242, "is_bot": False, "first_name": "QA", "username": "qa_tester"}

# How long the bot has to make its FIRST outbound call against the fake (the capability
# probe) before we declare TG_API_BASE unwired. Generous — a Node/Python bot's cold start +
# first long-poll can take a few seconds. Overridable via REVIEW_QA_BOT_PROBE_TIMEOUT_S so a
# slow bot gets more room and a test can shrink the unwired-detection wait.
_PROBE_TIMEOUT_S = float(os.environ.get("REVIEW_QA_BOT_PROBE_TIMEOUT_S", "30"))
# How long to wait for the bot's response to a single injected case update (its handler runs,
# then it sends). A case that produces no outbound within this window is a FAIL (the bot was
# silent when the case expected a reply). Overridable via REVIEW_QA_BOT_RESPONSE_TIMEOUT_S so a
# slow bot gets more room and a fast test suite can shrink it.
_CASE_RESPONSE_TIMEOUT_S = float(os.environ.get("REVIEW_QA_BOT_RESPONSE_TIMEOUT_S", "15"))
# A SILENT case (Expect-silent) only needs a SHORT window to confirm no reply — a bot that was
# going to reply does so within a beat of receiving the update, so we don't pay the full
# response timeout just to confirm silence. This keeps a suite full of silent cases fast.
_SILENT_CONFIRM_TIMEOUT_S = float(os.environ.get("REVIEW_QA_BOT_SILENT_TIMEOUT_S", "2"))
# Poll granularity while waiting for an outbound call to land.
_WAIT_POLL_S = 0.05
# The cap on how long an empty ``getUpdates`` BLOCKS server-side before returning ``[]``. A
# real long-poller sends ``getUpdates?timeout=30`` and relies on the SERVER holding the
# connection open until an update arrives (or the timeout) — WITHOUT a client-side sleep. If the
# fake returned ``[]`` instantly, such a bot would busy-loop at 100% CPU and flood the fake with
# thousands of requests/s, starving the GIL the driver's own capture-wait shares. So an empty
# queue is held up to this cap (a short hold keeps the driver responsive while killing the spin);
# the client's requested ``timeout`` is honored but clamped to this ceiling.
_GET_UPDATES_HOLD_CAP_S = 1.0


# --- the captured outbound call + injected update ------------------------------------
@dataclass(frozen=True)
class OutboundCall:
    """One Bot-API call the SUT bot made against the fake. ``method`` is the Bot-API method
    (``sendMessage`` / ``answerCallbackQuery`` / …); ``payload`` is the decoded request body
    (JSON or form). ``text`` is the convenience accessor for ``sendMessage``/``editMessageText``
    bodies (the most-asserted field); ``chat_id`` is the target chat (synthetic in a hermetic
    run). ``at`` is the monotonic capture time so the driver can window a per-case response."""

    method: str
    payload: dict
    at: float

    @property
    def text(self) -> str:
        return str(self.payload.get("text") or self.payload.get("caption") or "")

    @property
    def chat_id(self) -> str:
        cid = self.payload.get("chat_id")
        return "" if cid is None else str(cid)


def make_text_update(update_id: int, text: str, *, chat_id: int = TEST_CHAT_ID) -> dict:
    """A synthetic inbound text-message update (a human typing ``text``). The shape matches the
    real Bot API ``Update{update_id, message{message_id, from, chat, date, text}}`` so a real
    long-poller accepts it unchanged (spec §7.3 update grounding)."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": dict(TEST_FROM_USER),
            "chat": {"id": chat_id, "type": "private", "first_name": "QA"},
            "date": int(time.time()),
            "text": text,
        },
    }


def make_callback_update(update_id: int, data: str, *, chat_id: int = TEST_CHAT_ID) -> dict:
    """A synthetic inbound callback-query update (a human tapping an inline button whose
    callback_data is ``data``) — the only faithful way to exercise a bot's button handlers."""
    return {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "from": dict(TEST_FROM_USER),
            "message": {
                "message_id": update_id,
                "chat": {"id": chat_id, "type": "private", "first_name": "QA"},
                "date": int(time.time()),
                "text": "(button host message)",
            },
            "data": data,
        },
    }


# --- the fake Telegram Bot API server ------------------------------------------------
class FakeTelegram:
    """A hermetic in-process fake of the Telegram Bot API the SUT bot polls.

    Lifecycle: ``start()`` binds an ephemeral ``127.0.0.1`` port and serves in a daemon
    thread; ``base_url()`` is the ``TG_API_BASE`` the SUT is pointed at; ``inject(update)``
    queues a synthetic inbound update a subsequent ``getUpdates`` drains; ``outbound`` is the
    growing list of captured calls; ``stop()`` shuts the server down. Thread-safe — the
    server thread mutates the capture list / update queue under a lock the driver also holds
    when reading.

    The server is deliberately permissive about the Bot-API token path (``/bot<token>/<method>``):
    a hermetic run uses a throwaway token, and we only key off the ``<method>`` tail, so any
    token the SUT was configured with works. ``getUpdates`` honors the real long-poll contract
    enough for a poller to make progress: it returns queued updates immediately, and on an empty
    queue it BLOCKS up to the request's ``timeout`` (capped) so a long-polling bot doesn't spin."""

    def __init__(self) -> None:
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._updates: list[dict] = []
        self._delivered_offset = 0
        self.outbound: list[OutboundCall] = []

    def start(self) -> None:
        handler = _make_handler(self)
        # Port 0 -> the OS picks a free ephemeral port; bind loopback only (never remote).
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("FakeTelegram.start() must be called before base_url()")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # inject / drain (the inbound "human typing" side) --------------------------------
    def inject(self, update: dict) -> None:
        """Queue a synthetic inbound update for the next ``getUpdates`` to deliver."""
        with self._lock:
            self._updates.append(update)

    def _drain_updates(self, offset: int | None) -> list[dict]:
        """The ``getUpdates`` body: every queued-but-undelivered update. Honors the real
        ``offset`` ack contract — a poller passing ``offset = last_update_id + 1`` acks prior
        updates, so we never re-deliver them (re-delivery would make a bot re-handle a case and
        double-send, corrupting the capture)."""
        with self._lock:
            if offset is not None and offset > self._delivered_offset:
                self._delivered_offset = offset
            return [u for u in self._updates if u["update_id"] >= self._delivered_offset]

    def _long_poll_updates(self, offset: int | None, client_timeout: float) -> list[dict]:
        """The real ``getUpdates`` long-poll: return any pending updates immediately, else HOLD
        server-side until an update is injected OR a short capped timeout elapses, then return
        (possibly empty). This is what stops a real ``getUpdates?timeout=30`` poller from
        busy-looping against the fake — the bot's connection is held just like Telegram holds
        it, so it makes ONE request per hold window instead of thousands per second. The hold is
        clamped to ``_GET_UPDATES_HOLD_CAP_S`` so the driver stays responsive (it injects then
        waits for the reply on its own thread); a bot asking for less waits less."""
        deadline = time.monotonic() + min(max(client_timeout, 0.0), _GET_UPDATES_HOLD_CAP_S)
        while True:
            pending = self._drain_updates(offset)
            if pending or time.monotonic() >= deadline:
                return pending
            time.sleep(_WAIT_POLL_S)

    # capture (the outbound "bot replying" side) --------------------------------------
    def _capture(self, method: str, payload: dict) -> None:
        with self._lock:
            self.outbound.append(OutboundCall(method=method, payload=payload, at=time.monotonic()))

    def outbound_since(self, since: float) -> list[OutboundCall]:
        """Every captured outbound call AT OR AFTER monotonic time ``since`` — the per-case
        window the driver asserts against (calls made before the case's update was injected
        belong to an earlier case / the boot handshake and must not leak in)."""
        with self._lock:
            return [c for c in self.outbound if c.at >= since]

    def wait_for_outbound(self, since: float, *, timeout: float) -> list[OutboundCall]:
        """Block up to ``timeout`` for AT LEAST ONE outbound call at/after ``since``, then
        return everything captured in that window. Returns ``[]`` on timeout (the bot stayed
        silent) — the caller decides whether silence is a PASS (the case expected none) or a
        FAIL (it expected a reply)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            calls = self.outbound_since(since)
            if calls:
                # Give a beat for a multi-message reply to finish landing, then return all.
                time.sleep(_WAIT_POLL_S)
                return self.outbound_since(since)
            time.sleep(_WAIT_POLL_S)
        return self.outbound_since(since)

    def wait_until_satisfied(
        self, since: float, *, predicate: Callable[[list[OutboundCall]], bool], timeout: float,
    ) -> list[OutboundCall]:
        """Collect outbound calls at/after ``since`` until ``predicate`` is satisfied OR
        ``timeout`` expires, then return everything captured in the window.

        WHY (review finding). A purely time-based wait (return 50ms after the FIRST call) can
        false-FAIL a MULTI-message reply whose expected text is in a LATER message that lands
        after the grace — and can misattribute a delayed earlier-case/probe send. Returning the
        moment the EXPECTATIONS are met (predicate True) fixes both: a split reply keeps the
        window open until the expected substring arrives, and the predicate matching ``since``-
        windowed calls (not raw time) means a delayed send still belongs to the case whose
        window it lands in. On timeout it returns whatever arrived (the caller then classifies a
        miss honestly). For a SILENT case the predicate is "stay empty", so it waits the full
        (short) window to PROVE no reply came — exactly the right semantics."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            calls = self.outbound_since(since)
            if predicate(calls):
                return calls
            time.sleep(_WAIT_POLL_S)
        return self.outbound_since(since)


# Bot-API methods that produce an outbound call we CAPTURE (the bot replying). Anything else
# (getUpdates / getMe / a webhook teardown) is handled as a benign stub.
_CAPTURED_METHODS = frozenset({
    "sendMessage", "sendPhoto", "sendDocument", "sendVoice", "sendAudio", "sendVideo",
    "sendAnimation", "sendSticker", "sendDice", "sendChatAction", "answerCallbackQuery",
    "editMessageText", "editMessageReplyMarkup", "editMessageCaption", "deleteMessage",
    "answerInlineQuery", "sendMediaGroup",
})


def _make_handler(fake: FakeTelegram) -> type[BaseHTTPRequestHandler]:
    """Build the request handler class bound to ``fake``. A closure over ``fake`` keeps the
    server state out of the handler (BaseHTTPRequestHandler is instantiated per request)."""

    class _Handler(BaseHTTPRequestHandler):
        # Silence the default per-request stderr log line — a hermetic run captures hundreds
        # of long-poll requests and the noise would bury the real qa output.
        def log_message(self, *_a) -> None:  # noqa: A003
            return

        def do_POST(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
            self._dispatch(self._read_body())

        def do_GET(self) -> None:  # noqa: N802 (some bots use GET for getUpdates)
            self._dispatch(self._query_params())

        def _method_name(self) -> str:
            """The Bot-API method from ``/bot<token>/<method>`` (token ignored — hermetic)."""
            tail = urlparse(self.path).path.rstrip("/").rsplit("/", 1)
            return tail[-1] if tail else ""

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            return _decode_body(raw, self.headers.get("Content-Type", ""))

        def _query_params(self) -> dict:
            from urllib.parse import parse_qs

            q = parse_qs(urlparse(self.path).query)
            return {k: (v[0] if len(v) == 1 else v) for k, v in q.items()}

        def _dispatch(self, params: dict) -> None:
            method = self._method_name()
            if method == "getUpdates":
                self._respond_get_updates(params)
            elif method in _CAPTURED_METHODS:
                fake._capture(method, params)
                self._respond_ok(_fake_message_result(method, params))
            else:
                # getMe / deleteWebhook / setMyCommands / anything else: a benign ok stub so a
                # bot's startup handshake succeeds without us modelling every method.
                self._respond_ok(_benign_result(method))

        def _respond_get_updates(self, params: dict) -> None:
            offset = _as_int(params.get("offset"))
            # A real poller sends timeout=<N>; honor it (clamped server-side) so an empty queue
            # HOLDS the connection instead of returning instantly and inviting a busy-loop.
            client_timeout = _as_float(params.get("timeout")) or 0.0
            updates = fake._long_poll_updates(offset, client_timeout)
            self._respond_ok(updates)

        def _respond_ok(self, result: object) -> None:
            self._write_json(200, {"ok": True, "result": result})

        def _write_json(self, status: int, obj: dict) -> None:
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return _Handler


def _decode_body(raw: bytes, content_type: str) -> dict:
    """Decode a Bot-API request body: JSON (telegraf/grammy default), form-urlencoded
    (python-telegram-bot / a curl ``-d``), or empty. Multipart (file upload) is decoded only
    for its text fields — a hermetic run asserts on text/caption, not the binary blob."""
    if not raw:
        return {}
    ctype = content_type.lower()
    if "application/json" in ctype:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}
    if "application/x-www-form-urlencoded" in ctype:
        from urllib.parse import parse_qs

        q = parse_qs(raw.decode("utf-8", "replace"))
        return {k: (v[0] if len(v) == 1 else v) for k, v in q.items()}
    if "multipart/form-data" in ctype:
        return _decode_multipart_text(raw, content_type)
    # Unknown content-type: best-effort JSON, else empty.
    try:
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, UnicodeDecodeError):
        return {}


def _decode_multipart_text(raw: bytes, content_type: str) -> dict:
    """Pull the simple text fields out of a multipart body (chat_id, text, caption, …),
    ignoring file parts. Enough for a hermetic text/caption assertion; a real file upload's
    bytes are not modelled."""
    import re

    m = re.search(r"boundary=([^;]+)", content_type)
    if not m:
        return {}
    boundary = b"--" + m.group(1).strip().strip('"').encode()
    out: dict = {}
    for part in raw.split(boundary):
        name_m = re.search(rb'name="([^"]+)"', part)
        if not name_m or b"filename=" in part.split(b"\r\n\r\n", 1)[0]:
            continue
        if b"\r\n\r\n" not in part:
            continue
        value = part.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0]
        try:
            out[name_m.group(1).decode()] = value.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return out


def _fake_message_result(method: str, payload: dict) -> object:
    """A plausible ``result`` for a captured send, so a bot that READS the API response (e.g.
    to thread a reply or store the sent message_id) doesn't crash on a malformed stub. A
    message-producing send returns a Message; answerCallbackQuery returns True."""
    if method == "answerCallbackQuery":
        return True
    if method == "deleteMessage":
        return True
    mid = int(time.time() * 1000) % 1_000_000
    chat_id = payload.get("chat_id", TEST_CHAT_ID)
    try:
        chat_id = int(chat_id)
    except (TypeError, ValueError):
        chat_id = TEST_CHAT_ID
    return {
        "message_id": mid,
        "from": {"id": 1, "is_bot": True, "first_name": "QA-bot", "username": "qa_bot"},
        "chat": {"id": chat_id, "type": "private"},
        "date": int(time.time()),
        "text": payload.get("text", ""),
    }


def _benign_result(method: str) -> object:
    """The ``result`` for a non-captured handshake method. ``getMe`` returns a bot user;
    boolean methods (deleteWebhook / setMyCommands / close / logOut) return True."""
    if method == "getMe":
        return {"id": 1, "is_bot": True, "first_name": "QA-bot", "username": "qa_bot",
                "can_join_groups": True, "can_read_all_group_messages": False,
                "supports_inline_queries": False}
    return True


def _as_int(val: object) -> int | None:
    try:
        return int(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_float(val: object) -> float | None:
    try:
        return float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# --- driving the SUT bot against the fake --------------------------------------------
class BotHarnessError(RuntimeError):
    """A controlled bot-harness failure (could not boot the bot / TG_API_BASE unwired).
    Carries the qa exit class the handler should return so a hermetic-harness infra failure
    maps to a stable code (NOT a found bug — that is report-only)."""

    def __init__(self, message: str, *, exit_code: int):
        super().__init__(message)
        self.exit_code = exit_code


class BotProcess:
    """A running SUT bot subprocess pointed at the fake, plus the plan to reap it.

    Reaping sends the process GROUP a SIGTERM then SIGKILL (a bot may fork a poller child);
    idempotent and never raises so it is safe from a finally AND the global teardown sweep.

    STDOUT DRAIN (load-bearing — review finding). The bot's stdout is piped, and a chatty bot
    that logs more than the OS pipe buffer (~64 KiB) BEFORE it starts long-polling would BLOCK
    on the next ``print`` until someone reads the pipe — producing a false BLOCKED/FAIL because
    the bot never reaches its poll. So a daemon thread drains stdout CONTINUOUSLY from boot into
    a bounded in-memory tail (a deque capped at ``_OUTPUT_TAIL_BYTES``), keeping the pipe empty
    for the whole run. ``output_tail()`` returns that buffer for the BLOCKED-report proof — no
    blocking ``stdout.read()`` is ever needed."""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self._reaped = False
        self._tail: deque[str] = deque()
        self._tail_len = 0
        self._tail_lock = threading.Lock()
        self._drain_thread: threading.Thread | None = None
        if proc.stdout is not None:
            self._drain_thread = threading.Thread(target=self._drain_stdout, daemon=True)
            self._drain_thread.start()

    def _drain_stdout(self) -> None:
        """Continuously read the bot's stdout line-by-line into the bounded tail so the pipe
        never fills. Exits on EOF (the bot closed stdout / exited). Never raises."""
        stream = self.proc.stdout
        if stream is None:
            return
        try:
            for line in stream:  # blocks per line; EOF ends the loop when the bot exits
                with self._tail_lock:
                    self._tail.append(line)
                    self._tail_len += len(line)
                    while self._tail_len > _OUTPUT_TAIL_BYTES and self._tail:
                        self._tail_len -= len(self._tail.popleft())
        except (OSError, ValueError):
            pass

    def output_tail(self, *, limit: int = 2000) -> str:
        """The captured stdout tail (last ``limit`` chars) for a BLOCKED-report proof. Reads the
        in-memory buffer the drain thread fills — never the pipe — so it cannot block even if a
        forked child still holds the pipe open."""
        with self._tail_lock:
            text = "".join(self._tail)
        return text[-limit:] or "(no output captured)"

    def reap(self) -> None:
        if self._reaped:
            return
        self._reaped = True
        _terminate_group(self.proc)
        if self._drain_thread is not None:
            # The drain loop ends when the reaped bot closes stdout (EOF); join briefly so a
            # surviving child holding the pipe can't keep the thread alive past the run.
            self._drain_thread.join(timeout=2)


def _terminate_group(proc: subprocess.Popen) -> None:
    """SIGTERM then (after a grace) SIGKILL the bot's whole process GROUP, so a forked poller
    child can't outlive the run. Best-effort; never raises.

    Does NOT short-circuit on a dead LEADER (review finding). When ``sut.bot.command`` is a
    wrapper that forks/backgrounds the real poller and then EXITS, ``proc.poll()`` is already
    non-None while the forked poller keeps running in the same session/group — returning early
    would leak it and break the guaranteed-teardown promise. The group id equals the launcher
    pid (``start_new_session=True`` made it the session/group leader), so we can still signal
    the GROUP after the leader exited; the leader's own death just means its slot in the group
    is already gone. We capture the pgid up front so a reaped leader doesn't make ``getpgid``
    fail."""
    pgid = _pgid_of(proc)
    leader_alive = proc.poll() is None
    if pgid is None and not leader_alive:
        return  # nothing we can address (no pgid, leader already gone)
    _signal_group_or_proc(proc, pgid, signal.SIGTERM)
    if leader_alive:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    else:
        time.sleep(0.2)  # brief grace for a forked child to handle SIGTERM
    _signal_group_or_proc(proc, pgid, signal.SIGKILL)


def _pgid_of(proc: subprocess.Popen) -> int | None:
    """The bot's process-group id (== the launcher pid under ``start_new_session=True``), or
    ``None`` if it can't be determined. Captured BEFORE the leader is reaped so a forked-child
    group can still be addressed after the leader exits."""
    try:
        return os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        # The leader is already reaped; under start_new_session the pgid equals the pid.
        return proc.pid if proc.pid else None


def _signal_group_or_proc(proc: subprocess.Popen, pgid: int | None, sig: int) -> None:
    """Send ``sig`` to the whole process GROUP (preferred — reaps a forked poller child even
    after the leader exited), falling back to the single process. Best-effort; never raises."""
    if pgid is not None:
        try:
            os.killpg(pgid, sig)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        if sig == signal.SIGKILL:
            proc.kill()
        else:
            proc.terminate()
    except (OSError, ValueError):
        pass


def boot_bot(
    *,
    command: list[str],
    cwd: Path,
    api_base: str,
    extra_env: dict[str, str] | None = None,
    exit_boot_failed: int,
) -> BotProcess:
    """Spawn the SUT bot pointed at the fake Telegram (``TG_API_BASE=api_base``), in its OWN
    process group (so the whole bot tree can be reaped). The bot long-polls the fake from this
    point on. Fails CLOSED if the resolved env carries a real-looking ``TG_CHAT_ID`` — the one
    value that would target a real human if a bot ignored ``TG_API_BASE`` (we overwrite
    ``TG_API_BASE`` to the fake but cannot force a bot that hardcodes ``api.telegram.org`` to
    use it, so the chat-id guard is the safety net). Raises ``BotHarnessError(exit_boot_failed)``
    if the process cannot be launched at all."""
    env = dict(os.environ)
    env.update(extra_env or {})
    env["TG_API_BASE"] = api_base
    # A throwaway hermetic token — the fake ignores it, but a bot that requires one to start
    # needs SOMETHING non-empty. Never a real token.
    env.setdefault("BOT_TOKEN", "123456:hermetic-qa-test-token")
    env.setdefault("TG_BOT_TOKEN", env["BOT_TOKEN"])
    _refuse_real_telegram(env, exit_boot_failed=exit_boot_failed)
    try:
        proc = subprocess.Popen(  # noqa: S603 — command resolved from the SUT's own qa.yaml
            command, cwd=str(cwd), env=env, start_new_session=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
    except (OSError, ValueError) as exc:
        raise BotHarnessError(
            f"could not launch the SUT bot {command!r} in {cwd}: {exc}",
            exit_code=exit_boot_failed,
        ) from exc
    return BotProcess(proc=proc)


def _refuse_real_telegram(env: dict[str, str], *, exit_boot_failed: int) -> None:
    """Fail closed before booting if the env would point the bot at the REAL Telegram or carry
    a real chat id. ``TG_API_BASE`` is about to be overwritten to the fake, so the danger is a
    bot that IGNORES it and hardcodes ``api.telegram.org`` while a real ``TG_CHAT_ID`` is in the
    environment — that combination could leak a test message to the user's real chat. We can't
    stop a bot from hardcoding the host, but we CAN refuse to run with a real chat id present,
    which is the value that would target a real human."""
    real_chat = (env.get("TG_CHAT_ID") or "").strip()
    if real_chat and not real_chat.startswith("-100999"):
        raise BotHarnessError(
            "refusing a hermetic bot run with a real-looking TG_CHAT_ID in the environment "
            f"({real_chat!r}). A Tier-1 run must be fully hermetic — unset TG_CHAT_ID (or set "
            "it to a synthetic test id) so a bot that ignores TG_API_BASE can't reach a real "
            "chat.",
            exit_code=exit_boot_failed,
        )


def probe_reachable(fake: FakeTelegram, *, timeout: float = _PROBE_TIMEOUT_S) -> bool:
    """The POSITIVE capability probe (spec §7.3 footgun): inject a probe update and wait for
    the bot to make ANY outbound call against the fake within ``timeout``. Returns True the
    moment the bot is demonstrably talking to the fake. This DISAMBIGUATES 'the bot is
    correctly silent' from 'the bot's sender hardcodes api.telegram.org and never reached us'
    — without it, a passing run on zero captured sends could be a completely unwired harness.

    CAVEAT (state mutation). The probe sends a real ``/start`` that the bot HANDLES, so a
    STATEFUL bot's first-message state is touched before the suite's first ``## Case:`` runs —
    a bot whose SECOND ``/start`` differs ("already registered") could then FAIL a first-case
    ``/start``. For such a bot, set ``sut.bot.skip_probe: true`` (you forgo the unwired-sender
    safety net) and make the FIRST case itself prove reachability. ``/start`` is used because it
    is the one command essentially every bot answers; a less side-effecting trigger would not
    reliably elicit the outbound the probe needs."""
    since = time.monotonic()
    fake.inject(make_text_update(_PROBE_UPDATE_ID, "/start"))
    calls = fake.wait_for_outbound(since, timeout=timeout)
    return bool(calls)


# A high update_id for the probe so it never collides with a suite's case update ids.
_PROBE_UPDATE_ID = 900000001
