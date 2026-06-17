"""The dashboard as a MANAGED SERVICE — `review dashboard run|start|status|stop|enable|disable`.

These tests assert the WIRING (reviewlib.cli + reviewlib.dashboard.service), not the shared
``agenttools_service`` lib's own lifecycle (that lib has its own suite). The contract we own:

  * a BARE ``review dashboard`` prints HELP and launches NOTHING;
  * the lifecycle subcommands are wired (run/start/status/stop/enable/disable visible);
  * the FOREGROUND server argv targets the hidden ``__serve`` entry (no service-dispatch
    recursion / fork-bomb) with an ABSOLUTE argv[0];
  * ``--reviewlib-dir`` prints the running package dir (the live-symlink-trap probe);
  * only the blocking foreground server bypasses the backstop, not the fast lifecycle actions —
    INCLUDING when global options precede the action (``dashboard --port N run``).

Self-running (no pytest): the repo's CI runs ``python3 tests/test_dashboard_service.py`` from
smoke.sh, matching every other test file here, so this file uses bare ``assert`` + a tiny
discover-and-run ``__main__`` block instead of pytest fixtures.
"""
from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

from reviewlib import cli
from reviewlib.dashboard import service as svc

# The `Service` DESCRIPTOR tests below build a real `agenttools_service.Service`, so they only
# run when the shared service lib is installed (the `[dashboard]` extra). CI installs the core
# deps WITHOUT it on purpose (it isn't on PyPI yet), exercising the lib-ABSENT error path; when
# absent, the descriptor-shape tests SKIP loudly instead of erroring with a raw ImportError —
# the WIRING tests (which never touch the lib) still run and cover our own glue.
try:
    import agenttools_service as _agenttools_service  # noqa: F401

    _HAS_SERVICE_LIB = True
except ImportError:
    _HAS_SERVICE_LIB = False


# --- bare invocation prints HELP, launches nothing ---------------------------------------
def test_bare_dashboard_prints_help_and_does_not_launch():
    # If anything tried to start the server, run_dashboard would be called — fail loudly.
    import reviewlib.dashboard as dash

    def _boom(*a, **k):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError("bare `review dashboard` must NOT launch the server")

    with mock.patch.object(dash, "run_dashboard", _boom):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli._dashboard_subcommand([])
    out = buf.getvalue()
    assert rc == 0, rc
    # The help advertises every lifecycle action.
    for action in ("run", "start", "stop", "status", "enable", "disable"):
        assert action in out, (action, out)


@contextlib.contextmanager
def _force_lib_absent():
    """Make `import agenttools_service` raise ImportError, so a test can exercise the lib-ABSENT
    branch of `_dashboard_subcommand` regardless of whether the optional lib is actually
    installed on this host (the CI failure was on this path). Poisoning `sys.modules` with a
    `None` entry is the standard idiom — Python treats a None module entry as 'not importable'
    and raises ImportError, with no builtins monkey-patching."""
    with mock.patch.dict(sys.modules, {"agenttools_service": None}):
        yield


def test_dashboard_help_is_exit0_when_service_lib_is_absent():
    """REGRESSION (the CI failure): with the optional `agenttools_service` lib ABSENT, a bare
    `review dashboard` AND a help-only `review dashboard --help`/`-h` print help and launch
    nothing (return 0) — NOT the missing-lib error (exit 4). A `--help`/`-h` is not a lifecycle
    action. Forces the lib-absent import path so this holds even where the lib IS installed."""
    import reviewlib.dashboard as dash

    def _boom(*a, **k):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError("dashboard help must NOT launch the server")

    with _force_lib_absent(), mock.patch.object(dash, "run_dashboard", _boom):
        for argv in ([], ["--help"], ["-h"]):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli._dashboard_subcommand(argv)
            out = buf.getvalue()
            assert rc == 0, (argv, rc)
            for action in ("run", "start", "stop", "status", "enable", "disable"):
                assert action in out, (argv, action, out)


