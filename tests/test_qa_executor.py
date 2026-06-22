#!/usr/bin/env python3
"""review qa — Phase 2: the write/exec agentic EXECUTOR + judge + the 2-fixture DoD.

These pin the Phase-2 contract (docs/specs/review-qa.md §8/§9):

  * the tester SYSTEM PROMPT carries the role, the un-caged ground rules, the per-kind
    runbook, the human-authored suites, AND the machine-parsed ``## QA RESULTS`` contract;
  * the ``## QA RESULTS`` PARSER pulls the verdict / finding count / worst severity / case
    tally out of a full transcript, ignoring an earlier echo of the template;
  * the VERDICT -> EXIT mapping is report-only (a found bug exits 0; only BLOCKED / a
    missing verdict / the --strict finding flip are non-zero);
  * the isolated ``git worktree`` is created + REMOVED on every exit path;
  * the ``--in-place --full-auto`` (codex) guard refuses a dirty tree;
  * **the DoD**: a tiny SUT with a KNOWN BUG verdicts FAIL (with a finding) and a
    known-GOOD variant verdicts PASS, run through the REAL executor (worktree isolation +
    SUT exec + parser + verdict). The default CI path uses the deterministic MOCKED tester
    (``REVIEW_QA_FAKE_TESTER``) so no paid backend runs; the LIVE-backend variant is gated
    behind ``REVIEW_QA_LIVE=1`` (set it to prove a real claude/codex tester reaches the same
    verdicts).

WHICH RUNS WHERE:
  * normal CI / ``python tests/smoke.py``: the MOCKED-tester DoD + all prompt/parser/exit
    unit tests run — deterministic, no network, no model.
  * ``REVIEW_QA_LIVE=1 python3 tests/test_qa_executor.py``: ALSO runs the live-backend DoD
    (spawns the real claude/codex write/exec tester against the two fixtures). Costs tokens;
    opt-in only.

Runnable standalone (``python3 tests/test_qa_executor.py``) or under pytest.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib.qa import executor as ex  # noqa: E402
from reviewlib.qa import suites as suites_mod  # noqa: E402
from reviewlib.qa.suites import load_suites_text  # noqa: E402

_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "qa"


# --- helpers --------------------------------------------------------------------------
def _git(*args: str, cwd: Path) -> None:
    """Run a git command in ``cwd``, raising on failure (test setup must be solid).

    ``-c core.hooksPath=/dev/null`` disables any GLOBAL git hook for these setup commits:
    the dev machine may install a global ``core.hooksPath`` (e.g. a review-before-commit
    pre-commit gate) that would block the fixture's own ``git commit`` and break test setup
    that has nothing to do with the hook. The fixture repo is a throwaway temp dir."""
    subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=str(cwd), check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _materialize_fixture(name: str) -> Path:
    """Copy a fixture SUT into a fresh temp dir and ``git init`` + commit it, so the
    executor's default worktree isolation has a real standalone repo to branch from (the
    fixtures live inside review-cli; a worktree of review-cli itself would not put add.sh at
    the root). Returns the SUT path. The caller removes the temp dir."""
    sut = Path(tempfile.mkdtemp(prefix=f"qa-fixture-{name}-"))
    shutil.copytree(_FIXTURES / name, sut, dirs_exist_ok=True)
    _git("init", "-q", cwd=sut)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A", cwd=sut)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "fixture", cwd=sut)
    return sut


def _suite_files(sut: Path) -> list[Path]:
    return sorted((sut / "docs" / "tests" / "suites").glob("*.md"))


# --- the tester prompt (spec §8) ------------------------------------------------------
def test_prompt_has_role_groundrules_runbook_and_contract():
    """The built prompt must carry every §8 section so the agent knows it is an un-caged
    tester, what it may/may not do, the kind runbook, the suites, and the output contract."""
    suites_text = load_suites_text(_suite_files(_FIXTURES / "sut-good"), max_cases=1)
    prompt = ex.build_tester_prompt(
        kind="backend", suites_text=suites_text, sut_path=Path("/tmp/sut"), strict=False,
    )
    assert "ROLE." in prompt and "TESTER" in prompt
    assert "GROUND RULES." in prompt
    assert "run shell commands" in prompt  # un-caged grant
    assert "RUNBOOK (backend" in prompt
    assert "add 2 and 2 gives 4" in prompt  # the suite case bled into the prompt
    assert ex._QA_RESULTS_HEADER in prompt
    assert "VERDICT: PASS | FAIL | BLOCKED" in prompt


def test_prompt_injects_only_the_matching_runbook():
    """Only the kind's own runbook is injected — a backend prompt must not carry the web/ext
    runbook text (the agent shouldn't be told to drive Playwright for a CLI SUT)."""
    prompt = ex.build_tester_prompt(
        kind="backend", suites_text="## Case: x", sut_path=Path("/tmp/s"),
    )
    assert "RUNBOOK (backend" in prompt
    assert "RUNBOOK (web" not in prompt and "RUNBOOK (vscode extension" not in prompt


def test_prompt_strict_note_reflects_strictness():
    strict = ex.build_tester_prompt(kind="backend", suites_text="x", sut_path=Path("/s"), strict=True)
    soft = ex.build_tester_prompt(kind="backend", suites_text="x", sut_path=Path("/s"), strict=False)
    assert "--strict" in strict and "ANY finding fails the build" in strict
    assert "report-only run" in soft


# --- the ## QA RESULTS parser ---------------------------------------------------------
_PASS_TAIL = (
    "I ran the case.\n## QA RESULTS\n"
    "SUT: /s   KIND: backend   BRING-UP: local\n"
    "CASES: 1 run, 1 passed, 0 failed, 0 blocked\n\n"
    "### FINDINGS\nno findings\n\n### BLOCKED\nnone\n\nVERDICT: PASS\n"
)
_FAIL_TAIL = (
    "I ran the case and it broke.\n## QA RESULTS\n"
    "SUT: /s   KIND: backend   BRING-UP: local\n"
    "CASES: 1 run, 0 passed, 1 failed, 0 blocked\n\n"
    "### FINDINGS\n"
    "- [P1] add 2 and 2 gives 4 — printed 5, expected 4 — proof: stdout '5' — repro: sh add.sh 2 2\n"
    "\n### BLOCKED\nnone\n\nVERDICT: FAIL\n"
)


def test_parse_pass_tail():
    verdict, findings, max_sev, cases = ex.parse_qa_results(_PASS_TAIL)
    assert verdict == "PASS"
    assert findings == 0 and max_sev is None
    assert cases == {"run": 1, "passed": 1, "failed": 0, "blocked": 0}


def test_parse_fail_tail_with_finding():
    verdict, findings, max_sev, cases = ex.parse_qa_results(_FAIL_TAIL)
    assert verdict == "FAIL"
    assert findings == 1 and max_sev == "P1"
    assert cases == {"run": 1, "passed": 0, "failed": 1, "blocked": 0}


def test_parse_picks_the_last_results_block():
    """A chatty agent that echoes the contract template earlier must not poison the verdict —
    the binding one is the LAST ## QA RESULTS block (emitted after the work)."""
    chatty = "Here is the contract I'll emit:\n## QA RESULTS\nVERDICT: PASS\n...then I work...\n" + _FAIL_TAIL
    verdict, _findings, _sev, _cases = ex.parse_qa_results(chatty)
    assert verdict == "FAIL"


def test_parse_missing_verdict_is_unknown():
    verdict, _f, _s, _c = ex.parse_qa_results("the agent rambled but never emitted a verdict")
    assert verdict == ex.VERDICT_UNKNOWN


def test_missing_qa_results_header_is_unknown_not_green():
    """Without the `## QA RESULTS` header, stray CASES:/VERDICT: lines in unstructured prose
    must NOT be honored — the result is UNKNOWN, never a silent green (review finding)."""
    prose = (
        "I think the suite probably passes.\n"
        "CASES: 1 run, 1 passed, 0 failed, 0 blocked\n"  # stray, NOT under the header
        "VERDICT: PASS\n"
    )
    verdict, findings, _sev, cases = ex.parse_qa_results(prose)
    assert verdict == ex.VERDICT_UNKNOWN, verdict
    assert findings == 0 and cases == {}, (findings, cases)


def test_in_place_prompt_warns_writes_are_not_disposable():
    """An --in-place prompt must tell the agent it is the USER'S REAL tree and its writes are
    NOT disposable — never call the real checkout a disposable worktree (review security
    finding). The worktree prompt keeps the 'disposable' wording."""
    inplace = ex.build_tester_prompt(
        kind="backend", suites_text="## Case: x", sut_path=Path("/real/sut"), in_place=True,
    )
    assert "REAL working tree" in inplace and "NOT disposable" in inplace
    assert "disposable throwaway worktree" not in inplace

    worktree = ex.build_tester_prompt(
        kind="backend", suites_text="## Case: x", sut_path=Path("/tmp/wt"), in_place=False,
    )
    assert "disposable throwaway worktree" in worktree
    assert "REAL working tree" not in worktree


def test_parse_verdict_tolerates_trailing_text():
    """A real agent writes 'VERDICT: FAIL — off-by-one in add.sh', NOT a bare 'VERDICT:
    FAIL'. The parser must capture that as FAIL (a `$`-anchored regex would mis-read it as
    UNKNOWN → a false infra-fail on a report-only run). This is the live-run regression the
    relaxed regex fixes."""
    chatty = "## QA RESULTS\nVERDICT: FAIL — off-by-one in add.sh, the only problem found\n"
    verdict, _f, _s, _c = ex.parse_qa_results(chatty)
    assert verdict == "FAIL", verdict


def test_extract_claude_final_text_from_json_result():
    """claude-p --output-format json returns the final assistant text in a `result` field;
    the extractor pulls it so the parser sees the real ## QA RESULTS block, not the lossy TUI
    scrape (the live-run UNKNOWN regression)."""
    import json

    payload = json.dumps({"type": "result", "result": _FAIL_TAIL, "is_error": False})
    text = ex._extract_claude_final_text(payload)
    assert ex._QA_RESULTS_HEADER in text and "VERDICT: FAIL" in text


def test_extract_claude_final_text_from_stream_list():
    """A stream-json transcript (a JSON list) → the last text-bearing item's result wins."""
    import json

    payload = json.dumps([
        {"type": "assistant", "text": "working..."},
        {"type": "result", "result": _PASS_TAIL},
    ])
    text = ex._extract_claude_final_text(payload)
    assert "VERDICT: PASS" in text


def test_extract_claude_final_text_passthrough_on_non_json():
    """A plain-text backend (not JSON) flows through unchanged — the extractor must never
    blank a clean text transcript just because it isn't JSON."""
    plain = "## QA RESULTS\nVERDICT: PASS\n"
    assert ex._extract_claude_final_text(plain) == plain


def test_parse_worst_severity_is_p0():
    tail = (
        "## QA RESULTS\n### FINDINGS\n"
        "- [P2] minor — nit\n- [P0] crash — boom\n- [P3] tiny — meh\n\nVERDICT: FAIL\n"
    )
    _v, findings, max_sev, _c = ex.parse_qa_results(tail)
    assert findings == 3 and max_sev == "P0"


# --- verdict -> exit mapping (report-only) --------------------------------------------
def test_verdict_to_exit_report_only():
    """Report-only: PASS and FAIL both exit 0 (a found bug never fails the build); only
    BLOCKED and a missing verdict are non-zero."""
    assert ex.verdict_to_exit_code("PASS", findings=0, strict=False, exit_blocked=8) == 0
    assert ex.verdict_to_exit_code("FAIL", findings=2, strict=False, exit_blocked=8) == 0
    assert ex.verdict_to_exit_code("BLOCKED", findings=0, strict=False, exit_blocked=8) == 8
    assert ex.verdict_to_exit_code(ex.VERDICT_UNKNOWN, findings=0, strict=False, exit_blocked=8) == 1


def test_nonzero_backend_exit_downgrades_pass_to_unknown():
    """A tester that TIMED OUT (124) or crashed (non-zero exit) did NOT run to a trustworthy
    conclusion — even if it emitted 'VERDICT: PASS' before dying. The non-zero exit must
    downgrade PASS/FAIL to UNKNOWN (→ exit 1), so a timed-out backend is never a silent green
    (review P1)."""
    import subprocess as sp

    pass_tail = "## QA RESULTS\nCASES: 1 run, 1 passed, 0 failed, 0 blocked\nVERDICT: PASS\n"
    timed_out = sp.CompletedProcess(args=["x"], returncode=124, stdout=pass_tail, stderr="")
    assert ex._build_outcome(timed_out, backend="claude", wall=1.0).verdict == ex.VERDICT_UNKNOWN
    # A clean exit keeps the parsed verdict.
    clean = sp.CompletedProcess(args=["x"], returncode=0, stdout=pass_tail, stderr="")
    assert ex._build_outcome(clean, backend="claude", wall=1.0).verdict == "PASS"
    # A non-zero exit on a FAIL also downgrades (the run isn't trustworthy).
    fail_tail = "## QA RESULTS\nCASES: 1 run, 0 passed, 1 failed, 0 blocked\nVERDICT: FAIL\n"
    crashed = sp.CompletedProcess(args=["x"], returncode=1, stdout=fail_tail, stderr="")
    assert ex._build_outcome(crashed, backend="claude", wall=1.0).verdict == ex.VERDICT_UNKNOWN


def test_verdict_with_single_pipe_in_reason_is_not_over_skipped():
    """A REAL verdict whose free-text reason contains a pipe — 'VERDICT: FAIL | reproduced
    with sh add.sh 2 2' — names exactly ONE verdict word and must parse as FAIL. Only the
    multi-word placeholder 'PASS | FAIL | BLOCKED' is skipped (review P2: the |-skip was too
    broad)."""
    tail = "## QA RESULTS\nCASES: 1 run, 0 passed, 1 failed, 0 blocked\nVERDICT: FAIL | reproduced with sh add.sh 2 2\n"
    verdict, _f, _s, _c = ex.parse_qa_results(tail)
    assert verdict == "FAIL", verdict


def test_verdict_reason_mentioning_another_verdict_word_is_not_skipped():
    """A real verdict whose PROSE reason merely MENTIONS another verdict word —
    'VERDICT: FAIL — expected PASS behavior but got 5' — must parse as FAIL. Only the
    PIPE-SEPARATED placeholder (PASS | FAIL | BLOCKED) is skipped, not any line with two
    verdict words (review P2: word-counting was still too broad)."""
    tail = (
        "## QA RESULTS\nCASES: 1 run, 0 passed, 1 failed, 0 blocked\n"
        "VERDICT: FAIL — expected PASS behavior but got 5\n"
    )
    verdict, _f, _s, _c = ex.parse_qa_results(tail)
    assert verdict == "FAIL", verdict


def test_backend_launch_failure_yields_blocked_not_traceback():
    """If the tester backend can't be LAUNCHED (missing binary / exec error), run_tester must
    return a controlled BLOCKED outcome — never let the RuntimeError escape the exit-code
    contract as a traceback (review P1). Simulated by pointing at a non-existent backend via a
    monkeypatched spawn that raises like _which would."""
    sut = _materialize_fixture("sut-good")
    old_fake = os.environ.get("REVIEW_QA_FAKE_TESTER")
    old_spawn = ex._spawn_claude_writeexec
    os.environ.pop("REVIEW_QA_FAKE_TESTER", None)  # use the real dispatch path

    def _boom_spawn(*_a, **_k):
        raise RuntimeError("claude-p not found on PATH")

    ex._spawn_claude_writeexec = _boom_spawn  # type: ignore[assignment]
    try:
        outcome = ex.run_tester(
            prompt_builder=lambda cwd: "x", sut_path=sut, timeout=5, backend="claude",
        )
        assert outcome.verdict == ex.VERDICT_BLOCKED, outcome
        assert "could not launch" in outcome.transcript, outcome.transcript
        rc = ex.verdict_to_exit_code(outcome.verdict, findings=0, strict=False, exit_blocked=8)
        assert rc == 8, ("a launch failure maps to the SUT-boot-failed class", rc)
    finally:
        ex._spawn_claude_writeexec = old_spawn  # type: ignore[assignment]
        if old_fake is not None:
            os.environ["REVIEW_QA_FAKE_TESTER"] = old_fake
        shutil.rmtree(sut, ignore_errors=True)


def test_backend_resolution_honors_m_hint_and_env_precedence():
    """`-m codex`/`-m claude` (via ctx.models) is honored when REVIEW_QA_TESTER is unset, so
    `review qa -m codex` is not silently ignored (review P2). The env var still WINS."""
    old = os.environ.get("REVIEW_QA_TESTER")
    os.environ.pop("REVIEW_QA_TESTER", None)
    try:
        assert ex.resolved_tester_backend(["codex"]) == "codex"
        assert ex.resolved_tester_backend(["claude:claude-opus-4-8"]) == "claude"
        assert ex.resolved_tester_backend(["gemini"]) == "claude"  # unknown → default
        assert ex.resolved_tester_backend(None) == "claude"
        # env wins over the -m hint:
        os.environ["REVIEW_QA_TESTER"] = "codex"
        assert ex.resolved_tester_backend(["claude"]) == "codex"
    finally:
        if old is None:
            os.environ.pop("REVIEW_QA_TESTER", None)
        else:
            os.environ["REVIEW_QA_TESTER"] = old


def test_comma_separated_m_hint_is_split_by_the_handler():
    """`review qa -m codex,claude` must honor codex — the handler splits the comma form via
    _split_models before passing to the resolver, so the first explicit seat (codex) wins, not
    a silent fall-through to claude (review finding)."""
    from reviewlib.config import _split_models

    # The handler does: resolved_tester_backend(_split_models(args.model)).
    old = os.environ.get("REVIEW_QA_TESTER")
    os.environ.pop("REVIEW_QA_TESTER", None)
    try:
        assert ex.resolved_tester_backend(_split_models(["codex,claude"])) == "codex"
        assert ex.resolved_tester_backend(_split_models(["claude,codex"])) == "claude"
    finally:
        if old is not None:
            os.environ["REVIEW_QA_TESTER"] = old


def test_inconsistent_pass_tally_downgrades_to_unknown():
    """A PASS whose tally does NOT add up (run != passed+failed+blocked) or where passed !=
    run is not trustworthy — e.g. 'CASES: 2 run, 1 passed, 0 failed, 0 blocked' + PASS — and
    downgrades to UNKNOWN (review finding), never a silent green."""
    import subprocess as sp

    bad = "## QA RESULTS\nCASES: 2 run, 1 passed, 0 failed, 0 blocked\nVERDICT: PASS\n"
    p = sp.CompletedProcess(args=["x"], returncode=0, stdout=bad, stderr="")
    assert ex._build_outcome(p, backend="claude", wall=1.0).verdict == ex.VERDICT_UNKNOWN

    # A fully-consistent green is kept.
    good = "## QA RESULTS\nCASES: 2 run, 2 passed, 0 failed, 0 blocked\nVERDICT: PASS\n"
    pg = sp.CompletedProcess(args=["x"], returncode=0, stdout=good, stderr="")
    assert ex._build_outcome(pg, backend="claude", wall=1.0).verdict == "PASS"


def test_unsupported_tester_choice_is_rejected():
    """An explicit -m / REVIEW_QA_TESTER naming a backend qa can't use must raise (mapped to a
    usage error by the handler), NOT silently run the un-caged claude default (review)."""
    old = os.environ.get("REVIEW_QA_TESTER")
    os.environ.pop("REVIEW_QA_TESTER", None)
    try:
        ex.validate_tester_choice(["claude"])   # ok
        ex.validate_tester_choice(["codex"])     # ok
        ex.validate_tester_choice([])            # ok (default)
        raised = False
        try:
            ex.validate_tester_choice(["gemini"])
        except ex.UnsupportedTesterError:
            raised = True
        assert raised, "-m gemini must be rejected"
        # An env typo is rejected too.
        os.environ["REVIEW_QA_TESTER"] = "typo"
        raised_env = False
        try:
            ex.validate_tester_choice([])
        except ex.UnsupportedTesterError:
            raised_env = True
        assert raised_env, "REVIEW_QA_TESTER=typo must be rejected"
    finally:
        if old is None:
            os.environ.pop("REVIEW_QA_TESTER", None)
        else:
            os.environ["REVIEW_QA_TESTER"] = old


def test_in_place_fails_closed_when_git_state_unknown():
    """--in-place must FAIL CLOSED when the SUT's git state can't be determined — a False from
    a git probe ERROR must NOT skip the guard and run un-caged in-place over unknown state
    (review security finding). Simulated by pointing the git probe at a path where status
    errors but rev-parse claims a work tree."""
    # _git_state returns 'unknown' when the status probe errors. Drive the guard directly with
    # a monkeypatched _git_state to assert the unknown→refuse mapping (the integration of the
    # probe itself is covered by the clean-nongit / dirty tests).
    old = ex._git_state
    ex._git_state = lambda _p: "unknown"  # type: ignore[assignment]
    try:
        raised = False
        try:
            ex._guard_in_place(backend="claude", in_place=True, sut_path=Path("/whatever"))
        except ex.DirtyInPlaceError as exc:
            raised = True
            assert "could not be determined" in str(exc)
        assert raised, "unknown git state must fail closed (refuse --in-place)"
        # A confident non-git dir is allowed.
        ex._git_state = lambda _p: "clean-nongit"  # type: ignore[assignment]
        ex._guard_in_place(backend="claude", in_place=True, sut_path=Path("/whatever"))  # no raise
    finally:
        ex._git_state = old  # type: ignore[assignment]


def test_collision_safe_fence_outgrows_embedded_backticks():
    """A suite containing a fenced code block must not break the prompt's fence — the fence is
    a backtick run LONGER than any in the suite (review prompt-injection finding)."""
    suite_with_fence = "## Case: x\n```sh\necho hi\n```\nExpected: ok\n"
    prompt = ex.build_tester_prompt(kind="backend", suites_text=suite_with_fence, sut_path=Path("/s"))
    fence = ex._collision_safe_fence(suite_with_fence)
    assert len(fence) >= 4, fence  # longer than the embedded ``` (3)
    # The fence must wrap the suite and not be closed early by the embedded ```.
    assert prompt.count(fence) == 2, ("the suite must be wrapped by exactly one fence pair", fence)


def test_report_is_written_0600_new_and_existing():
    """A qa report can carry secrets; it must be 0600 whether the --report file is NEW or a
    PRE-EXISTING world-readable file (O_CREAT mode applies only on create, so fchmod is
    needed — review security finding)."""
    import stat

    sut = _materialize_fixture("sut-good")
    old = os.environ.get("REVIEW_QA_FAKE_TESTER")
    os.environ["REVIEW_QA_FAKE_TESTER"] = "1"
    try:
        d = Path(tempfile.mkdtemp())
        # NEW file → 0600.
        new_report = d / "new.md"
        ex.run_tester(prompt_builder=lambda cwd: "x", sut_path=sut, timeout=30, report_path=new_report)
        assert stat.S_IMODE(new_report.stat().st_mode) == 0o600, oct(new_report.stat().st_mode)

        # PRE-EXISTING 0644 file → fchmod'd down to 0600.
        existing = d / "existing.md"
        existing.write_text("old", encoding="utf-8")
        os.chmod(existing, 0o644)
        ex.run_tester(prompt_builder=lambda cwd: "x", sut_path=sut, timeout=30, report_path=existing)
        assert stat.S_IMODE(existing.stat().st_mode) == 0o600, oct(existing.stat().st_mode)
    finally:
        if old is None:
            os.environ.pop("REVIEW_QA_FAKE_TESTER", None)
        else:
            os.environ["REVIEW_QA_FAKE_TESTER"] = old
        shutil.rmtree(sut, ignore_errors=True)


def test_codex_spawn_includes_ephemeral():
    """The codex qa spawn must pass --ephemeral (like the read-only codex backend) so a run's
    suite/log/SUT details don't persist in codex session state and contaminate later runs
    (review security finding). Asserted by capturing the argv the spawn would build."""
    captured: dict = {}

    def _fake_streamed(argv, **_kw):
        captured["argv"] = argv
        import subprocess as sp
        return sp.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    old_streamed = ex._run_streamed
    old_which = ex._which
    ex._run_streamed = _fake_streamed  # type: ignore[assignment]
    ex._which = lambda name: f"/usr/bin/{name}"  # type: ignore[assignment]
    try:
        ex._spawn_codex_writeexec("prompt", Path("/tmp/sut"), 60)
        assert "--ephemeral" in captured["argv"], captured["argv"]
        assert "workspace-write" in captured["argv"] and "--full-auto" in captured["argv"]
    finally:
        ex._run_streamed = old_streamed  # type: ignore[assignment]
        ex._which = old_which  # type: ignore[assignment]


def test_suffixed_tester_is_rejected():
    """A model SUFFIX on the qa tester (`-m claude:some-model`, or an alias like `fable` that
    expands to `claude:claude-fable-5`) is rejected — qa does not forward the model, so it
    must NOT silently run the default seat's model (review finding)."""
    from reviewlib.config import _split_models

    old = os.environ.get("REVIEW_QA_TESTER")
    os.environ.pop("REVIEW_QA_TESTER", None)
    try:
        raised = False
        try:
            ex.validate_tester_choice(_split_models(["claude:claude-opus-4-8"]))
        except ex.UnsupportedTesterError:
            raised = True
        assert raised, "a model suffix must be rejected"
        # A bare seat is fine.
        ex.validate_tester_choice(_split_models(["claude"]))
        ex.validate_tester_choice(_split_models(["codex"]))
    finally:
        if old is not None:
            os.environ["REVIEW_QA_TESTER"] = old


def test_pass_contradicting_case_tally_is_downgraded():
    """A 'VERDICT: PASS' that CONTRADICTS its own CASES tally is not a real pass: a failed
    case downgrades PASS→FAIL, a blocked-but-none-failed case downgrades PASS→BLOCKED — so a
    self-contradicting green can't slip through even under --strict (review P1)."""
    import subprocess as sp

    # PASS but the tally says a case FAILED → FAIL.
    contra_fail = "## QA RESULTS\nCASES: 1 run, 0 passed, 1 failed, 0 blocked\nVERDICT: PASS\n"
    p1 = sp.CompletedProcess(args=["x"], returncode=0, stdout=contra_fail, stderr="")
    assert ex._build_outcome(p1, backend="claude", wall=1.0).verdict == "FAIL"

    # PASS but a case is BLOCKED (none failed) → BLOCKED.
    contra_block = "## QA RESULTS\nCASES: 1 run, 0 passed, 0 failed, 1 blocked\nVERDICT: PASS\n"
    p2 = sp.CompletedProcess(args=["x"], returncode=0, stdout=contra_block, stderr="")
    assert ex._build_outcome(p2, backend="claude", wall=1.0).verdict == "BLOCKED"

    # A CONSISTENT pass (all run cases passed) is untouched.
    ok = "## QA RESULTS\nCASES: 2 run, 2 passed, 0 failed, 0 blocked\nVERDICT: PASS\n"
    p3 = sp.CompletedProcess(args=["x"], returncode=0, stdout=ok, stderr="")
    assert ex._build_outcome(p3, backend="claude", wall=1.0).verdict == "PASS"


def test_launch_failure_persists_a_report():
    """A BLOCKED launch failure must still WRITE the report (the handler prints 'Report ->
    …', so the file must exist), not skip it (review finding)."""
    sut = _materialize_fixture("sut-good")
    old_fake = os.environ.get("REVIEW_QA_FAKE_TESTER")
    old_spawn = ex._spawn_claude_writeexec
    os.environ.pop("REVIEW_QA_FAKE_TESTER", None)
    ex._spawn_claude_writeexec = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("no claude-p"))
    try:
        report = sut / "out" / "r.md"
        outcome = ex.run_tester(
            prompt_builder=lambda cwd: "x", sut_path=sut, timeout=5, backend="claude",
            report_path=report,
        )
        assert outcome.verdict == ex.VERDICT_BLOCKED, outcome
        assert report.exists(), "the BLOCKED launch failure must still persist a report"
        assert "BLOCKED" in report.read_text(encoding="utf-8")
    finally:
        ex._spawn_claude_writeexec = old_spawn  # type: ignore[assignment]
        if old_fake is not None:
            os.environ["REVIEW_QA_FAKE_TESTER"] = old_fake
        shutil.rmtree(sut, ignore_errors=True)


