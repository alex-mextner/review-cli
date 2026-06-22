#!/usr/bin/env python3
"""review qa — Phase 1: the mode skeleton + the NO-SUITES gate.

These pin the Phase-1 contract of the agent-as-tester mode (docs/specs/review-qa.md §4/§6):

  * the `qa` subcommand AND its `test` alias resolve to the qa mode via the registry;
  * `review qa --help` builds (the mode's argparse surface is wired);
  * the NO-SUITES gate fires (no authored suites → exit EXIT_QA_NO_SUITES) and prints a
    3-part WHAT/WHY/HOW message (mirroring `_fail_not_a_repo`), BEFORE any agent/docker;
  * an authored-but-empty file (no `## Case:` block) is the SAME exit class with a distinct
    "found a file but no Case block" message — it must NOT claim "no suites" about a file
    the author DID write;
  * a non-empty suite with `## Case:` blocks parses to a non-zero case count, and because
    Phase 1 has no executor the run FAILS LOUDLY with EXIT_QA_NOT_IMPLEMENTED (non-zero) —
    NOT exit 0, which would be a false green (cases authored, zero executed).

All offline: the gate runs entirely on the filesystem (suite discovery), so no backend is
spawned. The `qa` mode is dispatched through the real `cli.main` exactly like any other
mode (no qa-specific dispatch surgery), so these also exercise the registry wiring.

Runnable standalone (`python3 tests/test_qa_mode.py`, what smoke.py does) or under pytest.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import cli  # noqa: E402
from reviewlib.modes import qa as _qa_mod  # noqa: E402
from reviewlib.modes import registry as _registry  # noqa: E402


# --- Helpers -------------------------------------------------------------------------
def _run(argv: list[str]) -> tuple[int, str, str]:
    """Run `cli.main(argv)` offline and capture (rc, stdout, stderr). A non-repo env file
    keeps any stray config/key read inert; the qa gate never reaches a backend anyway."""
    old_env = os.environ.get("GEMINI_ENV_FILE")
    os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = cli.main(argv)
            except SystemExit as exc:  # --help / argparse usage paths exit via SystemExit
                rc = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    finally:
        if old_env is None:
            os.environ.pop("GEMINI_ENV_FILE", None)
        else:
            os.environ["GEMINI_ENV_FILE"] = old_env
    return rc, out.getvalue(), err.getvalue()


def _sut_with_suite(case_headings: list[str]) -> str:
    """Create a temp SUT dir with one suite file holding the given `## Case:` headings.
    An empty list authors a file with prose but NO case block (the empty-suite variant)."""
    sut = tempfile.mkdtemp()
    suites = Path(sut) / "docs" / "tests" / "suites"
    suites.mkdir(parents=True)
    lines = ["# Suite: smoke", ""]
    if case_headings:
        for h in case_headings:
            lines += [f"## Case: {h}", "Steps:", "- do a thing", "Expected:", "- a result", ""]
    else:
        lines += ["## login flow", "some prose, but no Case heading", ""]
    (suites / "smoke.md").write_text("\n".join(lines), encoding="utf-8")
    return sut


# --- Registry: qa + its `test` alias resolve -----------------------------------------
def test_qa_subcommand_resolves_via_registry():
    mode = _registry.get_mode("qa")
    assert mode is not None and mode.name == "qa", mode
    assert mode.diff_policy == "none", mode
    assert "qa" in _registry.known_subcommands()


def test_test_alias_resolves_to_qa():
    """The `test` alias must select the same qa mode (a tester is also `review test`)."""
    mode = _registry.get_mode("test")
    assert mode is not None and mode.name == "qa", mode
    assert "test" in _registry.known_subcommands()
    assert "test" in _registry.get_mode("qa").aliases


def test_qa_is_registered_in_modes_tuple():
    """The mode must actually be in MODES (not just resolvable by a stale alias map)."""
    assert any(m.name == "qa" for m in _registry.iter_modes())


def test_qa_help_builds():
    """`review qa --help` must build the mode parser and exit 0 (argparse SystemExit(0))."""
    rc, out, _err = _run(["qa", "--help"])
    assert rc == 0, rc
    assert "--suites" in out, out


# --- The NO-SUITES gate fires with EXIT_QA_NO_SUITES + a 3-part message ---------------
def test_no_suites_gate_exits_with_exit_qa_no_suites():
    """A SUT with no authored suites → the gate exits EXIT_QA_NO_SUITES (NOT 0, NOT a
    finding code) and prints a 3-part WHAT/WHY/HOW message to stderr."""
    sut = tempfile.mkdtemp()  # no docs/tests/suites at all
    rc, _out, err = _run(["qa", sut])
    assert rc == cli.EXIT_QA_NO_SUITES, (rc, err)
    # WHAT / WHY / HOW — the three parts (mirrors _fail_not_a_repo's structure).
    assert "no test-case suites found" in err, err
    assert "why:" in err and "a green run would be a lie" in err, err
    assert "how:" in err and "## Case:" in err, err
    # The gate must be NON-ZERO even though Phase 1 has no --strict notion (it is a
    # contract failure, not a finding).
    assert rc != 0


def test_test_alias_also_fires_the_gate():
    """The `test` alias must hit the SAME gate (proves the alias dispatches to the handler,
    not just resolves in the registry)."""
    sut = tempfile.mkdtemp()
    rc, _out, err = _run(["test", sut])
    assert rc == cli.EXIT_QA_NO_SUITES, (rc, err)
    assert "no test-case suites found" in err, err


def test_authored_but_empty_file_is_distinct_no_case_message():
    """A suite file that exists but parses to ZERO `## Case:` blocks is the same exit class
    (EXIT_QA_NO_SUITES) but must NOT claim 'no suites found' — it tells the author the file
    is there but has no Case block, so they aren't confused about a file they DID write."""
    sut = _sut_with_suite([])  # a file with prose but no `## Case:`
    rc, _out, err = _run(["qa", sut])
    assert rc == cli.EXIT_QA_NO_SUITES, (rc, err)
    assert "none contain a '## Case:' block" in err, err
    assert "no test-case suites found" not in err, err


