"""The dashboard as a MANAGED SERVICE — run/start/status/stop/enable/disable.

Reached at runtime via ``review dashboard <action>`` (``reviewlib.cli._dashboard_subcommand``
wires the argparse tree here). The lifecycle machinery itself is NOT hand-rolled: it comes
from the shared ``agenttools_service`` lib (the one reusable service-manager the ecosystem
shares between the review dashboard, ``config-web``, ``tg-ctl`` and future daemons), so every
long-running server in agent-tools gets identical lifecycle subcommands instead of each
re-implementing pidfiles / launchd plists / systemd units slightly differently.

What this module owns (the review-cli-specific glue, nothing more):

  * ``dashboard_service(...)`` — build the :class:`agenttools_service.Service` descriptor for
    the dashboard: its foreground argv, default port/host, and (via the lib) where its pidfile
    / logfile / autostart unit live under ``$XDG_STATE_HOME`` / ``$XDG_CACHE_HOME``.
  * ``_serve_argv(...)`` — the argv the service runs in the FOREGROUND. It points at the HIDDEN
    ``review dashboard __serve`` entry (``run_dashboard`` directly), NOT back at this dispatcher
    — otherwise ``run``/``start`` would re-enter the service layer and fork-bomb. ``argv[0]`` is
    resolved to an ABSOLUTE path (launchd/systemd do not honor the caller's ``PATH``).

Invariants:
  * A bare ``review dashboard`` (no action) prints HELP and launches NOTHING — enforced by the
    caller via ``dispatch(on_no_subcommand=...)``.
  * The managed instance binds a STABLE default port (so ``status`` reports a meaningful URL and
    a re-``start`` lands on the same address), unlike the ad-hoc ``review dashboard run`` default
    of an ephemeral port. ``--port 0`` is still honored for an ephemeral bind in the foreground.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenttools_service import Service

# The dashboard's stable managed-service port. Ad-hoc `review dashboard run` (no service) keeps
# its historical ephemeral-port default; the MANAGED service needs a fixed port so `status`'s
# url is stable and a later `start`/autostart lands on the same address. 7878 mirrors the
# agenttools_service README example and is well outside the privileged / common-dev range.
DEFAULT_DASHBOARD_PORT = 7878

# Loopback by default — the logs persist prompts/diffs that may carry secrets, so the managed
# dashboard is loopback-only unless the operator passes --host 0.0.0.0 (Tailscale), mirroring
# `review dashboard run`'s default.
DEFAULT_DASHBOARD_HOST = "127.0.0.1"


def _review_argv0() -> list[str]:
    """The absolute command prefix that invokes review-cli, for the foreground/autostart argv.

    launchd / systemd do NOT resolve the caller's ``PATH``, so a bare ``"review"`` in the unit
    would fail to launch at login — the prefix must be absolute. Two cases:

    * **Installed** — the ``review`` console script on PATH IS this same ``reviewlib``. Use the
      absolute script: it is install-managed and stable across venv churn (a pinned
      ``sys.executable`` could point at a venv that is later deleted / upgraded).
    * **Dev / worktree** — the ``review`` on PATH resolves to a DIFFERENT checkout than the one
      running now (the classic live-symlink trap: the symlink points at the main checkout, not
      this worktree). Falling back to the PATH ``review`` would silently run the wrong code
      (e.g. one missing the hidden ``__serve`` entry). Use ``<this python> -m reviewlib`` so the
      managed service runs the SAME code that wired it.

    The "same reviewlib?" test imports ``reviewlib`` in the candidate ``review`` and compares its
    package dir to ours; any failure (no ``review``, probe errors) falls back to the running
    interpreter, which is always correct for the process doing the wiring.
    """
    found = shutil.which("review")
    if found and _review_on_path_is_us(found):
        return [found]
    return [sys.executable, "-m", "reviewlib"]


def _our_reviewlib_dir() -> str:
    """The directory of the ``reviewlib`` package currently executing (resolved, no symlinks)."""
    import reviewlib

    return str(Path(reviewlib.__file__).resolve().parent)


def _review_on_path_is_us(review_path: str) -> bool:
    """True iff the ``review`` at ``review_path`` imports the SAME ``reviewlib`` we are running.

    Invokes ``review --reviewlib-dir`` on the PATH candidate and compares its resolved ``reviewlib``
    package dir to ours. Best-effort: any probe error (no such flag on an old build, a crash, a
    timeout) ⇒ assume NOT us, so the caller falls back to the running interpreter and never launches
    a different checkout's code (the live-symlink trap)."""
    try:
        out = subprocess.run(
            [review_path, "--reviewlib-dir"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return False
    if out.returncode != 0:
        return False
    reported = out.stdout.strip()
    return bool(reported) and Path(reported).resolve() == Path(_our_reviewlib_dir())


def _serve_argv(*, port: int, host: str) -> list[str]:
    """The FOREGROUND server command the service runs (``run``) or detaches (``start``).

    Targets the hidden ``dashboard __serve`` entry (which calls ``run_dashboard`` directly), so
    neither ``run`` nor a detached ``start`` re-enters the service dispatcher. ``--no-open`` is
    always passed: a background/login daemon must never try to pop a browser.
    """
    return _review_argv0() + [
        "dashboard",
        "__serve",
        "--host",
        host,
        "--port",
        str(port),
        "--no-open",
    ]


def dashboard_service(
    *,
    port: int | None = None,
    host: str = DEFAULT_DASHBOARD_HOST,
) -> "Service":
    """Build the :class:`agenttools_service.Service` descriptor for the review dashboard.

    Imported lazily by the caller so the dashboard's service stack (and ``agenttools_service``)
    never loads on the hot ``review`` path. ``port=None`` ⇒ the stable default; pass an explicit
    port to pin a different one (the autostart unit is regenerated to match on ``enable``).
    """
    from agenttools_service import Service

    bound_port = DEFAULT_DASHBOARD_PORT if port is None else port
    return Service(
        name="dashboard",
        argv=_serve_argv(port=bound_port, host=host),
        port=bound_port,
        host=host,
        tool="review",
        description="review-cli dashboard — web view over review-cli runs",
    )
