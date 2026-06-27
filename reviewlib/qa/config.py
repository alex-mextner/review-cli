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
class SeedFile:
    """One pre-boot file the agent-side bot harness writes before the daemon starts (spec §7.3,
    AGENT-SIDE tier). ``path`` is resolved relative to the harness-allocated workdir and MUST stay
    inside it (a ``..`` escape or an out-of-tree absolute path is rejected at write time — the run
    is hermetic); ``content`` is the file body. Both support the harness's template tokens
    (``{config_dir}`` / ``{cwd}`` / ``{owner_id}`` / ``{sender_id}`` / ``{bot_id}`` / ``{home}`` /
    ``{workdir}`` / ``{api_base}``) so a precondition can reference the run's allocated paths — e.g.
    tg-ctl needs a ``tg-ctl.<bot_id>.registration.json`` whose ``cwd`` matches the ask process's
    cwd, written into the run's config dir before the daemon boots."""

    path: str
    content: str = ""


@dataclass(frozen=True)
class BotConfig:
    """The ``sut.bot`` block — how to run a CHAT-BOT SUT (spec §7.3).

    ``driver`` is ``mock`` (the hermetic fake-Telegram Tier-1 default) or ``mtproto`` (the Tier-2
    LIVE driver — a real test Telegram account; gated behind creds, see ``live_tier``).
    ``command`` is the argv that boots the bot's poller — for the mock driver it is run with
    ``TG_API_BASE`` pointed at the fake, so the bot long-polls the fake instead of
    api.telegram.org. ``env`` is extra NON-SECRET environment for the bot (a config flag, a
    feature toggle); secrets stay in host env. ``skip_probe`` opts out of the positive capability
    probe ONLY for a bot that legitimately never sends on the probe update (the default is to
    require the probe so a never-reached fake fails loud instead of false-passing on zero sends).

    THE AGENT-SIDE TIER (``ask_command`` set). A *bridge* bot (tg-ctl) is not driven inbound:
    the AGENT emits an AskUserQuestion/permission via a hook client that reads a payload on stdin,
    the bot forwards ONE inline-button card to Telegram, the user TAPS, and the answer flows back
    to the hook client's stdout. The inbound (``Send:``/``Expect:``) path cannot reach that loop —
    it has no way to inject the agent's question or read the answer. So when ``ask_command`` is set
    the harness runs the AGENT-SIDE flow instead: it boots ``command`` as the long-poll daemon
    against the fake, runs ``ask_command`` (the hook client) to emit each question, asserts the
    card count, injects the tap, and reads the answer off the hook client's stdout (the
    ``Ask-question:``/``Expect-card:``/``Tap:``/``Expect-answer:`` grammar). ``seed`` writes the
    daemon's preconditions first (e.g. tg-ctl's registration file). ``owner_id`` is the Telegram
    user the bot bridges to (the daemon's ``TG_CHAT_ID``); ``sender_id`` is the ``from.id`` stamped
    on an injected tap — it defaults to ``owner_id`` because a bridge bot gates a tap on the owner,
    so a tap from any other id is silently dropped.

    The LIVE (``mtproto``) tier still needs ``command`` (the SUT bot's poller boots either way).
    Whether the live run can actually RUN is decided at dispatch by ``live_tier.bot_live_available``
    (the test-account creds gate) — config only accepts the driver name; it never proves creds."""

    driver: str = "mock"
    command: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    skip_probe: bool = False
    ask_command: tuple[str, ...] = ()
    seed: tuple[SeedFile, ...] = ()
    owner_id: int | None = None
    sender_id: int | None = None
    ready_file: str = ""

    def __post_init__(self) -> None:
        from .live_tier import BOT_LIVE_DRIVER

        if self.driver not in ("mock", BOT_LIVE_DRIVER):
            raise QaConfigError(
                f"sut.bot.driver={self.driver!r} is not supported (use 'mock', the hermetic "
                f"Tier-1 fake-Telegram driver, or {BOT_LIVE_DRIVER!r}, the Tier-2 LIVE driver "
                "that drives a real test Telegram account — gated behind test-account creds)."
            )
        if not self.command:
            raise QaConfigError(
                "sut.bot.command is required for the bot driver — it is the argv that boots the "
                "bot's poller (the mock driver runs it with TG_API_BASE pointed at the fake)."
            )
        if self.is_agent_side and self.owner_id is None:
            raise QaConfigError(
                "sut.bot.owner_id is required when sut.bot.ask_command is set — the AGENT-SIDE "
                "tier bridges to a specific Telegram user, and a bridge bot gates an inbound tap "
                "on that owner id (a tap from any other id is dropped). Set it to a synthetic test "
                "id (e.g. 424242)."
            )
        if self.is_agent_side and self.is_live:
            # The AGENT-SIDE routing lives ONLY on the mock (hermetic) path
            # (run_hermetic_bot_test). A driver=mtproto block with an ask_command would dispatch to
            # the LIVE inbound path and SILENTLY ignore ask_command — the author thinks they are
            # running the agent-side loop but are not. Reject the combination loudly instead.
            raise QaConfigError(
                "sut.bot.ask_command (the AGENT-SIDE tier) requires driver: mock — it is a "
                "hermetic-only flow. A driver: mtproto block with ask_command would run the LIVE "
                "inbound path and silently ignore ask_command. Drop ask_command for a live inbound "
                "run, or set driver: mock for the agent-side loop."
            )

    @property
    def is_live(self) -> bool:
        """Whether this block selects the Tier-2 LIVE driver (vs the hermetic mock)."""
        from .live_tier import BOT_LIVE_DRIVER

        return self.driver == BOT_LIVE_DRIVER

    @property
    def is_agent_side(self) -> bool:
        """Whether this block selects the AGENT-SIDE tier (a hook-client ``ask_command`` drives the
        agent→bridge→Telegram→tap→answer loop) vs the inbound ``Send:``/``Expect:`` path."""
        return bool(self.ask_command)

    @property
    def effective_sender_id(self) -> int | None:
        """The ``from.id`` stamped on an injected tap: the explicit ``sender_id`` if set, else the
        ``owner_id`` (a bridge bot gates a tap on the owner, so defaulting the sender to the owner
        is what makes the tap land)."""
        return self.sender_id if self.sender_id is not None else self.owner_id


