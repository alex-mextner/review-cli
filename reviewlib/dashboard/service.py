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

import os
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
    a different checkout's code (the live-symlink trap).

    Every real caller of this function (``_review_argv0`` via ``run``/``start``/``enable``) is
    ITSELF an active ``review`` invocation, so ``$REVIEW_CLI_ACTIVE`` is already set in this
    process's environment. This probe is a full ``review --reviewlib-dir`` re-invocation, so
    without clearing that var here too it would ALWAYS be refused by the child's own
    ``_reject_if_reentrant`` — making this probe permanently return ``False`` in production and
    silently defeating the whole point of the installed-console-script branch above: `enable`
    would always pin a venv ``sys.executable`` into the persisted autostart unit instead of the
    stable installed script (review-cli#180 review finding, GLM). Same fix as ``_serve_argv``'s
    ``_env_clear_prefix()``, applied at this second, less-obvious call site."""
    from ..cli import REVIEW_CLI_ACTIVE_ENV

    env = {k: v for k, v in os.environ.items() if k != REVIEW_CLI_ACTIVE_ENV}
    try:
        out = subprocess.run(
            [review_path, "--reviewlib-dir"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
    except Exception:
        return False
    if out.returncode != 0:
        return False
    reported = out.stdout.strip()
    return bool(reported) and Path(reported).resolve() == Path(_our_reviewlib_dir())


def _env_clear_prefix() -> list[str]:
    """Argv prefix that strips ``$REVIEW_CLI_ACTIVE`` before exec'ing the real command.

    ``review dashboard run``/``start`` are themselves full ``review`` invocations, so by the
    time they reach here ``main()`` has already set ``$REVIEW_CLI_ACTIVE=1`` for THIS dispatch
    (the review-cli#180 reentrancy guard). ``agenttools_service.ServiceManager`` spawns the
    foreground/detached child via plain ``subprocess.call``/``Popen`` with no ``env=``
    override, so the child inherits that var — and since the child is itself a fresh ``review``
    invocation (the hidden ``__serve`` entry), its own ``main()`` would immediately trip
    ``_reject_if_reentrant`` on startup. That's a false positive: this is a deliberate,
    sanctioned top-level re-invocation, not the backend self-reinvocation the guard exists to
    catch — but without this prefix the managed dashboard/spec-web server could never actually
    launch via ``run``/``start`` (review-cli#180 review finding, chatgpt-codex-connector, PR
    #279).

    Deliberately HARDCODED to ``/usr/bin/env``, NOT ``shutil.which("env")``: a PATH lookup is
    caller-controlled — an attacker-poisoned PATH with a malicious ``env`` ahead of the real one
    would run BEFORE the trusted ``review`` binary, and ``enable`` persists this argv into a
    launchd/systemd autostart unit, turning a transient PATH poisoning into a boot-persistent
    one (review-cli#180 review finding, codex P1/P2). ``/usr/bin/env`` is guaranteed present by
    the same de-facto convention the ``#!/usr/bin/env`` shebang relies on universally on macOS
    and every mainstream Linux distro (POSIX standardizes the ``env`` utility, not literally
    this path — an exotic/minimal target without it would fail loudly at spawn, same as any
    other missing argv0). References ``cli.REVIEW_CLI_ACTIVE_ENV`` rather than a bare string
    literal so a rename of that constant can't silently decouple this prefix from the guard it
    exists to bypass (review-cli#180 review finding, GLM/GLM-cc).
    """
    from ..cli import REVIEW_CLI_ACTIVE_ENV

    return ["/usr/bin/env", "-u", REVIEW_CLI_ACTIVE_ENV]


def _serve_argv(*, port: int, host: str) -> list[str]:
    """The FOREGROUND server command the service runs (``run``) or detaches (``start``).

    Targets the hidden ``dashboard __serve`` entry (which calls ``run_dashboard`` directly), so
    neither ``run`` nor a detached ``start`` re-enters the service dispatcher. ``--no-open`` is
    always passed: a background/login daemon must never try to pop a browser. Prefixed with
    ``_env_clear_prefix()`` so the child never inherits a poisoned ``$REVIEW_CLI_ACTIVE`` from
    whatever ``review`` invocation is launching it (see that function's docstring).
    """
    return (
        _env_clear_prefix()
        + _review_argv0()
        + [
            "dashboard",
            "__serve",
            "--host",
            host,
            "--port",
            str(port),
            "--no-open",
        ]
    )


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