def test_pass_without_cases_run_downgrades_to_unknown():
    """A PASS verdict with NO executed case (no parseable CASES: line, or 0 run) is the
    execution-level 'cases authored, zero executed' lie — it must downgrade to UNKNOWN (→
    non-zero), never a silent green. _build_outcome enforces this (review finding)."""
    import subprocess as sp

    # PASS but no CASES: line at all → cases_run None → UNKNOWN.
    no_cases = "## QA RESULTS\n### FINDINGS\nno findings\nVERDICT: PASS\n"
    proc = sp.CompletedProcess(args=["x"], returncode=0, stdout=no_cases, stderr="")
    outcome = ex._build_outcome(proc, backend="claude", wall=1.0)
    assert outcome.verdict == ex.VERDICT_UNKNOWN, outcome

    # PASS with CASES: 0 run → still UNKNOWN.
    zero = "## QA RESULTS\nCASES: 0 run, 0 passed, 0 failed, 0 blocked\nVERDICT: PASS\n"
    proc0 = sp.CompletedProcess(args=["x"], returncode=0, stdout=zero, stderr="")
    assert ex._build_outcome(proc0, backend="claude", wall=1.0).verdict == ex.VERDICT_UNKNOWN

    # PASS with a real run → honored as PASS.
    ran = "## QA RESULTS\nCASES: 1 run, 1 passed, 0 failed, 0 blocked\nVERDICT: PASS\n"
    procr = sp.CompletedProcess(args=["x"], returncode=0, stdout=ran, stderr="")
    assert ex._build_outcome(procr, backend="claude", wall=1.0).verdict == "PASS"


