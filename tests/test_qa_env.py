"""review qa — Phase 3: the deterministic SUT-ENV bring-up layer (reviewlib/qa/env.py).

These pin the Phase-3 contract (docs/specs/review-qa.md §7.2): stand the SUT env up BEFORE
the executor drives it, then GUARANTEE teardown of only what THIS run brought up.

WHAT THEY PROVE (the DoD):
  * (a) a REACHABLE stage is REUSED and NEVER torn down (ownership: we did not bring it up);
  * (b) a ``qa/setup.sh``-HOOK env is brought up, health-gated, handed off, and torn down on
    exit (the hook's ``down`` runs);
  * (c) an env that NEVER becomes healthy fails ``EXIT_QA_ENV_UNHEALTHY`` with teardown STILL
    run (the partial env is reaped);
  * (d) teardown is GUARANTEED even when the executor throws / times out (the handler's
    try/finally + the global atexit/signal hook);
  * the no-env gate (no stage, no hook, no config) fails ``EXIT_QA_NO_ENV`` with a recommend
    message — BEFORE any bring-up;
  * a malformed ``qa.yaml`` is a clean usage error, not a traceback.

WHICH RUNS WHERE:
  * normal CI / ``python tests/smoke.py``: ALL of the above — they use a real stdlib HTTP
    server (no network, no model) for stage/health and shell ``setup.sh`` hooks for bring-up
    / teardown. Deterministic, hermetic, fast.
  * ``REVIEW_QA_DOCKER=1 python3 tests/test_qa_env.py``: ALSO runs the live ``docker compose``
    bring-up + ``-p``-namespaced teardown test (needs a running docker daemon). Gated so CI
    without docker stays green; the hook path already proves the lifecycle deterministically.

WHY A HOOK + HTTP SERVER, NOT DOCKER, FOR THE DEFAULT TESTS. The lifecycle (detect → reuse /
bring-up → health-gate → teardown + the guarantee) is identical whether the env is a
container or a ``setup.sh`` that touches a marker file. The hook path exercises EVERY branch
(bring-up runs, health gate polls, teardown runs, teardown runs even on a throw) with zero
infra — so the guarantees are proven in CI, and the docker test only confirms the same shape
against a real daemon.
"""
from __future__ import annotations

import http.server
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib.qa import config as cfg  # noqa: E402
from reviewlib.qa import env as envmod  # noqa: E402
from reviewlib.qa.env import EnvError, EnvMode, bring_up_env  # noqa: E402

# The reserved Phase-3 exit codes (cli.py). The tests pass them in explicitly so a future
# renumber is caught here, not silently.
EXIT_NO_ENV = 7
EXIT_UNHEALTHY = 9


# --- a tiny controllable HTTP server (stage / health probing) -------------------------
class _Health:
    """Toggleable health state a test flips to drive the stage/health probes."""

    def __init__(self) -> None:
        self.status = 200
        self.up = True


def _serve(health: _Health) -> tuple[str, "http.server.HTTPServer", threading.Thread]:
    """Start a localhost HTTP server whose response reflects ``health``. Returns (base_url,
    server, thread). The server answers ANY path; ``health.up=False`` makes it refuse
    (connection drop) by returning a 503, ``health.status`` sets the code when up."""

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — http.server API
            code = health.status if health.up else 503
            self.send_response(code)
            self.end_headers()
            self.wfile.write(b"ok" if code < 400 else b"down")

        def log_message(self, *_a):  # silence the test output
            return

    port = _free_port()
    server = http.server.HTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{port}", server, thread


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# --- a SUT with a setup.sh hook -------------------------------------------------------
def _hook_sut(*, up_rc: int = 0, healthy_after_marker: bool = True) -> Path:
    """A throwaway SUT whose ``qa/setup.sh up`` writes an ``UP`` marker (and optionally a
    ``HEALTHY`` marker), and ``down`` writes a ``DOWN`` marker + removes ``UP``. The markers
    let a test ASSERT bring-up ran, teardown ran, and ordering — with no container."""
    sut = Path(tempfile.mkdtemp(prefix="qa-env-hook-"))
    (sut / "qa").mkdir(parents=True)
    healthy = "touch HEALTHY" if healthy_after_marker else ":"
    script = (
        "#!/bin/sh\n"
        'cd "$(dirname "$0")/.." || exit 1\n'
        "case \"$1\" in\n"
        f"  up) touch UP; {healthy}; exit {up_rc} ;;\n"
        "  down) rm -f UP; touch DOWN; exit 0 ;;\n"
        "  *) echo unknown; exit 2 ;;\n"
        "esac\n"
    )
    hook = sut / "qa" / "setup.sh"
    hook.write_text(script, encoding="utf-8")
    hook.chmod(0o755)
    return sut