def test_dashboard_lifecycle_action_exit4_when_service_lib_is_absent():
    """The other half: a GENUINE lifecycle action (status/start/…) with the lib absent emits the
    actionable missing-lib error (exit 4), no traceback — structured-exit-codes."""
    import io as _io

    with _force_lib_absent():
        buf_err = _io.StringIO()
        old = sys.stderr
        sys.stderr = buf_err
        try:
            rc = cli._dashboard_subcommand(["status"])
        finally:
            sys.stderr = old
    assert rc == 4, rc
    err = buf_err.getvalue()
    assert "agenttools_service" in err
    assert "Traceback (most recent call last)" not in err


def test_dashboard_explicit_help_flag_prints_help_and_launches_nothing():
    # `review dashboard --help` / `-h` (no lifecycle action) prints HELP and launches nothing —
    # exit 0, the SAME bare-HELP contract whether or not the shared service lib is installed.
    # A `--help`/`-h` is NOT itself a lifecycle action, so it must never hit the missing-lib
    # error (exit 4) on a lib-less host (the bug the smoke `dashboard --help | grep status`
    # caught). The lib-PRESENT path serves it via argparse, which print_help()s + exits 0 too.
    import reviewlib.dashboard as dash

    def _boom(*a, **k):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError("`review dashboard --help` must NOT launch the server")

    for flag in ("--help", "-h"):
        with mock.patch.object(dash, "run_dashboard", _boom):
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    rc = cli._dashboard_subcommand([flag])
            except SystemExit as exc:  # lib-present: argparse's own --help exits 0
                rc = exc.code or 0
            out = buf.getvalue()
        assert rc == 0, (flag, rc)
        for action in ("run", "start", "stop", "status", "enable", "disable"):
            assert action in out, (flag, action, out)


# --- the foreground server argv: hidden __serve, absolute argv[0], no browser -------------
def test_serve_argv_targets_hidden_serve_entry():
    argv = svc._serve_argv(port=7878, host="127.0.0.1")
    assert "dashboard" in argv and "__serve" in argv
    # __serve must come right after dashboard (it's the subcommand).
    assert argv[argv.index("dashboard") + 1] == "__serve"
    assert "--no-open" in argv  # a daemon must never pop a browser
    assert "--port" in argv and "7878" in argv
    assert "--host" in argv and "127.0.0.1" in argv


def test_serve_argv0_is_absolute():
    argv = svc._serve_argv(port=7878, host="127.0.0.1")
    # argv[0] is either an absolute `review` on PATH or this interpreter (absolute) -m reviewlib.
    assert Path(argv[0]).is_absolute(), argv[0]
    if Path(argv[0]).name != "review":
        # fallback form: <python> -m reviewlib
        assert argv[1:3] == ["-m", "reviewlib"], argv


def test_review_argv0_falls_back_when_path_review_is_a_different_checkout():
    # PATH has a `review`, but it reports a DIFFERENT reviewlib dir than ours -> must fall
    # back to `<python> -m reviewlib` (never launch the wrong checkout — the live-symlink trap).
    with mock.patch.object(svc.shutil, "which", lambda name: "/somewhere/bin/review"), \
            mock.patch.object(svc, "_review_on_path_is_us", lambda path: False):
        argv0 = svc._review_argv0()
    assert argv0 == [sys.executable, "-m", "reviewlib"], argv0


def test_review_argv0_uses_path_review_when_it_is_us():
    with mock.patch.object(svc.shutil, "which", lambda name: "/usr/local/bin/review"), \
            mock.patch.object(svc, "_review_on_path_is_us", lambda path: True):
        argv0 = svc._review_argv0()
    assert argv0 == ["/usr/local/bin/review"], argv0


# A test raises this to signal "not applicable on this host" (vs PASS / FAIL); the runner
# below reports it as SKIP and does NOT count it as a failure. Mirrors smoke.sh's loud-skip of
# the visual suite when ImageMagick/Pillow are absent — a missing OPTIONAL dep skips, never fails.
# Subclasses unittest.SkipTest so a DIRECT `pytest tests/test_dashboard_service.py` run (not via
# smoke.py's standalone subprocess runner) also reports these as SKIP rather than a spurious
# ERROR — pytest treats SkipTest natively, and the `except _Skip` runner below still catches it.
class _Skip(unittest.SkipTest):
    pass