def test_parse_takes_first_verdict_not_trailing_template_echo():
    """The parser takes the FIRST VERDICT: in the final block, so an agent that echoes the
    contract's 'VERDICT: PASS | FAIL | BLOCKED' placeholder AFTER its real FAIL doesn't get a
    false green from the trailing PASS (review finding)."""
    tail = (
        "## QA RESULTS\nCASES: 1 run, 0 passed, 1 failed, 0 blocked\n"
        "VERDICT: FAIL\n"
        "VERDICT: PASS | FAIL | BLOCKED\n"  # echoed template placeholder, trailing
    )
    verdict, _f, _s, _c = ex.parse_qa_results(tail)
    assert verdict == "FAIL", verdict


def test_unfilled_placeholder_verdict_is_unknown_not_pass():
    """An agent that emits the ## QA RESULTS block but leaves the verdict line as the UNFILLED
    template placeholder 'VERDICT: PASS | FAIL | BLOCKED' must NOT be read as PASS — the `|`
    alternation marks it as a copied placeholder, so it parses to UNKNOWN (a non-zero
    launcher/agent fault), never a silent green even with CASES filled in (review finding)."""
    tail = (
        "## QA RESULTS\nCASES: 1 run, 1 passed, 0 failed, 0 blocked\n"
        "### FINDINGS\nno findings\n"
        "VERDICT: PASS | FAIL | BLOCKED\n"  # the unfilled placeholder, the ONLY verdict line
    )
    verdict, _f, _s, _c = ex.parse_qa_results(tail)
    assert verdict == ex.VERDICT_UNKNOWN, verdict
    # And the handler-level mapping must be non-zero (not a false green).
    assert ex.verdict_to_exit_code(verdict, findings=0, strict=False, exit_blocked=8) == 1