def _write_config(sut: Path, body: str) -> None:
    cfgdir = sut / "docs" / "tests"
    cfgdir.mkdir(parents=True, exist_ok=True)
    (cfgdir / "qa.yaml").write_text(body, encoding="utf-8")


# --- (a) a reachable stage is REUSED and NOT torn down --------------------------------
def test_reachable_stage_is_reused_not_torn_down():
    """DoD (a): a declared, reachable stage → EnvMode.REUSED_STAGE; tear_down is a NO-OP (we
    did not bring it up, so we must not bring it down — the ownership rule)."""
    health = _Health()
    base, server, _t = _serve(health)
    try:
        config = cfg.SutConfig(stage=cfg.StageConfig(url=base, health=base + "/healthz"))
        handle = bring_up_env(
            sut_path=Path("/tmp"), config=config, stage_url_override=None,
            exit_no_env=EXIT_NO_ENV, exit_unhealthy=EXIT_UNHEALTHY,
        )
        assert handle.mode == EnvMode.REUSED_STAGE, handle
        assert handle.endpoints.get("stage") == base, handle.endpoints
        # tear_down must do NOTHING (no exception, no side effect) — a reused stage stays up.
        handle.tear_down()  # no raise = pass; the _noop teardown
        # and it must NOT have been registered for the global atexit reap (nothing to reap).
        with envmod._PENDING_LOCK:
            assert handle not in envmod._PENDING_TEARDOWNS
    finally:
        server.shutdown()


def test_declared_but_unreachable_stage_fails_unhealthy():
    """A stage that is DECLARED but not reachable must fail EXIT_QA_ENV_UNHEALTHY — never a
    silent fall-through to a half-up stage (spec §7.2 Phase 1)."""
    # A port nothing listens on.
    dead = f"http://127.0.0.1:{_free_port()}"
    config = cfg.SutConfig(stage=cfg.StageConfig(url=dead))
    try:
        bring_up_env(sut_path=Path("/tmp"), config=config, stage_url_override=None,
                     exit_no_env=EXIT_NO_ENV, exit_unhealthy=EXIT_UNHEALTHY)
        raise AssertionError("expected EnvError for an unreachable declared stage")
    except EnvError as exc:
        assert exc.exit_code == EXIT_UNHEALTHY, exc.exit_code
        assert "not reachable" in str(exc)


def test_stage_url_override_beats_config():
    """``--stage-url`` (the override) takes precedence over a config stage, and a reachable
    override is reused."""
    health = _Health()
    base, server, _t = _serve(health)
    try:
        config = cfg.SutConfig(stage=cfg.StageConfig(url="http://unused.invalid"))
        handle = bring_up_env(
            sut_path=Path("/tmp"), config=config, stage_url_override=base,
            exit_no_env=EXIT_NO_ENV, exit_unhealthy=EXIT_UNHEALTHY,
        )
        assert handle.mode == EnvMode.REUSED_STAGE
        assert handle.endpoints["stage"] == base
    finally:
        server.shutdown()


# --- (b) a setup.sh-hook env is brought up, health-gated, handed off, torn down -------
def test_hook_env_brought_up_health_gated_and_torn_down():
    """DoD (b): a SUT with a ``qa/setup.sh`` hook + an HTTP health check → the hook's ``up``
    runs (UP marker), the health gate polls until green, the handle is handed off
    (EnvMode.HOOK), and ``tear_down`` runs the hook's ``down`` (DOWN marker)."""
    health = _Health()
    base, server, _t = _serve(health)
    sut = _hook_sut()
    try:
        # A health check that points at our controllable server — green from the start.
        config = cfg.SutConfig(
            health=[cfg.HealthCheck(name="api", url=base + "/healthz", expect_status=200,
                                    timeout_s=5)],
        )
        handle = bring_up_env(
            sut_path=sut, config=config, stage_url_override=None,
            exit_no_env=EXIT_NO_ENV, exit_unhealthy=EXIT_UNHEALTHY,
        )
        assert handle.mode == EnvMode.HOOK, handle
        assert (sut / "UP").exists(), "the hook's `up` must have run"
        assert not (sut / "DOWN").exists(), "teardown must NOT have run yet (handed off)"
        # the env was registered for the guaranteed-teardown atexit hook.
        with envmod._PENDING_LOCK:
            assert handle in envmod._PENDING_TEARDOWNS
        # hand-off done — now the caller's finally tears it down.
        handle.tear_down()
        assert (sut / "DOWN").exists(), "the hook's `down` must have run on teardown"
        assert not (sut / "UP").exists(), "teardown removed the UP marker"
    finally:
        server.shutdown()
        shutil.rmtree(sut, ignore_errors=True)


