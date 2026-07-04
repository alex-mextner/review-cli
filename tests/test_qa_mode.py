#!/usr/bin/env python3
"""review qa — the mode skeleton + the NO-SUITES gate + the Phase-2 executor wiring.

These pin the agent-as-tester mode contract (docs/specs/review-qa.md §4/§6/§8/§9):

  * the `qa` subcommand AND its `test` alias resolve to the qa mode via the registry;
  * `review qa --help` builds (the mode's argparse surface is wired);
  * the NO-SUITES gate fires (no authored suites → exit EXIT_QA_NO_SUITES) and prints a
    3-part WHAT/WHY/HOW message (mirroring `_fail_not_a_repo`), BEFORE any agent/docker;
  * an authored-but-empty file (no `## Case:` block) is the SAME exit class with a distinct
    "found a file but no Case block" message — it must NOT claim "no suites" about a file
    the author DID write;
  * a non-empty suite with `## Case:` blocks resolves and the handler runs the write/exec
    EXECUTOR (Phase 2) — report-only verdict→exit mapping (PASS/FAIL exit 0; --strict flips
    a finding to 10; a non-repo SUT can't be isolated → BLOCKED exit 8).

The gate tests are offline (filesystem-only). The executor-wiring tests force the
deterministic MOCKED tester (REVIEW_QA_FAKE_TESTER) so NO backend is spawned. The `qa` mode
is dispatched through the real `cli.main` exactly like any other mode (no qa-specific
dispatch surgery), so these also exercise the registry wiring.

Runnable standalone (`python3 tests/test_qa_mode.py`, what smoke.py does) or under pytest.
"""
from __future__ import annotations

import io
import os
import subprocess
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
    old_task = os.environ.get("REVIEW_TASK_CODE")
    os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
    os.environ["REVIEW_TASK_CODE"] = "TEST-1"
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
        if old_task is None:
            os.environ.pop("REVIEW_TASK_CODE", None)
        else:
            os.environ["REVIEW_TASK_CODE"] = old_task
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


def _git_init_commit(sut: str) -> None:
    """Turn a SUT dir into a committed git repo so the executor's worktree isolation has a
    real repo to branch from. Hooks disabled (the dev machine may carry a global
    review-before-commit pre-commit gate that would block this throwaway fixture commit)."""
    base = ["git", "-c", "core.hooksPath=/dev/null", "-c", "user.email=t@t", "-c", "user.name=t"]
    for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "fixture"]):
        subprocess.run(base + cmd, cwd=sut, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class _fake_tester_env:
    """Context manager: force the deterministic MOCKED qa tester (no backend spawn) so a
    handler-level run of the executor path is hermetic. ``verdict`` forces a specific
    verdict via REVIEW_QA_FAKE_VERDICT."""

    def __init__(self, verdict: str | None = None):
        self.verdict = verdict
        self._saved: dict[str, str | None] = {}

    def __enter__(self):
        for k, v in {"REVIEW_QA_FAKE_TESTER": "1", "REVIEW_QA_FAKE_VERDICT": self.verdict}.items():
            self._saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *exc):
        for k, old in self._saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


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


# --- A real authored suite resolves and runs the executor (Phase 2, mocked tester) ------
def test_authored_suite_runs_executor_and_maps_verdict():
    """A suite with two `## Case:` blocks resolves and the handler runs the write/exec
    executor (Phase 2) — NOT the old not-implemented scaffold. With the deterministic mocked
    tester forced to PASS, a committed-repo SUT exits 0 (report-only), and the stderr summary
    names the verdict. This proves the handler reaches the executor path through real
    dispatch."""
    sut = _sut_with_suite(["login rejects empty pw", "logout clears session"])
    _git_init_commit(sut)
    with _fake_tester_env(verdict="PASS"):
        rc, _out, err = _run(["qa", sut])
    assert rc == 0, (rc, err)  # report-only: a PASS verdict exits 0
    assert "VERDICT=PASS" in err, err
    assert "testing SUT" in err, err