# --- A real authored suite parses to cases and fails LOUD (no executor in Phase 1) -----
def test_authored_suite_counts_cases_and_fails_not_implemented():
    """A suite with two `## Case:` blocks resolves (case count == 2). Phase 1 has no
    executor, so the run must FAIL LOUDLY with EXIT_QA_NOT_IMPLEMENTED (NOT exit 0) — a 0
    would be a false green (cases authored, zero executed). The message goes to stderr."""
    sut = _sut_with_suite(["login rejects empty pw", "logout clears session"])
    rc, _out, err = _run(["qa", sut])
    assert rc == cli.EXIT_QA_NOT_IMPLEMENTED, (rc, err)
    assert rc != 0, rc
    assert "2 case(s)" in err, err
    assert "NOT IMPLEMENTED" in err, err
    assert "false green" in err, err


def test_resolve_suites_and_count_cases_directly():
    """Unit-level: resolve_suites returns the suite file and count_cases counts `## Case:`
    blocks (the CASES tally the spec's §4 defines). A bare `## title` is NOT counted."""
    sut = _sut_with_suite(["a", "b", "c"])
    suites = _qa_mod.resolve_suites(Path(sut), _qa_mod.DEFAULT_SUITES_GLOB)
    assert len(suites) == 1, suites
    assert _qa_mod.count_cases(suites[0]) == 3, suites

    empty = _sut_with_suite([])  # prose `## login flow`, no `## Case:`
    assert _qa_mod.resolve_suites(Path(empty), _qa_mod.DEFAULT_SUITES_GLOB) == [], empty