def test_verdict_to_exit_strict_flips_findings():
    """--strict flips a FAIL or any finding to 10; a clean PASS still exits 0; BLOCKED stays
    its own infra code regardless of strict."""
    assert ex.verdict_to_exit_code("FAIL", findings=1, strict=True, exit_blocked=8) == 10
    assert ex.verdict_to_exit_code("PASS", findings=1, strict=True, exit_blocked=8) == 10
    assert ex.verdict_to_exit_code("PASS", findings=0, strict=True, exit_blocked=8) == 0
    assert ex.verdict_to_exit_code("BLOCKED", findings=0, strict=True, exit_blocked=8) == 8


# --- the cost cap: load_suites_text + --max-cases truncation --------------------------
def _write_multicase_suite(n_cases: int) -> Path:
    """Write a temp suite file with ``n_cases`` ## Case: blocks; return its path."""
    d = Path(tempfile.mkdtemp(prefix="qa-suite-"))
    lines = ["# Suite: multi", ""]
    for i in range(n_cases):
        lines += [f"## Case: case {i}", "Steps:", f"- do {i}", "Expected:", f"- ok {i}", ""]
    f = d / "multi.md"
    f.write_text("\n".join(lines), encoding="utf-8")
    return f


def test_max_cases_truncates_to_first_n_and_notes_the_cap():
    """The mandatory cost cap: a 5-case suite capped to 2 keeps the first two ## Case:
    blocks, drops the rest, and discloses the cap in a NOTE — so a run never silently
    exercises more than --max-cases."""
    f = _write_multicase_suite(5)
    try:
        capped = load_suites_text([f], max_cases=2)
        assert capped.count("## Case:") == 2, capped
        assert "case 0" in capped and "case 1" in capped
        assert "case 2" not in capped and "case 4" not in capped
        assert "capped to the first 2 case(s)" in capped
        # No cap (None) keeps every case.
        full = load_suites_text([f], max_cases=None)
        assert full.count("## Case:") == 5, full
    finally:
        shutil.rmtree(f.parent, ignore_errors=True)


