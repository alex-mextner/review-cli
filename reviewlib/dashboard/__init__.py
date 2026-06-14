"""reviewlib.dashboard — a local-only web dashboard for review-cli runs.

`review dashboard [--port N] [--no-open]` starts a stdlib HTTP server (127.0.0.1 only)
serving a vanilla-JS single-page app that browses review-cli's real on-disk log
artifacts (per-call streamed logs + brainstorm discussion logs in ``log_dir()``) and
the overseer's annotations (feedback / conscious flag / PR+ticket links) persisted in
``~/.config/review-cli/dashboard.json``.

Public surface:
  * ``run_dashboard`` — the blocking CLI entry (used by ``reviewlib.cli``);
  * ``make_server``   — build the server without serving (tests);
  * ``parser``/``store`` submodules — the read (logs) and write (annotations) layers.
"""
from __future__ import annotations

from .server import make_server, run_dashboard

__all__ = ["run_dashboard", "make_server"]