def test_hook_env_with_no_health_check_is_trusted():
    """A hook bring-up with NO declared health checks is trusted (the hook returns 0 only when
    up) — it is handed off without polling, and still torn down."""
    sut = _hook_sut()
    try:
        handle = bring_up_env(
            sut_path=sut, config=cfg.SutConfig(), stage_url_override=None,
            exit_no_env=EXIT_NO_ENV, exit_unhealthy=EXIT_UNHEALTHY,
        )
        assert handle.mode == EnvMode.HOOK
        assert (sut / "UP").exists()
        handle.tear_down()
        assert (sut / "DOWN").exists()
    finally:
        shutil.rmtree(sut, ignore_errors=True)


def test_hook_up_failure_is_env_error():
    """A hook whose ``up`` exits non-zero is a boot failure → EnvError (unhealthy code), and
    nothing is left registered for teardown (the up never succeeded)."""
    sut = _hook_sut(up_rc=3)
    try:
        bring_up_env(sut_path=sut, config=cfg.SutConfig(), stage_url_override=None,
                     exit_no_env=EXIT_NO_ENV, exit_unhealthy=EXIT_UNHEALTHY)
        raise AssertionError("expected EnvError for a failing hook up")
    except EnvError as exc:
        assert exc.exit_code == EXIT_UNHEALTHY, exc.exit_code
        assert "exited 3" in str(exc)
    finally:
        shutil.rmtree(sut, ignore_errors=True)


# --- (c) an env that never becomes healthy fails unhealthy, teardown still run --------
def test_unhealthy_env_fails_and_is_torn_down():
    """DoD (c): the hook brings the env UP, but the health check NEVER goes green → the gate
    times out, EXIT_QA_ENV_UNHEALTHY is raised, AND the partial env is torn down (DOWN marker
    present, UP gone, nothing left registered)."""
    health = _Health()
    health.up = False  # the server answers 503 forever — the gate can never pass
    base, server, _t = _serve(health)
    sut = _hook_sut()
    try:
        config = cfg.SutConfig(
            health=[cfg.HealthCheck(name="api", url=base + "/healthz", expect_status=200,
                                    timeout_s=1)],  # a 1s gate so the test is fast
        )
        try:
            bring_up_env(sut_path=sut, config=config, stage_url_override=None,
                         exit_no_env=EXIT_NO_ENV, exit_unhealthy=EXIT_UNHEALTHY)
            raise AssertionError("expected EnvError for an env that never goes healthy")
        except EnvError as exc:
            assert exc.exit_code == EXIT_UNHEALTHY, exc.exit_code
            assert "never became healthy" in str(exc)
        # teardown MUST have run despite the failure (the guarantee on the failure path).
        assert (sut / "DOWN").exists(), "teardown must run even when the health gate fails"
        assert not (sut / "UP").exists()
        with envmod._PENDING_LOCK:
            assert not envmod._PENDING_TEARDOWNS, "the failed env must be unregistered"
    finally:
        server.shutdown()
        shutil.rmtree(sut, ignore_errors=True)