def test_suites_flag_accepts_a_directory_and_a_file():
    """`--suites` resolves a directory (every *.md in it) and a single file, not only the
    default glob — so an author can point qa at a custom suite location. A resolved suite
    in Phase 1 reaches the not-implemented branch (non-zero, stderr)."""
    sut = _sut_with_suite(["only case"])
    suites_dir = "docs/tests/suites"
    rc_dir, _out, err_dir = _run(["qa", sut, "--suites", suites_dir])
    assert rc_dir == cli.EXIT_QA_NOT_IMPLEMENTED and "1 case(s)" in err_dir, (rc_dir, err_dir)

    suite_file = str(Path(sut) / "docs" / "tests" / "suites" / "smoke.md")
    rc_file, _out2, err_file = _run(["qa", sut, "--suites", suite_file])
    assert rc_file == cli.EXIT_QA_NOT_IMPLEMENTED and "1 case(s)" in err_file, (rc_file, err_file)


def test_suites_flag_accepts_an_absolute_glob():
    """`--suites` with an ABSOLUTE glob (the non-trivial anchor-split branch of
    `_candidate_suite_files`) must still resolve the suite files."""
    sut = _sut_with_suite(["abs glob case"])
    abs_glob = str(Path(sut) / "docs" / "tests" / "suites" / "*.md")
    suites = _qa_mod.resolve_suites(Path(sut), abs_glob)
    assert len(suites) == 1, suites
    assert _qa_mod.count_cases(suites[0]) == 1, suites


def test_sut_resolves_from_dash_c_when_no_positional():
    """The SUT path falls back to the resolved `-C` cwd when no positional `sut_path` is
    given (the documented '-C value, else cwd' rule), and the positional WINS when both are
    present. Proven via the no-suites gate firing against the right directory."""
    sut = tempfile.mkdtemp()  # no suites -> the gate names this path
    rc, _out, err = _run(["qa", "-C", sut])
    assert rc == cli.EXIT_QA_NO_SUITES, (rc, err)
    assert sut in err, ("gate must name the -C-resolved SUT", err)

    # Positional wins over -C: point -C at one dir but the positional at another (the
    # positional one has no suites, so the gate must name the POSITIONAL path).
    other = tempfile.mkdtemp()
    rc2, _out2, err2 = _run(["qa", other, "-C", sut])
    assert rc2 == cli.EXIT_QA_NO_SUITES, (rc2, err2)
    assert other in err2, ("positional sut_path must win over -C", err2)


def test_qa_exit_codes_do_not_collide_with_existing_codes():
    """Both qa process-exit codes (EXIT_QA_NO_SUITES and EXIT_QA_NOT_IMPLEMENTED) must be
    distinct from the other stable classes AND from each other, so CI can branch on them.
    5 is taken by brainstorm's EXIT_DEAD_PANEL, so qa's no-suites code is 6; the transient
    not-implemented code is 70."""
    from reviewlib.modes.brainstorm import EXIT_DEAD_PANEL

    taken = {0, 2, cli.EXIT_NOT_A_REPO, cli.EXIT_GIT_DIFF_FAILED, EXIT_DEAD_PANEL, 10, 124}
    assert cli.EXIT_QA_NO_SUITES not in taken, cli.EXIT_QA_NO_SUITES
    assert cli.EXIT_QA_NOT_IMPLEMENTED not in taken, cli.EXIT_QA_NOT_IMPLEMENTED
    assert cli.EXIT_QA_NO_SUITES != cli.EXIT_QA_NOT_IMPLEMENTED


def test_verbless_suites_flag_gets_friendly_pointer():
    """A verb-less `review --suites …` (no subcommand) must emit the friendly "use the
    subcommand" pointer (exit 2), not argparse's opaque "unrecognized arguments" — the
    reason `--suites` is in `_SUBCOMMAND_ONLY_FLAGS`."""
    sut = tempfile.mkdtemp()
    rc, _out, err = _run(["--suites", "foo.md", "-C", sut])
    assert rc == 2, (rc, err)
    assert "subcommand" in err, err
    assert "unrecognized arguments" not in err, err


if __name__ == "__main__":
    failures = 0
    for _name, _fn in list(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"PASS {_name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {_name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {_name}: {type(exc).__name__}: {exc}")
    print(f"\n{'FAILED' if failures else 'OK'}: {failures} failure(s)")
    sys.exit(1 if failures else 0)
