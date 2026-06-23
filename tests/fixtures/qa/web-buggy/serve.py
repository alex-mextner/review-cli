#!/usr/bin/env python3
"""The BUGGY variant's static dev server (see ``web-good/serve.py``). Identical server; the bug
is in ``site/index.html`` (it does NOT say "Welcome"), so the suite's "home page greets" case
FAILs and the harness verdicts FAIL with a finding — proving the web Tier-1 harness catches a
real behavioral bug (wrong rendered text), mirroring the bot-good/bot-buggy DoD."""
from __future__ import annotations

import http.server
import os
import socketserver
from pathlib import Path

PORT = int(os.environ.get("PORT", "8080"))
SITE = Path(__file__).resolve().parent / "site"


class _Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SITE), **kwargs)

    def log_message(self, *_a) -> None:
        return


def main() -> int:
    with socketserver.TCPServer(("127.0.0.1", PORT), _Handler) as httpd:
        httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