def test_max_cases_above_total_is_a_noop():
    f = _write_multicase_suite(2)
    try:
        text = load_suites_text([f], max_cases=10)
        assert text.count("## Case:") == 2 and "capped to the first" not in text
    finally:
        shutil.rmtree(f.parent, ignore_errors=True)


def test_load_suites_unreadable_file_is_disclosed_in_band():
    """An unreadable suite file is surfaced as '(could not read …)' in the prompt text, not
    silently dropped (discovery already counted it as having cases — a read failure here is
    a surprising state worth surfacing, never a silently-short run)."""
    missing = Path(tempfile.mkdtemp(prefix="qa-missing-")) / "gone.md"  # never created
    try:
        text = suites_mod._one_suite_block(missing)
        assert "could not read" in text and "gone.md" in text
    finally:
        shutil.rmtree(missing.parent, ignore_errors=True)


# --- worktree isolation ---------------------------------------------------------------
def test_isolated_worktree_is_created_and_removed():
    """The default isolation creates a real ``git worktree`` of the SUT and removes it on
    exit — the agent's writes never touch the user's checkout."""
    sut = _materialize_fixture("sut-good")
    try:
        captured: dict = {}
        with ex.IsolatedSut(sut) as wt:
            captured["path"] = wt
            assert wt.exists() and (wt / "add.sh").exists()  # the committed tree is there
            (wt / "scratch.txt").write_text("agent wrote this", encoding="utf-8")
        # After exit the worktree dir is gone AND the SUT is untouched by the agent's write.
        assert not captured["path"].exists()
        assert not (sut / "scratch.txt").exists()
    finally:
        shutil.rmtree(sut, ignore_errors=True)


