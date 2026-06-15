"""reviewlib.dashboard — a web dashboard for review-cli runs.

`review dashboard [--host H] [--port N] [--no-open]` starts a stdlib HTTP server (bound to
127.0.0.1 by default; ``--host 0.0.0.0`` exposes it over Tailscale) serving a vanilla-JS
single-page app that browses review-cli's real on-disk log artifacts (per-call streamed
logs + brainstorm discussion logs in ``log_dir()``) and the overseer's annotations
(feedback / conscious flag / PR+ticket links) persisted in
``~/.config/review-cli/dashboard.json``. A ``/events`` Server-Sent Events stream pushes
live review activity to the page so it updates without a manual refresh.

Public surface:
  * ``run_dashboard`` — the blocking CLI entry (used by ``reviewlib.cli``);
  * ``make_server``   — build the server without serving (tests);
  * ``parser``/``store`` submodules — the read (logs) and write (annotations) layers.
"""
from __future__ import annotations

from .server import make_server, run_dashboard

__all__ = ["run_dashboard", "make_server"]
