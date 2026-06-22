"""The deterministic SUT-environment lifecycle that stands the System-Under-Test up
BEFORE the Phase-2 write/exec executor drives it — and GUARANTEES it is torn down after.

WHAT THIS IS (spec docs/specs/review-qa.md §7.2). A pure-Python, NOT-LLM-driven layer the
qa handler runs around the executor:

    stage-detect ──reachable?──> REUSE (test against it; NEVER tear it down)
         │ no stage
         ▼
    bring-up ── qa/setup.sh hook (lightweight default)
              └ docker compose -p <ns> up -d --wait (heavier option)
         │
         ▼
    health-gate (poll until healthy, or fail EXIT_QA_ENV_UNHEALTHY)
         │
         ▼
    hand the running env to the executor
         │
         ▼
    GUARANTEED teardown — only of what THIS run brought up

WHY DETERMINISTIC PYTHON, NOT THE AGENT. The un-caged tester (executor.py) drives the SUT,
but the env it runs IN must be owned by code that can never "forget" to clean up — an LLM
that leaks a container/dev-server is the exact failure this layer exists to prevent. So
bring-up + health + teardown are mechanical Python; the agent only TESTS the already-up env.

TEARDOWN IS NOT BACKSTOP-REAPING (the adversarial must-fix, spec lines 592/601).
``reviewlib/backstop.py`` only SIGKILLs registered backend SUBPROCESS groups. A container
started with ``docker compose up -d`` is daemonized by the Docker daemon — it is in NO child
process group of review, so SIGKILLing review's children leaves every container running.
This module therefore registers a REAL teardown action (keyed by the ``-p`` project name, or
the hook's own ``setup.sh down`` command) with an ``atexit`` + SIGINT/SIGTERM handler, so an
abnormal exit (Ctrl-C, crash, backstop fire) still reaps the env. The teardown runs
``docker compose -p <project> down`` — a daemon-level operation, independent of process
groups — exactly the operation backstop cannot do.

OWNERSHIP RULE (security, load-bearing). Teardown targets ONLY the namespaced ``-p`` project
this run CREATED (or the hook this run RAN). A REUSED stage — anything we did not bring up —
is NEVER torn down. We can only ever name our own ``-p <project_name>`` to ``docker compose
down``; we never run a bare ``down`` that could catch the dev's own compose project, and we
never touch a stage URL. If a bring-up cannot be made safe + guaranteed, the lifecycle fails
BLOCKED rather than running tests against a half-up env.
"""
from __future__ import annotations

import atexit
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ..process import _run
from .config import BringupConfig, HealthCheck, StageConfig, SutConfig

# The lightweight hook the lifecycle prefers over compose: an executable the SUT ships that
# brings its own env up (``up``) and tears it down (``down``). First match wins.
HOOK_RELPATHS = ("qa/setup.sh", ".review-qa/setup.sh", "docs/tests/env/setup.sh")

# How long a single stage liveness probe waits (the reuse decision must be fast — a slow
# stage is treated as not-reachable and we fall through to bring-up).
_STAGE_PROBE_TIMEOUT_S = 5

# Hard ceiling for a hook's `up`/`down` and a compose up/down spawn, so a wedged bring-up or
# teardown can never hang the run unbounded (it has its own bound; the run backstop is the
# last resort above it).
_HOOK_TIMEOUT_S = 600
_COMPOSE_UP_TIMEOUT_S = 900
_TEARDOWN_TIMEOUT_S = 300


class EnvMode(str, Enum):
    """How the SUT env was satisfied — drives the ownership/teardown decision.

    ``REUSED_STAGE``: a reachable declared stage; we own NOTHING, tear down NOTHING.
    ``HOOK``: we ran the SUT's ``setup.sh up``; we own it, tear down via ``setup.sh down``.
    ``COMPOSE``: we ran ``docker compose -p <ns> up``; we own it, tear down via ``down``.
    ``NONE``: no env was needed (a pure unit-style suite) — nothing brought up, nothing torn.
    """

    REUSED_STAGE = "reused-stage"
    HOOK = "hook"
    COMPOSE = "compose"
    NONE = "none"


class EnvError(RuntimeError):
    """A controlled env-lifecycle failure carrying the qa exit code the handler should
    return. Raised instead of letting a raw exception escape so the handler maps every
    failure to a stable exit class (NO_ENV / ENV_UNHEALTHY) with teardown already run."""

    def __init__(self, message: str, *, exit_code: int):
        super().__init__(message)
        self.exit_code = exit_code