@dataclass(frozen=True)
class WebConfig:
    """The ``sut.web`` block — how to run a WEB-APP SUT deterministically (spec §7.1, Tier 1).

    The web Tier-1 harness brings the app up locally, health-gates it reachable, then drives it
    in a headless browser (Playwright/Chromium) against the suite's ``Goto:``/``Click:``/… case
    grammar — a fully deterministic "drive the DOM, assert the DOM" run that needs no un-caged
    agent (the counterpart of the hermetic bot path).

    ``driver`` is ``playwright`` (the Tier-1 headless-Chromium-against-a-locally-booted-app
    default) or ``agent-browser`` (the Tier-2 LIVE driver — a real browser against a deployed
    test site; gated behind ``REVIEW_QA_WEB_LIVE`` + a site URL, see ``live_tier``). ``command``
    is the argv that boots the app's dev server (run from the SUT cwd) — e.g.
    ``[python3, -m, http.server, 8080]`` or ``[npm, run, dev]``; omit it for a SUT already
    reachable at ``base_url`` (a stage / already-running server). ``base_url`` is the address the
    browser navigates to (a relative ``Goto: /login`` resolves against it); its PRESENCE is what
    makes a Tier-1 run possible — without it the harness has nowhere to point the browser. The
    LIVE tier instead takes its site URL from ``REVIEW_QA_WEB_BASE_URL`` (the deployed test site,
    distinct from the locally-booted ``base_url``). ``ready_path`` is the path the health gate
    polls for an HTTP 2xx/3xx before any case runs (default ``/``). ``env`` is extra NON-SECRET
    environment for the dev server; secrets stay in host env. ``ready_timeout_s`` bounds the
    health gate."""

    driver: str = "playwright"
    base_url: str = ""
    command: tuple[str, ...] = ()
    ready_path: str = "/"
    env: dict[str, str] = field(default_factory=dict)
    ready_timeout_s: int = 30

    def __post_init__(self) -> None:
        from .live_tier import WEB_LIVE_DRIVER

        if self.driver not in ("playwright", WEB_LIVE_DRIVER):
            raise QaConfigError(
                f"sut.web.driver={self.driver!r} is not supported (use 'playwright', the Tier-1 "
                f"headless-Chromium driver, or {WEB_LIVE_DRIVER!r}, the Tier-2 LIVE driver that "
                "drives a real browser against a deployed test site — gated behind "
                "REVIEW_QA_WEB_LIVE + REVIEW_QA_WEB_BASE_URL)."
            )
        # The Tier-1 driver needs a local base_url to point the browser at; the LIVE driver takes
        # its site URL from REVIEW_QA_WEB_BASE_URL (checked at the gate, not here), so a live block
        # may legitimately omit base_url.
        if not self.base_url and self.driver != WEB_LIVE_DRIVER:
            raise QaConfigError(
                "sut.web.base_url is required for the Tier-1 web driver — it is the address the "
                "headless browser navigates to (a relative `Goto: /path` resolves against it). Set "
                "it to the dev server's URL (e.g. http://127.0.0.1:8080). The Tier-2 LIVE driver "
                "instead reads the site URL from REVIEW_QA_WEB_BASE_URL."
            )
        if self.ready_timeout_s <= 0:
            raise QaConfigError("sut.web.ready_timeout_s must be > 0.")

    @property
    def is_live(self) -> bool:
        """Whether this block selects the Tier-2 LIVE driver (vs the Tier-1 headless Chromium)."""
        from .live_tier import WEB_LIVE_DRIVER

        return self.driver == WEB_LIVE_DRIVER


