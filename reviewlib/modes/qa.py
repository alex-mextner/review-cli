"""qa: agent acts as a TESTER — bring up a System-Under-Test (SUT), run human-authored
test-case suites, hunt bugs.

THE MODE (this file) is the thin CLI surface + the NO-SUITES gate + the dispatch into the
write/exec ENGINE (``reviewlib/qa/executor.py``). It does NOT itself spawn the agent; it
resolves the suites, runs the gate, builds the tester prompt from the (max-cases-capped)
suites + the detected kind, and hands the run to ``run_tester`` — a single-seat write/exec
launcher that is deliberately NOT ``run_panel`` and NOT the read-only board. What this file
ships:

  * a self-describing ``MODE = ModeSpec(name="qa", subcommand="qa", aliases=("test",))``
    registered in ``modes/registry.py`` — so ``review qa`` / ``review test`` resolve;
  * suite DISCOVERY (``resolve_suites``): a conventional dir of prose ``*.md`` suites,
    each holding one or more ``## Case:`` blocks (the free-form markdown format §4 chose);
  * the NO-SUITES GATE (``_handler``): when ``qa`` runs but no suite resolves (or every
    resolved file parses to zero cases), it prints a 3-part WHAT/WHY/HOW message and exits
    ``EXIT_QA_NO_SUITES`` BEFORE any agent spawn — a green qa run with zero cases is a lie;
  * ``--kind auto`` SUT-shape detection (cheap stdlib; §5) to pick the runbook;
  * the EXECUTOR dispatch (``_run_executor``): build prompt → ``run_tester`` (isolated
    worktree by default) → parse the agent's ``VERDICT:`` → report-only exit (a found bug
    exits 0 with findings printed; only BLOCKED / a missing verdict / the ``--strict``
    finding flip are non-zero).

RUNTIME REACH: dispatched table-driven by ``cli._dispatch`` exactly like every other mode
(``get_mode("qa")`` → ``_build_mode_parser`` → ``mode.handler(ctx)``); no ``cli.py``
dispatch surgery beyond the qa exit-code constants + the long-timeout carve-out.
``diff_policy="none"`` keeps qa OFF the required-diff path — qa is about a running system,
not a diff. Single-seat: the executor runs ONE write/exec backend (``REVIEW_QA_TESTER``),
ignoring ``ctx.models`` / ``--pool`` — a panel of testers fighting one SUT/port is nonsense.

INVARIANT: the no-suites gate is the load-bearing contract of the whole mode. Do NOT let a
future change spawn an agent before ``resolve_suites`` has returned a non-empty list.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from .contract import ModeContext, ModeSpec

# The recommended, canonical suite location (relative to the SUT path). A ``*.md`` file
# here is a SUITE; each ``## Case:`` block in it is a CASE the tester must exercise. The
# default is a GLOB so the common "drop suites in one dir" layout works with no flags; the
# ``--suites`` flag overrides it with any glob, file, or directory.
DEFAULT_SUITES_GLOB = "docs/tests/suites/*.md"

# A CASE heading. The spec (§4) counts ``## Case: <title>`` blocks for the CASES tally.
# We accept the bare ``## Case:`` prefix (case-insensitive, optional trailing title) — the
# unambiguous, validated form ``--scaffold-env`` will emit. A plain ``## <title>`` is NOT
# counted as a case on purpose: it keeps the count machine-reliable and the teaching
# message honest (a file with prose headings but no ``## Case:`` is "authored but empty",
# a distinct, explained variant of the gate — see ``_handler``).
_CASE_HEADING_RE = re.compile(r"^\s*##\s*case\s*:", re.IGNORECASE | re.MULTILINE)


def resolve_suites(sut_path: Path, suites_arg: str) -> list[Path]:
    """Resolve ``--suites`` to a sorted list of suite files that each contain >=1 case.

    ``suites_arg`` may be a glob (the default ``docs/tests/suites/*.md``), a directory
    (every ``*.md`` directly inside it), or a single file. Relative values resolve against
    ``sut_path``. Only files that parse to >=1 ``## Case:`` block are returned — an
    authored-but-empty file is NOT a usable suite (the handler reports it distinctly).

    Phase 1 does discovery + a count only; it never executes a case. Returns ``[]`` when
    nothing resolves, which is what fires the no-suites gate.
    """
    candidates = _candidate_suite_files(sut_path, suites_arg)
    return sorted(p for p in candidates if count_cases(p) > 0)


def _candidate_suite_files(sut_path: Path, suites_arg: str) -> list[Path]:
    """Expand ``suites_arg`` (glob / dir / file) to the ``*.md`` files it names, without
    yet checking case counts. Relative args resolve against ``sut_path``.

    Split out from ``resolve_suites`` so the path-shape handling (the three input forms)
    stays separate from the case-count filtering — and so a caller that wants the raw
    candidate set (e.g. to distinguish "no files at all" from "files but no cases") can
    reuse it."""
    target = Path(suites_arg)
    if not target.is_absolute():
        target = sut_path / target

    if target.is_dir():
        return [p for p in sorted(target.glob("*.md")) if p.is_file()]
    if target.is_file():
        return [target]
    # A glob pattern. ``Path.glob`` only globs RELATIVE to a base, so split the pattern into
    # (base, relative-pattern): a relative pattern globs from the SUT path; an absolute one
    # splits at its first wildcard component (the non-magic parent walked, NOT the FS root).
    base, pattern = _glob_base_and_pattern(sut_path, suites_arg)
    return [p for p in sorted(base.glob(pattern)) if p.is_file() and p.suffix == ".md"]


_MAGIC_RE = re.compile(r"[*?\[]")


def _glob_base_and_pattern(sut_path: Path, suites_arg: str) -> tuple[Path, str]:
    """Split a glob ``suites_arg`` into the base directory to glob FROM and the relative
    pattern to glob WITH. A relative arg globs from ``sut_path``. An ABSOLUTE arg is split
    at its first magic (wildcard) component: the leading non-magic parts become the base
    (so the walk starts at the real parent dir, not the filesystem root), and the rest is
    the pattern. Falls back to (anchor, rest) if the whole path is magic."""
    if not Path(suites_arg).is_absolute():
        return sut_path, suites_arg
    parts = Path(suites_arg).parts  # ("/", "a", "b", "*.md")
    split_at = next(
        (i for i, part in enumerate(parts) if _MAGIC_RE.search(part)),
        len(parts),
    )
    base = Path(*parts[:split_at]) if split_at > 0 else Path(parts[0])
    pattern = str(Path(*parts[split_at:])) if split_at < len(parts) else "*"
    return base, pattern


def count_cases(suite_file: Path) -> int:
    """Number of ``## Case:`` blocks in ``suite_file`` (0 on an unreadable/binary file).

    This is the CASES tally the spec's §4 defines. An unreadable file counts as 0 cases
    (it cannot be exercised) rather than raising — the gate treats it the same as an empty
    suite."""
    try:
        text = suite_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0
    return len(_CASE_HEADING_RE.findall(text))


def _resolved_suites_path(sut_path: Path, suites_arg: str) -> str:
    """The human-readable absolute location ``--suites`` pointed at, for the error message.
    A relative arg is shown joined to the SUT; an absolute arg is shown as-is."""
    target = Path(suites_arg)
    return str(target if target.is_absolute() else (sut_path / target))


def _fail_no_suites(sut_path: Path, suites_arg: str, *, exit_code: int) -> int:
    """Print the 3-part WHAT/WHY/HOW message for "qa found no usable test-case suites" and
    return ``exit_code`` (``EXIT_QA_NO_SUITES``). Mirrors ``cli._fail_not_a_repo`` in tone
    and structure (an expected user/contract error, no traceback). Verb-named: it prints
    AND returns the code.

    Distinguishes two same-class cases so the teaching message is honest:
      * no file resolved at all → teach how to author the first suite;
      * a file resolved but parses to zero ``## Case:`` blocks → say so, and point at the
        required heading, so the author isn't told "no suites" about a file they DID write.
    """
    import sys

    location = _resolved_suites_path(sut_path, suites_arg)
    candidates = _candidate_suite_files(sut_path, suites_arg)
    empty_files = [p for p in candidates if count_cases(p) == 0]

    if empty_files:
        what = (
            f"[review-cli] qa: found {len(empty_files)} suite file(s) under {location}, "
            "but none contain a '## Case:' block."
        )
    else:
        what = f"[review-cli] qa: no test-case suites found at {location}."

    print(
        f"{what}\n"
        "  why: qa makes the agent act as a TESTER; without authored cases there is "
        "nothing to exercise and nothing to verify — a green run would be a lie.\n"
        "  how: author at least one suite (each *.md = a suite; one '## Case: <name>' "
        "block per case, with Steps / Expected), then re-run:\n"
        f"         mkdir -p {sut_path}/docs/tests/suites\n"
        "         # docs/tests/suites/smoke.md:\n"
        "         #   # Suite: smoke\n"
        "         #   ## Case: login rejects empty password\n"
        "         #   Steps:\n"
        "         #   - open /login\n"
        "         #   - submit an empty password\n"
        "         #   Expected:\n"
        "         #   - inline error 'password required', no network call\n"
        f"       review qa {sut_path} --suites docs/tests/suites/*.md",
        file=sys.stderr, flush=True,
    )
    return exit_code


def _sut_path(ctx: ModeContext) -> Path:
    """The SUT path: the optional ``sut_path`` positional if given, else the RAW ``-C`` value
    (the package dir the user pointed at), else the resolved cwd.

    CRITICAL: use the RAW ``-C`` (``ctx.args.cwd``), NOT ``ctx.cwd`` — the shared
    ``_effective_cwd`` rewrites an in-repo ``-C`` to the git TOPLEVEL, so for
    ``review qa -C <monorepo/package>`` ``ctx.cwd`` is the repo ROOT, not the package. qa must
    test the package the user named (and IsolatedSut then runs in the matching worktree
    subdir), so the raw ``-C`` is the SUT (review finding). Falls back to ``ctx.cwd`` only when
    ``-C`` is the default ``.`` (no explicit dir), preserving the "default: cwd" behavior."""
    raw_positional = getattr(ctx.args, "sut_path", None)
    if raw_positional:
        return Path(raw_positional).expanduser().resolve()
    raw_c = getattr(ctx.args, "cwd", ".") or "."
    if raw_c != ".":
        return Path(raw_c).expanduser().resolve()
    return Path(ctx.cwd)


def _handler(ctx: ModeContext) -> int:
    """qa handler: resolve suites (no-suites gate), bring the SUT env up (Phase 3), then run
    the write/exec TESTER (Phase 2) against the up env, with GUARANTEED env teardown.

    The no-suites gate (Phase 1) still runs FIRST, BEFORE any agent spawn OR any env bring-up
    — a write/exec agent must never launch, and no container/dev-server must come up, for an
    empty run. When suites resolve, the handler:
      * (Phase 3) stands the SUT env up via ``reviewlib/qa/env.py`` — stage-detect → reuse /
        ``qa/setup.sh`` hook / compose bring-up → health-gate — when an env is declared; a
        SUT that needs no env (no stage, no hook, no config) skips this gracefully and the
        agent does its own local bring-up per the runbook;
      * (Phase 2) builds the tester prompt + hands it to the single-seat write/exec launcher;
      * tears the env down on EVERY exit path (success / finding / error / timeout) — but
        ONLY what THIS run brought up (a reused stage is never torn down).
    The agent's evidence-backed ``VERDICT:`` maps to the exit code (report-only; only
    "couldn't run the tester" / BLOCKED / an env failure is non-zero, plus the --strict flip)."""
    # Imported here (not at module top) so the qa module stays import-light and does not
    # create a circular import with cli (cli imports the registry which imports this mode).
    from ..cli import (
        EXIT_QA_ENV_UNHEALTHY,
        EXIT_QA_NO_ENV,
        EXIT_QA_NO_SUITES,
        EXIT_QA_SUT_BOOT_FAILED,
    )

    sut_path = _sut_path(ctx)
    suites = resolve_suites(sut_path, ctx.args.suites)
    if not suites:
        return _fail_no_suites(sut_path, ctx.args.suites, exit_code=EXIT_QA_NO_SUITES)

    # BOT TIER-1 HERMETIC FAST PATH. A bot SUT with a declared mock-driver `sut.bot` config runs
    # the DETERMINISTIC hermetic harness (fake Telegram + inject/capture), NOT the un-caged
    # executor: "send this update -> expect this reply" needs no write/exec agent and no compose
    # env, so it stays off both the agent-cage blast radius and the env layer. Routed here, before
    # _run_with_env, so the bot path is a clean, self-contained branch. A bot SUT WITHOUT a mock
    # config (or --kind not bot) falls through to the normal env+executor flow, where the prose
    # bot runbook still tells an agent to stand a mock up by hand (the Phase-2 behavior).
    bot_route = _resolve_hermetic_bot(ctx, sut_path)
    if bot_route is not None:
        return _run_bot_hermetic(
            ctx, sut_path, suites, bot_config=bot_route,
            exit_blocked=EXIT_QA_SUT_BOOT_FAILED,
        )

    # WEB TIER-1 DETERMINISTIC FAST PATH. A web SUT with a declared `sut.web` config runs the
    # DETERMINISTIC headless-browser harness (Playwright bring-up + drive the DOM + assert), NOT
    # the un-caged executor: "goto -> click -> expect text/url" needs no write/exec agent, so it
    # stays off the agent-cage blast radius (mirroring the bot path). Routed here, before
    # _run_with_env, as a clean self-contained branch. A web SUT WITHOUT a sut.web config (or
    # --kind not web) falls through to the normal env+executor flow, where the prose web runbook
    # still tells an agent to drive the site by hand (the Phase-2 behavior).
    web_route = _resolve_deterministic_web(ctx, sut_path)
    if web_route is not None:
        return _run_web_deterministic(
            ctx, sut_path, suites, web_config=web_route,
            exit_blocked=EXIT_QA_SUT_BOOT_FAILED,
        )

    # EXT TIER-1 DETERMINISTIC FAST PATH. A VS-code-extension SUT with a declared `sut.ext` config
    # runs the DETERMINISTIC isolated-VS-Code harness (launchVSCode-over-CDP + run command + assert
    # window state), NOT the un-caged executor: "Command: -> Expect-notification:/editor-text/
    # webview" needs no write/exec agent, so it stays off the agent-cage blast radius (mirroring
    # the bot + web paths). Routed here, before _run_with_env, as a clean self-contained branch. An
    # ext SUT WITHOUT a sut.ext config (or --kind not ext) falls through to the normal env+executor
    # flow, where the prose ext runbook still tells an agent to drive VS Code by hand (Phase 2).
    ext_route = _resolve_deterministic_ext(ctx, sut_path)
    if ext_route is not None:
        return _run_ext_deterministic(
            ctx, sut_path, suites, ext_config=ext_route,
            exit_blocked=EXIT_QA_SUT_BOOT_FAILED,
        )

    return _run_with_env(
        ctx, sut_path, suites,
        exit_blocked=EXIT_QA_SUT_BOOT_FAILED,
        exit_no_env=EXIT_QA_NO_ENV,
        exit_unhealthy=EXIT_QA_ENV_UNHEALTHY,
    )


def _resolve_hermetic_bot(ctx: ModeContext, sut_path: Path):
    """Return the ``BotConfig`` when this run should take the hermetic Tier-1 bot path, else
    ``None``. The path activates when the effective kind is ``bot`` AND the SUT declares a
    ``sut.bot`` mock-driver config — that config is what makes a hermetic run possible (it
    names the bot command to boot against the fake). A bot SUT with no such config, or any
    other kind, returns ``None`` (the normal env+executor flow).

    Kind resolution: an explicit ``--kind`` wins; under ``--kind auto`` the SUT's declared
    ``sut.kind`` in qa.yaml ALSO counts as bot (not just package-marker detection) — a bot
    configured purely via qa.yaml, with no telegram dependency marker, must still take the
    hermetic path under the default ``auto`` rather than falling through to the un-caged backend
    runbook (review finding). A config-parse error is NOT swallowed here — it returns ``None`` so
    the normal flow's ``_run_with_env`` reports the same error once, in one place."""
    from ..qa.config import QaConfigError, load_qa_config

    try:
        config = load_qa_config(sut_path, ctx.args.config)
    except QaConfigError:
        return None  # let _run_with_env surface the parse error once
    if not _kind_is_bot(ctx, sut_path, config):
        return None
    # Both the Tier-1 mock driver AND the Tier-2 mtproto LIVE driver route here (the live branch
    # is selected inside _run_bot_hermetic via config.bot.is_live, which gates on creds + SKIPs
    # LOUD when absent — a tier:live block must NOT fall through to the un-caged executor).
    if config is not None and config.bot is not None:
        return config.bot
    return None


def _kind_is_bot(ctx: ModeContext, sut_path: Path, config: object | None) -> bool:
    """Whether the effective kind is ``bot`` (uses the shared ``_resolve_kind`` so the hermetic
    routing decision and the executor's runbook selection agree on what ``sut.kind`` means)."""
    return _resolve_kind(ctx, sut_path, config) == "bot"


def _resolve_deterministic_web(ctx: ModeContext, sut_path: Path):
    """Return the ``WebConfig`` when this run should take the deterministic Tier-1 web path, else
    ``None``. The path activates when the effective kind is ``web`` AND the SUT declares a
    ``sut.web`` config — that config (its ``base_url`` + optional dev-server ``command``) is what
    makes a deterministic headless-browser run possible. A web SUT with no such config, or any
    other kind, returns ``None`` (the normal env+executor flow, where the prose web runbook tells
    an un-caged agent to drive the site by hand).

    Kind resolution mirrors the bot path: an explicit ``--kind`` wins; under ``--kind auto`` the
    SUT's declared ``sut.kind`` in qa.yaml ALSO counts as web. A config-parse error is NOT
    swallowed here — it returns ``None`` so the normal flow's ``_run_with_env`` reports the same
    error once, in one place."""
    from ..qa.config import QaConfigError, load_qa_config

    try:
        config = load_qa_config(sut_path, ctx.args.config)
    except QaConfigError:
        return None  # let _run_with_env surface the parse error once
    if _resolve_kind(ctx, sut_path, config) != "web":
        return None
    # Both the Tier-1 playwright driver AND the Tier-2 agent-browser LIVE driver route here (the
    # live branch is selected inside _run_web_deterministic via config.web.is_live, which gates on
    # REVIEW_QA_WEB_LIVE + a site URL and SKIPs LOUD when absent — a tier:live block must NOT fall
    # through to the un-caged executor).
    if config is not None and config.web is not None:
        return config.web
    return None


def _resolve_deterministic_ext(ctx: ModeContext, sut_path: Path):
    """Return the ``ExtConfig`` when this run should take the deterministic Tier-1 ext path, else
    ``None``. The path activates when the effective kind is ``ext`` AND the SUT declares a
    ``sut.ext`` config — that config (its ``extension_path`` + ``workspace``) is what makes a
    deterministic isolated-VS-Code run possible. An ext SUT with no such config, or any other
    kind, returns ``None`` (the normal env+executor flow, where the prose ext runbook tells an
    un-caged agent to drive VS Code by hand).

    Kind resolution mirrors the bot + web paths: an explicit ``--kind`` wins; under ``--kind
    auto`` the SUT's declared ``sut.kind`` in qa.yaml ALSO counts as ext. A config-parse error is
    NOT swallowed here — it returns ``None`` so the normal flow's ``_run_with_env`` reports the
    same error once, in one place."""
    from ..qa.config import QaConfigError, load_qa_config

    try:
        config = load_qa_config(sut_path, ctx.args.config)
    except QaConfigError:
        return None  # let _run_with_env surface the parse error once
    if _resolve_kind(ctx, sut_path, config) != "ext":
        return None
    # Both the Tier-1 vscode driver AND the Tier-2 vscode-visual LIVE driver route here (the live
    # branch is selected inside _run_ext_deterministic via config.ext.is_live, which gates on
    # REVIEW_QA_EXT_LIVE + the VS Code gate + a baseline dir and SKIPs LOUD when absent — a
    # tier:live block must NOT fall through to the un-caged executor).
    if config is not None and config.ext is not None:
        return config.ext
    return None


def _effective_kind(ctx: ModeContext, sut_path: Path) -> str:
    """The effective ``--kind`` for the EXECUTOR (fallback) path. Loads the SUT's qa.yaml so a
    YAML-declared ``sut.kind`` is honored under ``--kind auto`` — otherwise a config-declared
    plain bot with no ``sut.bot`` mock config would get the BACKEND runbook from package-marker
    detection alone (review finding: the fallback path ignored sut.kind). A parse error falls
    back to detection (the env layer reports the error separately)."""
    from ..qa.config import QaConfigError, load_qa_config

    try:
        config = load_qa_config(sut_path, ctx.args.config)
    except QaConfigError:
        config = None
    return _resolve_kind(ctx, sut_path, config)


def _resolve_kind(ctx: ModeContext, sut_path: Path, config: object | None) -> str:
    """The single source of truth for resolving the SUT kind: an explicit ``--kind`` wins;
    under ``auto`` the config's declared ``sut.kind`` counts FIRST (qa.yaml is an explicit
    author signal), then the cheap package-marker detection. Used by BOTH the hermetic-routing
    decision and the executor's runbook selection so they can never disagree."""
    if ctx.args.kind != "auto":
        return ctx.args.kind
    from ..qa.config import SutConfig

    if isinstance(config, SutConfig) and config.kind in ("web", "ext", "backend", "bot"):
        return config.kind
    return _detect_kind(sut_path)


def _run_bot_hermetic(
    ctx: ModeContext, sut_path: Path, suites: list[Path], *, bot_config, exit_blocked: int,
) -> int:
    """Drive the bot Tier-1 HERMETIC harness: start the fake Telegram, boot the SUT bot against
    it, inject/capture per case, classify, and map the verdict to an exit code — the SAME
    report-only verdict->exit mapping the executor uses (a found bug is report-only; only a
    BLOCKED bring-up / the --strict finding flip are non-zero).

    Isolation: like every other kind, the bot boots inside an isolated ``git worktree`` of the
    SUT by default (so it sees the committed tree, not the dirty working tree); ``--in-place``
    boots it in the SUT directly. The hermetic driver itself owns its fake + bot teardown
    (try/finally inside ``run_hermetic_bot_test``), so no env handle is needed."""
    import sys

    from ..qa.executor import (
        DirtyInPlaceError,
        SutIsolationError,
        has_uncommitted_changes,
        is_git_worktree,
        parse_qa_results,
        verdict_to_exit_code,
    )
    from ..qa.suites import load_suites_text

    if ctx.args.max_cases is not None and ctx.args.max_cases < 0:
        print("[review-cli] qa: --max-cases must be >= 0 (got "
              f"{ctx.args.max_cases}); 0 means 'no cap'.", file=sys.stderr, flush=True)
        return 2
    strict = bool(getattr(ctx.args, "strict", False))
    report_path = _report_path(ctx, sut_path)

    # TIER-2 LIVE branch — short-circuited BEFORE the Tier-1 suite-load. A `driver: mtproto` bot
    # block drives a REAL test Telegram account, gated behind test-account creds. When the creds
    # gate is not satisfied the run SKIPs LOUD (a controlled BLOCKED naming the exact missing
    # creds); when it is, the live driver is invoked (today it raises LiveTierUnavailable →
    # BLOCKED, the live run is tracked in #82). The live path NEVER reads the Tier-1 suite, so it
    # is gated ahead of load_suites_text — otherwise a suite-stage failure would be mis-attributed
    # to a live run that was going to BLOCK on creds regardless. Either way the run NEVER silently
    # falls through to the un-caged executor or fakes a pass.
    if bot_config.is_live:
        return _run_bot_live(report_path, sut_path, strict=strict, exit_blocked=exit_blocked,
                             in_place=ctx.args.in_place)

    max_cases = ctx.args.max_cases if ctx.args.max_cases and ctx.args.max_cases > 0 else None
    suite_text = load_suites_text(suites, max_cases=max_cases)

    print(
        f"[review-cli] qa: testing BOT SUT {sut_path} (kind=bot, driver=hermetic-mock, "
        f"isolation={'in-place' if ctx.args.in_place else 'worktree'}, "
        f"cases<= {ctx.args.max_cases or 'all'}). Report -> {report_path}",
        file=sys.stderr, flush=True,
    )
    if not ctx.args.in_place and is_git_worktree(sut_path) and has_uncommitted_changes(sut_path):
        print("[review-cli] qa: WARNING — the SUT has uncommitted changes, but the default "
              "isolated worktree boots the bot from committed HEAD, not your working-tree "
              "edits. Commit/stash, or use --in-place to boot the working tree.",
              file=sys.stderr, flush=True)

    try:
        transcript = _drive_bot_in_isolation(
            sut_path=sut_path, suite_text=suite_text, bot_config=bot_config,
            in_place=ctx.args.in_place, exit_blocked=exit_blocked,
        )
    except DirtyInPlaceError as exc:
        print(f"[review-cli] qa: {exc}", file=sys.stderr, flush=True)
        return 2
    except SutIsolationError as exc:
        print(f"[review-cli] qa: {exc}", file=sys.stderr, flush=True)
        return exit_blocked

    _write_bot_report(report_path, transcript, sut_path=sut_path, in_place=ctx.args.in_place)
    verdict, findings, max_sev, cases = parse_qa_results(transcript)
    print(
        f"[review-cli] qa: VERDICT={verdict} findings={findings}"
        f"{f' (worst {max_sev})' if max_sev else ''}; backend=hermetic-bot; "
        f"report={report_path}",
        file=sys.stderr, flush=True,
    )
    return verdict_to_exit_code(verdict, findings=findings, strict=strict, exit_blocked=exit_blocked)


def _drive_bot_in_isolation(
    *, sut_path: Path, suite_text: str, bot_config, in_place: bool, exit_blocked: int,
) -> str:
    """Run the hermetic bot test in the SUT (``--in-place``) or an isolated worktree (default).
    Refuses ``--in-place`` over a dirty tree (an un-caged-equivalent runaway guard is moot here
    since the driver only spawns the configured bot, but a dirty in-place run still boots
    against the user's uncommitted state surprisingly — keep it consistent with the executor's
    refusal). Returns the ``## QA RESULTS`` transcript."""
    from ..qa.bot_driver import run_hermetic_bot_test
    from ..qa.executor import IsolatedSut, _guard_in_place

    if in_place:
        _guard_in_place(backend="hermetic-bot", in_place=True, sut_path=sut_path)
        return run_hermetic_bot_test(
            suite_text=suite_text, bot_config=bot_config, cwd=sut_path, sut_path=sut_path,
            exit_boot_failed=exit_blocked,
        )
    with IsolatedSut(sut_path) as worktree:
        return run_hermetic_bot_test(
            suite_text=suite_text, bot_config=bot_config, cwd=worktree, sut_path=sut_path,
            exit_boot_failed=exit_blocked,
        )


def _write_bot_report(
    report_path: Path, transcript: str, *, sut_path: Path, in_place: bool,
    backend: str = "hermetic-bot",
) -> None:
    """Persist a run's ``## QA RESULTS`` transcript to ``--report`` (0600, mirroring the
    executor's report write). Despite the ``_bot_`` name this is the shared report writer for
    every kind; ``backend`` is the footer's backend label and EACH caller passes its own so the
    saved footer matches the run's stderr ``VERDICT`` line: bot Tier-1 keeps the default
    ``hermetic-bot``, web Tier-1 passes ``playwright-web``, ext Tier-1 passes ``vscode-ext``, and
    the Tier-2 LIVE paths pass ``bot-live`` / ``web-live`` / ``ext-live`` — so no web/ext or live
    report is ever mislabelled ``hermetic-bot``. Best-effort: a write failure is surfaced but
    never fails the run."""
    import os
    import sys

    footer = (
        f"\n\n---\n[review-cli qa] SUT: {sut_path}   backend: {backend}   "
        f"isolation: {'in-place' if in_place else 'worktree'}\n"
    )
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(report_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(transcript + footer)
    except OSError as exc:
        print(f"[review-cli] qa: could not write bot report to {report_path}: {exc}",
              file=sys.stderr, flush=True)


def _run_web_deterministic(
    ctx: ModeContext, sut_path: Path, suites: list[Path], *, web_config, exit_blocked: int,
) -> int:
    """Drive the web Tier-1 DETERMINISTIC harness: bring the app's dev server up, health-gate it
    reachable, open a headless Chromium page, run each case's goto/click/fill + DOM assertions,
    classify, and map the verdict to an exit code — the SAME report-only verdict->exit mapping the
    executor + bot driver use (a found bug is report-only; only a BLOCKED bring-up / the --strict
    finding flip are non-zero).

    Playwright is gated: when ``REVIEW_QA_PLAYWRIGHT`` is off (the default) or the browser is not
    installed, the run is a controlled BLOCKED with the exact install command — never a crash. The
    PURE logic (parser, action mapping, QA RESULTS emission) is unit-tested with no browser via a
    fake page, so it still gates in normal CI.

    Isolation: like every other kind, the dev server boots inside an isolated ``git worktree`` of
    the SUT by default (so it serves the committed tree); ``--in-place`` boots it in the SUT
    directly. The harness owns its server + browser teardown (try/finally), so no env handle is
    needed."""
    import sys

    from ..qa.executor import (
        DirtyInPlaceError,
        SutIsolationError,
        has_uncommitted_changes,
        is_git_worktree,
        parse_qa_results,
        verdict_to_exit_code,
    )
    from ..qa.suites import load_suites_text
    from ..qa.web_harness import playwright_available

    if ctx.args.max_cases is not None and ctx.args.max_cases < 0:
        print("[review-cli] qa: --max-cases must be >= 0 (got "
              f"{ctx.args.max_cases}); 0 means 'no cap'.", file=sys.stderr, flush=True)
        return 2
    strict = bool(getattr(ctx.args, "strict", False))
    report_path = _report_path(ctx, sut_path)

    # TIER-2 LIVE branch — short-circuited BEFORE the Tier-1 suite-load. A `driver: agent-browser`
    # web block drives a REAL browser against a deployed test site, gated behind REVIEW_QA_WEB_LIVE
    # + REVIEW_QA_WEB_BASE_URL. When the gate is not satisfied the run SKIPs LOUD (a controlled
    # BLOCKED naming the exact missing creds); when it is, the live driver is invoked (today it
    # raises LiveTierUnavailable → BLOCKED, the live run is tracked in #82). The live path NEVER
    # reads the Tier-1 suite, so it is gated ahead of load_suites_text — otherwise a suite-stage
    # failure would be mis-attributed to a live run that BLOCKs on creds regardless. It NEVER falls
    # through to the un-caged executor or fakes a pass.
    if web_config.is_live:
        return _run_web_live(report_path, sut_path, web_config, strict=strict,
                             exit_blocked=exit_blocked, in_place=ctx.args.in_place)

    max_cases = ctx.args.max_cases if ctx.args.max_cases and ctx.args.max_cases > 0 else None
    suite_text = load_suites_text(suites, max_cases=max_cases)

    print(
        f"[review-cli] qa: testing WEB SUT {sut_path} (kind=web, driver=playwright, "
        f"base_url={web_config.base_url}, "
        f"isolation={'in-place' if ctx.args.in_place else 'worktree'}, "
        f"cases<= {ctx.args.max_cases or 'all'}). Report -> {report_path}",
        file=sys.stderr, flush=True,
    )
    if not ctx.args.in_place and is_git_worktree(sut_path) and has_uncommitted_changes(sut_path):
        print("[review-cli] qa: WARNING — the SUT has uncommitted changes, but the default "
              "isolated worktree serves committed HEAD, not your working-tree edits. "
              "Commit/stash, or use --in-place to serve the working tree.",
              file=sys.stderr, flush=True)

    # Gate the heavy Playwright dependency up front so an un-installed browser is a clear,
    # actionable BLOCKED (with the install command) rather than a crash mid-run.
    available, reason = playwright_available()
    if not available:
        return _emit_web_blocked(report_path, sut_path, web_config, reason, exit_blocked,
                                 strict=strict, in_place=ctx.args.in_place)

    try:
        transcript = _drive_web_in_isolation(
            sut_path=sut_path, suite_text=suite_text, web_config=web_config,
            report_path=report_path, in_place=ctx.args.in_place, exit_blocked=exit_blocked,
        )
    except DirtyInPlaceError as exc:
        print(f"[review-cli] qa: {exc}", file=sys.stderr, flush=True)
        return 2
    except SutIsolationError as exc:
        print(f"[review-cli] qa: {exc}", file=sys.stderr, flush=True)
        return exit_blocked

    _write_bot_report(report_path, transcript, sut_path=sut_path, in_place=ctx.args.in_place,
                      backend="playwright-web")
    verdict, findings, max_sev, cases = parse_qa_results(transcript)
    print(
        f"[review-cli] qa: VERDICT={verdict} findings={findings}"
        f"{f' (worst {max_sev})' if max_sev else ''}; backend=playwright-web; "
        f"report={report_path}",
        file=sys.stderr, flush=True,
    )
    return verdict_to_exit_code(verdict, findings=findings, strict=strict, exit_blocked=exit_blocked)


def _emit_web_blocked(
    report_path: Path, sut_path: Path, web_config, reason: str, exit_blocked: int,
    *, strict: bool, in_place: bool,
) -> int:
    """Emit a controlled BLOCKED for a web run that cannot use a real browser (Playwright off /
    not installed). Writes the same ``## QA RESULTS`` contract (so the report is consistent) and
    maps BLOCKED to the boot-failed exit class. NOT a crash — the operator gets the install
    command and a stable exit code."""
    import sys

    from ..qa.executor import parse_qa_results, verdict_to_exit_code
    from ..qa.web_driver import WebRunResult

    print(f"[review-cli] qa: web run BLOCKED — {reason}", file=sys.stderr, flush=True)
    transcript = WebRunResult(blocked_reason=reason).to_qa_results(
        sut_path=sut_path, base_url=web_config.base_url)
    _write_bot_report(report_path, transcript, sut_path=sut_path, in_place=in_place,
                      backend="playwright-web")
    verdict, findings, _max_sev, _cases = parse_qa_results(transcript)
    return verdict_to_exit_code(verdict, findings=findings, strict=strict, exit_blocked=exit_blocked)


# --- TIER-2 LIVE entry points (gated; SKIP LOUD when creds absent; live run = #82) ------------
def _live_blocked_reason(kind: str, exit_blocked: int) -> str:
    """The BLOCKED reason for a ``tier: live`` run of ``kind``: the live-tier gate's exact
    missing-creds message when the gate is not satisfied, else the live-driver's own
    not-yet-implemented message (the gate passed — creds are present — but the live run lands under
    #82). Either way a real, actionable reason; never a silent pass."""
    from ..qa.live_tier import LiveTierUnavailable, live_gate_for, live_driver_for

    gate = live_gate_for(kind)
    if not gate.ok:
        return gate.reason
    try:
        live_driver_for(kind, exit_blocked=exit_blocked).connect()
    except LiveTierUnavailable as exc:
        return str(exc)
    # connect() not raising would mean the live run is implemented; until #82 lands this is
    # unreachable, but guard it so a future live impl that forgets to return a transcript here
    # fails loud rather than silently passing.
    return (
        f"the {kind} Tier-2 live run reported available but produced no transcript — the live "
        "run wiring is incomplete (#82)."
    )


def _run_bot_live(
    report_path: Path, sut_path: Path, *, strict: bool, exit_blocked: int, in_place: bool,
) -> int:
    """Run (or SKIP-LOUD) the bot Tier-2 LIVE tier: real-Telegram MTProto. Emits a controlled
    BLOCKED — the creds gate's exact missing-creds message, or the live-driver's #82 message when
    creds are present — in the same ``## QA RESULTS`` contract the hermetic path uses."""
    import sys

    from ..qa.bot_driver import BotRunResult
    from ..qa.executor import parse_qa_results, verdict_to_exit_code

    reason = _live_blocked_reason("bot", exit_blocked)
    print(f"[review-cli] qa: bot Tier-2 LIVE run BLOCKED — {reason}", file=sys.stderr, flush=True)
    transcript = BotRunResult(blocked_reason=reason).to_qa_results(sut_path=sut_path)
    _write_bot_report(report_path, transcript, sut_path=sut_path, in_place=in_place,
                      backend="bot-live")
    verdict, findings, _max_sev, _cases = parse_qa_results(transcript)
    return verdict_to_exit_code(verdict, findings=findings, strict=strict, exit_blocked=exit_blocked)


def _run_web_live(
    report_path: Path, sut_path: Path, web_config, *, strict: bool, exit_blocked: int,
    in_place: bool,
) -> int:
    """Run (or SKIP-LOUD) the web Tier-2 LIVE tier: a real browser against a deployed test site.
    Emits a controlled BLOCKED — the gate's missing-creds message, or the #82 message when creds
    are present — in the same ``## QA RESULTS`` contract the deterministic path uses."""
    import sys

    from ..qa.executor import parse_qa_results, verdict_to_exit_code
    from ..qa.web_driver import WebRunResult

    reason = _live_blocked_reason("web", exit_blocked)
    print(f"[review-cli] qa: web Tier-2 LIVE run BLOCKED — {reason}", file=sys.stderr, flush=True)
    # The LIVE web target lives in REVIEW_QA_WEB_BASE_URL (the live block legitimately omits the
    # Tier-1 base_url), so report THAT as the target, falling back to the config's base_url.
    import os

    live_url = os.environ.get("REVIEW_QA_WEB_BASE_URL", "").strip() or web_config.base_url
    transcript = WebRunResult(blocked_reason=reason).to_qa_results(
        sut_path=sut_path, base_url=live_url)
    _write_bot_report(report_path, transcript, sut_path=sut_path, in_place=in_place,
                      backend="web-live")
    verdict, findings, _max_sev, _cases = parse_qa_results(transcript)
    return verdict_to_exit_code(verdict, findings=findings, strict=strict, exit_blocked=exit_blocked)


def _run_ext_live(
    report_path: Path, sut_path: Path, ext_config, *, strict: bool, exit_blocked: int,
    in_place: bool,
) -> int:
    """Run (or SKIP-LOUD) the ext Tier-2 LIVE tier: real VS Code + window-screenshot visual
    diffing. Emits a controlled BLOCKED — the gate's missing-creds/infra message, or the #82
    message when present — in the same ``## QA RESULTS`` contract the deterministic path uses."""
    import sys

    from ..qa.executor import parse_qa_results, verdict_to_exit_code
    from ..qa.ext_driver import ExtRunResult

    reason = _live_blocked_reason("ext", exit_blocked)
    print(f"[review-cli] qa: ext Tier-2 LIVE run BLOCKED — {reason}", file=sys.stderr, flush=True)
    transcript = ExtRunResult(blocked_reason=reason).to_qa_results(
        sut_path=sut_path, extension_path=ext_config.extension_path)
    _write_bot_report(report_path, transcript, sut_path=sut_path, in_place=in_place,
                      backend="ext-live")
    verdict, findings, _max_sev, _cases = parse_qa_results(transcript)
    return verdict_to_exit_code(verdict, findings=findings, strict=strict, exit_blocked=exit_blocked)


def _drive_web_in_isolation(
    *, sut_path: Path, suite_text: str, web_config, report_path: Path, in_place: bool,
    exit_blocked: int,
) -> str:
    """Run the deterministic web test in the SUT (``--in-place``) or an isolated worktree
    (default). Refuses ``--in-place`` over a dirty tree (consistent with the bot/executor refusal
    — the dev server would serve the user's uncommitted state surprisingly). Returns the
    ``## QA RESULTS`` transcript."""
    from ..qa.executor import IsolatedSut, _guard_in_place

    out_dir = _web_out_dir(report_path)
    if in_place:
        _guard_in_place(backend="playwright-web", in_place=True, sut_path=sut_path)
        return _bring_up_and_drive_web(
            cwd=sut_path, sut_path=sut_path, suite_text=suite_text, web_config=web_config,
            out_dir=out_dir, exit_blocked=exit_blocked,
        )
    with IsolatedSut(sut_path) as worktree:
        return _bring_up_and_drive_web(
            cwd=worktree, sut_path=sut_path, suite_text=suite_text, web_config=web_config,
            out_dir=out_dir, exit_blocked=exit_blocked,
        )


def _bring_up_and_drive_web(
    *, cwd: Path, sut_path: Path, suite_text: str, web_config, out_dir: Path | None,
    exit_blocked: int,
) -> str:
    """Bring the dev server up (when a ``command`` is declared), ALWAYS health-gate the target
    reachable, then drive the suite against ``base_url`` in a headless browser — with GUARANTEED
    server teardown. A boot failure / an unreachable target yields a BLOCKED transcript (never a
    traceback, never a silent report-only FAIL on a down stage); the browser session itself is
    owned by ``run_web_test``'s page factory (also try/finally)."""
    import sys

    from ..qa.web_driver import WebRunResult, run_web_test
    from ..qa.web_harness import WebHarnessError, boot_web_server, wait_until_reachable

    server = None
    try:
        if web_config.command:
            try:
                server = boot_web_server(
                    command=list(web_config.command), cwd=cwd, extra_env=web_config.env,
                    exit_boot_failed=exit_blocked,
                )
            except WebHarnessError as exc:
                return WebRunResult(blocked_reason=str(exc)).to_qa_results(
                    sut_path=sut_path, base_url=web_config.base_url)
        # ALWAYS health-gate before driving — for BOTH the just-booted dev server AND the
        # command-omitted "already-running base_url" path (README). A down target must BLOCK
        # (infra failure, exit 8), not become a report-only navigation FAIL (exit 0) that callers
        # can't tell from a found bug (codex PR review P1). When a server was booted, a crash
        # tail is attached; for an already-running target the message names it as unreachable.
        ready_url = web_config.base_url + web_config.ready_path
        if not wait_until_reachable(
            ready_url, timeout_s=web_config.ready_timeout_s, server=server,
        ):
            return WebRunResult(
                blocked_reason=_unreachable_reason(ready_url, web_config, server),
            ).to_qa_results(sut_path=sut_path, base_url=web_config.base_url)
        return run_web_test(
            suite_text=suite_text, base_url=web_config.base_url, sut_path=sut_path,
            out_dir=out_dir,
        )
    except Exception as exc:  # noqa: BLE001 — any unexpected error becomes a controlled BLOCKED
        print(f"[review-cli] qa: web harness error: {exc}", file=sys.stderr, flush=True)
        return WebRunResult(blocked_reason=f"unexpected web harness error: {exc}").to_qa_results(
            sut_path=sut_path, base_url=web_config.base_url)
    finally:
        if server is not None:
            server.reap()


def _unreachable_reason(ready_url: str, web_config, server) -> str:
    """The BLOCKED reason for a target that never answered the health gate. A just-booted dev
    server attaches its output tail (so a boot crash is diagnosable); an already-running target
    (no command) is named as simply unreachable — the stage/dev server the SUT pointed at is
    down, which is infra, not a bug in the app."""
    timeout = web_config.ready_timeout_s
    if server is not None:
        return (
            f"the web dev server did not become reachable at {ready_url!r} within {timeout}s "
            f"(it may have crashed on boot). Output tail:\n{server.output_tail()}"
        )
    return (
        f"the already-running web target at {ready_url!r} (no sut.web.command — qa did NOT boot "
        f"it) did not answer within {timeout}s. The stage / dev server the SUT points at is "
        "down or unreachable; bring it up (or set sut.web.command so qa boots it) and re-run."
    )


def _web_out_dir(report_path: Path) -> Path:
    """The directory FAIL screenshots are written to: a sibling ``<report-stem>-out/`` of the
    report file, OUTSIDE the SUT tree (the report already lives in the log dir by default, so a
    clean checkout stays clean). Created lazily by the screenshot writer."""
    return report_path.with_name(report_path.stem + "-out")


def _run_ext_deterministic(
    ctx: ModeContext, sut_path: Path, suites: list[Path], *, ext_config, exit_blocked: int,
) -> int:
    """Drive the ext Tier-1 DETERMINISTIC harness: launch an isolated VS Code with the extension
    on ``--extensionDevelopmentPath``, connect over CDP, run each case's commands/opens + window
    assertions (notification / editor text / webview body), classify, and map the verdict to an
    exit code — the SAME report-only verdict->exit mapping the executor + bot + web drivers use (a
    found bug is report-only; only a BLOCKED launch / the --strict finding flip are non-zero).

    The VS Code launch is gated: when ``REVIEW_QA_VSCODE`` is off (the default) or no node/tsx
    runtime is present, the run is a controlled BLOCKED with the exact enable/install command —
    never a crash. The PURE logic (parser, action mapping, QA RESULTS emission) is unit-tested with
    NO VS Code via a fake automation, so it still gates in normal CI.

    Isolation: like every other kind, VS Code opens an isolated ``git worktree`` of the SUT by
    default (so it loads the committed extension, not the dirty working tree); ``--in-place``
    launches against the SUT directly. The harness owns its VS Code + runner teardown (try/finally),
    so no env handle is needed."""
    import sys

    from ..qa.executor import (
        DirtyInPlaceError,
        SutIsolationError,
        has_uncommitted_changes,
        is_git_worktree,
        parse_qa_results,
        verdict_to_exit_code,
    )
    from ..qa.ext_harness import vscode_available
    from ..qa.suites import load_suites_text

    if ctx.args.max_cases is not None and ctx.args.max_cases < 0:
        print("[review-cli] qa: --max-cases must be >= 0 (got "
              f"{ctx.args.max_cases}); 0 means 'no cap'.", file=sys.stderr, flush=True)
        return 2
    strict = bool(getattr(ctx.args, "strict", False))
    report_path = _report_path(ctx, sut_path)

    # TIER-2 LIVE branch — short-circuited BEFORE the Tier-1 suite-load. A `driver: vscode-visual`
    # ext block launches a REAL VS Code and DIFFs window screenshots against a baseline (issue
    # #82's core ask), gated behind REVIEW_QA_EXT_LIVE + the VS Code gate + a baseline dir. When
    # the gate is not satisfied the run SKIPs LOUD (a controlled BLOCKED naming the exact missing
    # creds/infra); when it is, the live driver is invoked (today it raises LiveTierUnavailable →
    # BLOCKED, the live run is tracked in #82). The live path NEVER reads the Tier-1 suite, so it
    # is gated ahead of load_suites_text — otherwise a suite-stage failure would be mis-attributed
    # to a live run that BLOCKs on creds regardless. It NEVER falls through to the un-caged
    # executor or fakes a pass.
    if ext_config.is_live:
        return _run_ext_live(report_path, sut_path, ext_config, strict=strict,
                             exit_blocked=exit_blocked, in_place=ctx.args.in_place)

    max_cases = ctx.args.max_cases if ctx.args.max_cases and ctx.args.max_cases > 0 else None
    suite_text = load_suites_text(suites, max_cases=max_cases)

    print(
        f"[review-cli] qa: testing EXT SUT {sut_path} (kind=ext, driver=vscode, "
        f"extension_path={ext_config.extension_path}, "
        f"isolation={'in-place' if ctx.args.in_place else 'worktree'}, "
        f"cases<= {ctx.args.max_cases or 'all'}). Report -> {report_path}",
        file=sys.stderr, flush=True,
    )
    if not ctx.args.in_place and is_git_worktree(sut_path) and has_uncommitted_changes(sut_path):
        print("[review-cli] qa: WARNING — the SUT has uncommitted changes, but the default "
              "isolated worktree loads the extension from committed HEAD, not your working-tree "
              "edits. Commit/stash, or use --in-place to test the working tree.",
              file=sys.stderr, flush=True)

    # Gate the heavy VS Code launch up front so an un-enabled / un-installed runtime is a clear,
    # actionable BLOCKED (with the enable/install command) rather than a crash mid-run.
    available, reason = vscode_available()
    if not available:
        return _emit_ext_blocked(report_path, sut_path, ext_config, reason, exit_blocked,
                                 strict=strict, in_place=ctx.args.in_place)

    try:
        transcript = _drive_ext_in_isolation(
            sut_path=sut_path, suite_text=suite_text, ext_config=ext_config,
            report_path=report_path, in_place=ctx.args.in_place, exit_blocked=exit_blocked,
        )
    except DirtyInPlaceError as exc:
        print(f"[review-cli] qa: {exc}", file=sys.stderr, flush=True)
        return 2
    except SutIsolationError as exc:
        print(f"[review-cli] qa: {exc}", file=sys.stderr, flush=True)
        return exit_blocked

    _write_bot_report(report_path, transcript, sut_path=sut_path, in_place=ctx.args.in_place,
                      backend="vscode-ext")
    verdict, findings, max_sev, cases = parse_qa_results(transcript)
    print(
        f"[review-cli] qa: VERDICT={verdict} findings={findings}"
        f"{f' (worst {max_sev})' if max_sev else ''}; backend=vscode-ext; "
        f"report={report_path}",
        file=sys.stderr, flush=True,
    )
    return verdict_to_exit_code(verdict, findings=findings, strict=strict, exit_blocked=exit_blocked)


def _emit_ext_blocked(
    report_path: Path, sut_path: Path, ext_config, reason: str, exit_blocked: int,
    *, strict: bool, in_place: bool,
) -> int:
    """Emit a controlled BLOCKED for an ext run that cannot launch a real VS Code (REVIEW_QA_VSCODE
    off / no node runtime). Writes the same ``## QA RESULTS`` contract (so the report is consistent)
    and maps BLOCKED to the boot-failed exit class. NOT a crash — the operator gets the enable/
    install command and a stable exit code."""
    import sys

    from ..qa.executor import parse_qa_results, verdict_to_exit_code
    from ..qa.ext_driver import ExtRunResult

    print(f"[review-cli] qa: ext run BLOCKED — {reason}", file=sys.stderr, flush=True)
    transcript = ExtRunResult(blocked_reason=reason).to_qa_results(
        sut_path=sut_path, extension_path=ext_config.extension_path)
    _write_bot_report(report_path, transcript, sut_path=sut_path, in_place=in_place,
                      backend="vscode-ext")
    verdict, findings, _max_sev, _cases = parse_qa_results(transcript)
    return verdict_to_exit_code(verdict, findings=findings, strict=strict, exit_blocked=exit_blocked)


def _drive_ext_in_isolation(
    *, sut_path: Path, suite_text: str, ext_config, report_path: Path, in_place: bool,
    exit_blocked: int,
) -> str:
    """Run the deterministic ext test in the SUT (``--in-place``) or an isolated worktree
    (default). Refuses ``--in-place`` over a dirty tree (consistent with the bot/web/executor
    refusal — VS Code would load the user's uncommitted extension surprisingly). Returns the
    ``## QA RESULTS`` transcript.

    NOTE on the extension_path: it is resolved RELATIVE to the run cwd (the worktree or the SUT),
    so the isolated run loads the committed extension from the worktree, not the user's checkout —
    the same isolation discipline the un-caged executor uses for the agent's blast radius."""
    from ..qa.executor import IsolatedSut, _guard_in_place

    out_dir = _web_out_dir(report_path)
    if in_place:
        _guard_in_place(backend="vscode-ext", in_place=True, sut_path=sut_path)
        return _launch_and_drive_ext(
            cwd=sut_path, sut_path=sut_path, suite_text=suite_text, ext_config=ext_config,
            out_dir=out_dir, exit_blocked=exit_blocked, in_place=True,
        )
    with IsolatedSut(sut_path) as worktree:
        return _launch_and_drive_ext(
            cwd=worktree, sut_path=sut_path, suite_text=suite_text, ext_config=ext_config,
            out_dir=out_dir, exit_blocked=exit_blocked, in_place=False,
        )


def _path_escapes(cwd: Path, raw: str) -> bool:
    """True iff resolving ``raw`` against ``cwd`` lands OUTSIDE ``cwd`` — i.e. it breaks the
    worktree isolation. Catches BOTH an absolute path (``cwd / abs`` drops ``cwd``) AND a ``..``
    traversal (``cwd / '../x'`` climbs out), so the check matches the real escape set, not just
    the absolute case (review-cli#75)."""
    try:
        resolved = (cwd / raw).resolve()
        root = cwd.resolve()
    except (OSError, ValueError):
        return False
    return resolved != root and root not in resolved.parents


def _warn_abs_path_escapes_worktree(cwd: Path, in_place: bool, ext_config) -> None:
    """Warn when a ``sut.ext`` ``extension_path``/``workspace`` silently escapes the isolated
    worktree — an ABSOLUTE path (``cwd / abs`` drops ``cwd``) OR a ``..`` traversal out of it.
    Either resolves OUTSIDE the worktree even though the docstring promises "relative to the run
    cwd", so VS Code would load the extension from the user's real checkout, not the isolated copy.
    Only meaningful for the default (worktree) run; ``--in-place`` already runs in the SUT, so such
    a path there is expected (review-cli#75)."""
    import sys

    if in_place:
        return
    for label, raw in (("extension_path", ext_config.extension_path),
                       ("workspace", ext_config.workspace)):
        if raw and _path_escapes(cwd, raw):
            print(
                f"[review-cli] qa: WARNING — sut.ext {label}={raw!r} resolves OUTSIDE the isolated "
                f"worktree ({cwd}), so the ext run is NOT isolated (VS Code loads it from the real "
                "path, not the worktree copy). Use a path relative to the SUT that stays inside it, "
                "or pass --in-place if you intend to run against the real checkout.",
                file=sys.stderr, flush=True,
            )


def _launch_and_drive_ext(
    *, cwd: Path, sut_path: Path, suite_text: str, ext_config, out_dir: Path | None,
    exit_blocked: int, in_place: bool = False,
) -> str:
    """Launch the isolated VS Code (extension on ``--extensionDevelopmentPath``), drive the suite,
    and return the ``## QA RESULTS`` transcript — with GUARANTEED VS Code teardown (the session
    context manager). A launch failure yields a BLOCKED transcript (never a traceback). The
    extension_path / workspace are resolved against the actual run cwd so the isolated run loads
    the committed extension; an ABSOLUTE config path escapes that isolation and is warned about."""
    import sys

    from ..qa.ext_driver import ExtRunResult, run_ext_test

    _warn_abs_path_escapes_worktree(cwd, in_place, ext_config)
    workspace = (cwd / ext_config.workspace).resolve()
    extension_path = str((cwd / ext_config.extension_path).resolve())
    try:
        return run_ext_test(
            suite_text=suite_text, extension_path=extension_path, sut_path=sut_path,
            out_dir=out_dir, automation_factory=_ext_automation_factory(
                extension_path=extension_path, workspace=workspace, exit_blocked=exit_blocked,
                extra_env=dict(ext_config.env)),
        )
    except Exception as exc:  # noqa: BLE001 — any unexpected error becomes a controlled BLOCKED
        print(f"[review-cli] qa: ext harness error: {exc}", file=sys.stderr, flush=True)
        return ExtRunResult(blocked_reason=f"unexpected ext harness error: {exc}").to_qa_results(
            sut_path=sut_path, extension_path=ext_config.extension_path)


def _ext_automation_factory(
    *, extension_path: str, workspace: Path, exit_blocked: int,
    extra_env: dict[str, str] | None = None,
):
    """The real isolated-VS-Code automation context manager bound to this run's resolved
    extension_path + workspace. Split out so the run cwd (worktree / SUT) is threaded into the VS
    Code launch, mirroring how the executor builds its prompt at the actual run cwd. ``extra_env``
    is the SUT's declared NON-SECRET sut.ext.env, passed through so the extension under test sees
    its configured variables (codex PR review P2)."""
    from ..qa.ext_harness import vscode_session

    return vscode_session(
        extension_path=extension_path, workspace=workspace, exit_blocked=exit_blocked,
        extra_env=extra_env)


def _run_with_env(
    ctx: ModeContext, sut_path: Path, suites: list[Path], *,
    exit_blocked: int, exit_no_env: int, exit_unhealthy: int,
) -> int:
    """Phase 3 wrapper: bring the SUT env up (when one is declared), run the executor against
    it, and GUARANTEE teardown of what we brought up on every exit path.

    The env layer plugs in BEFORE the executor: ``bring_up_env`` does the deterministic
    detect/reuse/bring-up/health-gate, returning an ``EnvHandle`` whose ``tear_down`` reaps
    EXACTLY what this run owns (a no-op for a reused stage). The try/finally here is the
    NORMAL teardown path; the env module ALSO registers an atexit/signal hook so an abnormal
    exit (Ctrl-C, crash, backstop fire) still reaps a daemonized container backstop cannot
    reach. A SUT that needs no env (no stage / no hook / no config) short-circuits via
    ``_env_declared`` ABOVE — it never enters bring-up, the agent does its own local bring-up
    per the runbook (the Phase-2 behavior)."""
    import sys

    from ..qa.config import QaConfigError, load_qa_config
    from ..qa.env import EnvError, EnvMode, bring_up_env

    try:
        config = load_qa_config(sut_path, ctx.args.config)
    except QaConfigError as exc:
        print(f"[review-cli] qa: {exc}", file=sys.stderr, flush=True)
        return 2

    if not _env_declared(sut_path, config, ctx.args.stage_url):
        # No env declared anywhere — the agent does its own local bring-up per the runbook
        # (the Phase-2 behavior). Nothing for the deterministic env layer to own.
        return _run_executor(ctx, sut_path, suites, exit_blocked=exit_blocked, endpoints={})

    # --keep-env (the flag) OR sut.teardown.keep_on_failure (the config) keeps an unhealthy
    # env up for triage. Both are honored — the config docstring + the --keep-env help both
    # promise "or the config field", so a SUT declaring keep_on_failure: true must take effect
    # without the flag (review finding: the config field was dead).
    keep_env = bool(ctx.args.keep_env) or (config is not None and config.teardown.keep_on_failure)
    try:
        handle = bring_up_env(
            sut_path=sut_path, config=config, stage_url_override=ctx.args.stage_url,
            exit_no_env=exit_no_env, exit_unhealthy=exit_unhealthy,
            keep_env=keep_env,
        )
    except EnvError as exc:
        print(f"[review-cli] qa: {exc}", file=sys.stderr, flush=True)
        return exc.exit_code

    # REUSED_STAGE → the agent tests against the already-up stage; HOOK/COMPOSE → the env is
    # already up, the agent must NOT boot a second copy ("none"); NONE → no env was owned (a
    # stale ambient stage var that fell through), so the agent does its own Phase-2 local
    # bring-up ("local"), exactly as if no env had been declared.
    if handle.mode == EnvMode.REUSED_STAGE:
        bring_up = "stage"
    elif handle.mode == EnvMode.NONE:
        bring_up = "local"
    else:
        bring_up = "none"
    try:
        return _run_executor(
            ctx, sut_path, suites, exit_blocked=exit_blocked,
            endpoints=handle.endpoints, bring_up=bring_up,
        )
    finally:
        # GUARANTEED teardown on EVERY exit path (return, finding, exception, timeout). Only
        # tears down what THIS run brought up — a reused stage's teardown is a no-op (ownership
        # rule). ``tear_down`` self-unregisters from the global atexit registry and is
        # idempotent, so the atexit/signal hook's call after this one is a no-op.
        handle.tear_down()


def _env_declared(sut_path: Path, config: object | None, stage_url: str | None) -> bool:
    """True when SOME SUT env is declared and the deterministic env layer should own bring-up:
    an explicit ``--stage-url``, a ``qa.yaml`` config (stage or bringup), or a ``setup.sh``
    hook. False means "no env declared" — the agent does its own local bring-up (Phase 2), so
    no container/dev-server is owned by qa. ``REVIEW_QA_STAGE_URL`` in the environment also
    counts (the env layer honors it)."""
    import os

    from ..qa.config import SutConfig
    from ..qa.env import _find_setup_hook

    if stage_url or os.environ.get("REVIEW_QA_STAGE_URL", "").strip():
        return True
    if isinstance(config, SutConfig) and (config.stage is not None or config.bringup is not None):
        return True
    return _find_setup_hook(sut_path) is not None


def _run_executor(
    ctx: ModeContext, sut_path: Path, suites: list[Path], *,
    exit_blocked: int, endpoints: dict | None = None, bring_up: str = "local",
) -> int:
    """Build the tester prompt + run the single-seat write/exec launcher, then map the
    parsed verdict to an exit code. Split from ``_handler`` so the gate stays the first,
    obvious thing the handler does and the (heavier) executor wiring is one call away.

    ``endpoints`` (resolved by the Phase-3 env layer) + ``bring_up`` thread the ALREADY-UP
    env into the tester prompt: a reused stage passes ``bring_up="stage"`` + the stage URL so
    the agent tests AGAINST it rather than booting; a hook/compose env passes ``bring_up="none"``
    (the deterministic layer already brought it up — the agent must NOT boot a second copy).
    No env declared → ``bring_up="local"`` (the agent does its own bring-up, the Phase-2
    behavior)."""
    import sys

    endpoints = endpoints or {}

    # Lazy-imported (heavy: pulls the qa package) so the mode stays import-light and the
    # no-suites gate above never pays for the executor.
    from ..config import _split_models
    from ..qa.executor import (
        DirtyInPlaceError,
        SutIsolationError,
        UnsupportedTesterError,
        build_tester_prompt,
        resolved_tester_backend,
        resolved_tester_model,
        run_tester,
        validate_tester_choice,
        verdict_to_exit_code,
    )
    from ..qa.suites import load_suites_text

    if ctx.args.max_cases is not None and ctx.args.max_cases < 0:
        print(
            f"[review-cli] qa: --max-cases must be >= 0 (got {ctx.args.max_cases}); "
            "0 means 'no cap' (run the full suite).",
            file=sys.stderr, flush=True,
        )
        return 2

    # Reject an explicit -m / REVIEW_QA_TESTER naming a backend qa can't use (gemini, a typo)
    # — a usage error, NOT a silent fall-through to the un-caged claude default (review finding).
    explicit_models = _split_models(getattr(ctx.args, "model", []) or [])
    try:
        validate_tester_choice(explicit_models)
    except UnsupportedTesterError as exc:
        print(f"[review-cli] qa: {exc}", file=sys.stderr, flush=True)
        return 2

    kind = _effective_kind(ctx, sut_path)
    # max_cases == 0 means "no cap" (run all); a positive N caps to the first N cases.
    max_cases = ctx.args.max_cases if ctx.args.max_cases and ctx.args.max_cases > 0 else None
    suites_text = _with_endpoint_note(load_suites_text(suites, max_cases=max_cases),
                                      bring_up=bring_up, endpoints=endpoints)
    strict = bool(getattr(ctx.args, "strict", False))

    # SECURITY: build the prompt at the ACTUAL run cwd, not sut_path. The prompt fences the
    # agent to "ONLY inside `{path}`"; that path must be the worktree (or sut_path under
    # --in-place) the agent actually runs in — building it with the user's real checkout
    # would point the un-caged agent at the real repo by absolute path (review finding). The
    # executor invokes this closure with the resolved cwd after the worktree exists.
    stage_url = endpoints.get("stage") if bring_up == "stage" else None

    def _prompt_builder(run_cwd: Path) -> str:
        return build_tester_prompt(
            kind=kind, suites_text=suites_text, sut_path=run_cwd, bring_up=bring_up,
            stage_url=stage_url, strict=strict, in_place=ctx.args.in_place,
        )

    report_path = _report_path(ctx, sut_path)
    # SINGLE-SEAT: qa runs ONE write/exec tester, not the panel. There is no model fan-out to
    # collapse here — the executor selects exactly ONE backend and ignores --pool / the panel
    # (the spec's single-seat override). The startup log names that ACTUAL backend (claude
    # default / codex), not a model alias, so it matches the outcome (review finding).
    #
    # Backend precedence: REVIEW_QA_TESTER env > an EXPLICIT `-m codex`/`-m claude` > claude
    # default. ``explicit_models`` (validated above) is the RAW `--model` flag split on commas
    # — NOT ``ctx.models`` (the shared DEFAULT panel list whose first entry is codex, which
    # would make bare `review qa` pick codex over the documented claude default — review).
    backend = resolved_tester_backend(explicit_models)
    model = resolved_tester_model(explicit_models)
    env_note = f", env={bring_up}" if bring_up != "local" else ""
    model_note = f":{model}" if model else ""
    print(
        f"[review-cli] qa: testing SUT {sut_path} (kind={kind}, backend={backend}{model_note}, "
        f"isolation={'in-place' if ctx.args.in_place else 'worktree'}{env_note}, cases<= "
        f"{ctx.args.max_cases or 'all'}). Report -> {report_path}",
        file=sys.stderr, flush=True,
    )
    _warn_if_dirty_worktree_run(ctx, sut_path)

    try:
        outcome = run_tester(
            prompt_builder=_prompt_builder, sut_path=sut_path, timeout=ctx.timeout,
            in_place=ctx.args.in_place, report_path=report_path, backend=backend, model=model,
        )
    except DirtyInPlaceError as exc:
        # A user/usage error (you asked for --in-place over a dirty tree), NOT an infra/boot
        # failure — exit 2 (usage), so CI can tell "you refused the dirty in-place run" apart
        # from "the SUT could not be brought up" (exit_blocked).
        print(f"[review-cli] qa: {exc}", file=sys.stderr, flush=True)
        return 2
    except SutIsolationError as exc:
        print(f"[review-cli] qa: {exc}", file=sys.stderr, flush=True)
        return exit_blocked

    print(_summarize_outcome(outcome, report_path), file=sys.stderr, flush=True)
    return verdict_to_exit_code(
        outcome.verdict, findings=outcome.findings, strict=strict, exit_blocked=exit_blocked,
    )


def _with_endpoint_note(suites_text: str, *, bring_up: str, endpoints: dict) -> str:
    """Prepend a one-line ENDPOINT note to the suites text when the deterministic env layer
    already brought the env up (``bring_up="none"``) and knows its base address. The tester is
    told "the env is ready, do NOT boot anything" — without the address it would be left
    hunting for the port (review finding). A reused stage already gets its URL via the
    ``stage`` bring-up path; ``local`` (no env declared) has no machine-known endpoint."""
    base = endpoints.get("base")
    if bring_up != "none" or not base:
        return suites_text
    note = (
        f"ENV ENDPOINT: the SUT env is ALREADY UP and reachable at `{base}` — drive the "
        "cases against it; do NOT boot a second copy.\n\n"
    )
    return note + suites_text


def _warn_if_dirty_worktree_run(ctx: ModeContext, sut_path: Path) -> None:
    """Warn (loudly, once) when the DEFAULT worktree run is about to test committed ``HEAD``
    while the SUT has UNCOMMITTED changes — those changes are NOT in the isolated worktree, so
    the tester exercises stale code and a PASS would be against code that is not what's on
    disk (review P1). A no-op for ``--in-place`` (which DOES test the working tree) and for a
    clean / non-git SUT. Imported lazily to keep the mode import-light."""
    import sys

    if ctx.args.in_place:
        return
    from ..qa.executor import has_uncommitted_changes, is_git_worktree

    if is_git_worktree(sut_path) and has_uncommitted_changes(sut_path):
        print(
            "[review-cli] qa: WARNING — the SUT has uncommitted changes, but the default "
            "isolated worktree tests committed HEAD, NOT your working-tree edits. The tester "
            "will exercise stale code; a PASS would not cover your uncommitted changes. "
            "Commit/stash them first, or use --in-place to test the working tree directly "
            "(refused if the tree is dirty — commit/stash, then --in-place).",
            file=sys.stderr, flush=True,
        )


def _report_path(ctx: ModeContext, sut_path: Path) -> Path:
    """Where the transcript report is written.

    ``--report`` if given (the user owns that path). Otherwise the default goes to
    review-cli's per-user LOG DIR — NOT into the SUT git tree. Writing the default report
    under ``<sut>/docs/tests/reports/`` would (a) violate the "never touch your checkout"
    promise by leaving an untracked file in a clean repo, and (b) make the NEXT
    ``--in-place`` run see a dirty tree and be wrongly refused (review finding). The log dir
    is the right home for a run artifact; the SUT name + a UTC stamp keep it identifiable."""
    if ctx.args.report:
        return Path(ctx.args.report).expanduser()
    import os
    from datetime import datetime, timezone

    from ..process import log_dir

    # Microsecond stamp + a short random suffix so two rapid/concurrent runs for the SAME SUT
    # never collide on the same path (which `_write_report`'s O_TRUNC would overwrite — review
    # finding). Second-precision alone collides under a fast repeat.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    rand = os.urandom(3).hex()
    safe_sut = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in sut_path.name) or "sut"
    return log_dir() / f"qa-{safe_sut}-{stamp}-{rand}.md"


def _summarize_outcome(outcome, report_path: Path) -> str:
    """A one-paragraph human summary of the tester run for stderr (the full transcript is in
    the report). Surfaces the verdict, the case tally, finding count + worst severity, and
    the cost accounting (wall time)."""
    cases = ""
    if outcome.cases_run is not None:
        cases = (
            f" cases={outcome.cases_run} run / {outcome.cases_passed} passed / "
            f"{outcome.cases_failed} failed / {outcome.cases_blocked} blocked;"
        )
    sev = f" (worst {outcome.max_severity})" if outcome.max_severity else ""
    return (
        f"[review-cli] qa: VERDICT={outcome.verdict}{cases} findings={outcome.findings}{sev}; "
        f"backend={outcome.backend}; wall={outcome.wall_seconds:.1f}s; report={report_path}"
    )


# --- --kind auto detection (stdlib, cheap; spec §5) ----------------------------------
def _detect_kind(sut_path: Path) -> str:
    """Cheap stdlib SUT-shape detection (spec §5), first match wins. Inconclusive falls
    back to ``backend`` (the agent CAN run commands and is the real detector; this only
    seeds the right runbook). Order: ext -> web -> bot -> backend."""
    pkg = _read_package_json(sut_path)
    if _looks_like_ext(sut_path, pkg):
        return "ext"
    if _looks_like_web(sut_path, pkg):
        return "web"
    if _looks_like_bot(sut_path, pkg):
        return "bot"
    return "backend"


def _read_package_json(sut_path: Path) -> dict:
    """Parse ``<sut>/package.json`` to a dict (``{}`` on absent/unreadable/invalid). Used by
    the kind detectors so each reads the manifest once."""
    import json

    path = sut_path / "package.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _looks_like_ext(sut_path: Path, pkg: dict) -> bool:
    engines = pkg.get("engines") if isinstance(pkg.get("engines"), dict) else {}
    if "contributes" in pkg or "vscode" in engines:
        return True
    if (sut_path / ".vscode-test").exists() or (sut_path / "extension.ts").exists():
        return True
    return any(sut_path.glob("*.vsix"))


def _looks_like_web(sut_path: Path, pkg: dict) -> bool:
    deps = _all_deps(pkg)
    if deps & {"vite", "next", "react-scripts"}:
        return True
    return any(sut_path.glob("playwright.config.*"))


# Bot-framework dependency markers, by ecosystem. The JS ones live in package.json deps; the
# Python ones (a normal Python bot has NO package.json) live in requirements.txt / pyproject.toml.
# `auto` only SEEDS the runbook — the agent is the real detector and the user can pass `--kind
# bot` — so this just has to recognize the common case, not be exhaustive.
_JS_BOT_MARKERS = frozenset({"telegraf", "grammy"})
# Python distribution names, in PEP 503-CANONICAL form (lower-case, separators collapsed to a
# single `-`). `pytelegrambotapi` is the PyPI distribution of the "telebot" import package — the
# common one — so the dist name, not the import name, is what a requirements file carries.
_PY_BOT_MARKERS = frozenset({
    "python-telegram-bot", "aiogram", "pyrogram", "telethon", "pytelegrambotapi",
})


def _canon_dist(name: str) -> str:
    """PEP 503 canonical form of a distribution name: lower-case with runs of `-`/`_`/`.`
    collapsed to a single `-`. `python_telegram_bot` and `Python.Telegram.Bot` both canonicalize
    to `python-telegram-bot`, so a marker match is separator-insensitive."""
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def _looks_like_bot(sut_path: Path, pkg: dict) -> bool:
    if _all_deps(pkg) & _JS_BOT_MARKERS:
        return True
    return bool(_python_deps(sut_path) & _PY_BOT_MARKERS)


def _python_deps(sut_path: Path) -> set[str]:
    """The set of Python distribution names a SUT declares, lower-cased, from
    ``requirements.txt`` + ``pyproject.toml`` (both PEP 621 ``project.dependencies`` and a
    Poetry ``tool.poetry.dependencies`` table). Best-effort + stdlib-only: a parse failure or
    an absent file contributes nothing. Used by the bot detector so a Python Telegram bot (no
    package.json) is recognised by ``--kind auto`` instead of falling through to ``backend``."""
    return _requirements_deps(sut_path) | _pyproject_deps(sut_path)


def _requirements_deps(sut_path: Path) -> set[str]:
    """Distribution names from ``requirements.txt`` (the leading name token of each non-comment
    line, before any version/extras/marker), lower-cased. Best-effort; never raises."""
    path = sut_path / "requirements.txt"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()
    out: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "-")):  # skip blanks, comments, `-r`/`-e`/options
            continue
        name = _dist_name(line)
        if name:
            out.add(name)
    return out


def _pyproject_deps(sut_path: Path) -> set[str]:
    """Distribution names from ``pyproject.toml`` — PEP 621 ``[project].dependencies`` plus a
    Poetry ``[tool.poetry.dependencies]`` table — canonicalized. Prefers the stdlib ``tomllib``
    (3.11+), falling back to ``tomli`` when it happens to be installed on an older runtime; with
    NEITHER available (a bare 3.9/3.10 host) it contributes nothing — a pyproject-ONLY Python bot
    is then not auto-detected (the user can pass ``--kind bot``). Best-effort: a parse failure
    contributes nothing."""
    toml_loads = _toml_loader()
    if toml_loads is None:
        return set()
    path = sut_path / "pyproject.toml"
    try:
        data = toml_loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return set()
    out: set[str] = set()
    project = data.get("project")
    if isinstance(project, dict):
        deps = project.get("dependencies")
        if isinstance(deps, list):
            out |= {n for n in (_dist_name(str(d)) for d in deps) if n}
    poetry = (data.get("tool") or {}).get("poetry") if isinstance(data.get("tool"), dict) else None
    if isinstance(poetry, dict) and isinstance(poetry.get("dependencies"), dict):
        # Poetry keys ARE the distribution names (the value is the version constraint).
        out |= {_canon_dist(k) for k in poetry["dependencies"] if _canon_dist(k) != "python"}
    return out


def _toml_loader():
    """The best available ``loads(str) -> dict`` TOML parser: stdlib ``tomllib`` (3.11+), else
    ``tomli`` if installed, else ``None``. Lets the pyproject path work on a 3.9/3.10 host that
    happens to have ``tomli`` without making it a hard dependency (review keeps a minimal dep set)."""
    try:
        import tomllib
        return tomllib.loads
    except ImportError:
        pass
    try:
        import tomli
        return tomli.loads
    except ImportError:
        return None


def _dist_name(spec: str) -> str:
    """The bare distribution name from a PEP 508 / requirements spec — the leading name token,
    stripped of an extras group, version constraint, environment marker, or URL — in PEP 503
    CANONICAL form (lower-case, separators collapsed). `aiogram[fast]>=3,<4 ; python_version>'3.8'`
    -> `aiogram`; `python_telegram_bot>=20` -> `python-telegram-bot`. `""` when no name."""
    head = re.split(r"[\s;]", spec.strip(), maxsplit=1)[0]  # drop markers / trailing tokens
    head = head.split("@", 1)[0]            # drop a direct URL reference (`name @ url`)
    head = re.split(r"[<>=!~\[\(]", head, maxsplit=1)[0]  # drop extras / version constraint
    return _canon_dist(head) if head.strip() else ""


def _all_deps(pkg: dict) -> set[str]:
    """The union of ``dependencies`` + ``devDependencies`` keys from a parsed package.json."""
    out: set[str] = set()
    for key in ("dependencies", "devDependencies"):
        block = pkg.get(key)
        if isinstance(block, dict):
            out |= set(block)
    return out


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    """The qa-mode-unique arguments. The shared ``-C`` / ``-m`` / ``--pool`` / ``--timeout``
    / visual flags come from the CLI's ``_add_global_options`` / ``_add_mode_options``.

    Phase 2 added the EXECUTOR flags (``--kind``, ``--in-place``, ``--report``,
    ``--max-cases``). Phase 3 adds the SUT-ENV flags (``--stage-url``, ``--config``,
    ``--keep-env``) — the deterministic detect/reuse/bring-up/health-gate/teardown layer
    (``reviewlib/qa/env.py``) that stands the env up BEFORE the executor drives it. The
    remaining flags (``--bring-up``, ``--harness``, ``--scaffold-env``, ``--out`` artifact
    sink) arrive with their owning phases."""
    parser.add_argument(
        "sut_path", nargs="?", default=None,
        help="path to the System-Under-Test repo/checkout (default: the -C value, else cwd)",
    )
    parser.add_argument(
        "--suites", default=DEFAULT_SUITES_GLOB, metavar="PATH",
        help=(
            "glob, directory, or file of human-authored test-case suites "
            f"(default: {DEFAULT_SUITES_GLOB}, relative to the SUT). MUST resolve to >=1 "
            "file with >=1 '## Case:' block, else qa fails the no-suites gate (exit 6)."
        ),
    )
    parser.add_argument(
        "--kind", choices=("web", "ext", "backend", "bot", "auto"), default="auto",
        help="SUT shape; drives which runbook the tester prompt activates. 'auto' (default) "
        "runs cheap stdlib detection and falls back to 'backend' when inconclusive. 'bot' with "
        "a sut.bot mock config runs the DETERMINISTIC Tier-1 hermetic harness (fake Telegram + "
        "inject/capture, no un-caged agent, no real token/network); a sut.bot with an ask_command "
        "(a BRIDGE bot like tg-ctl) runs the AGENT-SIDE tier instead — the agent emits a question "
        "via the hook client, the harness asserts the card, taps it, and reads the answer back "
        "(the Ask-question:/Expect-card:/Tap:/Expect-answer: grammar). 'web' with a sut.web config "
        "runs the DETERMINISTIC Tier-1 headless-browser harness (Playwright drives the DOM + "
        "asserts; gated behind REVIEW_QA_PLAYWRIGHT=1, no un-caged agent). 'ext' with a sut.ext "
        "config runs the DETERMINISTIC Tier-1 isolated-VS-Code harness (launchVSCode-over-CDP "
        "runs commands + asserts notification/editor-text/webview; gated behind REVIEW_QA_VSCODE=1, "
        "no un-caged agent).",
    )
    parser.add_argument(
        "--in-place", action="store_true",
        help="run the tester in the SUT working tree instead of an isolated git worktree "
        "(riskier; opt-in). The safe default is a throwaway worktree, removed on exit.",
    )
    parser.add_argument(
        "--report", default=None, metavar="PATH",
        help="where to write the full tester transcript + accounting footer (default: "
        "review-cli's log dir, OUTSIDE the SUT tree, so a clean checkout stays clean).",
    )
    parser.add_argument(
        "--max-cases", type=int, default=1, metavar="N",
        help="cap the number of cases exercised this run (cost control). Default 1 (a "
        "cheap smoke); pass a larger N or 0 for 'no cap' to run the full suite. Negative "
        "values are rejected.",
    )
    parser.add_argument(
        "--stage-url", default=None, metavar="URL",
        help="an EXISTING stage/preview env to test against instead of booting locally. If "
        "reachable, qa REUSES it and never tears it down (you own it). Overrides any "
        "sut.stage in the qa config.",
    )
    parser.add_argument(
        "--config", default=None, metavar="PATH",
        help="env-harness config for the SUT bring-up (default: docs/tests/qa.yaml, relative "
        "to the SUT). Declares the stage / compose bring-up / health gate, OR (kind=bot) a "
        "sut.bot mock block that drives the hermetic Tier-1 bot harness, OR (kind=web) a "
        "sut.web block (base_url + dev-server command) that drives the deterministic Tier-1 "
        "browser harness, OR (kind=ext) a sut.ext block (extension_path + workspace) that drives "
        "the deterministic Tier-1 isolated-VS-Code harness. Absent is fine — qa then uses a "
        "qa/setup.sh hook if present, else skips env bring-up.",
    )
    parser.add_argument(
        "--keep-env", action="store_true",
        help="on an UNHEALTHY bring-up, skip teardown and leave the env up for triage, "
        "printing the exact manual `down` command (a reused stage is never torn down "
        "regardless).",
    )


MODE = ModeSpec(
    name="qa",
    subcommand="qa",
    diff_policy="none",
    stats_mode="qa",
    summary="agent acts as a TESTER: bring up the SUT, run docs/tests/suites/*.md, hunt bugs (report-only)",
    handler=_handler,
    add_arguments=_add_arguments,
    announce_logs=True,
    aliases=("test",),
)