@dataclass
class EnvHandle:
    """A running (or no-op) SUT env handed to the executor, plus the plan to tear it down.

    ``mode`` says what we own. ``teardown`` is the ZERO-ARG closure that reaps EXACTLY what
    this run brought up (a no-op for ``REUSED_STAGE``/``NONE`` — the ownership rule). It is
    idempotent (safe to call from the normal finally AND the atexit/signal hook). ``endpoints``
    surfaces the resolved base URL(s) for the tester prompt/log. ``project_name`` records the
    ``-p`` namespace for a compose env (the handle the global teardown registry keys on).
    ``compose_args`` is the FULL ``docker compose -p <project> -f <file> [--env-file …]`` prefix
    used for ``up``/``down`` — a ``compose_service`` health probe MUST reuse it so its ``ps``
    discovers the SAME compose file (a file outside the SUT root is invisible to a bare
    ``-p <project> ps`` that re-discovers from cwd)."""

    mode: EnvMode
    teardown: Callable[[], None]
    endpoints: dict[str, str] = field(default_factory=dict)
    project_name: str | None = None
    compose_args: list[str] | None = None
    _torn_down: bool = field(default=False, repr=False)

    def tear_down(self) -> None:
        """Run teardown ONCE (idempotent), then DROP this handle from the global pending
        registry so the atexit hook never iterates an already-down env. The normal path calls
        this in a finally; the global atexit/signal hook also calls it on an abnormal exit —
        the guard makes the second call a no-op so a container is never double-`down`ed."""
        if self._torn_down:
            return
        self._torn_down = True
        try:
            self.teardown()
        except Exception as exc:  # noqa: BLE001 — teardown must never raise out of the finally
            print(f"[review-cli] qa: env teardown error ({self.mode.value}): {exc}",
                  file=sys.stderr, flush=True)
        finally:
            # Self-unregister so a torn-down handle never lingers in the pending list (the
            # atexit/signal sweep would otherwise iterate a no-op handle). Idempotent.
            _unregister_pending(self)


# --- the GUARANTEED-teardown registry (independent of backstop subprocess-reaping) --------
#
# Each entry is a (project_name | None, down_callable). On an abnormal exit (SIGINT/SIGTERM
# or atexit), every registered teardown is run — this is the layer that reaps daemonized
# containers backstop's SIGKILL-the-process-group cannot reach. Lock-guarded; the handlers
# are installed exactly once. We register the COMPOSE `down`/HOOK `down` closures, never a
# reused stage (nothing to reap).
_PENDING_TEARDOWNS: "list[EnvHandle]" = []
_PENDING_LOCK = threading.Lock()
_HOOKS_INSTALLED = False
_PREV_SIGINT = None
_PREV_SIGTERM = None


def _install_global_teardown_hooks() -> None:
    """Install the atexit + SIGINT/SIGTERM handlers that reap every still-pending env on an
    abnormal exit, ONCE per process. The signal handler runs all pending teardowns, restores
    the previous handler, and re-raises the signal so the default disposition (or the
    caller's own handler) still applies — qa does not swallow the user's Ctrl-C, it just
    cleans up its env on the way out."""
    global _HOOKS_INSTALLED, _PREV_SIGINT, _PREV_SIGTERM
    if _HOOKS_INSTALLED:
        return
    _HOOKS_INSTALLED = True
    atexit.register(_run_pending_teardowns)
    try:
        _PREV_SIGINT = signal.signal(signal.SIGINT, _signal_teardown)
        _PREV_SIGTERM = signal.signal(signal.SIGTERM, _signal_teardown)
    except (ValueError, OSError):
        # signal() only works on the main thread; in a worker thread we still have atexit.
        pass


def _signal_teardown(signum, frame) -> None:
    """SIGINT/SIGTERM handler: reap pending envs, then chain to the previous handler / default
    disposition so the process still terminates as the user intended (we clean up, we do not
    hijack the signal)."""
    _run_pending_teardowns()
    prev = _PREV_SIGINT if signum == signal.SIGINT else _PREV_SIGTERM
    if callable(prev):
        prev(signum, frame)
        return
    # No python-level previous handler (SIG_DFL/SIG_IGN): restore default and re-raise so the
    # process dies with the conventional signal semantics instead of being silently swallowed.
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