@dataclass(frozen=True)
class ExtConfig:
    """The ``sut.ext`` block — how to run a VS-CODE-EXTENSION SUT deterministically (spec §7.1,
    Tier 1, ext kind).

    The ext Tier-1 harness launches an isolated VS Code with the extension on
    ``--extensionDevelopmentPath``, connects over CDP, then drives it against the suite's
    ``Command:``/``Open:``/``Expect-notification:``/… case grammar — a fully deterministic "run a
    command, assert the window state" run that needs no un-caged agent (the counterpart of the
    hermetic bot and the deterministic web paths).

    ``driver`` is ``vscode`` (the Tier-1 isolated-VS-Code-over-CDP default) or ``vscode-visual``
    (the Tier-2 LIVE driver — real VS Code plus window-screenshot visual diffing against a
    baseline; gated behind ``REVIEW_QA_EXT_LIVE`` + the VS Code gate + a baseline dir, see
    ``live_tier`` and issue #82). ``extension_path`` is the directory passed to
    ``--extensionDevelopmentPath`` (the extension under test; default ``.`` = the SUT itself is the
    extension). ``workspace`` is the folder VS Code opens (default ``.`` = the SUT); a relative
    ``Open: file`` resolves against it. ``env`` is extra NON-SECRET environment for the runner;
    secrets stay in host env."""

    driver: str = "vscode"
    extension_path: str = "."
    workspace: str = "."
    env: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        from .live_tier import EXT_LIVE_DRIVER

        if self.driver not in ("vscode", EXT_LIVE_DRIVER):
            raise QaConfigError(
                f"sut.ext.driver={self.driver!r} is not supported (use 'vscode', the Tier-1 "
                f"isolated-VS-Code-over-CDP driver, or {EXT_LIVE_DRIVER!r}, the Tier-2 LIVE driver "
                "that adds window-screenshot visual diffing — gated behind REVIEW_QA_EXT_LIVE + "
                "the VS Code gate + a baseline dir)."
            )

    @property
    def is_live(self) -> bool:
        """Whether this block selects the Tier-2 LIVE driver (vs the Tier-1 CDP runner)."""
        from .live_tier import EXT_LIVE_DRIVER

        return self.driver == EXT_LIVE_DRIVER


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
    bot: BotConfig | None = None
    web: WebConfig | None = None
    ext: ExtConfig | None = None


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
        bot=_bot_from(sut.get("bot"), path),
        web=_web_from(sut.get("web"), path),
        ext=_ext_from(sut.get("ext"), path),
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


def _bot_from(block: object, path: Path) -> BotConfig | None:
    """Parse the ``sut.bot`` block into a ``BotConfig`` (or ``None`` when absent). ``command``
    is a list of argv strings; ``env`` is a string->string mapping. The dataclass's own
    ``__post_init__`` validates the driver + required command, so this only shapes the YAML."""
    if block is None:
        return None
    if not isinstance(block, dict):
        raise QaConfigError(f"{path}: sut.bot must be a mapping.")
    return BotConfig(
        driver=str(block.get("driver", "mock")),
        command=tuple(_str_list(block.get("command"), "sut.bot.command", path)),
        env=_str_env(block.get("env"), path),
        skip_probe=_require_bool(block.get("skip_probe", False), "sut.bot.skip_probe", path),
        ask_command=tuple(_str_list(block.get("ask_command"), "sut.bot.ask_command", path)),
        seed=_bot_seed_from(block.get("seed"), path),
        owner_id=_opt_int(block.get("owner_id"), "sut.bot.owner_id", path),
        sender_id=_opt_int(block.get("sender_id"), "sut.bot.sender_id", path),
        ready_file=str(block.get("ready_file", "")),
    )


