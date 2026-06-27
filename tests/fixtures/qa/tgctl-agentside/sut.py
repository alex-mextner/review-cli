"""A tiny, dependency-free AGENT-SIDE bridge-bot fixture for the qa bot agent-side harness.

It is a faithful miniature of tg-ctl's agent-side contract (NOT a mock of the review-cli
driver — it is a second, independent implementation of the SUT side, exactly as bot-good/bot.py
is a real subprocess bot for the inbound path). Two subcommands:

  * ``run``  — the DAEMON: long-polls the fake Telegram (``TG_API_BASE``), listens on a Unix
    socket (``<config_dir>/tg-ctl.<bot_id>.sock``), and on a hook request forwards ONE
    inline-button CARD to the fake; on the user's tap (a ``callback_query`` from the fake) it
    writes the chosen answer back down the requesting hook client's socket and closes it.
  * ``ask`` — the HOOK CLIENT: reads a Claude-Code-style hook payload on stdin, derives a
    STABLE requestId from (session, question text), sends the request to the daemon over the
    socket, and prints the answer the daemon returns to STDOUT (what the agent receives).

The #98 class is reproduced behind ``SUT_DUP_BUG=1``: when set, a re-fire of an ALREADY-ANSWERED
requestId posts a SECOND, duplicate card instead of replaying the stored answer. With the env
unset (the fixed behaviour) the re-fire replays the answer down the socket and posts NO card.

Registration is honoured: the daemon declines (no card) a request whose cwd is not in the seeded
``tg-ctl.<bot_id>.registration.json`` — so the harness's seed knob is exercised end-to-end.

stdlib-only (urllib for the Bot API, socket/threading for the daemon) so it runs in normal CI
with zero install.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

API_BASE = os.environ.get("TG_API_BASE", "https://api.telegram.org").rstrip("/")
TOKEN = os.environ.get("TG_BOT_TOKEN", "0:none")
BOT_ID = TOKEN.split(":", 1)[0]
OWNER_ID = int(os.environ.get("TG_CHAT_ID", "0"))
CONFIG_DIR = Path(os.environ.get("TG_CTL_CONFIG_DIR", os.path.expanduser("~/.config/tg-cli")))
DUP_BUG = os.environ.get("SUT_DUP_BUG") == "1"

SOCKET_PATH = CONFIG_DIR / f"tg-ctl.{BOT_ID}.sock"
REGISTRATION = CONFIG_DIR / f"tg-ctl.{BOT_ID}.registration.json"


def _api(method: str, payload: dict) -> dict:
    url = f"{API_BASE}/bot{TOKEN}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 — hermetic localhost fake
        return json.loads(resp.read().decode("utf-8"))


def _request_id(session: str, question: str) -> str:
    """A STABLE hash of (session, question) — the same question re-asked in the same session
    re-uses it, which is what makes the answered-replay / duplicate-card path fire (tg-cli#97)."""
    return hashlib.sha1(f"{session}\n{question}".encode()).hexdigest()[:12]


def _registered_cwds() -> set[str]:
    try:
        rows = json.loads(REGISTRATION.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    # realpath both sides: a tap is keyed on cwd, and /var vs /private/var (macOS) would otherwise
    # never match the process's canonical os.getcwd().
    return {os.path.realpath(str(r.get("cwd"))) for r in rows if isinstance(r, dict)}


# =========================== the DAEMON (`run`) ======================================
class Daemon:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # callback_data -> (client socket, requestId, chosen label) for an in-flight question.
        self._pending: dict[str, tuple[socket.socket, str, str]] = {}
        self._answered: dict[str, str] = {}  # requestId -> answer label (the replay cache)

    # --- the hook socket: a client (ask) sends a request, blocks for the answer ---------
    def serve(self) -> None:
        if SOCKET_PATH.exists():
            SOCKET_PATH.unlink()
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(SOCKET_PATH))
        srv.listen(8)
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=self._on_client, args=(conn,), daemon=True).start()

    def _on_client(self, conn: socket.socket) -> None:
        line = _read_line(conn)
        if not line:
            conn.close()
            return
        try:
            req = json.loads(line)
        except ValueError:
            conn.close()
            return
        if os.path.realpath(str(req.get("cwd"))) not in _registered_cwds():
            conn.close()  # not registered → decline, post no card
            return
        self._handle_request(conn, req)

    def _handle_request(self, conn: socket.socket, req: dict) -> None:
        rid = str(req.get("requestId"))
        question = str(req.get("question"))
        options = [str(o) for o in req.get("options", [])]
        with self._lock:
            prior = self._answered.get(rid)
            if prior is not None and not DUP_BUG:
                # FIXED behaviour: replay the stored answer, post NO second card (tg-cli#97 fix).
                _send_answer(conn, prior)
                return
            # New question (or the BUGGY re-fire): post a card and register its buttons.
            self._post_card(conn, rid, question, options)

    def _post_card(self, conn: socket.socket, rid: str, question: str, options: list[str]) -> None:
        keyboard = [[{"text": opt, "callback_data": f"cb:{rid}:{i}"}]
                    for i, opt in enumerate(options)]
        _api("sendMessage", {
            "chat_id": OWNER_ID,
            "text": f"Question from agent\n\n{question}",
            "reply_markup": {"inline_keyboard": keyboard},
        })
        for i, opt in enumerate(options):
            self._pending[f"cb:{rid}:{i}"] = (conn, rid, opt)

    # --- the poll loop: a tap arrives as a callback_query, resolve the waiting client ----
    def poll(self) -> None:
        offset = 0
        deadline = time.monotonic() + float(os.environ.get("SUT_MAX_RUNTIME_S", "120"))
        while time.monotonic() < deadline:
            try:
                resp = _api("getUpdates", {"offset": offset, "timeout": 1})
            except (urllib.error.URLError, OSError, ValueError):
                time.sleep(0.1)
                continue
            for update in resp.get("result", []):
                offset = max(offset, int(update["update_id"]) + 1)
                cb = update.get("callback_query")
                if cb:
                    self._on_tap(cb)
            time.sleep(0.02)

    def _on_tap(self, cb: dict) -> None:
        if int(cb.get("from", {}).get("id", 0)) != OWNER_ID:
            return  # a tap from a non-owner is dropped (the bridge gates on the owner)
        data = str(cb.get("data"))
        with self._lock:
            entry = self._pending.get(data)
            if entry is None:
                return
            conn, rid, label = entry
            _send_answer(conn, label)
            self._answered[rid] = label
            for key in [k for k in self._pending if k.startswith(f"cb:{rid}:")]:
                self._pending.pop(key, None)
        try:
            _api("answerCallbackQuery", {"callback_query_id": cb.get("id"), "text": "sent"})
        except (urllib.error.URLError, OSError, ValueError):
            pass