def test_worktree_is_removed_even_when_the_tester_raises_midrun():
    """SAFETY: if the tester raises mid-run, the isolated worktree must STILL be removed (the
    IsolatedSut context manager's __exit__ runs on the exception path), and the run must
    return a controlled BLOCKED outcome rather than letting the error escape as a traceback
    (review P1). A leaked worktree would accumulate disk + git-worktree registrations."""
    sut = _materialize_fixture("sut-good")
    seen: dict = {}

    def _boom(cwd: Path) -> str:
        seen["worktree"] = cwd
        raise RuntimeError("tester blew up mid-run")

    try:
        outcome = ex.run_tester(prompt_builder=_boom, sut_path=sut, timeout=5, backend="claude")
        assert outcome.verdict == ex.VERDICT_BLOCKED, ("a mid-run error → controlled BLOCKED", outcome)
        assert "worktree" in seen, "the prompt builder must have run (worktree created)"
        assert not seen["worktree"].exists(), ("the worktree must be removed on the "
                                               "exception path", seen["worktree"])
    finally:
        shutil.rmtree(sut, ignore_errors=True)


def test_worktree_runs_in_the_subdir_for_a_monorepo_package_sut():
    """A SUT that is a SUBDIRECTORY of the repo: the worktree checks out the repo ROOT, but
    the tester must run in the CORRESPONDING subdir of the worktree, not the root (review
    finding). IsolatedSut returns <worktree_root>/<relpath>."""
    repo = Path(tempfile.mkdtemp(prefix="qa-monorepo-"))
    pkg = repo / "packages" / "mypkg"
    pkg.mkdir(parents=True)
    (pkg / "marker.txt").write_text("i am the package", encoding="utf-8")
    (repo / "root-only.txt").write_text("root", encoding="utf-8")
    _git("init", "-q", cwd=repo)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A", cwd=repo)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "x", cwd=repo)
    try:
        with ex.IsolatedSut(pkg) as run_dir:
            # run_dir is the package subdir inside the worktree, NOT the worktree root.
            assert run_dir.name == "mypkg", run_dir
            assert (run_dir / "marker.txt").exists(), ("the tester must run in the package "
                                                       "subdir of the worktree", run_dir)
            assert not (run_dir / "root-only.txt").exists()  # that's at the worktree ROOT
            assert (run_dir.parent.parent / "root-only.txt").exists()  # the root IS checked out
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_worktree_add_timeout_is_sut_isolation_error_with_cleanup():
    """A `git worktree add` that TIMES OUT (a wedged git) must surface as SutIsolationError
    (→ controlled BLOCKED), not a raw TimeoutExpired traceback, and must clean up partial
    state (review finding)."""
    import subprocess as sp

    sut = _materialize_fixture("sut-good")
    old_run = ex._run
    cleaned: dict = {}

    def _timeout_run(argv, **kw):
        # Only the `worktree add` call times out; let the rev-parse probes run normally.
        if "add" in argv:
            raise sp.TimeoutExpired(cmd=argv, timeout=120)
        return old_run(argv, **kw)

    ex._run = _timeout_run  # type: ignore[assignment]
    try:
        raised = False
        try:
            with ex.IsolatedSut(sut) as wt:
                cleaned["wt"] = wt
        except ex.SutIsolationError as exc:
            raised = True
            assert "worktree add" in str(exc)
        assert raised, "a worktree-add timeout must raise SutIsolationError, not TimeoutExpired"
    finally:
        ex._run = old_run  # type: ignore[assignment]
        shutil.rmtree(sut, ignore_errors=True)