def _bot_seed_from(block: object, path: Path) -> tuple[SeedFile, ...]:
    """Parse ``sut.bot.seed`` into ``SeedFile`` tuples (empty when absent). Each entry is a
    mapping with a required ``path`` (resolved relative to the run's workdir) and an optional
    ``content`` body; both carry the harness's template tokens, substituted at run time."""
    if block is None:
        return ()
    if not isinstance(block, list):
        raise QaConfigError(f"{path}: sut.bot.seed must be a list of {{path, content}} mappings.")
    seeds: list[SeedFile] = []
    for i, entry in enumerate(block):
        if not isinstance(entry, dict):
            raise QaConfigError(f"{path}: sut.bot.seed[{i}] must be a mapping.")
        fpath = entry.get("path")
        if not fpath or not isinstance(fpath, str):
            raise QaConfigError(
                f"{path}: sut.bot.seed[{i}].path is required and must be a string.")
        seeds.append(SeedFile(path=fpath, content=str(entry.get("content", ""))))
    return tuple(seeds)


def _opt_int(val: object, label: str, path: Path) -> int | None:
    """Coerce a YAML scalar to ``int`` (or ``None`` when absent) — an owner/sender id. A
    non-numeric value is a clean config error, not a bare ``ValueError`` deep in the run."""
    if val is None:
        return None
    try:
        return int(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise QaConfigError(f"{path}: {label} must be an integer (got {val!r}).") from None


def _web_from(block: object, path: Path) -> WebConfig | None:
    """Parse the ``sut.web`` block into a ``WebConfig`` (or ``None`` when absent). ``command``
    is a list of argv strings (the dev-server boot, optional); ``env`` is a string->string
    mapping. The dataclass's own ``__post_init__`` validates the driver + required base_url, so
    this only shapes the YAML."""
    if block is None:
        return None
    if not isinstance(block, dict):
        raise QaConfigError(f"{path}: sut.web must be a mapping.")
    return WebConfig(
        driver=str(block.get("driver", "playwright")),
        base_url=str(block.get("base_url", "")).rstrip("/"),
        command=tuple(_str_list(block.get("command"), "sut.web.command", path)),
        ready_path=str(block.get("ready_path", "/")),
        env=_str_env(block.get("env"), path),
        ready_timeout_s=_require_int(block.get("ready_timeout_s", 30),
                                     "sut.web.ready_timeout_s", path),
    )


def _ext_from(block: object, path: Path) -> ExtConfig | None:
    """Parse the ``sut.ext`` block into an ``ExtConfig`` (or ``None`` when absent). ``env`` is a
    string->string mapping. The dataclass's own ``__post_init__`` validates the driver, so this
    only shapes the YAML. ``extension_path`` / ``workspace`` default to ``.`` (the SUT is itself
    the extension and the workspace)."""
    if block is None:
        return None
    if not isinstance(block, dict):
        raise QaConfigError(f"{path}: sut.ext must be a mapping.")
    return ExtConfig(
        driver=str(block.get("driver", "vscode")),
        extension_path=str(block.get("extension_path", ".")),
        workspace=str(block.get("workspace", ".")),
        env=_str_env(block.get("env"), path),
    )


def _require_bool(val: object, label: str, path: Path) -> bool:
    """Coerce a YAML scalar to a real ``bool`` for a SAFETY flag, rejecting the ambiguous cases
    ``bool()`` gets wrong. ``skip_probe`` disables the unwired-sender safety net, so a typo must
    NOT silently flip it on: ``bool("false")`` is ``True`` in Python, so a quoted
    ``skip_probe: "false"`` would disable the probe (review finding). Accept a native YAML bool,
    or the conventional truthy/falsy strings, and reject anything else as a clean config error."""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        s = val.strip().lower()
        if s in ("true", "yes", "on", "1"):
            return True
        if s in ("false", "no", "off", "0", ""):
            return False
    raise QaConfigError(
        f"{path}: {label} must be a boolean (true/false), got {val!r}."
    )


def _str_env(block: object, path: Path) -> dict[str, str]:
    """A YAML mapping coerced to a ``dict[str, str]`` (empty when absent). Used for
    ``sut.bot.env`` — non-secret extra environment for the bot. Values are stringified so a
    bare ``FEATURE_X: 1`` does not crash the typed access."""
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise QaConfigError(f"{path}: sut.bot.env must be a mapping of string keys to values.")
    return {str(k): str(v) for k, v in block.items()}


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
