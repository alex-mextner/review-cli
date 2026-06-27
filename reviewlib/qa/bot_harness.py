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
    return _spawn_bot_process(command, cwd=cwd, env=env, exit_boot_failed=exit_boot_failed)


def _spawn_bot_process(
    command: list[str], *, cwd: Path, env: dict[str, str], exit_boot_failed: int,
) -> BotProcess:
    """Spawn ``command`` in its OWN process group (so the whole tree can be reaped), piping output
    into the bounded drain. Shared by ``boot_bot`` (the inbound SUT poller) and
    ``boot_agent_daemon`` (the agent-side bridge daemon) so spawn + reap + drain live in one place.
    Raises ``BotHarnessError(exit_boot_failed)`` if the process cannot be launched."""
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


# --- the AGENT-SIDE seam (a bridge bot: agent emits a question -> tap -> answer) -------
# WHY a SECOND path beside inject/capture. A bridge bot (tg-ctl) is never driven inbound: the
# AGENT emits an AskUserQuestion/permission via a hook client that reads a payload on stdin, the
# daemon forwards ONE inline-button CARD to Telegram, the user TAPS, and the answer flows back to
# the hook client's STDOUT. The Tier-1 inject/capture path cannot reach that loop — it has no way
# to (a) emit the agent's question or (b) read the answer the agent receives. The pieces below are
# exactly (a)+(b): emit_question runs the hook client and keeps its handle; AskHandle.await_answer
# reads the answer off its stdout (a hang / None is the tap-loss bug, assertable); cards_captured
# filters the fake's outbound to inline-button cards; tap injects a synthetic callback_query so the
# daemon delivers the answer. FakeTelegram itself is UNCHANGED — these only read fake.outbound and
# call fake.inject, the same surface the inbound path uses.

# How long the agent-side daemon has to create its readiness file (e.g. its hook socket) before the
# first question is emitted. A cold bun/node daemon + first long-poll can take a few seconds.
_DAEMON_READY_TIMEOUT_S = float(os.environ.get("REVIEW_QA_BOT_DAEMON_READY_TIMEOUT_S", "20"))
# The fixed boot grace used when no ready_file is configured (a generic bridge bot whose readiness
# signal the harness can't name): wait this long after boot before the first emit.
_DAEMON_BOOT_GRACE_S = float(os.environ.get("REVIEW_QA_BOT_DAEMON_BOOT_GRACE_S", "3"))
# How long to wait for a question's card(s) to land after emitting it.
_CARD_TIMEOUT_S = float(os.environ.get("REVIEW_QA_BOT_CARD_TIMEOUT_S", "15"))
# How long to confirm NO new card lands (the #98 re-fire assertion: an answered re-ask must post
# nothing). A daemon that was going to post does so within a beat, so this stays short.
_NO_CARD_CONFIRM_S = float(os.environ.get("REVIEW_QA_BOT_NO_CARD_TIMEOUT_S", "3"))
# How long to wait for the hook client to print the answer after a tap (or a replay). A miss /
# hang here IS the tap-loss bug — the answer never reached the agent.
_ANSWER_TIMEOUT_S = float(os.environ.get("REVIEW_QA_BOT_ANSWER_TIMEOUT_S", "20"))


def make_agent_tap_update(update_id: int, data: str, *, from_id: int) -> dict:
    """A synthetic inbound callback-query update for the AGENT-SIDE tap: the owner taps an inline
    button whose ``callback_data`` is ``data``. ``from.id`` is ``from_id`` (a bridge bot gates a
    tap on the owner id — a tap from any other id is dropped). Carries NO ``message`` field on
    purpose: a bridge daemon then skips the host-message-id match, so the tap lands without the
    harness having to thread the card's server-assigned message id back in."""
    return {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "from": {"id": from_id, "is_bot": False, "first_name": "QA"},
            "data": data,
        },
    }


def cards_captured(fake: FakeTelegram) -> list[OutboundCall]:
    """Every NEW inline-button CARD the bridge bot POSTED — a ``sendMessage`` whose
    ``reply_markup.inline_keyboard`` is non-empty. This is the question card the user taps; a
    re-fire that posts a SECOND one is the #98 duplicate bug.

    Deliberately ONLY ``sendMessage`` (NOT ``editMessageText``/``editMessageReplyMarkup``): an edit
    MUTATES an existing card, it does not post a new one, so counting an edit as a 'new card' would
    false-FAIL the ``Expect-card: 0`` re-fire assertion for a bridge that legitimately edits the
    card after answering. The duplicate the #98 class produces is a fresh ``sendMessage``."""
    out: list[OutboundCall] = []
    for c in fake.outbound:
        if c.method != "sendMessage":
            continue
        markup = c.payload.get("reply_markup")
        if isinstance(markup, dict) and markup.get("inline_keyboard"):
            out.append(c)
    return out


