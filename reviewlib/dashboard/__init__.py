"""reviewlib.dashboard — a web dashboard for review-cli runs.

The dashboard is a MANAGED SERVICE: ``review dashboard [run|start|status|stop|enable|disable]``
(bare ``review dashboard`` prints HELP and launches nothing). The lifecycle subcommands come
from the shared ``agenttools_service`` lib (see ``reviewlib.dashboard.service``), so it gets
the same run/start/status/stop/enable/disable + launchd/systemd autostart every long-running
agent-tools server shares. ``run`` (and the hidden ``__serve`` it dispatches to) starts the
actual server: a stdlib HTTP server (bound to 127.0.0.1 by default; ``--host 0.0.0.0`` exposes
it over Tailscale) serving a vanilla-JS single-page app that browses review-cli's real on-disk
log artifacts (per-call streamed logs + brainstorm discussion logs in ``log_dir()``) and the
overseer's annotations (feedback / conscious flag / PR+ticket links) persisted in
``~/.config/review-cli/dashboard.json``. A ``/events`` Server-Sent Events stream pushes live
review activity to the page so it updates without a manual refresh.

Public surface:
  * ``run_dashboard`` — the blocking server entry (used by ``reviewlib.cli`` for ``__serve``);
  * ``make_server``   — build the server without serving (tests);
  * ``service`` submodule — the managed-service descriptor (``dashboard_service``);
  * ``parser``/``store`` submodules — the read (logs) and write (annotations) layers.
"""
from __future__ import annotations

from .server import make_server, run_dashboard

__all__ = ["run_dashboard", "make_server"]