def _run_pending_teardowns() -> None:
    """Tear down every still-pending env (idempotent per handle). Best-effort, never raises —
    runs from atexit and the signal handler, so it must not itself throw.

    REENTRANCY (review finding). A SIGINT/SIGTERM is delivered on the MAIN thread; if it lands
    while the main thread is INSIDE ``_register_pending``/``_unregister_pending`` holding the
    (non-reentrant) ``_PENDING_LOCK``, a blocking ``with _PENDING_LOCK`` here would DEADLOCK
    the same thread against itself — killing the exact teardown guarantee this exists for. So
    snapshot under a NON-BLOCKING acquire; if the lock is already held (the narrow reentrant
    window), fall back to a direct ``list(...)`` copy. The registry mutations are single
    list append/remove (atomic under the GIL), so an unlocked snapshot is safe enough for this
    last-resort reap — a momentarily-stale view at worst misses a just-added handle the atexit
    sweep then catches, and never deadlocks."""
    if _PENDING_LOCK.acquire(blocking=False):
        try:
            snapshot = list(_PENDING_TEARDOWNS)
        finally:
            _PENDING_LOCK.release()
    else:
        snapshot = list(_PENDING_TEARDOWNS)  # reentrant window: GIL-atomic copy, no deadlock
    for handle in snapshot:
        handle.tear_down()


def sweep_pending_teardowns() -> None:
    """Public, best-effort reap of every still-pending owned env — the entry point the run
    BACKSTOP calls before its ``os._exit(124)``.

    ``backstop._fire`` force-exits with ``os._exit``, which BYPASSES ``atexit`` (codex P2): a
    qa tester that wedges until the backstop after a hook/compose env was brought up would
    otherwise leak the daemonized SUT, defeating this layer's whole guarantee. The backstop
    only knows how to SIGKILL registered subprocess GROUPS — it cannot reach a daemonized
    container. So it calls this sweep first, which runs the same idempotent, never-raising
    teardown the atexit/signal path uses. A no-op when nothing is pending (no qa env was ever
    brought up), so a plain ``review`` run pays nothing.

    The registry SNAPSHOT is taken under a non-blocking lock (no deadlock with a concurrent
    signal-path teardown), but each ``tear_down`` is a SYNCHRONOUS ``down`` spawn bounded by
    its own timeout — so this can take up to (pending envs x teardown timeout). On the backstop
    path that is fine: the deadman armed in ``_fire`` is the hard exit guarantee and will cut a
    long sweep off mid-flight."""
    _run_pending_teardowns()


def _register_pending(handle: EnvHandle) -> None:
    """Track an env so the global hooks reap it on an abnormal exit. Only OWNED envs
    (compose/hook) are registered — a reused stage / no-op env has nothing to reap."""
    if handle.mode in (EnvMode.REUSED_STAGE, EnvMode.NONE):
        return
    _install_global_teardown_hooks()
    with _PENDING_LOCK:
        _PENDING_TEARDOWNS.append(handle)


def _unregister_pending(handle: EnvHandle) -> None:
    """Drop a handle once its normal teardown has run, so the atexit hook does not iterate a
    stale, already-down env (idempotent).

    Uses a NON-BLOCKING acquire for the same reentrancy reason as ``_run_pending_teardowns``:
    this runs from ``tear_down``, which the signal handler also calls — a blocking acquire
    while the main thread already holds the lock (a signal landing inside ``_register_pending``)
    would deadlock the thread against itself. A ``list.remove`` is GIL-atomic, so the unlocked
    fallback is safe."""
    if _PENDING_LOCK.acquire(blocking=False):
        try:
            _safe_remove(handle)
        finally:
            _PENDING_LOCK.release()
    else:
        _safe_remove(handle)


def _safe_remove(handle: EnvHandle) -> None:
    try:
        _PENDING_TEARDOWNS.remove(handle)
    except ValueError:
        pass


