#!/usr/bin/env python3
"""Guarded smoke test for review-cli — the Python port of the old tests/smoke.sh.

The ecosystem is Python-only, so the smoke suite is Python too: this file drives the real
``bin/review`` CLI through ``subprocess`` (the same assertions the bash script made) and then
runs the standalone ``tests/test_*.py`` unit files. No API keys or live backends are needed —
every check is ``--help`` / ``--list-defaults`` / ``--show-board`` / a guarded error path / a
unit file that stubs the backend boundary, so CI stays green without secrets.

Two ways to run it, both green-or-red with a real exit code:
  * ``python tests/smoke.py``  — standalone, prints PASS/SKIP/FAIL per check (what CI runs);
  * ``pytest tests/smoke.py``  — every ``test_*`` function is collected as a pytest test.

REVIEW_STATS_FILE is redirected to a throwaway temp file for the whole process so no check that
invokes the real CLI appends to the user's real ~/.config/review-cli/run-stats.jsonl.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REVIEW = str(REPO / "bin" / "review")


def _redirect_run_stats() -> None:
    """Redirect the run-stats store to a throwaway temp file for the WHOLE process (exported so
    every child `review`/python3 sees it), so no CLI invocation pollutes the user's real
    ~/.config/review-cli/run-stats.jsonl. Idempotent; called from the standalone runner AND a
    pytest fixture — NEVER at import time, so collecting this file under pytest can't silently
    mutate the pytest process's environment for unrelated tests (glm review finding). Only
    creates the temp dir when the env var is unset (no leaked dirs on repeated calls)."""
    if not os.environ.get("REVIEW_STATS_FILE"):
        os.environ["REVIEW_STATS_FILE"] = str(Path(tempfile.mkdtemp()) / "run-stats.jsonl")


# When collected by pytest, redirect run-stats via an autouse session fixture (still NOT at
# import time). Under the standalone runner, main() calls _redirect_run_stats() directly.
try:
    import pytest

    @pytest.fixture(autouse=True, scope="session")
    def _smoke_session_env():  # noqa: D401
        _redirect_run_stats()
        yield
except ImportError:
    pytest = None  # type: ignore[assignment]


class SmokeError(AssertionError):
    """A failed smoke assertion (carries the command + captured output for the report)."""


class _SkipCheck(Exception):
    """Raised by a check that is not applicable on this host (an OPTIONAL dep is missing). The
    standalone runner reports it as SKIP and does NOT count it as a failure; under pytest the
    check calls pytest.skip() instead, so it never reaches this."""


# --- shelling out to the real CLI -------------------------------------------------------------
def run(
    *args: str,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    stdin: str | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    """Invoke ``bin/review`` (or an arbitrary argv when the first arg is absolute) and capture
    stdout+stderr together. Never raises on a non-zero exit — the caller asserts on rc."""
    argv = list(args)
    # Prepend the shim unless an absolute argv[0] was passed (an explicit binary path); a bare
    # `run()` with no args runs `bin/review` with no subcommand.
    if not argv or not os.path.isabs(argv[0]):
        argv = [REVIEW, *argv]
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        argv,
        input=stdin,
        capture_output=True,
        text=True,
        env=full_env,
        cwd=cwd,
        timeout=timeout,
    )


def review_out(*args: str, **kw) -> str:
    """Run ``bin/review <args>``, require exit 0, return combined stdout+stderr.

    Capture-then-assert (never ``review | grep``): a shim that crashes mid-output must fail the
    rc check, not slip through because a later pipe stage matched.
    """
    p = run(*args, **kw)
    if p.returncode != 0:
        raise SmokeError(f"`review {' '.join(args)}` exited {p.returncode}\n{p.stdout}\n{p.stderr}")
    return p.stdout + p.stderr


def assert_in(needle: str, haystack: str, what: str = "") -> None:
    if needle not in haystack:
        raise SmokeError(f"expected {needle!r} in output {what}\n--- output ---\n{haystack[:2000]}")


def assert_not_in(needle: str, haystack: str, what: str = "") -> None:
    if needle in haystack:
        raise SmokeError(f"did NOT expect {needle!r} in output {what}\n--- output ---\n{haystack[:2000]}")


def assert_fails(*args: str, **kw) -> subprocess.CompletedProcess:
    """The CLI must EXIT NON-ZERO for these args (the bash `! review …` idiom)."""
    p = run(*args, **kw)
    if p.returncode == 0:
        raise SmokeError(f"`review {' '.join(args)}` unexpectedly SUCCEEDED (expected non-zero)")
    return p


def run_unit(test_file: str, *, env: dict[str, str] | None = None, timeout: int = 600) -> None:
    """Run a standalone ``tests/test_*.py`` unit file as a subprocess (matching how it ran under
    smoke.sh) and require exit 0, surfacing its captured output on failure."""
    p = subprocess.run(
        [sys.executable, str(REPO / "tests" / test_file)],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        timeout=timeout,
    )
    if p.returncode != 0:
        raise SmokeError(f"{test_file} exited {p.returncode}\n{p.stdout}\n{p.stderr}")


def _tmp() -> str:
    return tempfile.mkdtemp()


def _has(mod: str) -> bool:
    return subprocess.run([sys.executable, "-c", f"import {mod}"], capture_output=True).returncode == 0


# --- CLI surface: subcommands, removed flags, meta ------------------------------------------
def test_list_defaults_and_help():
    assert_in("codex", review_out("--list-defaults"))
    review_out("--help")  # exit 0 is the assertion


def test_shim_bootstrap_from_outside_repo_with_pythonpath_cleared():
    """The installed `review` is a SYMLINK into this repo; its bin/review shim is the ONLY thing
    that makes `import reviewlib` resolve from an arbitrary cwd. Prove it from a dir OUTSIDE the
    repo with PYTHONPATH cleared — a stale shadowing console-script would die here."""
    shim = Path(REVIEW)
    if not os.access(shim, os.X_OK):
        raise SmokeError(f"shim missing its exec bit: {shim}")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    p = subprocess.run([str(shim), "--list-defaults"], capture_output=True, text=True, env=env, cwd=_tmp())
    if p.returncode != 0:
        raise SmokeError(f"shim from outside repo exited {p.returncode}\n{p.stdout}\n{p.stderr}")
    assert_in("codex", p.stdout)


def test_modes_are_subcommands_not_flags():
    top = review_out("--help")
    assert_in("subcommands:", top)
    for sub in ("brainstorm", "just-ask", "quorum"):
        assert_in(sub, top)
    assert_in("topic", review_out("brainstorm", "--help"))
    assert_in("question", review_out("just-ask", "--help"))
    assert_in("question", review_out("quorum", "--help"))


def test_brainstorm_only_flags_are_scoped():
    assert_in("--rounds", review_out("brainstorm", "--help"))
    assert_fails("just-ask", "q", "--rounds", "5")
    assert_not_in("--rounds", review_out("--help"))


def test_removed_mode_flags_error_helpfully():
    for flag, hint in (("--brainstorm", "review brainstorm"), ("--quorum", "review quorum"), ("--just-ask", "review just-ask")):
        p = assert_fails(flag, "x")
        assert_in(hint, p.stdout + p.stderr)


def test_removed_mcp_and_ln_flags_fail_loud():
    p = assert_fails("--mcp")
    out = p.stdout + p.stderr
    assert_in("`--mcp` was removed", out)
    assert_in("mcp.json", out)
    assert_not_in("unrecognized arguments", out)
    p2 = assert_fails("--ln")
    assert_in("`--ln` was removed", p2.stdout + p2.stderr)


def test_diff_subcommand_and_review_review_pointer():
    # The diff review is the `diff` SUBCOMMAND (review-cli#44, renamed from the stuttering
    # `review review`). A bare meta `review --list-defaults` still works; `review diff
    # --list-defaults` works explicitly; bare `review` (no subcommand) points at `review diff`;
    # the removed `review review` verb errors (exit 2) pointing at the new verb.
    assert_in("codex", review_out("--list-defaults"))
    assert_in("codex", review_out("diff", "--list-defaults"))
    assert_in("review diff", review_out())  # bare review points at the new verb
    p = assert_fails("review")             # `review review` is gone -> usage exit 2
    assert_in("review diff", p.stdout + p.stderr)


def test_non_git_dir_fails_gracefully_with_stable_code():
    nongit = _tmp()
    p = run("diff", "-C", nongit, stdin="")
    if p.returncode != 3:
        raise SmokeError(f"expected EXIT_NOT_A_REPO=3, got {p.returncode}\n{p.stdout}\n{p.stderr}")
    out = p.stdout + p.stderr
    assert_in("not in a git repository", out)
    assert_in("diff review needs a repo", out)
    assert_in("just-ask", out)
    assert_in("cd into a repo", out)
    assert_not_in("Traceback (most recent call last)", out)
    assert_not_in("RuntimeError", out)
    # A no-git mode + meta flags still work from a non-git dir.
    assert_in("the question to ask", review_out("just-ask", "-C", nongit, "--help"))
    assert_in("codex", review_out("-C", nongit, "--list-defaults"))


def test_visual_flags_in_help():
    # The composable --visual feature + companions ride the `diff` subcommand (review-cli#44),
    # so they show on `review diff --help`, not the top-level overview.
    diff_help = review_out("diff", "--help")
    for flag in ("--visual", "--no-ai", "--strict", "--no-local-model"):
        assert_in(flag, diff_help)


def test_board_flags_and_listing():
    top = review_out("--help")
    assert_in("--show-board", top)
    assert_in("--pool", top)
    assert_not_in("--no-board", top)
    board = review_out("--show-board")
    for needle in (
        "architect", "claude:claude-fable-5", "claude:claude-opus-4-8",
        "oc:commandcode/deepseek/deepseek-v4-pro", "oc:zai/glm-5.2", "contracts", "8 seats", "#1",
    ):
        assert_in(needle, board, "in --show-board")
    assert_in("agentic", board.lower())
    assert_in("diff-only", board.lower())
    assert_in("priority", board.lower())
    # Seat 3 is the agentic codex route.
    codex_line = next((ln for ln in board.splitlines() if "Codex" in ln), "")
    assert_in("codex", codex_line)
    assert_in("agentic", codex_line)


def test_failover_pool_listing():
    board = review_out("--show-board")
    assert_in("live pool", board.lower())
    assert_in("reserve", board)
    assert_in("pool 4", board.lower())
    pool2 = review_out("--show-board", "--pool", "2")
    if pool2.count("[pool") > 2:
        raise SmokeError(f"--pool 2 tagged more than 2 seats:\n{pool2}")
    pool0 = review_out("--show-board", "--pool", "0")
    if "[reserve]" in pool0:
        raise SmokeError("--pool 0 should leave no reserve seats")
    assert_fails("--no-board", "--show-board")


def test_retry_flag_surface_and_export():
    """`--retry` is on the diff surface, documents the transient/seat-fatal split, and the CLI
    export+clamp path accepts an out-of-range value (clamped, never an argparse error)."""
    diff_help = review_out("diff", "--help")
    assert_in("--retry", diff_help)
    assert_in("transient", diff_help.lower())
    assert_in("REVIEW_RETRY_COUNT", diff_help)
    # An out-of-range `--retry` must be ACCEPTED (clamped), not rejected: the export path runs
    # at parse time, before any backend call. With no staged diff the run exits 1 ("No diff to
    # review") — a CLEAN, non-argparse exit (argparse usage errors are exit 2). So the flag
    # parsed and clamped fine; assert it is NOT a usage error.
    empty = _tmp()
    subprocess.run(["git", "init", "-q"], cwd=empty, check=True)
    for val in ("9999", "-4", "0", "3"):
        p = run("diff", "--staged", "--retry", val, "-C", empty)
        if p.returncode == 2:
            raise SmokeError(f"--retry {val} was an argparse usage error (should clamp):\n{p.stderr}")


def test_output_flag():
    top = review_out("--help")
    assert_in("-o FILE", top)
    assert_in("noclobber", top.lower())
    out_path = Path(_tmp()) / "sub" / "out.txt"  # parent does not exist yet
    assert_in("codex", review_out("-o", str(out_path), "--list-defaults"))
    assert_in("codex", out_path.read_text())  # parent dir made + file written
    # Overwrite must work even under the shell's noclobber (the bug -o fixes) — a fresh write.
    review_out("-o", str(out_path), "--show-board")
    assert_in("architect", out_path.read_text())


def test_brainstorm_combines_with_diff_grounding():
    bs = review_out("brainstorm", "--help")
    assert_in("--diff", bs)
    assert_in("--staged", bs)


def test_specweb_subcommands():
    sw = review_out("spec-web", "--help")
    for flag in ("--seed", "--host", "--exit-on-submit"):
        assert_in(flag, sw)
    reply = review_out("spec-web", "reply", "--help")
    assert_in("--spec", reply)
    assert_in("comment_id", reply)


def test_single_file_cli_always_parses():
    subprocess.run(
        [sys.executable, "-c", "import ast; ast.parse(open('bin/review').read())"],
        cwd=str(REPO), check=True,
    )


# --- the dashboard as a managed service (the lib-present + lib-ABSENT contracts) ------------
def test_dashboard_serve_help_parses():
    serve = review_out("dashboard", "__serve", "--help")
    for flag in ("--no-open", "--port", "--host"):
        assert_in(flag, serve)


def test_dashboard_managed_surface_and_lib_absent_fallback():
    """The managed-service surface depends on the optional `agenttools_service` lib. When it is
    installed, the lifecycle subcommands + bare-HELP-no-launch are wired; when ABSENT, a genuine
    lifecycle action fails with an ACTIONABLE error (exit 4, no traceback) BUT the bare-HELP /
    `--help` contract still holds (exit 0, help-only, no launch) — the lib-absent fallback path."""
    if _has("agenttools_service"):
        top = review_out("dashboard", "--help")
        assert_in("--port", top)
        for action in ("run", "start", "stop", "status", "enable", "disable"):
            assert_in(action, top)
        bare = review_out("dashboard")
        assert_in("status", bare)
        assert_in("managed service", bare.lower())
        # `status` with nothing running reports a clean state + stable exit 3, no traceback.
        p = run("dashboard", "status", env={"XDG_STATE_HOME": _tmp()})
        if p.returncode != 3:
            raise SmokeError(f"dashboard status (nothing running) expected exit 3, got {p.returncode}")
        assert_not_in("Traceback (most recent call last)", p.stdout + p.stderr)
    else:
        # A genuine lifecycle action without the lib: stable exit 4 + actionable error, no traceback.
        p = run("dashboard", "status")
        if p.returncode != 4:
            raise SmokeError(f"lib-absent dashboard status expected exit 4, got {p.returncode}")
        out = p.stdout + p.stderr
        assert_in("agenttools_service", out)
        assert_in("pip install", out.lower())
        assert_not_in("Traceback (most recent call last)", out)
        # The bare-HELP contract does NOT depend on the lib: bare `review dashboard` AND a
        # help-only `review dashboard --help` print help + launch nothing (exit 0), advertising
        # every action — this is the lib-absent fallback the CI gate exercises.
        bare = run("dashboard")
        if bare.returncode != 0:
            raise SmokeError(f"lib-absent bare `dashboard` expected exit 0, got {bare.returncode}")
        for action in ("run", "start", "stop", "status", "enable", "disable"):
            assert_in(action, bare.stdout + bare.stderr)
        assert_not_in("Traceback (most recent call last)", bare.stdout + bare.stderr)
        help_out = run("dashboard", "--help")
        if help_out.returncode != 0:
            raise SmokeError(f"lib-absent `dashboard --help` expected exit 0, got {help_out.returncode}")
        assert_in("status", help_out.stdout + help_out.stderr)


# --- resumable sessions listing (against a temp log dir) ------------------------------------
def test_sessions_listing():
    s = review_out("sessions", "--help")
    for flag in ("--all", "--resume", "--diff", "--force"):
        assert_in(flag, s)
    sess_dir = Path(_tmp())
    (sess_dir / "20260101T000000_000001Z-brainstorm.md").write_text(
        "# Brainstorm: smoke-complete\n\npanel=codex moderator=opus rounds>=5 max=8\n"
        "# Round 1\n#### codex\nx\n## Moderator (round 1)\nok\nDECISION: STOP\n# Final synthesis\ndone\n"
    )
    (sess_dir / "20260101T000100_000001Z-brainstorm.md").write_text(
        "# Brainstorm: smoke-dead\n\npanel=codex moderator=opus rounds>=5 max=8\n# Round 1\n#### codex\n(no output)\n"
    )
    env = {"REVIEW_LOG_DIR": str(sess_dir)}
    assert_in("smoke-complete", review_out("sessions", env=env))
    assert_not_in("smoke-dead", review_out("sessions", env=env))  # default hides interrupted
    all_out = review_out("sessions", "-a", env=env)
    assert_in("smoke-dead", all_out)
    assert_in("interrupted", all_out)
    assert_fails("sessions", "-s", "NOPE", env=env)


# --- the standalone unit files (each was `python3 tests/test_*.py` in smoke.sh) -------------
# The dashboard wiring + data/parser/store/server tests, plus the rest of the reviewlib suite.
# Each runs as its own subprocess (self-contained, redirects its own temp dirs). A dedicated
# sentinel (not a bare None) means "give this file a FRESH temp dir for that env var".
_FRESH_TMP = object()
_UNIT_FILES = [
    ("test_dashboard_service.py", {}),
    ("test_dashboard.py", {}),
    ("test_streaming.py", {"REVIEW_LOG_DIR": _FRESH_TMP}),
    ("test_workspace_trust.py", {}),
    ("test_moderator.py", {}),
    ("test_cwd.py", {}),
    ("test_shim_bootstrap.py", {}),
    ("test_install_shadow_warning.py", {}),
    ("test_no_git_repo.py", {}),
    ("test_output_flag.py", {}),
    ("test_opencode_realrepo.py", {}),
    ("test_specweb.py", {}),
    ("test_claude_api.py", {}),
    ("test_provider_keys.py", {}),
    ("test_reviewer_board.py", {}),
    ("test_review_marker.py", {}),
    ("test_failover_pool.py", {}),
    ("test_inseat_retry.py", {"REVIEW_LOG_DIR": _FRESH_TMP}),
    ("test_brainstorm_diff.py", {}),
    ("test_brainstorm_dead_panel.py", {"REVIEW_LOG_DIR": _FRESH_TMP}),
    ("test_mode_subcommands.py", {}),
    ("test_sessions.py", {}),
    ("test_e2e_resume.py", {}),
    ("test_run_stats.py", {"REVIEW_LOG_DIR": _FRESH_TMP}),
    ("test_backstop.py", {}),
    # From main's review-UX-chain (review-cli#44): help defaults, install hook text / state, topic help.
    ("test_help_defaults.py", {}),
    ("test_install_hook_text.py", {}),
    ("test_install_state.py", {}),
    ("test_topic_help.py", {}),
]
# The visual-verification files run from test_visual_verification_suite (gated on magick/Pillow);
# smoke.py itself is the runner, not a unit file. Everything else in tests/test_*.py must be in
# _UNIT_FILES — a new file that isn't listed would silently never run, so assert coverage below.
_VISUAL_UNIT_FILES = frozenset({
    "test_cv_gate.py", "test_vision_client.py", "test_policy_engine.py", "test_pipeline.py",
    "test_preclassifier.py", "test_visual_compose.py", "test_visual_registry.py",
    "test_selection_highlight.py", "test_visual_fanout.py",
})


def _unit_env(spec: dict) -> dict[str, str]:
    return {k: (_tmp() if v is _FRESH_TMP else v) for k, v in spec.items()}


def test_every_unit_file_is_run():
    """A new tests/test_*.py that is added but not listed in _UNIT_FILES (nor the visual set)
    would silently never run. Fail loudly here so the smoke runner stays exhaustive."""
    on_disk = {p.name for p in (REPO / "tests").glob("test_*.py")}
    listed = {f for f, _ in _UNIT_FILES} | _VISUAL_UNIT_FILES
    missing = on_disk - listed
    assert not missing, f"unit test files not run by smoke.py: {sorted(missing)} — add them to _UNIT_FILES"


def test_reviewlib_unit_files():
    for fname, env_spec in _UNIT_FILES:
        run_unit(fname, env=_unit_env(env_spec))


def test_visual_verification_suite():
    """Stage-1 visual-verification suite. Needs ImageMagick v7 (`magick`) + Pillow; on a bare
    CI without them, SKIP loudly (they are not a runtime requirement) rather than fail."""
    have_magick = subprocess.run(["bash", "-lc", "command -v magick"], capture_output=True).returncode == 0
    if not (have_magick and _has("PIL")):
        msg = "visual-verification: need ImageMagick (`magick`) + Pillow (pip install -e '.[test]')"
        # Only emit a pytest SKIP when pytest is the ACTIVE runner (PYTEST_CURRENT_TEST is set
        # during a pytest test invocation). `pytest is not None` only means it's importable —
        # the [test] extra always installs it, so under the standalone CI runner (`python
        # tests/smoke.py`) that check was true even though pytest isn't driving, and
        # `pytest.skip()`'s `Skipped` exception fell through main()'s `except Exception` as an
        # ERROR instead of a SKIP. Gate on the active-runner signal so standalone raises
        # `_SkipCheck` (reported as SKIP) and pytest still gets a real pytest SKIP.
        if pytest is not None and os.environ.get("PYTEST_CURRENT_TEST"):
            pytest.skip(msg)  # a true pytest SKIP, not a silent pass
        # Standalone runner: signal SKIP (main() reports it, does NOT count as a failure).
        raise _SkipCheck(msg)
    for fname in (
        "test_cv_gate.py", "test_vision_client.py", "test_policy_engine.py", "test_pipeline.py",
        "test_preclassifier.py", "test_visual_compose.py", "test_visual_registry.py",
        "test_selection_highlight.py", "test_visual_fanout.py",
    ):
        run_unit(fname)


# --- standalone runner (CI: `python tests/smoke.py`) ----------------------------------------
def _all_tests():
    return [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]


def main() -> int:
    _redirect_run_stats()
    tests = _all_tests()
    failures = skipped = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except _SkipCheck as exc:
            skipped += 1
            print(f"SKIP {name}: {exc}")
        except SmokeError as exc:
            failures += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            import traceback

            failures += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
    print(f"\n{'OK' if not failures else 'FAILED'}: {len(tests)} checks, {failures} failures, {skipped} skipped")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
