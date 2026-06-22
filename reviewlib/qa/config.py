"""Parse the optional ``docs/tests/qa.yaml`` env-harness config into typed dataclasses.

WHY HERE (and why a separate module from ``env.py``). ``env.py`` owns the deterministic
LIFECYCLE (detect → reuse/bring-up → health-gate → teardown). This module owns READING the
declared config that drives it — the ``sut.stage`` / ``sut.bringup`` / ``sut.health`` /
``sut.teardown`` blocks the spec's §7.2 defines. Keeping the parse here means ``env.py`` is
handed already-validated typed objects (no dict-fishing in the lifecycle code), and a
malformed config fails with ONE clear error instead of a ``KeyError`` deep in bring-up.

WHAT IT DOES NOT DO. It never reaches the network, never spawns a process, never touches
docker. It is a pure YAML→dataclass transform with defaults + validation, so it is fully
unit-testable with no infra. The lifecycle decisions (reuse vs bring-up vs recommend) live
in ``env.py``; this module only says what was DECLARED.

A non-existent ``qa.yaml`` is NOT an error here — it returns ``None`` so the caller can run
the lightweight ``qa/setup.sh`` hook path (which needs no yaml) or print the recommend/
no-env gate. Only a PRESENT-but-malformed file raises ``QaConfigError``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# The conventional config location (relative to the SUT), per spec §3 (`--config` default).
DEFAULT_CONFIG_REL = "docs/tests/qa.yaml"


class QaConfigError(ValueError):
    """A present ``qa.yaml`` is malformed (bad YAML, wrong shape, or a contradictory block).
    Distinct from "no config" (``None``) so the caller can tell "you wrote a broken config"
    apart from "you wrote none" — only the former is a hard error."""


@dataclass(frozen=True)
class StageConfig:
    """A declared EXISTING stage/preview env to test against instead of booting locally.

    ``url`` is the SUT's base; ``health`` is the endpoint probed to decide REUSE (a 2xx
    within a short timeout → reuse the stage, never tear it down). A stage with no ``health``
    falls back to probing ``url`` itself."""

    url: str
    health: str | None = None

    def health_target(self) -> str:
        """The URL to probe for the reuse decision: the explicit ``health`` if set, else the
        base ``url`` (probing the root is a weak but non-zero liveness signal)."""
        return self.health or self.url


@dataclass(frozen=True)
class BringupConfig:
    """How to BRING the SUT up locally when no reachable stage exists.

    v1 ships the ``compose`` driver (the team's existing pattern). ``project_name`` is the
    ``docker compose -p`` namespace — it isolates every run AND is the exact handle teardown
    targets, so it is REQUIRED for a compose bring-up (without it teardown could not name
    what to reap). ``compose_file`` is resolved relative to the SUT. ``env_file`` (optional)
    carries NON-SECRET defaults only."""

    driver: str  # "compose" (k3s deferred to v2)
    compose_file: str
    project_name: str
    env_file: str | None = None
    build: bool = False

    def __post_init__(self) -> None:
        if self.driver != "compose":
            raise QaConfigError(
                f"sut.bringup.driver={self.driver!r} is not supported in v1 "
                "(only 'compose'; k3s is deferred to v2)."
            )
        if not self.compose_file:
            raise QaConfigError("sut.bringup.compose_file is required for a compose bring-up.")
        if not self.project_name:
            raise QaConfigError(
                "sut.bringup.project_name is required for a compose bring-up — it is the "
                "`docker compose -p` namespace teardown targets; without it qa could not "
                "name exactly what to tear down."
            )


@dataclass(frozen=True)
class HealthCheck:
    """One health gate the env must pass before any test runs.

    Exactly one of ``url`` (poll an HTTP endpoint for ``expect_status``) or
    ``compose_service`` (gate on the container's compose ``healthy`` state) is set. ``name``
    labels the check in the error/log when it times out. ``timeout_s`` bounds the poll."""

    name: str
    url: str | None = None
    expect_status: int = 200
    compose_service: str | None = None
    timeout_s: int = 90

    def __post_init__(self) -> None:
        if bool(self.url) == bool(self.compose_service):
            raise QaConfigError(
                f"health check {self.name!r} must set EXACTLY one of `url` "
                "(HTTP poll) or `compose_service` (container healthcheck) — "
                f"got url={self.url!r}, compose_service={self.compose_service!r}."
            )
        if self.timeout_s <= 0:
            raise QaConfigError(f"health check {self.name!r}: timeout_s must be > 0.")


@dataclass(frozen=True)
class TeardownConfig:
    """Teardown policy. ``keep_on_failure`` (or the ``--keep-env`` flag) skips teardown on a
    FAILED run for triage, printing the exact manual ``down`` command instead. A SUCCESSFUL
    run always tears down what it brought up."""

    keep_on_failure: bool = False


@dataclass(frozen=True)
class SutConfig:
    """The parsed ``sut:`` block — everything ``env.py`` needs to run the lifecycle.

    ``kind`` mirrors the ``--kind`` shapes. ``stage`` / ``bringup`` are each optional; their
    PRESENCE is what the lifecycle branches on (a reachable stage → reuse; else a bringup →
    boot; neither → the recommend/no-env gate). ``health`` gates a local bring-up (a reused
    stage uses ``stage.health`` instead). ``seed`` scripts run AFTER a green health gate."""

    kind: str = "backend"
    stage: StageConfig | None = None
    bringup: BringupConfig | None = None
    health: list[HealthCheck] = field(default_factory=list)
    seed: list[str] = field(default_factory=list)
    teardown: TeardownConfig = field(default_factory=TeardownConfig)


def load_qa_config(sut_path: Path, config_arg: str | None) -> SutConfig | None:
    """Load + validate the SUT's ``qa.yaml`` into a ``SutConfig``, or ``None`` when absent.

    ``config_arg`` is the ``--config`` value (a path, relative resolves against ``sut_path``);
    ``None`` uses the conventional ``docs/tests/qa.yaml``. A MISSING file returns ``None`` —
    the caller then runs the hook path or the recommend gate; only a PRESENT-but-malformed
    file raises ``QaConfigError``. An EXPLICIT ``--config`` that does not exist IS an error
    (the user named a file that isn't there) — distinct from the convention silently absent.
    """
    explicit = config_arg is not None
    rel = config_arg or DEFAULT_CONFIG_REL
    path = Path(rel)
    if not path.is_absolute():
        path = sut_path / path
    if not path.exists():
        if explicit:
            raise QaConfigError(f"--config {rel!r} does not exist (looked at {path}).")
        return None
    return _parse_config_file(path)


def _parse_config_file(path: Path) -> SutConfig:
    """Read + parse a PRESENT ``qa.yaml`` into a ``SutConfig`` (raises ``QaConfigError`` on
    any malformed shape). PyYAML is a declared dependency (``pyproject.toml``), imported
    lazily so the qa module stays import-light."""
    import yaml

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise QaConfigError(f"could not parse qa config {path}: {exc}") from exc
    if raw is None:
        raise QaConfigError(f"qa config {path} is empty.")
    if not isinstance(raw, dict):
        raise QaConfigError(f"qa config {path} must be a YAML mapping at the top level.")
    sut = raw.get("sut")
    if not isinstance(sut, dict):
        raise QaConfigError(f"qa config {path} must have a `sut:` mapping.")
    return _sut_from_mapping(sut, path)


def _sut_from_mapping(sut: dict, path: Path) -> SutConfig:
    """Build the ``SutConfig`` from the ``sut:`` mapping, validating each sub-block."""
    return SutConfig(
        kind=str(sut.get("kind", "backend")),
        stage=_stage_from(sut.get("stage"), path),
        bringup=_bringup_from(sut.get("bringup"), path),
        health=_healthchecks_from(sut.get("health"), path),
        seed=_str_list(sut.get("seed"), "sut.seed", path),
        teardown=_teardown_from(sut.get("teardown")),
    )


def _stage_from(block: object, path: Path) -> StageConfig | None:
    if block is None:
        return None
    if not isinstance(block, dict):
        raise QaConfigError(f"{path}: sut.stage must be a mapping.")
    url = block.get("url")
    if not url or not isinstance(url, str):
        raise QaConfigError(f"{path}: sut.stage.url is required and must be a string.")
    health = block.get("health")
    if health is not None and not isinstance(health, str):
        raise QaConfigError(f"{path}: sut.stage.health must be a string URL.")
    return StageConfig(url=url, health=health)


def _bringup_from(block: object, path: Path) -> BringupConfig | None:
    if block is None:
        return None
    if not isinstance(block, dict):
        raise QaConfigError(f"{path}: sut.bringup must be a mapping.")
    return BringupConfig(
        driver=str(block.get("driver", "compose")),
        compose_file=str(block.get("compose_file", "")),
        project_name=str(block.get("project_name", "")),
        env_file=_opt_str(block.get("env_file")),
        build=bool(block.get("build", False)),
    )


def _healthchecks_from(block: object, path: Path) -> list[HealthCheck]:
    if block is None:
        return []
    if not isinstance(block, list):
        raise QaConfigError(f"{path}: sut.health must be a list of checks.")
    checks: list[HealthCheck] = []
    for i, entry in enumerate(block):
        if not isinstance(entry, dict):
            raise QaConfigError(f"{path}: sut.health[{i}] must be a mapping.")
        name = str(entry.get("name", f"check-{i}"))
        checks.append(
            HealthCheck(
                name=name,
                url=_opt_str(entry.get("url")),
                expect_status=_require_int(entry.get("expect_status", 200),
                                           f"sut.health[{i}].expect_status", path),
                compose_service=_opt_str(entry.get("compose_service")),
                timeout_s=_require_int(entry.get("timeout_s", 90),
                                       f"sut.health[{i}].timeout_s", path),
            )
        )
    return checks


def _require_int(val: object, label: str, path: Path) -> int:
    """Coerce a YAML scalar to ``int`` or raise ``QaConfigError`` — a non-numeric
    ``expect_status: abc`` must be a CLEAN config error, not a bare ``ValueError`` escaping
    the parse (the contract is "one clear error, never a KeyError/ValueError in the internals"
    — review finding)."""
    try:
        return int(val)
    except (TypeError, ValueError):
        raise QaConfigError(f"{path}: {label} must be an integer (got {val!r}).") from None


def _teardown_from(block: object) -> TeardownConfig:
    if block is None or not isinstance(block, dict):
        return TeardownConfig()
    return TeardownConfig(keep_on_failure=bool(block.get("keep_on_failure", False)))


def _str_list(block: object, label: str, path: Path) -> list[str]:
    if block is None:
        return []
    if not isinstance(block, list) or not all(isinstance(x, str) for x in block):
        raise QaConfigError(f"{path}: {label} must be a list of strings.")
    return list(block)


def _opt_str(val: object) -> str | None:
    """A YAML scalar coerced to ``str`` (or ``None`` when absent/empty), so ``url: 8080``
    written without quotes does not crash the typed access downstream."""
    if val is None:
        return None
    s = str(val)
    return s or None