# --- the top-level lifecycle --------------------------------------------------------------
def bring_up_env(
    *,
    sut_path: Path,
    config: SutConfig | None,
    stage_url_override: str | None = None,
    exit_no_env: int,
    exit_unhealthy: int,
    keep_env: bool = False,
) -> EnvHandle:
    """Stand the SUT env up (or reuse a stage), health-gate it, and return an ``EnvHandle``.

    Decision order (spec §7.2 phases 1→3):
      1. STAGE DETECT — a reachable declared stage → ``REUSED_STAGE`` (no teardown, ever).
      2. BRING-UP — a ``qa/setup.sh`` hook (lightweight default) else a compose config.
         Neither present → ``EnvError(exit_no_env)`` (the recommend/no-env gate).
      3. HEALTH GATE — poll every declared check until healthy; a timeout tears down what we
         brought up (unless ``keep_env``) and raises ``EnvError(exit_unhealthy)``.

    On ANY failure after bring-up, the partially-up env is torn down before the error
    propagates (the guarantee holds on the failure path too). The returned handle's
    ``tear_down`` is what the caller runs in its own finally for the SUCCESS path."""
    stage, explicit = _resolve_stage(config, stage_url_override)
    if stage is not None and _stage_reachable(stage):
        print(f"[review-cli] qa: reusing reachable stage {stage.url} (will NOT tear it down).",
              file=sys.stderr, flush=True)
        return EnvHandle(mode=EnvMode.REUSED_STAGE, teardown=_noop,
                         endpoints={"stage": stage.url})
    if stage is not None and explicit:
        # An EXPLICITLY-declared stage (--stage-url or qa.yaml sut.stage) is not reachable.
        # Never silently fall through to a half-up stage; this is an infra failure the caller
        # must see (spec §7.2 Phase 1).
        raise EnvError(
            f"the declared stage {stage.url} is not reachable "
            f"(probed {stage.health_target()}); refusing to run tests against a half-up "
            "stage. Bring the stage up, or remove sut.stage to bring the SUT up locally.",
            exit_code=exit_unhealthy,
        )
    ambient_stage_only = stage is not None and not explicit
    if ambient_stage_only:
        # An UNREACHABLE stage from the ambient REVIEW_QA_STAGE_URL env var is a SOFT hint, not
        # a declaration — a stale leftover var must NOT hard-fail a SUT that has a working
        # setup.sh/compose. Fall through to local bring-up (review finding: an ambient var
        # hard-failing unhealthy was an acute foot-gun).
        print(f"[review-cli] qa: REVIEW_QA_STAGE_URL={stage.url} is not reachable; ignoring it "
              "and bringing the SUT up locally.", file=sys.stderr, flush=True)
    return _bring_up_local(sut_path=sut_path, config=config,
                           exit_no_env=exit_no_env, exit_unhealthy=exit_unhealthy,
                           keep_env=keep_env, ambient_stage_only=ambient_stage_only)


def _bring_up_local(
    *, sut_path: Path, config: SutConfig | None,
    exit_no_env: int, exit_unhealthy: int, keep_env: bool,
    ambient_stage_only: bool = False,
) -> EnvHandle:
    """No reachable stage — bring the SUT up locally via the hook (preferred) or compose,
    then health-gate. Raises ``EnvError(exit_no_env)`` when NEITHER a hook nor a compose
    config exists (the recommend gate).

    ``ambient_stage_only`` is True when the env layer was entered ONLY because a stale,
    unreachable ``REVIEW_QA_STAGE_URL`` was set — no hook, no compose, no explicit stage. In
    that case a missing local bring-up is NOT a NO_ENV failure: the soft hint must never
    hard-fail an otherwise unit-style SUT, so we return a NONE handle and let the executor do
    its own Phase-2 local bring-up (review finding: a leftover ambient var flipped a green unit
    run to EXIT_QA_NO_ENV — the very foot-gun the ambient-is-soft rule exists to close)."""
    hook = _find_setup_hook(sut_path)
    if hook is not None:
        handle = _bring_up_hook(sut_path=sut_path, hook=hook, exit_unhealthy=exit_unhealthy)
    elif config is not None and config.bringup is not None:
        handle = _bring_up_compose(sut_path=sut_path, bringup=config.bringup,
                                   exit_unhealthy=exit_unhealthy)
    elif ambient_stage_only:
        return EnvHandle(mode=EnvMode.NONE, teardown=_noop)
    else:
        raise EnvError(_no_env_message(sut_path), exit_code=exit_no_env)

    _register_pending(handle)
    checks = _health_checks(config)
    _gate_health_or_teardown(
        handle, sut_path=sut_path, checks=checks,
        exit_unhealthy=exit_unhealthy, keep_env=keep_env,
    )
    # Seed AFTER a fully-green health gate (spec §7.2 Phase 3): idempotent fixture scripts run
    # only once the env is proven up. A seed failure tears the env down (we own it) and fails
    # the run — a half-seeded env would give the tester misleading state.
    _run_seed_scripts(handle, sut_path=sut_path, scripts=_seed_scripts(config),
                      exit_unhealthy=exit_unhealthy, keep_env=keep_env)
    # Surface a base endpoint for the tester prompt: the FIRST HTTP health check's URL is the
    # best machine-known address of the now-up env, so the agent (told "env is ready, don't
    # boot") knows WHERE to drive it. A compose env on a published port / a hook env both
    # carry it; with no HTTP check the agent reads the address from the SUT's own config
    # (review finding — a "none" bring-up with no endpoint left the agent address-less).
    endpoint = _first_http_endpoint(checks)
    if endpoint:
        handle.endpoints["base"] = endpoint
    return handle


def _seed_scripts(config: SutConfig | None) -> list[str]:
    return list(config.seed) if config is not None else []


