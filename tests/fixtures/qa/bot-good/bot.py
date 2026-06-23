#!/usr/bin/env python3
"""A tiny, dependency-free Telegram bot fixture for the qa bot Tier-1 hermetic harness.

It is a minimal long-poller: it reads ``TG_API_BASE`` (the hermetic fake the harness points
it at), polls ``getUpdates``, and replies to a couple of commands via ``sendMessage``. No
telegraf/grammy/aiogram — pure stdlib — so the DoD runs in normal CI with zero install.

THE GOOD VARIANT: ``/start`` -> a welcome message containing "welcome"; ``/help`` -> a help
message containing "commands". The buggy sibling (``bot-buggy/bot.py``) replies with the
WRONG text on ``/start`` so the harness verdicts FAIL.

It honors ``TG_API_BASE`` exactly as a real bot's poller does (the load-bearing contract the
hermetic harness depends on); a bot that hardcoded api.telegram.org would fail the harness's
positive capability probe, which is the point of that probe.
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
    """POST a Bot-API call to ``<API_BASE>/bot<TOKEN>/<method>`` and return the decoded result.
    This is the one place the bot talks to Telegram — it reads API_BASE, so the hermetic fake
    receives every call (the harness's capture side)."""
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
        _reply(chat_id, "Welcome to the QA bot! Send /help for the list of commands.")
    elif text.startswith("/help"):
        _reply(chat_id, "Available commands: /start, /help, /echo <text>")
    elif text.startswith("/echo"):
        _reply(chat_id, text[len("/echo"):].strip() or "(nothing to echo)")
    # Any other message: stay silent (no reply) — a real bot ignores chatter.


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
