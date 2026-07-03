"""The spec-web DAEMON as a MANAGED SERVICE — run/start/status/stop/enable/disable.

Reached at runtime via ``review spec-web <action>`` (``reviewlib.cli._spec_web`` wires the
lifecycle here). Like the review dashboard, the lifecycle machinery is NOT hand-rolled: it
comes from the shared ``agenttools_service`` lib (the one reusable service-manager the
ecosystem shares — review dashboard, config-web, tg-ctl, and now spec-web), so every
long-running server gets identical lifecycle subcommands + pidfile + launchd/systemd autostart
instead of a per-tool copy. Only the spec-web-UNIQUE surface (register a spec, the spec
registry, the navigator, ``/spec/<name>`` path routing, SSE live-reload) lives in this package.

What this module owns (the review-cli-specific glue, nothing more):

  * ``spec_web_service(...)`` — build the :class:`agenttools_service.Service` descriptor for the
    spec-web daemon: its foreground argv (the hidden ``review spec-web __serve`` entry), default
    port/host, and (via the lib) where its pidfile / logfile / autostart unit live under
    ``$XDG_STATE_HOME`` / ``$XDG_CACHE_HOME``.
  * ``_serve_argv(...)`` — the argv the service runs in the FOREGROUND. It targets the hidden
    ``review spec-web __serve`` entry (which calls ``run_daemon`` directly), NOT this dispatcher
    — otherwise ``run``/``start`` would re-enter the service layer and fork-bomb. ``argv[0]`` is
    resolved to an ABSOLUTE path via the dashboard's shared resolver (launchd/systemd don't
    honor the caller's ``PATH``).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenttools_service import Service

# The daemon's STABLE managed-service port. A fixed port (not the legacy ephemeral per-spec
# bind) so ``status``'s URL is stable and a re-``start``/autostart lands on the same address.
# Clear of 7878 (dashboard) and 7911/7912 (the legacy per-spec spec-web servers). `server.py`
# imports these so there is ONE source of truth.
DEFAULT_SPECWEB_PORT = 7920
# Bind all interfaces by default: the daemon exists for phone/Tailscale spec review (reads are
# open, writes are origin-guarded), matching the legacy per-spec servers that bound ``*``. Pass
# ``--host 127.0.0.1`` to keep it loopback-only.
DEFAULT_SPECWEB_HOST = "0.0.0.0"


def _serve_argv(*, port: int, host: str, agent: str | None = None) -> list[str]:
    """The FOREGROUND daemon command the service runs (``run``) or detaches (``start``).

    Targets the hidden ``spec-web __serve`` entry (which calls ``run_daemon`` directly), so
    neither ``run`` nor a detached ``start`` re-enters the service dispatcher. Reuses the
    dashboard's ``_review_argv0`` so the argv[0] resolution (installed console script vs
    ``<this python> -m reviewlib`` in a dev worktree — the live-symlink trap) is shared, not
    re-implemented. ``agent`` (the daemon's default submit-delivery target) is baked into the
    argv so a managed restart / OS autostart keeps delivering to the same session.
    """
    from ..dashboard.service import _review_argv0

    argv = _review_argv0() + [
        "spec-web",
        "__serve",
        "--host",
        host,
        "--port",
        str(port),
    ]
    if agent:
        argv += ["--agent", agent]
    return argv


def spec_web_service(
    *,
    port: int | None = None,
    host: str = DEFAULT_SPECWEB_HOST,
    agent: str | None = None,
) -> "Service":
    """Build the :class:`agenttools_service.Service` descriptor for the spec-web daemon.

    Imported lazily by the caller so the service stack (and ``agenttools_service``) never loads
    on the hot ``review`` path. ``port=None`` ⇒ the stable default; pass an explicit port to pin
    a different one (the autostart unit is regenerated to match on ``enable``). ``agent`` is
    only needed by the actions that LAUNCH the daemon (start/run/enable — the CLI requires it
    there); status/stop resolve the service by name/pidfile and pass ``None``.
    """
    from agenttools_service import Service

    bound_port = DEFAULT_SPECWEB_PORT if port is None else port
    return Service(
        name="spec-web",
        argv=_serve_argv(port=bound_port, host=host, agent=agent),
        port=bound_port,
        host=host,
        tool="review",
        description="review-cli spec-web daemon — multi-spec markdown reviewer",
    )