def _run_seed_scripts(
    handle: EnvHandle, *, sut_path: Path, scripts: list[str],
    exit_unhealthy: int, keep_env: bool,
) -> None:
    """Run each ``sut.seed`` script (relative to the SUT) AFTER the health gate, in order. A
    non-zero seed is a setup failure: tear the (owned) env down unless ``keep_env`` and raise
    ``EnvError`` — a partially-seeded env would feed the tester misleading state."""
    for rel in scripts:
        script = _resolve_under_sut(sut_path, rel)
        proc = _run_env_command(["/bin/sh", str(script)], cwd=sut_path, timeout=_HOOK_TIMEOUT_S)
        if proc.returncode == 0:
            continue
        if not keep_env:
            handle.tear_down()
        else:
            # Symmetric with the health-gate keep-env branch: leave the env up but tell the
            # human how to reap it by hand (review finding: the seed path skipped the hint).
            print(f"[review-cli] qa: --keep-env set; leaving the seeded-failed env up for "
                  f"triage ({_manual_down_hint(handle, sut_path)}).", file=sys.stderr, flush=True)
            _unregister_pending(handle)
        raise EnvError(
            f"seed script {rel} exited {proc.returncode} — the env is up but could not be "
            f"seeded.\n{_tail(proc.stderr or proc.stdout)}",
            exit_code=exit_unhealthy,
        )


def _first_http_endpoint(checks: list[HealthCheck]) -> str | None:
    """The first HTTP health check's URL (the machine-known base of the up env), or ``None``
    when no check is an HTTP one. Used to tell the tester where to reach a hook/compose env."""
    for check in checks:
        if check.url:
            return check.url
    return None


def _gate_health_or_teardown(
    handle: EnvHandle, *, sut_path: Path, checks: list[HealthCheck],
    exit_unhealthy: int, keep_env: bool,
) -> None:
    """Run the health gate; on a timeout, tear down what we brought up (unless ``keep_env``)
    and raise ``EnvError(exit_unhealthy)``. A green gate returns and the env is handed off.
    A hook bring-up with NO declared checks is trusted (the hook is responsible for only
    returning 0 once the env is up); a compose bring-up always runs ``--wait``'s gate plus
    any declared checks."""
    try:
        _health_gate(checks, sut_path=sut_path, project_name=handle.project_name,
                     compose_args=handle.compose_args, exit_unhealthy=exit_unhealthy)
    except EnvError:
        if keep_env:
            # Leave the env up for triage, but DROP it from the pending registry so the atexit
            # hook does not reap what the user explicitly asked to keep.
            print(f"[review-cli] qa: --keep-env set; leaving the unhealthy env up for triage "
                  f"({_manual_down_hint(handle, sut_path)}).", file=sys.stderr, flush=True)
            _unregister_pending(handle)
        else:
            handle.tear_down()  # self-unregisters
        raise


# --- phase 1: stage detection -------------------------------------------------------------
def _resolve_stage(config: SutConfig | None, override: str | None) -> tuple[StageConfig | None, bool]:
    """The stage to probe + whether it was EXPLICITLY declared. Precedence: ``--stage-url``
    override > ``qa.yaml`` stage > the ambient ``REVIEW_QA_STAGE_URL`` env.

    Returns ``(stage, explicit)``. ``explicit=True`` for ``--stage-url`` / ``qa.yaml
    sut.stage`` (a deliberate "test against THIS stage" — an unreachable one is a hard infra
    error). ``explicit=False`` for the ambient ``REVIEW_QA_STAGE_URL`` env var (a soft hint —
    an unreachable one is ignored and we bring up locally, so a stale leftover var never
    hard-fails a SUT with a working setup.sh/compose). ``(None, False)`` when no stage
    anywhere."""
    if override:
        return StageConfig(url=override), True
    if config is not None and config.stage is not None:
        return config.stage, True
    env_url = os.environ.get("REVIEW_QA_STAGE_URL", "").strip()
    return (StageConfig(url=env_url), False) if env_url else (None, False)


def _stage_reachable(stage: StageConfig) -> bool:
    """A fast liveness probe of the stage's health target — a 2xx/3xx within a short timeout
    means REUSE. Any error (connection refused, timeout, 5xx) means not-reachable → fall
    through to bring-up. Never raises."""
    return _http_ok(stage.health_target(), expect_status=None, timeout_s=_STAGE_PROBE_TIMEOUT_S)


# --- phase 2: bring-up via the setup.sh hook (lightweight default) -------------------------
def _find_setup_hook(sut_path: Path) -> Path | None:
    """The first EXISTING + EXECUTABLE ``setup.sh`` hook the SUT ships, or ``None``. An
    existing-but-not-executable file is reported (a common foot-gun) rather than silently
    skipped, so the author knows to ``chmod +x`` it."""
    for rel in HOOK_RELPATHS:
        candidate = sut_path / rel
        if candidate.is_file():
            if os.access(candidate, os.X_OK):
                return candidate
            print(f"[review-cli] qa: found {candidate} but it is not executable; "
                  f"`chmod +x` it to use the hook bring-up. Skipping.", file=sys.stderr, flush=True)
    return None


