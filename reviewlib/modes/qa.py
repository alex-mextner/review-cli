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
    """Phase 2 handler: resolve suites (no-suites gate), then run the write/exec TESTER.

    The no-suites gate (Phase 1) still runs FIRST, BEFORE any agent spawn — a write/exec
    agent must never launch for an empty run. When suites resolve, the handler builds the
    tester prompt from the (max-cases-capped) suites + the detected kind, then hands it to
    the single-seat write/exec launcher (``reviewlib/qa/executor.run_tester``) — NOT
    ``run_panel``, NOT the read-only board. The agent's evidence-backed ``VERDICT:`` maps to
    the exit code (report-only: a found bug exits 0 with findings printed; only "couldn't
    run the tester" / BLOCKED is non-zero, plus the ``--strict`` finding flip)."""
    # Imported here (not at module top) so the qa module stays import-light and does not
    # create a circular import with cli (cli imports the registry which imports this mode).
    from ..cli import EXIT_QA_NO_SUITES, EXIT_QA_SUT_BOOT_FAILED

    sut_path = _sut_path(ctx)
    suites = resolve_suites(sut_path, ctx.args.suites)
    if not suites:
        return _fail_no_suites(sut_path, ctx.args.suites, exit_code=EXIT_QA_NO_SUITES)

    return _run_executor(ctx, sut_path, suites, exit_blocked=EXIT_QA_SUT_BOOT_FAILED)


def _run_executor(ctx: ModeContext, sut_path: Path, suites: list[Path], *, exit_blocked: int) -> int:
    """Build the tester prompt + run the single-seat write/exec launcher, then map the
    parsed verdict to an exit code. Split from ``_handler`` so the gate stays the first,
    obvious thing the handler does and the (heavier) executor wiring is one call away."""
    import sys

    # Lazy-imported (heavy: pulls the qa package) so the mode stays import-light and the
    # no-suites gate above never pays for the executor.
    from ..config import _split_models
    from ..qa.executor import (
        DirtyInPlaceError,
        SutIsolationError,
        UnsupportedTesterError,
        build_tester_prompt,
        resolved_tester_backend,
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

    kind = _detect_kind(sut_path) if ctx.args.kind == "auto" else ctx.args.kind
    # max_cases == 0 means "no cap" (run all); a positive N caps to the first N cases.
    max_cases = ctx.args.max_cases if ctx.args.max_cases and ctx.args.max_cases > 0 else None
    suites_text = load_suites_text(suites, max_cases=max_cases)
    strict = bool(getattr(ctx.args, "strict", False))

    # SECURITY: build the prompt at the ACTUAL run cwd, not sut_path. The prompt fences the
    # agent to "ONLY inside `{path}`"; that path must be the worktree (or sut_path under
    # --in-place) the agent actually runs in — building it with the user's real checkout
    # would point the un-caged agent at the real repo by absolute path (review finding). The
    # executor invokes this closure with the resolved cwd after the worktree exists.
    def _prompt_builder(run_cwd: Path) -> str:
        return build_tester_prompt(
            kind=kind, suites_text=suites_text, sut_path=run_cwd, bring_up="local",
            strict=strict, in_place=ctx.args.in_place,
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
    print(
        f"[review-cli] qa: testing SUT {sut_path} (kind={kind}, backend={backend}, "
        f"isolation={'in-place' if ctx.args.in_place else 'worktree'}, cases<= "
        f"{ctx.args.max_cases or 'all'}). Report -> {report_path}",
        file=sys.stderr, flush=True,
    )
    _warn_if_dirty_worktree_run(ctx, sut_path)

    try:
        outcome = run_tester(
            prompt_builder=_prompt_builder, sut_path=sut_path, timeout=ctx.timeout,
            in_place=ctx.args.in_place, report_path=report_path, backend=backend,
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


def _looks_like_bot(sut_path: Path, pkg: dict) -> bool:
    deps = _all_deps(pkg)
    return bool(deps & {"telegraf", "grammy", "python-telegram-bot", "aiogram"})


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

    Phase 2 adds the EXECUTOR flags (``--kind``, ``--in-place``, ``--report``,
    ``--max-cases``). The env/harness flags (``--stage-url``, ``--bring-up``, ``--config``,
    ``--harness``, ``--keep-env``, ``--scaffold-env``, ``--out`` artifact sink) arrive with
    their owning phases."""
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
        "runs cheap stdlib detection and falls back to 'backend' when inconclusive.",
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
