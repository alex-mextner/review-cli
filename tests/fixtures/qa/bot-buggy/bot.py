#!/usr/bin/env python3
"""The BUGGY variant of the qa bot Tier-1 fixture (see ``bot-good/bot.py``).

The bug: ``/start`` replies with the wrong text — it does NOT contain "welcome", so the suite
case "Case: /start greets the user" (which Expects "welcome") FAILs. Everything else is
identical to the good bot. This proves the hermetic harness catches a real behavioral bug
(wrong outbound reply) and verdicts FAIL with a finding, mirroring the Phase-2 2-fixture DoD.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

API_BASE = os.environ.get("TG_API_BASE", "https://api.telegram.org").rstrip("/")
TOKEN = os.environ.get("BOT_TOKEN", "test-token")


def _api(method: str, payload: dict) -> dict:
    url = f"{API_BASE}/bot{TOKEN}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 — hermetic localhost fake
        return json.loads(resp.read().decode("utf-8"))


def _reply(chat_id: int, text: str) -> None:
    _api("sendMessage", {"chat_id": chat_id, "text": text})


def _handle(message: dict) -> None:
    chat_id = message.get("chat", {}).get("id")
    text = (message.get("text") or "").strip()
    if chat_id is None:
        return
    if text.startswith("/start"):
        # BUG: the greeting is missing the "welcome" the suite expects — a regression where the
        # /start handler was rewired to a generic acknowledgement.
        _reply(chat_id, "OK. Send /help for the list of commands.")
    elif text.startswith("/help"):
        _reply(chat_id, "Available commands: /start, /help, /echo <text>")
    elif text.startswith("/echo"):
        _reply(chat_id, text[len("/echo"):].strip() or "(nothing to echo)")


def main() -> int:
    offset = 0
    deadline = time.monotonic() + float(os.environ.get("BOT_MAX_RUNTIME_S", "120"))
    while time.monotonic() < deadline:
        try:
            resp = _api("getUpdates", {"offset": offset, "timeout": 1})
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(0.1)
            continue
        for update in resp.get("result", []):
            offset = max(offset, int(update["update_id"]) + 1)
            if "message" in update:
                _handle(update["message"])
        time.sleep(0.05)
    return 0


if __name__ == "__main__":
    sys.exit(main())