def _bring_up_hook(*, sut_path: Path, hook: Path, exit_unhealthy: int) -> EnvHandle:
    """Bring the SUT up by running the hook with ``up``; the matching teardown runs it with
    ``down``. The hook owns its OWN lifecycle (it may start compose, a dev server, seed data)
    — qa just calls ``up``/``down`` and trusts the exit code. A non-zero ``up`` is a boot
    failure (``EnvError`` → the handler's BLOCKED/unhealthy class)."""
    def _down() -> None:
        _run_env_command([str(hook), "down"], cwd=sut_path, timeout=_TEARDOWN_TIMEOUT_S)

    print(f"[review-cli] qa: bringing the SUT up via {hook} up", file=sys.stderr, flush=True)
    proc = _run_env_command([str(hook), "up"], cwd=sut_path, timeout=_HOOK_TIMEOUT_S)
    if proc.returncode != 0:
        # Run a best-effort `down` before erroring — a hook that wraps compose / a dev server
        # can have STARTED resources before its `up` exited non-zero, and we own them the moment
        # we ran `up`. Symmetric with the compose `up`-failure path (codex P2: a failed hook up
        # otherwise leaked the partially-created env). `down` is the hook's own idempotent reaper.
        try:
            _down()
        except Exception:  # noqa: BLE001 — best-effort cleanup; surface the original boot failure
            pass
        raise EnvError(
            f"the SUT setup hook `{hook} up` exited {proc.returncode} — could not bring the "
            f"env up.\n{_tail(proc.stderr or proc.stdout)}",
            exit_code=exit_unhealthy,
        )

    return EnvHandle(mode=EnvMode.HOOK, teardown=_down)


# --- phase 2: bring-up via docker compose (the heavier option) ----------------------------
def _bring_up_compose(*, sut_path: Path, bringup: BringupConfig, exit_unhealthy: int) -> EnvHandle:
    """Bring the SUT up with ``docker compose -p <project> up -d --wait``. ``-p`` namespaces
    EVERY container so parallel runs / leftovers never collide AND teardown targets exactly
    this project. The teardown closure runs ``down -v --remove-orphans`` for the SAME
    ``-p`` — only ever this namespace, never a bare ``down`` (the ownership rule). A daemon
    that is down, or a non-zero ``up``, is a boot failure."""
    compose_file = _resolve_under_sut(sut_path, bringup.compose_file)
    if not compose_file.is_file():
        raise EnvError(
            f"compose file {compose_file} not found (sut.bringup.compose_file).",
            exit_code=exit_unhealthy,
        )
    project = bringup.project_name
    argv = ["docker", "compose", "-p", project, "-f", str(compose_file)]
    if bringup.env_file:
        argv += ["--env-file", str(_resolve_under_sut(sut_path, bringup.env_file))]
    up_argv = [*argv, "up", "-d", "--wait"]
    if bringup.build:
        up_argv.append("--build")

    print(f"[review-cli] qa: bringing the SUT up via docker compose -p {project} up -d --wait",
          file=sys.stderr, flush=True)
    proc = _run_env_command(up_argv, cwd=sut_path, timeout=_COMPOSE_UP_TIMEOUT_S)
    if proc.returncode != 0:
        # Tear down anything that DID come up before erroring — a partial `up` can leave
        # half the project running, and we own it the moment we ran `up`.
        _compose_down(argv, cwd=sut_path)
        raise EnvError(
            f"`docker compose -p {project} up` exited {proc.returncode} — could not bring the "
            f"env up (is the docker daemon running? a port already in use?).\n"
            f"{_tail(proc.stderr or proc.stdout)}",
            exit_code=exit_unhealthy,
        )

    def _down() -> None:
        _compose_down(argv, cwd=sut_path)

    return EnvHandle(mode=EnvMode.COMPOSE, teardown=_down, project_name=project,
                     compose_args=argv)


def _compose_down(base_argv: list[str], *, cwd: Path) -> None:
    """Tear down the compose project named in ``base_argv`` (the ``-p <project> -f <file>``
    prefix): ``down -v --remove-orphans``, bounded by a timeout. ONLY ever this ``-p``
    namespace — never a bare ``docker compose down`` that could catch the dev's own project."""
    _run_env_command([*base_argv, "down", "-v", "--remove-orphans"],
                     cwd=cwd, timeout=_TEARDOWN_TIMEOUT_S)


# --- phase 3: health gating ---------------------------------------------------------------
def _health_checks(config: SutConfig | None) -> list[HealthCheck]:
    return list(config.health) if config is not None else []