def card_button_data(card: OutboundCall, button_label: str) -> str | None:
    """The ``callback_data`` of the button labelled ``button_label`` on ``card`` (case-insensitive,
    trimmed), or ``None`` when no such button exists. The label match is what a suite's ``Tap:``
    directive resolves against."""
    markup = card.payload.get("reply_markup")
    if not isinstance(markup, dict):
        return None
    want = button_label.strip().lower()
    for row in markup.get("inline_keyboard", []):
        for btn in row:
            if isinstance(btn, dict) and str(btn.get("text", "")).strip().lower() == want:
                data = btn.get("callback_data")
                return str(data) if data is not None else None
    return None


def card_button_labels(card: OutboundCall) -> list[str]:
    """Every button label on ``card`` (row-major) — for an error message when a ``Tap:`` names a
    button the card does not have."""
    markup = card.payload.get("reply_markup")
    labels: list[str] = []
    if isinstance(markup, dict):
        for row in markup.get("inline_keyboard", []):
            labels += [str(b.get("text", "")) for b in row if isinstance(b, dict)]
    return labels


def tap(fake: FakeTelegram, card: OutboundCall, button_label: str, *, from_id: int) -> bool:
    """Inject the user's TAP of the ``button_label`` button on ``card``: extract that button's
    ``callback_data`` and queue a synthetic ``callback_query`` from ``from_id``. Returns True when
    the button was found and the tap injected, False when ``card`` has no such button (the caller
    fails the case with the available labels). The daemon delivers the answer down the hook client
    on the next poll."""
    data = card_button_data(card, button_label)
    if data is None:
        return False
    fake.inject(make_agent_tap_update(_next_tap_update_id(), data, from_id=from_id))
    return True


# A monotonic update-id counter for injected TAPS, starting above the probe id so a daemon that
# acked the probe (offset past it) still receives the tap (a lower id would be filtered as acked).
_TAP_UPDATE_ID = _PROBE_UPDATE_ID + 5000


def _next_tap_update_id() -> int:
    global _TAP_UPDATE_ID
    _TAP_UPDATE_ID += 1
    return _TAP_UPDATE_ID


@dataclass
class AskHandle:
    """A running hook-client (``ask_command``) subprocess that emitted ONE question. The agent is
    blocked on this process until the answer comes back on its stdout — so reading that stdout IS
    reading what the agent receives. ``await_answer`` returns the answer text (possibly ``""`` if it
    exited silently), or ``None`` when the process hangs past the timeout (the tap never reached the
    agent — the tap-loss bug)."""

    proc: subprocess.Popen
    _answer: str | None = None
    _read: bool = False

    def await_answer(self, *, timeout: float = _ANSWER_TIMEOUT_S) -> str | None:
        """Wait up to ``timeout`` for the hook client to EXIT and return its stdout (the answer the
        agent received) — ``""`` if it exited with no stdout, ``None`` if it never exits in time (a
        hang = the answer was lost / tap-loss). Uses ``communicate`` so BOTH stdout AND stderr are
        drained while waiting: a real bridge (bun/node ``tg-ctl``) may log to stderr, and a naive
        ``wait`` + later ``read`` would deadlock the moment that output exceeds the ~64 KiB pipe
        buffer (the bot blocks on ``write``, never exits → a false ``None``). On a hang the process
        GROUP is reaped (it may have forked children) and its pipes drained so nothing leaks.
        Idempotent — caches the first read."""
        if self._read:
            return self._answer
        self._read = True
        try:
            out, _err = self.proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._terminate()
            try:  # drain the now-dead process's pipes so they close (no leak)
                self.proc.communicate(timeout=3)
            except (subprocess.TimeoutExpired, ValueError, OSError):
                pass
            self._answer = None
            return None
        self._answer = (out or "").strip()
        return self._answer

    def _terminate(self) -> None:
        """SIGTERM->SIGKILL the hook client's whole process GROUP (it is spawned in its own session
        in ``emit_question``, so a forked child can't outlive it). Best-effort; never raises."""
        _terminate_group(self.proc)

    def reap(self) -> None:
        """Kill the hook client if it is still running (guaranteed teardown). Never raises."""
        if self.proc.poll() is None:
            self._terminate()