def test_isolation_refuses_non_git_sut():
    """A non-repo SUT can't be isolated — IsolatedSut raises so the handler reports BLOCKED
    rather than silently running in-place."""
    sut = Path(tempfile.mkdtemp(prefix="qa-nonrepo-"))
    try:
        raised = False
        try:
            with ex.IsolatedSut(sut):
                pass
        except ex.SutIsolationError:
            raised = True
        assert raised, "expected SutIsolationError for a non-git SUT"
    finally:
        shutil.rmtree(sut, ignore_errors=True)


def _assert_in_place_dirty_refused(tester: str | None) -> None:
    """Run the in-place guard against a dirty tree for the given REVIEW_QA_TESTER selection
    (None = default claude) and assert it is refused. Both seats are un-caged, so BOTH must
    be guarded — not just codex."""
    sut = _materialize_fixture("sut-good")
    old = os.environ.get("REVIEW_QA_TESTER")
    if tester is None:
        os.environ.pop("REVIEW_QA_TESTER", None)
    else:
        os.environ["REVIEW_QA_TESTER"] = tester
    try:
        (sut / "add.sh").write_text("# dirtied\n", encoding="utf-8")  # uncommitted change
        raised = False
        try:
            ex.run_tester(prompt_builder=lambda cwd: "x", sut_path=sut, timeout=5, in_place=True)
        except ex.DirtyInPlaceError as exc:
            raised = True
            assert "uncommitted changes" in str(exc)
            assert isinstance(exc, ex.SutIsolationError)  # base catch still works
        assert raised, f"expected the dirty-tree --in-place guard to fire for tester={tester}"
    finally:
        if old is None:
            os.environ.pop("REVIEW_QA_TESTER", None)
        else:
            os.environ["REVIEW_QA_TESTER"] = old
        shutil.rmtree(sut, ignore_errors=True)


def test_in_place_dirty_tree_refused_for_codex():
    """The un-caged codex tester (--full-auto) refuses --in-place over a dirty tree."""
    _assert_in_place_dirty_refused("codex")


def test_in_place_dirty_tree_refused_for_default_claude():
    """The DEFAULT claude tester is ALSO un-caged (bypassPermissions), so it must refuse
    --in-place over a dirty tree too — the guard must not single out codex (review finding)."""
    _assert_in_place_dirty_refused(None)


def test_in_place_on_non_git_dir_is_allowed_not_misreported_as_dirty():
    """--in-place over a NON-git directory must NOT be refused with a bogus 'uncommitted
    changes' message — a non-repo has no tracked work to protect, so in-place is fine there.
    The dirty guard only fires for a real git repo with a dirty tree (review finding)."""
    sut = Path(tempfile.mkdtemp(prefix="qa-nonrepo-inplace-"))
    (sut / "sut.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    old = os.environ.get("REVIEW_QA_FAKE_TESTER")
    os.environ["REVIEW_QA_FAKE_TESTER"] = "1"
    try:
        # No DirtyInPlaceError raised → the guard let it through; the fake tester runs.
        outcome = ex.run_tester(
            prompt_builder=lambda cwd: "x", sut_path=sut, timeout=10, in_place=True,
        )
        assert outcome is not None  # it ran, not refused
    finally:
        if old is None:
            os.environ.pop("REVIEW_QA_FAKE_TESTER", None)
        else:
            os.environ["REVIEW_QA_FAKE_TESTER"] = old
        shutil.rmtree(sut, ignore_errors=True)


def test_prompt_is_built_at_the_worktree_not_the_real_sut():
    """SECURITY (review finding 1): the prompt's fenced path MUST be the worktree the agent
    actually runs in, NOT the user's real checkout. run_tester invokes prompt_builder with
    the run cwd; for the default (worktree) path that cwd is the throwaway worktree, so the
    'ONLY inside `{path}`' fence points at the disposable tree — never the real SUT."""
    sut = _materialize_fixture("sut-good")
    seen: dict = {}
    old = os.environ.get("REVIEW_QA_FAKE_TESTER")
    os.environ["REVIEW_QA_FAKE_TESTER"] = "1"
    try:
        def _builder(cwd: Path) -> str:
            seen["cwd"] = cwd
            return f"work ONLY inside {cwd}"
        ex.run_tester(prompt_builder=_builder, sut_path=sut, timeout=30)
        run_cwd = seen["cwd"]
        assert run_cwd != sut, ("the prompt must be built at the worktree, not the real SUT", run_cwd, sut)
        assert "review-qa-wt-" in str(run_cwd), run_cwd  # it's the throwaway worktree
    finally:
        if old is None:
            os.environ.pop("REVIEW_QA_FAKE_TESTER", None)
        else:
            os.environ["REVIEW_QA_FAKE_TESTER"] = old
        shutil.rmtree(sut, ignore_errors=True)


def test_default_worktree_run_leaves_the_sut_tree_clean():
    """A default (worktree) run must NOT write anything into the real SUT git tree — no
    report, no scratch — so the checkout stays clean and a subsequent --in-place run is not
    wrongly refused as dirty (review finding 2). The handler picks the default report path
    (review-cli's log dir, outside the SUT); here we run the executor with NO report_path and
    assert the SUT is byte-for-byte clean afterwards."""
    sut = _materialize_fixture("sut-good")
    old = os.environ.get("REVIEW_QA_FAKE_TESTER")
    os.environ["REVIEW_QA_FAKE_TESTER"] = "1"
    try:
        ex.run_tester(
            prompt_builder=lambda cwd: "x", sut_path=sut, timeout=30, report_path=None,
        )
        # The SUT working tree is clean (no untracked report / scratch left behind).
        status = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "status", "--porcelain"],
            cwd=str(sut), capture_output=True, text=True,
        )
        assert status.stdout.strip() == "", ("worktree run dirtied the SUT", status.stdout)
    finally:
        if old is None:
            os.environ.pop("REVIEW_QA_FAKE_TESTER", None)
        else:
            os.environ["REVIEW_QA_FAKE_TESTER"] = old
        shutil.rmtree(sut, ignore_errors=True)


