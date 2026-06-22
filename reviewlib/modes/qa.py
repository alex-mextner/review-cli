"""qa: agent acts as a TESTER — bring up a System-Under-Test (SUT), run human-authored
test-case suites, hunt bugs.

PHASE 1 (this file): the mode SKELETON + the NO-SUITES gate ONLY. There is deliberately
NO executor, NO LLM-judge, NO env-harness here — those are Phase 2+ in
``docs/specs/review-qa.md`` ("Phased implementation plan"). What this slice ships:

  * a self-describing ``MODE = ModeSpec(name="qa", subcommand="qa", aliases=("test",))``
    registered in ``modes/registry.py`` — so ``review qa`` / ``review test`` resolve;
  * suite DISCOVERY (``resolve_suites``): a conventional dir of prose ``*.md`` suites,
    each holding one or more ``## Case:`` blocks (the free-form markdown format the
    spec's §4 chose — easy for a human to author);
  * the NO-SUITES GATE (``_handler``): when ``qa`` runs but no suite resolves (or every
    resolved file parses to zero cases), it prints a 3-part WHAT/WHY/HOW message
    (mirroring ``cli._fail_not_a_repo``) and exits ``EXIT_QA_NO_SUITES``. A green qa run
    with zero authored cases is a lie, so this gate runs BEFORE any agent/docker/browser.

RUNTIME REACH: dispatched table-driven by ``cli._dispatch`` exactly like every other mode
(``get_mode("qa")`` → ``_build_mode_parser`` → ``mode.handler(ctx)``); no ``cli.py``
dispatch surgery. ``diff_policy="none"`` keeps qa OFF the required-diff path — qa is about
a running system, not a diff. Because Phase 1 has no executor, the handler returns before
any model/panel call, so qa never rides the read-only board/panel here.

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
    """The SUT path: the optional ``sut_path`` positional if given, else the resolved
    ``-C`` cwd. Lets ``review qa /path/to/sut`` and ``review qa -C /path/to/sut`` both
    work, with the positional winning when both are present."""
    raw = getattr(ctx.args, "sut_path", None)
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(ctx.cwd)


def _handler(ctx: ModeContext) -> int:
    """Phase 1 handler: resolve suites, run the NO-SUITES gate, then stop.

    The gate runs BEFORE any agent/docker/browser (there are none yet in Phase 1). When
    suites DO resolve, Phase 1 has no executor to run them, so it FAILS LOUDLY with the
    distinct ``EXIT_QA_NOT_IMPLEMENTED`` code rather than exiting 0 — a 0 here would be the
    same lie the no-suites gate prevents (cases authored, ZERO executed, but CI reads "qa
    passed"). The verb, the gate, and the suite convention are what this slice ships."""
    # Imported here (not at module top) so the qa module stays import-light and does not
    # create a circular import with cli (cli imports the registry which imports this mode).
    from ..cli import EXIT_QA_NO_SUITES, EXIT_QA_NOT_IMPLEMENTED

    sut_path = _sut_path(ctx)
    suites = resolve_suites(sut_path, ctx.args.suites)
    if not suites:
        return _fail_no_suites(sut_path, ctx.args.suites, exit_code=EXIT_QA_NO_SUITES)

    return _report_not_implemented(sut_path, suites, exit_code=EXIT_QA_NOT_IMPLEMENTED)


def _report_not_implemented(sut_path: Path, suites: list[Path], *, exit_code: int) -> int:
    """Suites resolved, but Phase 1 has no executor. Report what WOULD run (so the author
    sees their suites were discovered + parsed correctly) and return a NON-ZERO
    "not-implemented" code, to stderr — never a silent green. Verb-named: prints AND
    returns the code."""
    import sys

    total_cases = sum(count_cases(p) for p in suites)
    lines = [
        f"[review-cli] qa: resolved {len(suites)} suite(s) with {total_cases} case(s) "
        f"under {sut_path}, but the tester is NOT IMPLEMENTED yet.",
        "  qa Phase 1 ships the mode skeleton + the no-suites gate only — the write/exec "
        "tester (the agentic launcher) lands in Phase 2 (docs/specs/review-qa.md). No SUT "
        "was brought up and no case was executed.",
        "  exiting NON-ZERO on purpose: a 0 here would be a false green (cases authored, "
        "zero executed) — exactly the lie the no-suites gate exists to prevent.",
    ]
    lines += [f"    - {suite}  ({count_cases(suite)} case(s))" for suite in suites]
    print("\n".join(lines), file=sys.stderr, flush=True)
    return exit_code


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    """The qa-mode-unique arguments. The shared ``-C`` / ``-m`` / ``--pool`` / ``--timeout``
    / visual flags come from the CLI's ``_add_global_options`` / ``_add_mode_options``.

    Phase 1 exposes only the flags the skeleton + gate use; the executor/env/harness flags
    (``--kind``, ``--stage-url``, ``--bring-up``, ``--config``, ``--harness``, ``--in-place``,
    ``--keep-env``, ``--scaffold-env`` …) arrive with their owning phases."""
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


MODE = ModeSpec(
    name="qa",
    subcommand="qa",
    diff_policy="none",
    stats_mode="qa",
    summary="agent acts as a TESTER: run docs/tests/suites/*.md, hunt bugs (Phase 1: skeleton + no-suites gate)",
    handler=_handler,
    add_arguments=_add_arguments,
    announce_logs=True,
    aliases=("test",),
)