def _send_answer(conn: socket.socket, label: str) -> None:
    """Write the answer JSON the agent receives (the chosen label) down the socket and close it —
    the hook client is blocked reading until this closes."""
    body = json.dumps({"hookSpecificOutput": {"updatedInput": {"answers": {"choice": label}}}})
    try:
        conn.sendall((body + "\n").encode("utf-8"))
        conn.close()
    except OSError:
        pass


def _read_line(conn: socket.socket) -> str:
    buf = b""
    while b"\n" not in buf:
        try:
            chunk = conn.recv(4096)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
    return buf.decode("utf-8", "replace").strip()


def run_daemon() -> int:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    daemon = Daemon()
    threading.Thread(target=daemon.poll, daemon=True).start()
    daemon.serve()  # blocks
    return 0


# =========================== the HOOK CLIENT (`ask`) =================================
def run_ask() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except ValueError:
        return 0
    q = payload.get("tool_input", {}).get("questions", [{}])[0]
    question = str(q.get("question", ""))
    options = [str(o.get("label")) for o in q.get("options", []) if isinstance(o, dict)]
    session = str(payload.get("session_id", ""))
    request = {
        "requestId": _request_id(session, question),
        "cwd": os.getcwd(),
        "question": question,
        "options": options,
    }
    answer = _ask_daemon(json.dumps(request))
    if answer:
        sys.stdout.write(answer)
        sys.stdout.flush()
    return 0


def _ask_daemon(request_line: str) -> str:
    """Connect to the daemon socket, send the request, block for the answer line, return it."""
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(120)
        conn.connect(str(SOCKET_PATH))
        conn.sendall((request_line + "\n").encode("utf-8"))
        return _read_line(conn)
    except OSError:
        return ""


def main(argv: list[str]) -> int:
    if len(argv) >= 1 and argv[0] == "run":
        return run_daemon()
    if len(argv) >= 1 and argv[0] == "ask":
        return run_ask()
    sys.stderr.write(f"usage: sut.py run|ask (got {argv!r})\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