# --- THE DoD: mocked tester drives both fixtures to the right verdict -----------------
def _run_executor_on_fixture(name: str) -> ex.QaRunOutcome:
    """Run the REAL executor (default worktree isolation) against a fixture with the MOCKED
    tester. The fake tester actually RUNS the fixture's sut.sh through the isolated worktree
    and verdicts by its exit code — so this exercises prompt build -> worktree -> SUT exec ->
    parse -> outcome end to end, deterministically and with no paid backend."""
    sut = _materialize_fixture(name)
    old = os.environ.get("REVIEW_QA_FAKE_TESTER")
    os.environ["REVIEW_QA_FAKE_TESTER"] = "1"
    try:
        suites_text = load_suites_text(_suite_files(sut), max_cases=1)
        # The prompt is built at the ACTUAL run cwd (the worktree), proving the security fix:
        # the agent is fenced to the disposable worktree, not the user's checkout.
        report = sut / "report.md"
        return ex.run_tester(
            prompt_builder=lambda cwd: ex.build_tester_prompt(
                kind="backend", suites_text=suites_text, sut_path=cwd),
            sut_path=sut, timeout=30, report_path=report,
        )
    finally:
        if old is None:
            os.environ.pop("REVIEW_QA_FAKE_TESTER", None)
        else:
            os.environ["REVIEW_QA_FAKE_TESTER"] = old
        shutil.rmtree(sut, ignore_errors=True)


def test_dod_mocked_buggy_fixture_verdicts_fail_with_finding():
    """DoD half 1: the KNOWN-BUG SUT (add 2 2 -> 5) is classified FAIL with a finding, by
    the executor actually driving the SUT through an isolated worktree."""
    outcome = _run_executor_on_fixture("sut-buggy")
    assert outcome.verdict == "FAIL", outcome
    assert outcome.findings >= 1, outcome
    assert outcome.cases_failed == 1, outcome
    rc = ex.verdict_to_exit_code(outcome.verdict, findings=outcome.findings, strict=False, exit_blocked=8)
    assert rc == 0, ("report-only: a found bug exits 0", rc)
    rc_strict = ex.verdict_to_exit_code(outcome.verdict, findings=outcome.findings, strict=True, exit_blocked=8)
    assert rc_strict == 10, ("--strict flips a FAIL to 10", rc_strict)


def test_dod_mocked_good_fixture_verdicts_pass():
    """DoD half 2: the known-GOOD SUT (add 2 2 -> 4) is classified PASS, no findings."""
    outcome = _run_executor_on_fixture("sut-good")
    assert outcome.verdict == "PASS", outcome
    assert outcome.findings == 0, outcome
    assert outcome.cases_passed == 1, outcome
    rc = ex.verdict_to_exit_code(outcome.verdict, findings=outcome.findings, strict=False, exit_blocked=8)
    assert rc == 0, rc


def test_dod_report_is_written_with_accounting_footer():
    """The run must persist the transcript + a cost/accounting footer to the report sink."""
    sut = _materialize_fixture("sut-good")
    old = os.environ.get("REVIEW_QA_FAKE_TESTER")
    os.environ["REVIEW_QA_FAKE_TESTER"] = "1"
    try:
        report = sut / "out" / "report.md"
        ex.run_tester(prompt_builder=lambda cwd: "x", sut_path=sut, timeout=30, report_path=report)
        assert report.exists(), "report file must be written"
        text = report.read_text(encoding="utf-8")
        assert "QA RESULTS" in text and "[review-cli qa]" in text and "wall:" in text
    finally:
        if old is None:
            os.environ.pop("REVIEW_QA_FAKE_TESTER", None)
        else:
            os.environ["REVIEW_QA_FAKE_TESTER"] = old
        shutil.rmtree(sut, ignore_errors=True)


# --- THE DoD (LIVE): a real backend reaches the right CASE outcomes (gated) -----------
def test_dod_live_buggy_and_good():
    """LIVE DoD (gated on REVIEW_QA_LIVE=1): spawn the REAL claude/codex write/exec tester
    against both fixtures and assert the AUTHORED CASE outcome is correct — the buggy SUT's
    case FAILS (with a finding citing the 5-vs-4 mismatch) and the good SUT's case PASSES.

    Why the assertion is on the CASE tally, not the whole VERDICT: the §8 prompt makes the
    tester HOSTILE ("hunt for ANY problem; assume there ARE bugs"), so against the GOOD
    fixture a real agent will often file an out-of-scope edge-case finding (e.g. shell
    integer overflow on huge inputs) and flip the overall VERDICT to FAIL even though the
    authored case passed — that is correct hostile-tester behavior, not a fixture failure.
    The falsifiable, design-faithful signal is the IN-SCOPE case: ``cases_failed`` for the
    buggy SUT vs the good SUT. (The hermetic MOCKED DoD above, which runs ONLY the authored
    case, keeps the strict good→PASS / buggy→FAIL whole-verdict assertion.)

    Skipped in normal CI (no var) — it costs tokens and needs a backend on PATH. Set
    REVIEW_QA_TESTER=codex to drive the codex seat (claude's local TUI-scrape build can lose
    the contract block → UNKNOWN; codex emits clean structured text)."""
    if os.environ.get("REVIEW_QA_LIVE", "").strip().lower() in ("", "0", "false", "no"):
        _skip("REVIEW_QA_LIVE not set — skipping the live-backend DoD (set it to run)")

    buggy = _run_live_on_fixture("sut-buggy")
    assert buggy.cases_failed == 1, ("the buggy SUT's authored case must FAIL", buggy)
    assert buggy.findings >= 1, ("a real tester must cite a finding for the buggy SUT", buggy)

    good = _run_live_on_fixture("sut-good")
    assert good.cases_passed == 1 and good.cases_failed == 0, (
        "the good SUT's authored case must PASS", good)


def _run_live_on_fixture(name: str) -> ex.QaRunOutcome:
    """Run the executor against a fixture with a REAL backend (no fake env). Worktree
    isolation + the un-caged spawn + the parser, end to end."""
    sut = _materialize_fixture(name)
    try:
        suites_text = load_suites_text(_suite_files(sut), max_cases=1)
        return ex.run_tester(
            prompt_builder=lambda cwd: ex.build_tester_prompt(
                kind="backend", suites_text=suites_text, sut_path=cwd),
            sut_path=sut, timeout=600, report_path=sut / "report.md",
        )
    finally:
        shutil.rmtree(sut, ignore_errors=True)


class _Skip(Exception):
    """Internal: a gated test signals a skip (not a failure) when its env flag is unset."""


def _skip(reason: str):
    """Skip a gated test the right way for whichever runner is ACTIVE. Use ``pytest.skip``
    ONLY when actually running UNDER pytest (detected via ``PYTEST_CURRENT_TEST``), else raise
    ``_Skip`` for the standalone ``__main__`` runner. Detecting by "is pytest importable" is
    WRONG: the test extra installs pytest, so smoke.py running this file STANDALONE would
    still import pytest and ``pytest.skip()`` would raise ``Skipped`` — which the standalone
    runner treats as an ERROR, failing smoke for an unset REVIEW_QA_LIVE (review P1). Never
    returns."""
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