def _health_gate(
    checks: list[HealthCheck], *, sut_path: Path, project_name: str | None,
    exit_unhealthy: int, compose_args: list[str] | None = None,
) -> None:
    """Poll EVERY declared health check until it passes within its ``timeout_s``; ALL must
    pass before any test runs. The first one that times out raises ``EnvError`` with the
    unhealthy code and the failing check named. No declared checks → trust the bring-up's own
    readiness (a hook returns 0 only when up; compose ``--wait`` already gated on healthchecks)."""
    for check in checks:
        # A `compose_service` check only makes sense for a COMPOSE bring-up (it reads
        # `docker compose -p <project> ps`). On a hook env there is no `-p` project, so the
        # check could never pass — fail FAST with a clear message instead of silently waiting
        # out the whole timeout and reporting a generic "never became healthy" (review finding:
        # the mismatch was an opaque timeout).
        if check.compose_service and not project_name:
            raise EnvError(
                f"health check {check.name!r} uses `compose_service`, but this env was NOT "
                "brought up via docker compose (it has no -p project) — a compose-service "
                "health check requires a compose bring-up. Use a `url` check for a hook env, "
                "or declare a compose bringup.",
                exit_code=exit_unhealthy,
            )
        if not _poll_one_check(check, sut_path=sut_path, project_name=project_name,
                               compose_args=compose_args):
            raise EnvError(
                f"health check {check.name!r} did not pass within {check.timeout_s}s — the env "
                "came up but never became healthy. Not running tests against an unhealthy env.",
                exit_code=exit_unhealthy,
            )


def _poll_one_check(
    check: HealthCheck, *, sut_path: Path, project_name: str | None,
    compose_args: list[str] | None = None,
) -> bool:
    """Poll a single check (HTTP endpoint or compose-service health) with bounded exponential
    backoff until it passes or ``timeout_s`` elapses. Returns True on a pass, False on timeout."""
    deadline = time.monotonic() + check.timeout_s
    delay = 0.5
    while True:
        if _check_passes_once(check, sut_path=sut_path, project_name=project_name,
                              compose_args=compose_args):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
        delay = min(delay * 2, 5.0)


def _check_passes_once(
    check: HealthCheck, *, sut_path: Path, project_name: str | None,
    compose_args: list[str] | None = None,
) -> bool:
    """One immediate evaluation of a check (no waiting). HTTP: a matching status code.
    compose: the service reports ``healthy`` (or ``running`` when it declares no healthcheck)."""
    if check.url:
        return _http_ok(check.url, expect_status=check.expect_status, timeout_s=5)
    return _compose_service_healthy(project_name, check.compose_service, cwd=sut_path,
                                    compose_args=compose_args)