def emit_question(
    *, ask_command: list[str], cwd: Path, env: dict[str, str], payload: str,
) -> AskHandle:
    """Run the hook client (``ask_command``) that emits ONE agent question: spawn it in its OWN
    session (so a forked child can be reaped with the group) from ``cwd``, write ``payload`` (the
    raw hook payload JSON) to its stdin and close it, and return the handle whose stdout carries the
    answer. The daemon, seeing the request on its socket, forwards a card to the fake; the answer
    flows back to this process's stdout once the tap is injected."""
    proc = subprocess.Popen(  # noqa: S603 — ask_command resolved from the SUT's own qa.yaml
        ask_command, cwd=str(cwd), env=env, start_new_session=True,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if proc.stdin is not None:
        try:
            proc.stdin.write(payload)
            proc.stdin.close()
        except (OSError, BrokenPipeError):
            pass
        # Detach the (now-closed) stdin from the Popen so a later ``communicate()`` in
        # ``await_answer`` never touches it. CPython's threaded ``_communicate`` (the path taken when
        # both stdout and stderr are captured) calls ``self.stdin.flush()`` before draining, and a
        # flush on a CLOSED stream raises ``ValueError: I/O operation on closed file``. Observed: this
        # crashed the AskHandle/DoD tests on the CI matrix's 3.10/3.11/3.12 and passed on 3.13/3.14
        # (the newer flush no longer trips on it). Nulling the attribute makes communicate skip stdin
        # on every version — the fd is already closed at the OS level, which is what delivers EOF to
        # the hook client's ``sys.stdin.read()`` so it can proceed.
        proc.stdin = None
    return AskHandle(proc=proc)


def wait_for_file(target: Path, *, timeout: float) -> bool:
    """Poll up to ``timeout`` for ``target`` to exist — the agent-side daemon's readiness signal
    (e.g. its hook socket). Returns True the moment it appears, False on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if target.exists():
            return True
        time.sleep(_WAIT_POLL_S)
    return target.exists()


def build_agent_env(
    *, api_base: str, owner_id: int, token: str, config_dir: Path, home: Path,
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """The env SHARED by the agent-side daemon and the hook client (they must agree on the config
    dir so the client finds the daemon's socket). Fully harness-controlled so nothing real leaks:
    ``TG_API_BASE`` -> the loopback fake, ``TG_CHAT_ID`` -> the synthetic ``owner_id``,
    ``TG_BOT_TOKEN`` -> the hermetic ``token``, ``TG_CTL_CONFIG_DIR`` / ``HOME`` -> the run's
    throwaway dirs. ``TMUX``/``TMUX_PANE`` are stripped so a forwarded question stays UNSCOPED (it
    would otherwise bind to this harness's own pane). ``extra_env`` (the SUT's ``sut.bot.env``) is
    applied first; the core hermetic overrides always win."""
    env = dict(os.environ)
    env.update(extra_env or {})
    # Strip TMUX AFTER the merge so a forwarded question stays UNSCOPED no matter the source (an
    # inherited TMUX from the harness's own pane, or an stray one in sut.bot.env).
    for k in ("TMUX", "TMUX_PANE"):
        env.pop(k, None)
    env["TG_API_BASE"] = api_base
    env["TG_CHAT_ID"] = str(owner_id)
    env["TG_BOT_TOKEN"] = token
    env["BOT_TOKEN"] = token
    env["TG_CTL_CONFIG_DIR"] = str(config_dir)
    env["HOME"] = str(home)
    return env


def boot_agent_daemon(
    *, command: list[str], cwd: Path, env: dict[str, str], exit_boot_failed: int,
) -> BotProcess:
    """Boot the agent-side bridge DAEMON (the long-poller, e.g. ``tg-ctl run``) against the fake,
    using the harness-built ``env`` (see ``build_agent_env``). The hermetic guarantee is the
    ``TG_API_BASE`` loopback override in that env (a synthetic owner is fine here — unlike the
    inbound path it is the bridge target, not an inherited real chat), so the inbound
    ``_refuse_real_telegram`` guard is not applied; the ``TG_API_BASE`` host is asserted loopback
    instead so a misbuilt env can never point the daemon at the real Telegram."""
    api_base = env.get("TG_API_BASE", "")
    if not _is_loopback(api_base):
        raise BotHarnessError(
            f"agent-side daemon refuses a non-loopback TG_API_BASE ({api_base!r}); the fake must "
            "bind 127.0.0.1 so the run stays hermetic.",
            exit_code=exit_boot_failed,
        )
    return _spawn_bot_process(command, cwd=cwd, env=env, exit_boot_failed=exit_boot_failed)


def _is_loopback(api_base: str) -> bool:
    host = urlparse(api_base).hostname or ""
    return host in ("127.0.0.1", "localhost", "::1")