def test_authored_suite_fail_verdict_is_report_only_zero_but_strict_blocks():
    """A FAIL verdict is report-only (exit 0) by default but flips to 10 under --strict — the
    handler honours the report-only contract and the strict finding flip."""
    sut = _sut_with_suite(["a buggy case"])
    _git_init_commit(sut)
    with _fake_tester_env(verdict="FAIL"):
        rc, _out, err = _run(["qa", sut])
        assert rc == 0, (rc, err)  # report-only
        assert "VERDICT=FAIL" in err, err
        rc_strict, _o2, err2 = _run(["qa", sut, "--strict"])
        assert rc_strict == 10, (rc_strict, err2)


def _add_setup_hook(sut: str, *, up_rc: int = 0) -> Path:
    """Add an executable ``qa/setup.sh`` hook to a SUT dir that writes UP/DOWN markers, so a
    handler-level run can assert the Phase-3 env layer brought it up + tore it down."""
    qadir = Path(sut) / "qa"
    qadir.mkdir(parents=True, exist_ok=True)
    hook = qadir / "setup.sh"
    hook.write_text(
        "#!/bin/sh\n"
        'cd "$(dirname "$0")/.." || exit 1\n'
        "case \"$1\" in\n"
        f"  up) touch UP; exit {up_rc} ;;\n"
        "  down) rm -f UP; touch DOWN; exit 0 ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    return hook


def test_handler_brings_env_up_and_tears_it_down_around_executor():
    """Phase-3 wiring through the REAL `review qa` dispatch: a SUT with a `qa/setup.sh` hook
    has its env brought up (UP marker) BEFORE the (mocked) executor runs and torn down (DOWN
    marker) on exit — proving env.bring_up_env + the guaranteed teardown are wired into the
    handler, not just unit-tested in isolation (review finding: the real handler wiring was
    uncovered)."""
    sut = _sut_with_suite(["a case the tester runs"])
    _git_init_commit(sut)  # commit BEFORE adding the hook so the worktree HEAD is clean
    _add_setup_hook(sut)
    with _fake_tester_env(verdict="PASS"):
        rc, _out, err = _run(["qa", sut])
    assert rc == 0, (rc, err)
    assert "VERDICT=PASS" in err, err
    assert (Path(sut) / "DOWN").exists(), "the hook env must be torn down on exit"
    assert not (Path(sut) / "UP").exists(), "teardown removed the UP marker"
    assert "env=none" in err, ("the agent was told the env is already up", err)


def test_handler_no_env_declared_skips_bringup():
    """A SUT with NO stage / hook / config skips Phase-3 bring-up gracefully — the executor
    still runs (the agent does its own local bring-up), no env is owned, no marker is written.
    Proves env bring-up is SKIPPED for pure unit-style SUTs (the task's graceful-skip case)."""
    sut = _sut_with_suite(["a case"])
    _git_init_commit(sut)
    with _fake_tester_env(verdict="PASS"):
        rc, _out, err = _run(["qa", sut])
    assert rc == 0, (rc, err)
    assert "env=" not in err, ("no env layer engaged for a bare SUT", err)
    assert not (Path(sut) / "DOWN").exists()


def test_handler_malformed_config_is_usage_error():
    """A malformed qa.yaml is a clean usage error (exit 2) from the handler, not a traceback."""
    sut = _sut_with_suite(["a case"])
    _git_init_commit(sut)
    cfgdir = Path(sut) / "docs" / "tests"
    (cfgdir / "qa.yaml").write_text(
        "sut:\n  health:\n    - { name: bad, url: http://x, compose_service: db }\n",
        encoding="utf-8")
    with _fake_tester_env(verdict="PASS"):
        rc, _out, err = _run(["qa", sut])
    assert rc == 2, (rc, err)
    assert "EXACTLY one" in err, err


def test_non_repo_sut_blocked_exit_when_isolation_fails():
    """A SUT that is NOT a git repo can't be isolated into a worktree, so the run is BLOCKED
    (EXIT_QA_SUT_BOOT_FAILED) — distinct from a finding. No backend is spawned (the isolation
    check precedes the spawn), so this is hermetic even without the fake tester."""
    sut = _sut_with_suite(["only case"])  # NOT git-init'd
    with _fake_tester_env(verdict="PASS"):
        rc, _out, err = _run(["qa", sut])
    assert rc == cli.EXIT_QA_SUT_BOOT_FAILED, (rc, err)
    assert "not a git work tree" in err, err


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
    default glob — so an author can point qa at a custom suite location. Both resolve to the
    executor path (mocked tester forced PASS → exit 0, report-only)."""
    sut = _sut_with_suite(["only case"])
    _git_init_commit(sut)
    with _fake_tester_env(verdict="PASS"):
        suites_dir = "docs/tests/suites"
        rc_dir, _out, err_dir = _run(["qa", sut, "--suites", suites_dir])
        assert rc_dir == 0 and "VERDICT=PASS" in err_dir, (rc_dir, err_dir)

        suite_file = str(Path(sut) / "docs" / "tests" / "suites" / "smoke.md")
        rc_file, _out2, err_file = _run(["qa", sut, "--suites", suite_file])
        assert rc_file == 0 and "VERDICT=PASS" in err_file, (rc_file, err_file)


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
    """The qa process-exit codes must be distinct from the other stable classes AND from
    each other, so CI can branch on them. 5 is taken by brainstorm's EXIT_DEAD_PANEL, so
    qa's block is 6 (NO_SUITES) / 7 (NO_ENV) / 8 (SUT_BOOT_FAILED) / 9 (ENV_UNHEALTHY)."""
    from reviewlib.modes.brainstorm import EXIT_DEAD_PANEL

    taken = {0, 2, cli.EXIT_NOT_A_REPO, cli.EXIT_GIT_DIFF_FAILED, EXIT_DEAD_PANEL, 10, 124}
    qa_codes = [
        cli.EXIT_QA_NO_SUITES, cli.EXIT_QA_NO_ENV,
        cli.EXIT_QA_SUT_BOOT_FAILED, cli.EXIT_QA_ENV_UNHEALTHY,
    ]
    for code in qa_codes:
        assert code not in taken, code
    assert len(set(qa_codes)) == len(qa_codes), ("qa codes must be mutually distinct", qa_codes)


def test_sut_path_uses_raw_dash_c_not_rewritten_toplevel():
    """`review qa -C <monorepo/package>` must test the PACKAGE, not the git toplevel: the
    shared _effective_cwd rewrites an in-repo -C to the toplevel (so ctx.cwd is the repo
    root), but _sut_path must use the RAW -C so the package the user named is the SUT (review
    finding). A positional still wins; a default '.' falls back to ctx.cwd."""
    import argparse

    # Raw -C names a package; ctx.cwd is the (rewritten) repo root — _sut_path must pick -C.
    pkg = Path(tempfile.mkdtemp()) / "packages" / "mypkg"
    pkg.mkdir(parents=True)
    ns = argparse.Namespace(sut_path=None, cwd=str(pkg))
    ctx = _ctx_with(ns, cwd="/the/repo/root")
    assert _qa_mod._sut_path(ctx) == pkg.resolve(), _qa_mod._sut_path(ctx)

    # A positional still wins over -C.
    other = Path(tempfile.mkdtemp())
    ns2 = argparse.Namespace(sut_path=str(other), cwd=str(pkg))
    assert _qa_mod._sut_path(_ctx_with(ns2, cwd="/x")) == other.resolve()

    # Default -C '.' falls back to ctx.cwd (the resolved/effective cwd).
    ns3 = argparse.Namespace(sut_path=None, cwd=".")
    assert _qa_mod._sut_path(_ctx_with(ns3, cwd=str(other))) == Path(str(other))


def _ctx_with(args, *, cwd):
    """A minimal ModeContext for unit-testing _sut_path (only args + cwd are read)."""
    from reviewlib.modes.contract import ModeContext

    return ModeContext(
        args=args, models=[], diff="", cwd=cwd, timeout=60,
        with_visual=lambda t: t,
    )


# --- --kind auto detection (spec §5) -------------------------------------------------
def _sut_with_files(files: dict[str, str]) -> Path:
    """Create a temp SUT dir containing the given relative-path → content files."""
    sut = Path(tempfile.mkdtemp())
    for rel, content in files.items():
        p = sut / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return sut


def test_detect_kind_ext_from_vscode_engine():
    sut = _sut_with_files({"package.json": '{"engines": {"vscode": "^1.80.0"}}'})
    assert _qa_mod._detect_kind(sut) == "ext"


def test_detect_kind_web_from_vite_dep():
    sut = _sut_with_files({"package.json": '{"dependencies": {"vite": "^5.0.0"}}'})
    assert _qa_mod._detect_kind(sut) == "web"


def test_detect_kind_web_from_playwright_config():
    """A repo with a playwright.config.* (and no vscode/bot markers) classifies as web."""
    sut = _sut_with_files({"playwright.config.ts": "export default {};", "index.html": "<html>"})
    assert _qa_mod._detect_kind(sut) == "web"


def test_detect_kind_bot_from_telegram_dep():
    sut = _sut_with_files({"package.json": '{"dependencies": {"telegraf": "^4.0.0"}}'})
    assert _qa_mod._detect_kind(sut) == "bot"


def test_detect_kind_bot_from_python_requirements():
    """A Python Telegram bot (NO package.json) is detected from requirements.txt — without this
    it falls through to `backend` and the wrong runbook (review-cli#61)."""
    sut = _sut_with_files({
        "requirements.txt": "# a bot\npython-telegram-bot>=20.0\nrequests==2.31.0\n",
        "bot.py": "print('bot')",
    })
    assert _qa_mod._detect_kind(sut) == "bot"


def test_detect_kind_bot_from_pyproject_pep621():
    """A PEP 621 pyproject.toml `[project].dependencies` Telegram marker classifies as bot."""
    sut = _sut_with_files({
        "pyproject.toml": (
            '[project]\nname = "mybot"\n'
            'dependencies = ["aiogram[fast]>=3,<4", "httpx"]\n'
        ),
    })
    assert _qa_mod._detect_kind(sut) == "bot"


def test_detect_kind_bot_from_pyproject_poetry():
    """A Poetry `[tool.poetry.dependencies]` Telegram marker classifies as bot (the table keys
    ARE the distribution names)."""
    sut = _sut_with_files({
        "pyproject.toml": (
            '[tool.poetry.dependencies]\npython = "^3.11"\npyrogram = "^2.0"\n'
        ),
    })
    assert _qa_mod._detect_kind(sut) == "bot"


def test_detect_kind_python_non_bot_is_backend():
    """A Python project whose deps carry NO bot marker stays `backend` (no false positive)."""
    sut = _sut_with_files({"requirements.txt": "flask\nsqlalchemy\n", "app.py": "x = 1"})
    assert _qa_mod._detect_kind(sut) == "backend"


def test_detect_kind_pyproject_no_marker_is_backend():
    """A pyproject.toml with non-bot deps stays `backend` (the pyproject equivalent of the
    requirements.txt no-marker case)."""
    sut = _sut_with_files({
        "pyproject.toml": '[project]\nname = "svc"\ndependencies = ["fastapi", "uvicorn"]\n',
    })
    assert _qa_mod._detect_kind(sut) == "backend"


def test_detect_kind_bot_separator_insensitive():
    """PEP 503 canonicalization: `python_telegram_bot` (underscores) matches the canonical
    `python-telegram-bot` marker — the same false-negative class the fix closes."""
    sut = _sut_with_files({"requirements.txt": "python_telegram_bot>=20\n"})
    assert _qa_mod._detect_kind(sut) == "bot"


def test_detect_kind_bot_pytelegrambotapi_dist():
    """The 'telebot' import package ships on PyPI as `pyTelegramBotAPI` — the DIST name in a
    requirements file. It is recognised (case + canonical form)."""
    sut = _sut_with_files({"requirements.txt": "pyTelegramBotAPI==4.14\n"})
    assert _qa_mod._detect_kind(sut) == "bot"
    assert _qa_mod._canon_dist("python_telegram_bot") == "python-telegram-bot"
    assert _qa_mod._canon_dist("Py.Telegram.Bot.API") == "py-telegram-bot-api"


def test_python_dep_parsing_is_robust():
    """The Python dep parser is best-effort: a broken requirements.txt / pyproject.toml, an
    options line, and an unreadable file never crash detection."""
    sut = _sut_with_files({
        "requirements.txt": "-r other.txt\n--index-url https://x\naiogram ; python_version>'3.8'\n",
        "pyproject.toml": "this is [not valid toml",
    })
    # The `aiogram` line (with an env marker) is still recognised despite the noise.
    assert _qa_mod._detect_kind(sut) == "bot"
    assert _qa_mod._dist_name("aiogram[fast]>=3,<4 ; python_version>'3.8'") == "aiogram"
    assert _qa_mod._dist_name("python-telegram-bot @ https://example/x.whl") == "python-telegram-bot"
    assert _qa_mod._dist_name("Telethon==1.0") == "telethon"  # lower-cased
    assert _qa_mod._dist_name("") == ""


def test_detect_kind_falls_back_to_backend_when_inconclusive():
    """No package.json / no markers → backend (the agent is the real detector; the Python
    pass only seeds the runbook). A broken/invalid package.json must NOT crash detection."""
    plain = _sut_with_files({"main.py": "print('hi')"})
    assert _qa_mod._detect_kind(plain) == "backend"
    broken = _sut_with_files({"package.json": "{not valid json"})
    assert _qa_mod._detect_kind(broken) == "backend"


def test_detect_kind_ext_wins_over_web_markers():
    """First-match-wins order is ext → web → bot → backend: a package with BOTH a vscode
    engine and a vite dep classifies as ext (the more specific shape)."""
    sut = _sut_with_files({
        "package.json": '{"engines": {"vscode": "^1.80.0"}, "dependencies": {"vite": "^5"}}',
    })
    assert _qa_mod._detect_kind(sut) == "ext"


def test_negative_max_cases_is_rejected():
    """--max-cases must be >= 0 (0 = no cap). A negative value is a usage error (exit 2), not
    silently treated as 'no cap'."""
    sut = _sut_with_suite(["only case"])
    _git_init_commit(sut)
    with _fake_tester_env(verdict="PASS"):
        rc, _out, err = _run(["qa", sut, "--max-cases", "-1"])
    assert rc == 2, (rc, err)
    assert "--max-cases must be >= 0" in err, err


def test_in_place_dirty_tree_is_usage_exit_2_not_blocked():
    """--in-place over a tree with uncommitted changes is a USAGE error (exit 2), distinct
    from EXIT_QA_SUT_BOOT_FAILED (the SUT couldn't be brought up) — so CI tells 'you refused
    a dirty in-place run' apart from 'infra broke' (review finding)."""
    sut = _sut_with_suite(["only case"])
    _git_init_commit(sut)
    (Path(sut) / "dirty.txt").write_text("uncommitted", encoding="utf-8")  # dirty the tree
    with _fake_tester_env(verdict="PASS"):
        rc, _out, err = _run(["qa", sut, "--in-place"])
    assert rc == 2, (rc, err)
    assert "uncommitted changes" in err, err


def test_visual_is_rejected_for_qa_before_vision_work():
    """qa does not consume --visual; passing it is a usage error (exit 2), rejected in cli.py
    BEFORE the paid vision pipeline runs (review finding). Works regardless of the image being
    valid — the guard fires before cvGate."""
    sut = _sut_with_suite(["only case"])
    _git_init_commit(sut)
    import tempfile as _tf
    img = Path(_tf.mkdtemp()) / "shot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")  # not a real image — guard must fire before cvGate
    with _fake_tester_env(verdict="PASS"):
        rc, _out, err = _run(["qa", sut, "--visual", str(img)])
    assert rc == 2, (rc, err)
    assert "--visual is not supported" in err, err


