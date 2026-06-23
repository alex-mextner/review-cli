#!/usr/bin/env python3
"""A tiny, dependency-free static dev server for the qa web Tier-1 harness fixture.

It serves the sibling ``site/`` directory over stdlib ``http.server`` on the port given by the
``PORT`` env var (default 8080). No flask/vite/npm — pure stdlib — so the DoD runs in normal CI
with zero install. The web harness boots this exactly as it boots a real ``npm run dev`` (its
own process group, ``TG``-style env passthrough), health-gates it reachable at ``base_url``, then
drives it with a headless browser.

The GOOD fixture's index says "Welcome ..."; the buggy sibling (``web-buggy/site/index.html``)
says the WRONG thing so the harness verdicts FAIL.
"""
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

    def log_message(self, *_a) -> None:  # silence the per-request stderr noise
        return


def main() -> int:
    with socketserver.TCPServer(("127.0.0.1", PORT), _Handler) as httpd:
        httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