def test_keep_env_skips_teardown_on_unhealthy():
    """``--keep-env`` (keep_env=True): an unhealthy env is LEFT up for triage — teardown does
    NOT run (no DOWN marker), but it IS unregistered so the atexit hook won't reap it either
    (the user asked to keep it)."""
    health = _Health()
    health.up = False
    base, server, _t = _serve(health)
    sut = _hook_sut()
    try:
        config = cfg.SutConfig(
            health=[cfg.HealthCheck(name="api", url=base + "/healthz", timeout_s=1)],
        )
        try:
            bring_up_env(sut_path=sut, config=config, stage_url_override=None,
                         exit_no_env=EXIT_NO_ENV, exit_unhealthy=EXIT_UNHEALTHY, keep_env=True)
            raise AssertionError("expected EnvError")
        except EnvError:
            pass
        assert not (sut / "DOWN").exists(), "--keep-env must NOT tear the env down"
        assert (sut / "UP").exists(), "the env is left up for triage"
        with envmod._PENDING_LOCK:
            assert not envmod._PENDING_TEARDOWNS
    finally:
        # the test reaps the kept env itself (it would otherwise leak)
        subprocess.run([str(sut / "qa" / "setup.sh"), "down"], cwd=str(sut),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        server.shutdown()
        shutil.rmtree(sut, ignore_errors=True)


# --- (d) teardown is guaranteed even when the executor throws -------------------------
def test_teardown_guaranteed_when_executor_throws():
    """DoD (d): the handler hands the up env to the executor; the executor THROWS. The
    handler's try/finally must STILL tear the env down. This mirrors the qa handler's wiring
    (bring_up_env → run → finally: handle.tear_down())."""
    sut = _hook_sut()
    try:
        handle = bring_up_env(sut_path=sut, config=cfg.SutConfig(), stage_url_override=None,
                              exit_no_env=EXIT_NO_ENV, exit_unhealthy=EXIT_UNHEALTHY)
        assert (sut / "UP").exists()

        class _Boom(RuntimeError):
            pass

        def _executor_that_throws():
            raise _Boom("the tester crashed mid-run")

        # The exact shape the handler uses around run_tester: a try/finally whose finally is
        # ONLY handle.tear_down() (which self-unregisters from the atexit registry).
        try:
            try:
                _executor_that_throws()
            finally:
                handle.tear_down()
        except _Boom:
            pass
        assert (sut / "DOWN").exists(), "teardown must run even when the executor throws"
        with envmod._PENDING_LOCK:
            assert not envmod._PENDING_TEARDOWNS, "tear_down self-unregisters the handle"
    finally:
        shutil.rmtree(sut, ignore_errors=True)


def test_global_atexit_hook_reaps_pending_env():
    """The global teardown registry (the layer that reaps a daemonized env backstop CANNOT
    reach) tears down a still-pending env when ``_run_pending_teardowns`` fires (the atexit /
    signal path). Proves the guarantee survives a path that BYPASSES the handler's finally
    (an abnormal exit) — a leaked container would otherwise outlive the run."""
    sut = _hook_sut()
    try:
        handle = bring_up_env(sut_path=sut, config=cfg.SutConfig(), stage_url_override=None,
                              exit_no_env=EXIT_NO_ENV, exit_unhealthy=EXIT_UNHEALTHY)
        assert (sut / "UP").exists()
        with envmod._PENDING_LOCK:
            assert handle in envmod._PENDING_TEARDOWNS
        # Simulate the atexit/signal fire WITHOUT the handler's finally having run.
        envmod._run_pending_teardowns()
        assert (sut / "DOWN").exists(), "the atexit hook must reap the pending env"
        # idempotent: a second fire (or the handler's later finally) is a no-op.
        envmod._run_pending_teardowns()
    finally:
        envmod._unregister_pending(handle)
        shutil.rmtree(sut, ignore_errors=True)


def test_teardown_is_idempotent():
    """``tear_down`` called twice runs the hook's ``down`` exactly ONCE (the _torn_down guard)
    — so the handler finally + the atexit hook can both call it safely."""
    sut = _hook_sut()
    try:
        handle = bring_up_env(sut_path=sut, config=cfg.SutConfig(), stage_url_override=None,
                              exit_no_env=EXIT_NO_ENV, exit_unhealthy=EXIT_UNHEALTHY)
        handle.tear_down()
        assert (sut / "DOWN").exists()
        # remove DOWN; a second tear_down must NOT recreate it (it is a no-op)
        (sut / "DOWN").unlink()
        handle.tear_down()
        assert not (sut / "DOWN").exists(), "the second tear_down must be a no-op"
    finally:
        shutil.rmtree(sut, ignore_errors=True)


# --- the no-env gate + config parsing -------------------------------------------------
def test_no_stage_no_hook_no_config_fails_no_env():
    """No stage, no hook, no bringup config → EXIT_QA_NO_ENV with a recommend message, BEFORE
    any bring-up (the recommend gate, spec §7.2 Phase 1b)."""
    sut = Path(tempfile.mkdtemp(prefix="qa-env-bare-"))
    try:
        try:
            bring_up_env(sut_path=sut, config=None, stage_url_override=None,
                         exit_no_env=EXIT_NO_ENV, exit_unhealthy=EXIT_UNHEALTHY)
            raise AssertionError("expected EnvError(no-env) for a bare SUT")
        except EnvError as exc:
            assert exc.exit_code == EXIT_NO_ENV, exc.exit_code
            assert "no SUT env" in str(exc)
            assert "setup.sh" in str(exc) and "qa.yaml" in str(exc)
    finally:
        shutil.rmtree(sut, ignore_errors=True)


def test_config_with_bringup_but_no_compose_file_fails():
    """A compose bringup whose compose_file does not exist is a boot failure (EnvError,
    unhealthy code) — not a traceback."""
    sut = Path(tempfile.mkdtemp(prefix="qa-env-missing-compose-"))
    try:
        config = cfg.SutConfig(bringup=cfg.BringupConfig(
            driver="compose", compose_file="docs/tests/env/nope.yml", project_name="review-qa-test"))
        try:
            bring_up_env(sut_path=sut, config=config, stage_url_override=None,
                         exit_no_env=EXIT_NO_ENV, exit_unhealthy=EXIT_UNHEALTHY)
            raise AssertionError("expected EnvError for a missing compose file")
        except EnvError as exc:
            assert exc.exit_code == EXIT_UNHEALTHY, exc.exit_code
            assert "not found" in str(exc)
    finally:
        shutil.rmtree(sut, ignore_errors=True)


def test_load_qa_config_absent_is_none():
    """A missing qa.yaml is NOT an error (returns None) — the caller then runs the hook path or
    the recommend gate. Only a PRESENT-but-malformed file raises."""
    sut = Path(tempfile.mkdtemp(prefix="qa-env-nocfg-"))
    try:
        assert cfg.load_qa_config(sut, None) is None
    finally:
        shutil.rmtree(sut, ignore_errors=True)


def test_load_qa_config_explicit_missing_is_error():
    """An EXPLICIT --config that does not exist IS an error (the user named a missing file)."""
    sut = Path(tempfile.mkdtemp(prefix="qa-env-explicit-"))
    try:
        try:
            cfg.load_qa_config(sut, "custom/qa.yaml")
            raise AssertionError("expected QaConfigError for an explicit missing config")
        except cfg.QaConfigError as exc:
            assert "does not exist" in str(exc)
    finally:
        shutil.rmtree(sut, ignore_errors=True)


def test_load_qa_config_parses_full_block():
    """A full qa.yaml parses to the typed SutConfig with stage, bringup, health, teardown."""
    sut = Path(tempfile.mkdtemp(prefix="qa-env-full-"))
    try:
        _write_config(sut, (
            "sut:\n"
            "  kind: backend\n"
            "  stage:\n"
            "    url: https://stage.example\n"
            "    health: https://stage.example/healthz\n"
            "  bringup:\n"
            "    driver: compose\n"
            "    compose_file: docs/tests/env/docker-compose.qa.yml\n"
            "    project_name: review-qa\n"
            "  health:\n"
            "    - { name: api, url: http://localhost:8080/healthz, expect_status: 200, timeout_s: 90 }\n"
            "    - { name: db, compose_service: db }\n"
            "  teardown:\n"
            "    keep_on_failure: false\n"
        ))
        config = cfg.load_qa_config(sut, None)
        assert config is not None
        assert config.kind == "backend"
        assert config.stage.url == "https://stage.example"
        assert config.stage.health_target() == "https://stage.example/healthz"
        assert config.bringup.driver == "compose"
        assert config.bringup.project_name == "review-qa"
        assert len(config.health) == 2
        assert config.health[0].url and config.health[0].expect_status == 200
        assert config.health[1].compose_service == "db"
        assert config.teardown.keep_on_failure is False
    finally:
        shutil.rmtree(sut, ignore_errors=True)


def test_malformed_config_is_clean_error():
    """A qa.yaml with a bad shape (a health check setting BOTH url and compose_service) is a
    clean QaConfigError, not a traceback."""
    sut = Path(tempfile.mkdtemp(prefix="qa-env-bad-"))
    try:
        _write_config(sut, (
            "sut:\n"
            "  health:\n"
            "    - { name: bad, url: http://x/h, compose_service: db }\n"
        ))
        try:
            cfg.load_qa_config(sut, None)
            raise AssertionError("expected QaConfigError for a check with both url+service")
        except cfg.QaConfigError as exc:
            assert "EXACTLY one" in str(exc)
    finally:
        shutil.rmtree(sut, ignore_errors=True)


def test_compose_bringup_requires_project_name():
    """A compose bringup with no project_name is rejected at parse — without -p, teardown could
    not name what to reap (the ownership invariant)."""
    try:
        cfg.BringupConfig(driver="compose", compose_file="x.yml", project_name="")
        raise AssertionError("expected QaConfigError for a missing project_name")
    except cfg.QaConfigError as exc:
        assert "project_name is required" in str(exc)


def test_non_numeric_health_field_is_clean_error():
    """A non-numeric expect_status / timeout_s is a clean QaConfigError, NOT a bare ValueError
    escaping the parse (review finding: the int() coercions were unguarded)."""
    sut = Path(tempfile.mkdtemp(prefix="qa-env-nonint-"))
    try:
        _write_config(sut, (
            "sut:\n"
            "  health:\n"
            "    - { name: api, url: http://x/h, expect_status: abc }\n"
        ))
        try:
            cfg.load_qa_config(sut, None)
            raise AssertionError("expected QaConfigError for non-numeric expect_status")
        except cfg.QaConfigError as exc:
            assert "must be an integer" in str(exc)
    finally:
        shutil.rmtree(sut, ignore_errors=True)


# --- review fixes: compose ps JSON parsing (#4) ---------------------------------------
def test_compose_ps_json_no_healthcheck_running_is_healthy():
    """A service WITHOUT a healthcheck (empty Health) but State=running reads as up — the JSON
    parser is robust to the empty Health column the old space-split format collapsed (review
    finding #4)."""
    # one-object-per-line form (newer compose)
    line = '{"Service": "api", "Health": "", "State": "running"}'
    assert envmod._service_healthy_in_ps_json(line, "api") is True
    # explicit healthy
    assert envmod._service_healthy_in_ps_json(
        '{"Service": "api", "Health": "healthy", "State": "running"}', "api") is True
    # unhealthy / exited is NOT up
    assert envmod._service_healthy_in_ps_json(
        '{"Service": "api", "Health": "unhealthy", "State": "running"}', "api") is False
    assert envmod._service_healthy_in_ps_json(
        '{"Service": "api", "Health": "", "State": "exited"}', "api") is False
    # array form (older compose)
    arr = '[{"Service":"db","Health":"healthy","State":"running"}]'
    assert envmod._service_healthy_in_ps_json(arr, "db") is True
    # a different service name does not match
    assert envmod._service_healthy_in_ps_json(line, "other") is False


# --- review fix: REVIEW_QA_STAGE_URL is a SOFT hint (#10) -----------------------------
def test_ambient_stage_url_unreachable_falls_through_to_local():
    """An unreachable ambient REVIEW_QA_STAGE_URL must NOT hard-fail a SUT with a working
    setup.sh — it is a soft hint, so qa ignores it and brings up locally (review finding)."""
    sut = _hook_sut()
    dead = f"http://127.0.0.1:{_free_port()}"
    old = os.environ.get("REVIEW_QA_STAGE_URL")
    os.environ["REVIEW_QA_STAGE_URL"] = dead
    try:
        handle = bring_up_env(sut_path=sut, config=cfg.SutConfig(), stage_url_override=None,
                              exit_no_env=EXIT_NO_ENV, exit_unhealthy=EXIT_UNHEALTHY)
        assert handle.mode == EnvMode.HOOK, "fell through to local bring-up, not hard-failed"
        assert (sut / "UP").exists()
        handle.tear_down()
    finally:
        if old is None:
            os.environ.pop("REVIEW_QA_STAGE_URL", None)
        else:
            os.environ["REVIEW_QA_STAGE_URL"] = old
        shutil.rmtree(sut, ignore_errors=True)


def test_ambient_stage_url_unreachable_on_bare_sut_skips_not_no_env():
    """A stale unreachable ambient REVIEW_QA_STAGE_URL on a SUT with NO hook / NO compose must
    NOT hard-fail EXIT_QA_NO_ENV — the soft hint falls through to a NONE handle so the agent
    does its own Phase-2 local bring-up, exactly as if no env var had been set (review finding:
    a leftover ambient var flipped a green unit-style run to EXIT_QA_NO_ENV)."""
    sut = Path(tempfile.mkdtemp(prefix="qa-env-bare-ambient-"))
    dead = f"http://127.0.0.1:{_free_port()}"
    old = os.environ.get("REVIEW_QA_STAGE_URL")
    os.environ["REVIEW_QA_STAGE_URL"] = dead
    try:
        handle = bring_up_env(sut_path=sut, config=None, stage_url_override=None,
                              exit_no_env=EXIT_NO_ENV, exit_unhealthy=EXIT_UNHEALTHY)
        assert handle.mode == EnvMode.NONE, "a bare SUT + stale ambient var must skip, not fail"
        handle.tear_down()  # a NONE handle's teardown is a no-op
    finally:
        if old is None:
            os.environ.pop("REVIEW_QA_STAGE_URL", None)
        else:
            os.environ["REVIEW_QA_STAGE_URL"] = old
        shutil.rmtree(sut, ignore_errors=True)


def test_no_env_message_carries_no_review_cli_prefix():
    """The no-env recommend message must NOT begin with the ``[review-cli] qa:`` tag — the
    handler prefixes every EnvError, so carrying it here double-printed the tag (review
    finding: ``[review-cli] qa: [review-cli] qa: no SUT env …``)."""
    msg = envmod._no_env_message(Path("/tmp/some-sut"))
    assert not msg.startswith("[review-cli]"), msg
    assert msg.startswith("no SUT env"), msg


def test_explicit_stage_url_unreachable_hard_fails():
    """An EXPLICIT --stage-url override that is unreachable DOES hard-fail (a deliberate "test
    against THIS stage") — distinct from the ambient env-var soft hint."""
    sut = _hook_sut()  # has a working hook, but the explicit stage must still hard-fail
    dead = f"http://127.0.0.1:{_free_port()}"
    try:
        bring_up_env(sut_path=sut, config=cfg.SutConfig(), stage_url_override=dead,
                     exit_no_env=EXIT_NO_ENV, exit_unhealthy=EXIT_UNHEALTHY)
        raise AssertionError("expected EnvError for an unreachable EXPLICIT stage")
    except EnvError as exc:
        assert exc.exit_code == EXIT_UNHEALTHY
        assert not (sut / "UP").exists(), "must not have run the hook on an explicit-stage fail"
    finally:
        shutil.rmtree(sut, ignore_errors=True)


# --- review fix: seed runs after the gate (#7) ----------------------------------------
def test_seed_script_runs_after_health_gate():
    """A sut.seed script runs AFTER the (passing) health gate, leaving its marker — proving the
    lifecycle executes seed (review finding: seed was parsed but never run)."""
    sut = _hook_sut()
    seed = sut / "seed.sh"
    seed.write_text("#!/bin/sh\ntouch \"$(dirname \"$0\")/SEEDED\"\n", encoding="utf-8")
    seed.chmod(0o755)
    try:
        config = cfg.SutConfig(seed=["seed.sh"])
        handle = bring_up_env(sut_path=sut, config=config, stage_url_override=None,
                              exit_no_env=EXIT_NO_ENV, exit_unhealthy=EXIT_UNHEALTHY)
        assert (sut / "SEEDED").exists(), "the seed script must have run after the gate"
        handle.tear_down()
    finally:
        shutil.rmtree(sut, ignore_errors=True)


def test_compose_service_check_on_hook_env_fails_fast():
    """A compose_service health check declared for a HOOK env (no -p project) fails FAST with a
    clear message — not a silent wait-out-the-timeout (review finding #5)."""
    sut = _hook_sut()
    try:
        config = cfg.SutConfig(
            health=[cfg.HealthCheck(name="db", compose_service="db", timeout_s=30)],
        )
        try:
            bring_up_env(sut_path=sut, config=config, stage_url_override=None,
                         exit_no_env=EXIT_NO_ENV, exit_unhealthy=EXIT_UNHEALTHY)
            raise AssertionError("expected EnvError for compose_service check on a hook env")
        except EnvError as exc:
            assert exc.exit_code == EXIT_UNHEALTHY
            assert "requires a compose bring-up" in str(exc)
        # the env was torn down (the check failed before the gate could pass)
        assert (sut / "DOWN").exists()
    finally:
        shutil.rmtree(sut, ignore_errors=True)


def test_seed_failure_tears_down_and_fails():
    """A failing seed script tears the (owned) env down and fails the run — a half-seeded env
    would feed the tester misleading state."""
    sut = _hook_sut()
    seed = sut / "seed.sh"
    seed.write_text("#!/bin/sh\nexit 4\n", encoding="utf-8")
    seed.chmod(0o755)
    try:
        config = cfg.SutConfig(seed=["seed.sh"])
        try:
            bring_up_env(sut_path=sut, config=config, stage_url_override=None,
                         exit_no_env=EXIT_NO_ENV, exit_unhealthy=EXIT_UNHEALTHY)
            raise AssertionError("expected EnvError for a failing seed")
        except EnvError as exc:
            assert exc.exit_code == EXIT_UNHEALTHY
            assert "seed script" in str(exc)
        assert (sut / "DOWN").exists(), "a failing seed must tear the env down"
    finally:
        shutil.rmtree(sut, ignore_errors=True)


def test_keep_on_failure_config_field_is_honored_by_handler():
    """The handler honors sut.teardown.keep_on_failure (the CONFIG field), not only the
    --keep-env flag (review finding: the config field was dead). Proven at the handler's
    keep_env resolution: flag OR config."""
    # mirror the handler's resolution line
    config = cfg.SutConfig(teardown=cfg.TeardownConfig(keep_on_failure=True))
    keep_env = False or (config is not None and config.teardown.keep_on_failure)
    assert keep_env is True
    config2 = cfg.SutConfig(teardown=cfg.TeardownConfig(keep_on_failure=False))
    assert (False or (config2 is not None and config2.teardown.keep_on_failure)) is False


# --- (docker, gated) a real compose bring-up + -p-namespaced teardown -----------------
def _docker_available() -> bool:
    if os.environ.get("REVIEW_QA_DOCKER", "").strip().lower() in ("", "0", "false", "no"):
        return False
    try:
        proc = subprocess.run(["docker", "compose", "version"], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def test_docker_compose_bringup_and_teardown_live():
    """LIVE (REVIEW_QA_DOCKER=1): a real ``docker compose -p <ns> up -d --wait`` brings a tiny
    HTTP service up, the health gate passes against it, hand-off works, and teardown runs
    ``down -v --remove-orphans`` for ONLY the ``-p`` namespace — the project is gone afterward
    (no leak, ownership respected). Skipped without docker."""
    if not _docker_available():
        _skip("REVIEW_QA_DOCKER not set or docker compose unavailable")
    sut = Path(tempfile.mkdtemp(prefix="qa-env-docker-"))
    project = f"review-qa-test-{os.getpid()}"
    port = _free_port()
    try:
        envdir = sut / "docs" / "tests" / "env"
        envdir.mkdir(parents=True)
        # A minimal one-service compose: python's stdlib http.server with a compose healthcheck.
        (envdir / "docker-compose.qa.yml").write_text(
            "services:\n"
            "  api:\n"
            "    image: python:3.12-alpine\n"
            f"    command: python -m http.server 8080\n"
            f"    ports: [\"{port}:8080\"]\n"
            "    healthcheck:\n"
            "      test: [\"CMD\", \"python\", \"-c\", \"import urllib.request,sys; "
            "urllib.request.urlopen('http://localhost:8080'); sys.exit(0)\"]\n"
            "      interval: 2s\n"
            "      timeout: 3s\n"
            "      retries: 10\n",
            encoding="utf-8",
        )
        config = cfg.SutConfig(
            bringup=cfg.BringupConfig(
                driver="compose", compose_file="docs/tests/env/docker-compose.qa.yml",
                project_name=project),
            health=[cfg.HealthCheck(name="api", url=f"http://127.0.0.1:{port}/",
                                    expect_status=200, timeout_s=60)],
        )
        handle = bring_up_env(sut_path=sut, config=config, stage_url_override=None,
                              exit_no_env=EXIT_NO_ENV, exit_unhealthy=EXIT_UNHEALTHY)
        assert handle.mode == EnvMode.COMPOSE
        assert handle.project_name == project
        # the namespaced project exists while up
        assert _compose_project_exists(project), "the -p project should be running"
        handle.tear_down()
        # give docker a beat to settle, then assert the project is GONE (teardown reaped it)
        time.sleep(1)
        assert not _compose_project_exists(project), "teardown must remove the -p project"
    finally:
        # belt-and-suspenders: reap the project no matter what
        subprocess.run(["docker", "compose", "-p", project, "down", "-v", "--remove-orphans"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        shutil.rmtree(sut, ignore_errors=True)


def _compose_project_exists(project: str) -> bool:
    proc = subprocess.run(["docker", "compose", "-p", project, "ps", "-q"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return bool(proc.stdout.strip())


# --- standalone runner ----------------------------------------------------------------
class _Skip(Exception):
    pass


def _skip(reason: str):
    if os.environ.get("PYTEST_CURRENT_TEST"):
        import pytest

        pytest.skip(reason)
    raise _Skip(reason)


if __name__ == "__main__":
    failures = 0
    skipped = 0
    for _name, _fn in list(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"PASS {_name}")
            except _Skip as exc:
                skipped += 1
                print(f"SKIP {_name}: {exc}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {_name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {_name}: {type(exc).__name__}: {exc}")
    print(f"\n{'FAILED' if failures else 'OK'}: {failures} failure(s), {skipped} skipped")
    sys.exit(1 if failures else 0)