def test_qa_run_stats_record_a_single_seat_not_the_default_panel():
    """qa is single-seat, so its run-stats / ETA must record pool_size=1 (the resolved
    backend), NOT len(DEFAULT_MODELS) (review finding). Verified by reading the stats file
    after a fake qa run."""
    import json

    sut = _sut_with_suite(["only case"])
    _git_init_commit(sut)
    stats = Path(tempfile.mkdtemp()) / "stats.jsonl"
    old_stats = os.environ.get("REVIEW_STATS_FILE")
    old_tester = os.environ.get("REVIEW_QA_TESTER")
    os.environ["REVIEW_STATS_FILE"] = str(stats)
    os.environ.pop("REVIEW_QA_TESTER", None)
    try:
        with _fake_tester_env(verdict="PASS"):
            rc, _out, _err = _run(["qa", sut])
        assert rc == 0, rc
        # The run MUST be recorded (qa explicitly tallies its single-seat outcome), with
        # exactly one model seat — not the default panel.
        assert stats.exists() and stats.read_text(encoding="utf-8").strip(), \
            "qa must record a run-stats entry (it tallies its own single-seat outcome)"
        rec = json.loads(stats.read_text(encoding="utf-8").splitlines()[-1])
        assert rec.get("mode") == "qa", rec
        assert len(rec.get("models", [])) == 1, ("qa must record a single seat", rec)
        assert rec["models"] == ["claude"], rec
    finally:
        for k, v in (("REVIEW_STATS_FILE", old_stats), ("REVIEW_QA_TESTER", old_tester)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_unsupported_m_tester_is_usage_error():
    """`review qa -m gemini` is a usage error (exit 2) — qa can't use gemini as a tester, and
    must not silently fall through to the un-caged claude default (review)."""
    sut = _sut_with_suite(["only case"])
    _git_init_commit(sut)
    old = os.environ.get("REVIEW_QA_TESTER")
    os.environ.pop("REVIEW_QA_TESTER", None)
    with _fake_tester_env(verdict="PASS"):
        try:
            rc, _out, err = _run(["qa", sut, "-m", "gemini"])
        finally:
            if old is not None:
                os.environ["REVIEW_QA_TESTER"] = old
    assert rc == 2, (rc, err)
    assert "qa cannot use" in err or "not a supported qa tester" in err, err


def test_bare_qa_defaults_to_claude_backend_not_codex():
    """CRITICAL (review P1 regression): bare `review qa <sut>` with NO -m and NO
    REVIEW_QA_TESTER must select the DOCUMENTED DEFAULT claude — NOT codex (which is the first
    entry of the shared default panel list). The handler must pass only EXPLICIT -m to the
    backend resolver, never the implicit default list."""
    sut = _sut_with_suite(["only case"])
    _git_init_commit(sut)
    old = os.environ.get("REVIEW_QA_TESTER")
    os.environ.pop("REVIEW_QA_TESTER", None)
    with _fake_tester_env(verdict="PASS"):
        try:
            rc, _out, err = _run(["qa", sut])  # no -m
        finally:
            if old is not None:
                os.environ["REVIEW_QA_TESTER"] = old
    assert rc == 0, (rc, err)
    assert "backend=claude" in err, ("bare qa must default to claude, not codex", err)


def test_m_codex_selects_codex_backend_label():
    """`review qa -m codex` must select the codex backend (not silently run claude) — the
    startup log names backend=codex (review P2). Driven via the mocked tester so no real
    backend spawns; the label proves the resolution."""
    sut = _sut_with_suite(["only case"])
    _git_init_commit(sut)
    old = os.environ.get("REVIEW_QA_TESTER")
    os.environ.pop("REVIEW_QA_TESTER", None)
    with _fake_tester_env(verdict="PASS"):
        try:
            rc, _out, err = _run(["qa", sut, "-m", "codex"])
        finally:
            if old is not None:
                os.environ["REVIEW_QA_TESTER"] = old
    assert rc == 0, (rc, err)
    assert "backend=codex" in err, err


def test_dirty_worktree_run_warns_about_stale_head():
    """A default (worktree) run on a SUT with uncommitted changes must WARN that the worktree
    tests committed HEAD, not the working-tree edits — so a PASS isn't mistaken for covering
    uncommitted code (review P1)."""
    sut = _sut_with_suite(["only case"])
    _git_init_commit(sut)
    (Path(sut) / "extra.txt").write_text("uncommitted", encoding="utf-8")  # dirty the tree
    with _fake_tester_env(verdict="PASS"):
        rc, _out, err = _run(["qa", sut])
    assert rc == 0, (rc, err)
    assert "uncommitted changes" in err and "committed HEAD" in err, err


def test_blocked_verdict_maps_to_sut_boot_failed_at_handler():
    """A BLOCKED verdict (the SUT couldn't be brought up) maps to EXIT_QA_SUT_BOOT_FAILED at
    the handler — distinct from a finding and from no-suites, proving the handler maps the
    PARSED verdict to its exit class, not a fixed code."""
    sut = _sut_with_suite(["only case"])
    _git_init_commit(sut)
    with _fake_tester_env(verdict="BLOCKED"):
        rc, _out, err = _run(["qa", sut])
    assert rc == cli.EXIT_QA_SUT_BOOT_FAILED, (rc, err)
    assert "VERDICT=BLOCKED" in err, err


def test_custom_report_path_via_cli_flag_is_written():
    """`--report <path>` routes the transcript to the user's path (covering the CLI flag →
    _report_path branch, including ~-expansion is exercised by the path resolver)."""
    sut = _sut_with_suite(["only case"])
    _git_init_commit(sut)
    report = Path(tempfile.mkdtemp()) / "my-report.md"
    with _fake_tester_env(verdict="PASS"):
        rc, _out, _err = _run(["qa", sut, "--report", str(report)])
    assert rc == 0, rc
    assert report.exists(), "the custom --report path must be written"
    assert "QA RESULTS" in report.read_text(encoding="utf-8")


def test_new_qa_flags_get_friendly_verbless_pointer():
    """A verb-less `review --kind …` / `--report …` / `--max-cases …` / `--in-place` must
    emit the friendly 'use the subcommand' pointer (exit 2), not argparse's opaque error —
    they are in _SUBCOMMAND_ONLY_FLAGS."""
    for flag, val in (("--kind", "backend"), ("--report", "r.md"), ("--max-cases", "1"), ("--in-place", None)):
        argv = ["--in-place"] if flag == "--in-place" else [flag, val]
        rc, _out, err = _run(argv)
        assert rc == 2, (flag, rc, err)
        assert "subcommand" in err, (flag, err)
        assert "unrecognized arguments" not in err, (flag, err)


def test_default_report_goes_outside_the_sut_tree():
    """The default report must NOT land in the SUT git tree (else it dirties a clean repo and
    wrongly trips the next --in-place guard). A default-report worktree run must leave the SUT
    working tree clean (review finding)."""
    sut = _sut_with_suite(["only case"])
    _git_init_commit(sut)
    with _fake_tester_env(verdict="PASS"):
        rc, _out, _err = _run(["qa", sut])  # no --report → default path
    assert rc == 0, rc
    status = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "status", "--porcelain"],
        cwd=sut, capture_output=True, text=True,
    )
    assert status.stdout.strip() == "", ("default report dirtied the SUT tree", status.stdout)


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