def _compose_service_healthy(
    project_name: str | None, service: str | None, *, cwd: Path,
    compose_args: list[str] | None = None,
) -> bool:
    """True when ``docker compose -p <project> -f <file> ps`` reports ``service`` as
    healthy/running. A compose-service check only makes sense for a compose bring-up; with no
    project name (a hook env) it cannot be evaluated and returns False (the gate then times out
    with a clear message rather than silently passing).

    Reuses the bring-up's FULL ``compose_args`` prefix (``-p <project> -f <file> [--env-file …]``)
    so ``ps`` reads the SAME compose file as ``up``/``down``. A bare ``-p <project> ps`` re-
    discovers a default compose file from ``cwd`` and FAILS for the recommended out-of-root
    layout (``docs/tests/env/docker-compose.qa.yml``), so the check could never pass and the
    swallowed failure timed the gate out (codex P2). Falls back to ``-p <project>`` only if no
    prefix was threaded (defensive — should not happen for a real compose env).

    Parses the JSON-lines ``--format json`` output, NOT a space-separated template: a service
    WITHOUT a healthcheck has an EMPTY ``Health`` column, and a positional ``line.split()``
    would collapse the double space and shift ``State`` into the health slot — so a
    perfectly-running no-healthcheck service would read as unhealthy and the gate would time
    out (review finding). JSON keys are unambiguous regardless of empty fields."""
    if not project_name or not service:
        return False
    base = compose_args if compose_args else ["docker", "compose", "-p", project_name]
    try:
        proc = _run(
            [*base, "ps", "--format", "json"],
            cwd=cwd, timeout=15,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    if proc.returncode != 0:
        return False
    return _service_healthy_in_ps_json(proc.stdout, service)


def _service_healthy_in_ps_json(stdout: str, service: str) -> bool:
    """Scan ``docker compose ps --format json`` output for ``service`` and report whether it
    is healthy/running. The output is one JSON object PER LINE (newer compose) OR a single
    JSON array (older) — both handled. A service is "up" when ``Health == healthy``, or (no
    healthcheck declared → empty/``starting`` health) when ``State == running``."""
    import json

    records: list[dict] = []
    stripped = stdout.strip()
    if not stripped:
        return False
    try:
        parsed = json.loads(stripped)
        records = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        for line in stripped.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    for rec in records:
        if not isinstance(rec, dict) or rec.get("Service") != service:
            continue
        health = (rec.get("Health") or "").lower()
        state = (rec.get("State") or "").lower()
        return health == "healthy" or (health in ("", "starting") and state == "running")
    return False


# --- HTTP probing -------------------------------------------------------------------------
def _http_ok(url: str, *, expect_status: int | None, timeout_s: int) -> bool:
    """GET ``url`` and report success. ``expect_status=None`` accepts any 2xx/3xx (the loose
    stage-liveness probe); an int requires that exact status. Never raises — any connection
    error / timeout / bad status is a clean False. Only ``http(s)`` is probed; a non-URL
    health target (a future command-style check) returns False here."""
    if not url.lower().startswith(("http://", "https://")):
        return False
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 — http(s) only, checked above
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code  # a 4xx/5xx still carries a status we can match against
    except (urllib.error.URLError, OSError, ValueError):
        return False
    if expect_status is None:
        return 200 <= status < 400
    return status == expect_status


# --- spawning env commands (bounded, never the agent) -------------------------------------
def _run_env_command(argv: list[str], *, cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    """Run a bring-up/teardown command (hook or docker) with a hard timeout, capturing
    output. A missing binary / timeout is normalized to a non-zero CompletedProcess so the
    caller branches on ``returncode`` instead of catching — bring-up failures are expected,
    not exceptional. NEVER the agent: these are mechanical, deterministic spawns."""
    try:
        return _run(argv, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            argv, returncode=124,
            stdout=_decode(exc.stdout), stderr=f"timed out after {timeout}s\n{_decode(exc.stderr)}",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(argv, returncode=127, stdout="", stderr=str(exc))


# --- helpers ------------------------------------------------------------------------------
def _noop() -> None:
    """The teardown for an env we do NOT own (a reused stage / no-op): tear down NOTHING."""


def _resolve_under_sut(sut_path: Path, rel: str) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else sut_path / p


def _decode(raw: object) -> str:
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return raw if isinstance(raw, str) else ""


def _tail(text: str, *, lines: int = 20) -> str:
    """The last ``lines`` lines of a captured stream, for an actionable boot-failure error
    (the full output is too noisy; the tail usually carries the real cause)."""
    body = (text or "").strip().splitlines()
    return "\n".join(body[-lines:])


def _manual_down_hint(handle: EnvHandle, sut_path: Path) -> str:
    """The exact manual teardown command for a ``--keep-env`` run, so the human can reap the
    env they kept up for triage."""
    if handle.mode == EnvMode.COMPOSE and handle.project_name:
        return f"manual teardown: docker compose -p {handle.project_name} down -v --remove-orphans"
    if handle.mode == EnvMode.HOOK:
        hook = _find_setup_hook(sut_path)
        return f"manual teardown: {hook} down" if hook else "manual teardown: run your setup.sh down"
    return "nothing to tear down"


def _no_env_message(sut_path: Path) -> str:
    """The 3-part recommend message for "no stage AND no bring-up config" (spec §7.2 Phase
    1b), mirroring the no-suites gate's tone — teach the human to declare an env, don't crash.

    No ``[review-cli] qa:`` prefix here: this rides an ``EnvError`` and the handler already
    prefixes every EnvError as ``[review-cli] qa: {exc}`` — carrying it here too double-printed
    the tag (review finding: the no-env message read ``[review-cli] qa: [review-cli] qa: …``)."""
    return (
        f"no SUT env to test against for {sut_path}.\n"
        "  why: this SUT needs a running env, but no reachable stage is declared and there is "
        "no bring-up config — qa will not fabricate a half-up environment.\n"
        f"  how: ship a lightweight hook at {sut_path}/qa/setup.sh (executable; `up` brings the "
        "env up, `down` tears it down), OR declare a compose bring-up in "
        f"{sut_path}/docs/tests/qa.yaml:\n"
        "         sut:\n"
        "           kind: backend\n"
        "           bringup:\n"
        "             driver: compose\n"
        "             compose_file: docs/tests/env/docker-compose.qa.yml\n"
        "             project_name: review-qa\n"
        "           health:\n"
        "             - { name: api, url: http://localhost:8080/healthz, expect_status: 200 }\n"
        "       OR point qa at an existing stage: review qa --stage-url https://stage.example"
    )