# --- the Service descriptor --------------------------------------------------------------
def test_dashboard_service_descriptor_shape():
    if not _HAS_SERVICE_LIB:
        raise _Skip("agenttools_service not installed (the [dashboard] extra)")
    s = svc.dashboard_service()
    assert s.name == "dashboard"
    assert s.tool == "review"
    assert s.port == svc.DEFAULT_DASHBOARD_PORT
    assert s.host == svc.DEFAULT_DASHBOARD_HOST
    # the argv the service runs is the hidden foreground server, not a re-entrant action.
    assert "__serve" in s.argv


def test_dashboard_service_honors_explicit_port_and_host():
    if not _HAS_SERVICE_LIB:
        raise _Skip("agenttools_service not installed (the [dashboard] extra)")
    s = svc.dashboard_service(port=9999, host="0.0.0.0")
    assert s.port == 9999
    assert s.host == "0.0.0.0"
    assert "9999" in s.argv and "0.0.0.0" in s.argv


# --- the --reviewlib-dir probe flag ------------------------------------------------------
def test_reviewlib_dir_flag_prints_running_package_dir():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(["--reviewlib-dir"])
    assert rc == 0, rc
    out = buf.getvalue().strip()
    import reviewlib

    assert Path(out) == Path(reviewlib.__file__).resolve().parent, out


# --- __serve is routed to the blocking server, not the service dispatcher ----------------
def test_serve_subcommand_routes_to_run_dashboard():
    import reviewlib.dashboard as dash

    captured: dict[str, object] = {}

    def _fake_run(port=None, *, host="127.0.0.1", open_browser=True, verbose=False):  # noqa: ANN001
        captured.update(port=port, host=host, open_browser=open_browser)
        return 0

    with mock.patch.object(dash, "run_dashboard", _fake_run):
        rc = cli._dashboard_subcommand(
            ["__serve", "--port", "7878", "--host", "127.0.0.1", "--no-open"]
        )
    assert rc == 0
    assert captured == {"port": 7878, "host": "127.0.0.1", "open_browser": False}, captured


# --- the action resolver: the action is the first NON-OPTION token -----------------------
def test_dashboard_action_resolves_action_after_global_options():
    # The managed parser accepts `--host`/`--port` BEFORE the action; the resolver must skip
    # them (and their values) and still find the action — otherwise the backstop classifier
    # below misfires.
    assert cli._dashboard_action(["run"]) == "run"
    assert cli._dashboard_action(["--port", "7878", "run"]) == "run"
    assert cli._dashboard_action(["--host", "0.0.0.0", "--port", "9999", "start"]) == "start"
    assert cli._dashboard_action(["--port=7878", "__serve"]) == "__serve"
    assert cli._dashboard_action([]) is None
    assert cli._dashboard_action(["--port", "7878"]) is None  # options only, no action


# --- backstop bypass semantics -----------------------------------------------------------
def test_only_foreground_server_bypasses_backstop():
    # The blocking server (run / __serve) is persistent and must bypass the backstop.
    assert cli._is_persistent_server_invocation(["dashboard", "run"]) is True
    assert cli._is_persistent_server_invocation(["dashboard", "__serve", "--port", "0"]) is True
    # …including when the global options come BEFORE the action (the P1 bug: argv[1] is then
    # `--port`, not `run`, so a naive `argv[1] in (...)` check would mis-wrap the server).
    assert cli._is_persistent_server_invocation(["dashboard", "--port", "7878", "run"]) is True
    assert (
        cli._is_persistent_server_invocation(
            ["dashboard", "--host", "0.0.0.0", "--port", "7878", "__serve"]
        )
        is True
    )
    # The bare HELP and the fast lifecycle actions are NOT persistent — they go through the
    # normal backstop-wrapped path (they return immediately).
    assert cli._is_persistent_server_invocation(["dashboard"]) is False
    for action in ("start", "status", "stop", "enable", "disable"):
        assert cli._is_persistent_server_invocation(["dashboard", action]) is False, action
        # …and still NOT persistent with options before a non-server action.
        assert (
            cli._is_persistent_server_invocation(["dashboard", "--port", "7878", action]) is False
        ), action


if __name__ == "__main__":
    failures = 0
    for _name, _fn in list(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"PASS {_name}")
            except _Skip as exc:
                print(f"SKIP {_name}: {exc}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {_name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                import traceback

                failures += 1
                print(f"ERROR {_name}: {type(exc).__name__}: {exc}")
                traceback.print_exc()
    sys.exit(1 if failures else 0)
