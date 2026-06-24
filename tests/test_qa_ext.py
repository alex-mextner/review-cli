#!/usr/bin/env python3
"""review qa — VS Code extension Tier-1 DETERMINISTIC harness (isolated-VS-Code-over-CDP +
command-driving driver).

These pin the ext-kind contract (docs/specs/review-qa.md §7.1, Tier 1, ext kind):

  * the CASE GRAMMAR parser turns a prose ``## Case:`` block with a
    ``Command:``/``Open:``/``Expect-notification:``/``Expect-editor-text:``/``Expect-webview:``/
    ``Expect-no:`` grammar into ordered actions + window-state assertions (and BLOCKS a prose-only
    case it cannot drive);
  * the DRIVER runs the actions against an ``ExtAutomation`` and classifies PASS/FAIL with
    evidence, emitting the ``## QA RESULTS`` contract the executor's parser reads;
  * the VS-CODE GATE: with REVIEW_QA_VSCODE off (the default) or no node runtime, an ext run is a
    controlled BLOCKED with the enable/install command, never a crash;
  * the JSON-protocol automation (``ShellRunnerAutomation``): a runner death / a hung action is a
    controlled ``ExtActionError`` (a case FAIL / a BLOCKED), never a wedge;
  * **the 2-fixture DoD**: the good extension (myext.hello -> 'Hello from myext') verdicts PASS;
    the buggy extension (wrong notification) verdicts FAIL with a finding — both driven through
    the REAL driver against a fake automation that mirrors each fixture's behavior.json (no VS
    Code needed for the deterministic CI path; the in-memory automation speaks the same
    ExtAutomation protocol the real CDP-backed one does).

The deterministic CI path needs NO VS Code: the driver speaks only the small ``ExtAutomation``
protocol, so an in-memory behavior-backed fake automation exercises the parser + action mapping +
QA RESULTS emission fully. A LIVE-VS-Code variant of the DoD is gated on REVIEW_QA_VSCODE=1 and
SKIPs when node / VS Code isn't available (so it runs locally / in a provisioned CI but never
blocks normal CI). Runnable standalone (``python3 tests/test_qa_ext.py``) or under pytest.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib.qa import ext_driver as ed  # noqa: E402
from reviewlib.qa import ext_harness as eh  # noqa: E402
from reviewlib.qa.config import ExtConfig, QaConfigError, load_qa_config  # noqa: E402
from reviewlib.qa.executor import parse_qa_results, verdict_to_exit_code  # noqa: E402
from reviewlib.qa.suites import load_suites_text  # noqa: E402

_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "qa"


# --- the case-grammar parser (no VS Code) --------------------------------------------
def test_parse_actions_in_order():
    """Command/Open parse into ordered actions; the order is preserved top-to-bottom."""
    suite = (
        "## Case: open then run\n"
        "Open: src/file.ts\n"
        "Command: myext.format\n"
        "Expect-editor-text: formatted\n"
    )
    cases = ed.parse_ext_cases(suite)
    assert len(cases) == 1
    c = cases[0]
    assert [a.kind for a in c.actions] == ["open", "command"]
    assert c.actions[0] == ed.ExtAction("open", "src/file.ts")
    assert c.actions[1] == ed.ExtAction("command", "myext.format")
    assert c.expect_editor_text == ("formatted",)


def test_parse_all_assertion_kinds():
    """Expect-notification / Expect-editor-text / Expect-webview / Expect-no each parse into their
    own bucket; Expect-no does not eat the others."""
    suite = (
        "## Case: rich\n"
        "Command: myext.go\n"
        "Expect-notification: Hello\n"
        "Expect-editor-text: const x\n"
        "Expect-webview: <h1>Panel\n"
        "Expect-no: error\n"
    )
    c = ed.parse_ext_cases(suite)[0]
    assert c.expect_notification == ("Hello",)
    assert c.expect_editor_text == ("const x",)
    assert c.expect_webview == ("<h1>Panel",)
    assert c.expect_no == ("error",)


def test_multiple_same_kind_assertions_accumulate():
    c = ed.parse_ext_cases(
        "## Case: x\nCommand: c\nExpect-notification: a\nExpect-notification: b\n")[0]
    assert c.expect_notification == ("a", "b")


def test_prose_only_case_is_not_runnable():
    """A case with no driveable directive is non-runnable (the driver BLOCKS it)."""
    c = ed.parse_ext_cases("## Case: just words\nThis is a description with no directives.\n")[0]
    assert c.runnable is False


def test_case_with_only_assertion_is_runnable():
    """An assertion-only case (no action, asserts the prior window state) is still runnable."""
    c = ed.parse_ext_cases("## Case: assert prior\nExpect-notification: ok\n")[0]
    assert c.runnable is True


def test_case_title_extracted():
    c = ed.parse_ext_cases("## Case: hello greets\nCommand: myext.hello\nExpect-notification: hi\n")[0]
    assert c.title == "hello greets"


# --- an in-memory ExtAutomation backed by a fixture's behavior.json -------------------
class _BehaviorFakeAutomation:
    """An in-memory ExtAutomation: a command-id -> {notifications, editor_text, webview} behavior
    map (mirroring a fixture's behavior.json). Speaks the same protocol the real
    ShellRunnerAutomation does, so it exercises the WHOLE driver (parse -> run actions -> assert ->
    classify) with no VS Code. Running a command APPENDS its notifications + replaces editor/webview
    state, exactly as a real extension's command would mutate the window."""

    def __init__(self, behavior: dict):
        self._behavior = behavior.get("commands", {})
        self._notifications: list[str] = []
        self._editor = ""
        self._webview = ""
        self._opened: list[str] = []

    def run_command(self, command_id: str) -> None:
        spec = self._behavior.get(command_id)
        if spec is None:
            raise eh.ExtActionError(f"unknown command {command_id!r}")
        self._notifications.extend(spec.get("notifications", []))
        if "editor_text" in spec:
            self._editor = spec["editor_text"]
        if "webview" in spec:
            self._webview = spec["webview"]

    def open_file(self, rel_path: str) -> None:
        self._opened.append(rel_path)

    def notifications(self) -> list[str]:
        return list(self._notifications)

    def editor_text(self) -> str:
        return self._editor

    def webview_text(self) -> str:
        return self._webview

    def screenshot(self, path: Path) -> bool:
        return False


def test_pass_when_all_assertions_hold():
    auto = _BehaviorFakeAutomation({"commands": {"c": {"notifications": ["Hello there"]}}})
    res = ed.run_ext_suite(cases=ed.parse_ext_cases(
        "## Case: hi\nCommand: c\nExpect-notification: Hello\nExpect-no: error\n"), automation=auto)
    assert res.verdict == "PASS"
    assert res.results[0].status == "PASS"


def test_fail_on_missing_notification_with_proof():
    auto = _BehaviorFakeAutomation({"commands": {"c": {"notifications": ["Goodbye"]}}})
    res = ed.run_ext_suite(cases=ed.parse_ext_cases(
        "## Case: hi\nCommand: c\nExpect-notification: Hello\n"), automation=auto)
    assert res.verdict == "FAIL"
    assert res.results[0].status == "FAIL"
    assert "Hello" in res.results[0].detail
    assert "Goodbye" in res.results[0].detail  # the proof shows what WAS seen


def test_fail_on_forbidden_text_across_surfaces():
    auto = _BehaviorFakeAutomation({"commands": {"c": {"notifications": ["Fatal error: boom"]}}})
    res = ed.run_ext_suite(cases=ed.parse_ext_cases(
        "## Case: hi\nCommand: c\nExpect-no: error\n"), automation=auto)
    assert res.results[0].status == "FAIL"
    assert "forbidden" in res.results[0].detail


def test_editor_text_assertion():
    auto = _BehaviorFakeAutomation({"commands": {"fmt": {"editor_text": "const formatted = 1;"}}})
    res = ed.run_ext_suite(cases=ed.parse_ext_cases(
        "## Case: fmt\nCommand: fmt\nExpect-editor-text: formatted\n"), automation=auto)
    assert res.results[0].status == "PASS"
    # a miss is a FAIL.
    res2 = ed.run_ext_suite(cases=ed.parse_ext_cases(
        "## Case: fmt\nCommand: fmt\nExpect-editor-text: NOPE\n"), automation=auto)
    assert res2.results[0].status == "FAIL"


def test_webview_assertion():
    auto = _BehaviorFakeAutomation({"commands": {"panel": {"webview": "<h1>My Panel</h1>"}}})
    res = ed.run_ext_suite(cases=ed.parse_ext_cases(
        "## Case: panel\nCommand: panel\nExpect-webview: My Panel\n"), automation=auto)
    assert res.results[0].status == "PASS"


def test_action_failure_is_a_fail_not_a_crash():
    """An unknown command raises ExtActionError inside the driver — classified as a FAIL with the
    failing command, never an escaping traceback."""
    auto = _BehaviorFakeAutomation({"commands": {}})
    res = ed.run_ext_suite(cases=ed.parse_ext_cases(
        "## Case: nope\nCommand: missing.cmd\n"), automation=auto)
    assert res.results[0].status == "FAIL"
    assert "missing.cmd" in res.results[0].detail


def test_open_action_is_actually_invoked():
    """An ``Open:`` directive drives the automation's open_file (the run-actions mapping must
    actually call it, not just parse it) — proven by reading the fake's opened-files record. A
    parse-only mapping that never invoked open_file would still PASS the assertions but never open
    the file (review finding: Open: was mapped but never asserted as invoked)."""
    auto = _BehaviorFakeAutomation({"commands": {"c": {"notifications": ["ok"]}}})
    res = ed.run_ext_suite(cases=ed.parse_ext_cases(
        "## Case: open and run\nOpen: src/file.ts\nCommand: c\nExpect-notification: ok\n"),
        automation=auto)
    assert res.results[0].status == "PASS"
    assert auto._opened == ["src/file.ts"]  # the Open: action reached open_file


def test_prose_only_case_blocks_the_run():
    auto = _BehaviorFakeAutomation({"commands": {}})
    res = ed.run_ext_suite(cases=ed.parse_ext_cases(
        "## Case: prose\njust words, no directives\n"), automation=auto)
    assert res.results[0].status == "BLOCKED"
    assert res.verdict == "BLOCKED"  # an unexercised authored case is not a green run


def test_mixed_pass_and_prose_block_is_blocked_not_pass():
    """A suite mixing a passing structured case with a prose-only case verdicts BLOCKED (the prose
    case was not exercised), never PASS — mirrors the bot/web drivers' same invariant."""
    auto = _BehaviorFakeAutomation({"commands": {"c": {"notifications": ["Hello"]}}})
    res = ed.run_ext_suite(cases=ed.parse_ext_cases(
        "## Case: ok\nCommand: c\nExpect-notification: Hello\n## Case: prose\njust words\n"),
        automation=auto)
    assert {r.status for r in res.results} == {"PASS", "BLOCKED"}
    assert res.verdict == "BLOCKED"


def test_session_continuity_across_cases():
    """All cases share one automation: a command's notification persists so a later assertion-only
    case can assert against it (intentional multi-step-flow support)."""
    auto = _BehaviorFakeAutomation({"commands": {"c": {"notifications": ["Hello world"]}}})
    res = ed.run_ext_suite(cases=ed.parse_ext_cases(
        "## Case: act\nCommand: c\n## Case: assert prior\nExpect-notification: Hello\n"),
        automation=auto)
    assert all(r.status == "PASS" for r in res.results)


# --- the QA RESULTS contract round-trips through the executor parser -------------------
def test_qa_results_contract_parses():
    auto = _BehaviorFakeAutomation({"commands": {"c": {"notifications": ["Goodbye"]}}})
    res = ed.run_ext_suite(cases=ed.parse_ext_cases(
        "## Case: hi\nCommand: c\nExpect-notification: Hello\n"), automation=auto)
    transcript = res.to_qa_results(sut_path=Path("/tmp/sut"), extension_path="/tmp/sut")
    verdict, findings, max_sev, cases = parse_qa_results(transcript)
    assert verdict == "FAIL"
    assert findings == 1 and max_sev == "P1"
    assert cases == {"run": 1, "passed": 0, "failed": 1, "blocked": 0}
    # report-only FAIL is exit 0; under --strict it flips to 10.
    assert verdict_to_exit_code(verdict, findings=findings, strict=False, exit_blocked=8) == 0
    assert verdict_to_exit_code(verdict, findings=findings, strict=True, exit_blocked=8) == 10


def test_blocked_transcript_maps_to_boot_failed():
    res = ed.ExtRunResult(blocked_reason="REVIEW_QA_VSCODE off")
    transcript = res.to_qa_results(sut_path=Path("/tmp/sut"), extension_path="/tmp/x")
    verdict, findings, _sev, _cases = parse_qa_results(transcript)
    assert verdict == "BLOCKED"
    assert verdict_to_exit_code(verdict, findings=findings, strict=False, exit_blocked=8) == 8


def test_blocked_under_strict_still_maps_to_boot_failed_not_strict_flip():
    """A BLOCKED transcript carries a [P0] ext-bring-up finding (so findings>=1), but BLOCKED must
    win over the --strict any-finding flip: a VS-Code-off ext run under --strict is INFRA (exit
    8 = boot-failed), NOT a strict-finding 10. Pins the precedence so a future verdict_to_exit_code
    change can't silently turn an env failure into a strict gate (review finding)."""
    res = ed.ExtRunResult(blocked_reason="REVIEW_QA_VSCODE off")
    transcript = res.to_qa_results(sut_path=Path("/tmp/sut"), extension_path="/tmp/x")
    verdict, findings, _sev, _cases = parse_qa_results(transcript)
    assert verdict == "BLOCKED" and findings >= 1  # the [P0] bring-up line counts as a finding
    assert verdict_to_exit_code(verdict, findings=findings, strict=True, exit_blocked=8) == 8


# --- the config block -----------------------------------------------------------------
def test_ext_config_defaults():
    cfg = ExtConfig()
    assert cfg.driver == "vscode"
    assert cfg.extension_path == "."
    assert cfg.workspace == "."


def test_ext_config_rejects_unknown_driver():
    try:
        ExtConfig(driver="electron-by-hand")
    except QaConfigError:
        pass
    else:
        raise AssertionError("an unsupported driver must raise QaConfigError")


def test_ext_config_parsed_from_fixture_yaml():
    cfg = load_qa_config(_FIXTURES / "ext-good", None)
    assert cfg is not None and cfg.kind == "ext"
    assert cfg.ext is not None
    assert cfg.ext.driver == "vscode"
    assert cfg.ext.extension_path == "."
    assert cfg.ext.workspace == "."


# --- the --kind ext routing into the deterministic fast path (the modes/qa.py glue) ---
class _Args:
    """A minimal stand-in for the parsed argparse namespace the routing reads (kind + config)."""

    def __init__(self, kind="auto", config=None):
        self.kind = kind
        self.config = config


class _Ctx:
    """A minimal ModeContext stand-in: the ext routing only reads ctx.args."""

    def __init__(self, args):
        self.args = args


def test_routing_ext_kind_with_config_takes_deterministic_path():
    """--kind ext (or sut.kind: ext) + a sut.ext config resolves to the ExtConfig — the
    deterministic fast path. The fixture declares both, so explicit and auto both route."""
    from reviewlib.modes.qa import _resolve_deterministic_ext

    sut = _FIXTURES / "ext-good"
    cfg = _resolve_deterministic_ext(_Ctx(_Args(kind="ext")), sut)
    assert cfg is not None and cfg.driver == "vscode"
    # under --kind auto, the package.json's contributes/engines.vscode marker (and the qa.yaml's
    # sut.kind: ext) still routes ext.
    cfg_auto = _resolve_deterministic_ext(_Ctx(_Args(kind="auto")), sut)
    assert cfg_auto is not None


def test_routing_non_ext_kind_falls_through():
    """A non-ext --kind against the ext fixture does NOT take the ext fast path (it would fall to
    the normal env+executor flow). Pins the resolver's kind gate."""
    from reviewlib.modes.qa import _resolve_deterministic_ext

    sut = _FIXTURES / "ext-good"
    assert _resolve_deterministic_ext(_Ctx(_Args(kind="web")), sut) is None
    assert _resolve_deterministic_ext(_Ctx(_Args(kind="backend")), sut) is None


def test_routing_ext_kind_without_config_falls_through():
    """--kind ext against a SUT with NO sut.ext config returns None (the prose-runbook executor
    path handles it); the deterministic harness only activates with a declared sut.ext block."""
    from reviewlib.modes.qa import _resolve_deterministic_ext

    # web-good is a non-ext fixture with no sut.ext block; forcing --kind ext must still return
    # None because there is no sut.ext config to drive the deterministic run.
    assert _resolve_deterministic_ext(_Ctx(_Args(kind="ext")), _FIXTURES / "web-good") is None


# --- the VS Code gate -----------------------------------------------------------------
def test_vscode_off_by_default_is_a_clear_skip():
    """With REVIEW_QA_VSCODE unset, vscode_available returns (False, <enable hint>)."""
    saved = os.environ.pop("REVIEW_QA_VSCODE", None)
    try:
        ok, reason = eh.vscode_available()
        assert ok is False
        assert "REVIEW_QA_VSCODE=1" in reason
    finally:
        if saved is not None:
            os.environ["REVIEW_QA_VSCODE"] = saved


def test_runner_command_never_returns_bun():
    """The runner-runtime resolver must never pick bun (bun hangs Electron launch on macOS — the
    user's CLAUDE.md). It returns tsx/node or None, but never a bun path."""
    cmd = eh._runner_command()
    if cmd is not None:
        assert "bun" not in cmd[0].lower()


def test_session_missing_runtime_is_a_controlled_blocked():
    """A vscode_session with no node/tsx runtime found raises ExtHarnessError carrying the
    boot-failed exit class — a controlled BLOCKED, never a raw traceback. Forced by pointing the
    runtime override at a non-existent binary so the resolver still returns it but the spawn
    fails... actually the resolver returns the override as-is; the spawn raising is the controlled
    path. Here we cover the no-runtime branch by clearing PATH lookups via the override to empty
    and asserting the gate."""
    # Force the runner-script-missing branch: a session whose runner script does not exist raises a
    # controlled ExtHarnessError (exit_blocked), proving the launch path fails cleanly.
    saved = os.environ.get("REVIEW_QA_EXT_RUNNER")
    saved_node = os.environ.get("REVIEW_QA_EXT_NODE")
    os.environ["REVIEW_QA_EXT_RUNNER"] = "/nonexistent/ext_runner.mts"
    os.environ["REVIEW_QA_EXT_NODE"] = sys.executable  # a real binary so the runtime resolves
    try:
        sess = eh.vscode_session(
            extension_path="/tmp/x", workspace=Path("/tmp"), exit_blocked=8)
        try:
            sess.__enter__()
        except eh.ExtHarnessError as exc:
            assert exc.exit_code == 8
            assert "runner is missing" in str(exc)
        else:
            sess.__exit__(None, None, None)
            raise AssertionError("a missing runner script must raise ExtHarnessError")
    finally:
        _restore_env("REVIEW_QA_EXT_RUNNER", saved)
        _restore_env("REVIEW_QA_EXT_NODE", saved_node)


def _restore_env(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


# --- the runner subprocess contract (a tiny fake .py runner, no VS Code) --------------
def _with_fake_runner(script_body: str):
    """A context-manager-ish helper: write a tiny python "runner" that the harness spawns via the
    REVIEW_QA_EXT_NODE override (a real python interpreter) + REVIEW_QA_EXT_RUNNER (the script),
    so the harness's subprocess + protocol + timeout logic is exercised with NO VS Code. Returns
    (env_saver_restore_fn). The fake runner speaks the same stdout JSON the real one does."""
    import tempfile

    tmp = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
    tmp.write(script_body)
    tmp.close()
    saved_runner = os.environ.get("REVIEW_QA_EXT_RUNNER")
    saved_node = os.environ.get("REVIEW_QA_EXT_NODE")
    os.environ["REVIEW_QA_EXT_RUNNER"] = tmp.name
    os.environ["REVIEW_QA_EXT_NODE"] = sys.executable
    os.environ["REVIEW_QA_VSCODE"] = "1"

    def restore():
        _restore_env("REVIEW_QA_EXT_RUNNER", saved_runner)
        _restore_env("REVIEW_QA_EXT_NODE", saved_node)
        _restore_env("REVIEW_QA_VSCODE", None)
        os.unlink(tmp.name)

    return restore


def test_extra_env_reaches_the_runner_subprocess():
    """sut.ext.env is merged into the runner's environment so the extension under test sees its
    configured non-secret variables (codex PR review P2: it was parsed but discarded). A fake
    runner echoes the env var into its `ready` line; the harness must boot and the var must be
    present, proving the passthrough."""
    # The fake runner reports the env var QA_FLAG inside ready, then exits — we only need the boot.
    body = (
        "import os, sys, json\n"
        "print(json.dumps({'type': 'ready', 'qa_flag': os.environ.get('QA_FLAG')}), flush=True)\n"
        "# read one request then exit so the session tears down cleanly\n"
        "sys.stdin.readline()\n"
    )
    restore = _with_fake_runner(body)
    try:
        # We can't read the runner's ready payload from the public API, so assert the env reached
        # the child by having the runner FAIL ready unless the var is set: rewrite to emit error
        # when missing. Simpler: drive a session and assert no ExtHarnessError (boot succeeded with
        # the var present).
        sess = eh.vscode_session(
            extension_path="/tmp/x", workspace=Path("/tmp"), exit_blocked=8,
            extra_env={"QA_FLAG": "on"})
        auto = sess.__enter__()
        try:
            assert auto is not None  # boot succeeded; the fake saw QA_FLAG in its env
        finally:
            sess.__exit__(None, None, None)
    finally:
        restore()


def test_env_required_runner_blocks_when_var_missing():
    """Proves the passthrough is REAL: a fake runner that emits a launch error unless QA_FLAG is set
    BLOCKS when extra_env is empty, and BOOTS when extra_env supplies it — so a discarded env would
    be caught here (the var must actually reach the child)."""
    body = (
        "import os, sys, json\n"
        "if os.environ.get('QA_FLAG') != 'on':\n"
        "    print(json.dumps({'type': 'error', 'error': 'QA_FLAG not in env'}), flush=True)\n"
        "    sys.exit(1)\n"
        "print(json.dumps({'type': 'ready'}), flush=True)\n"
        "sys.stdin.readline()\n"
    )
    restore = _with_fake_runner(body)
    try:
        # missing -> BLOCKED
        sess = eh.vscode_session(
            extension_path="/tmp/x", workspace=Path("/tmp"), exit_blocked=8, extra_env={})
        try:
            sess.__enter__()
        except eh.ExtHarnessError as exc:
            assert exc.exit_code == 8 and "QA_FLAG" in str(exc)
        else:
            sess.__exit__(None, None, None)
            raise AssertionError("a runner that errors without QA_FLAG must BLOCK when env is empty")
        # present -> BOOTS
        sess2 = eh.vscode_session(
            extension_path="/tmp/x", workspace=Path("/tmp"), exit_blocked=8,
            extra_env={"QA_FLAG": "on"})
        auto = sess2.__enter__()
        try:
            assert auto is not None
        finally:
            sess2.__exit__(None, None, None)
    finally:
        restore()


def test_await_ready_times_out_on_a_silent_but_alive_runner():
    """A runner that stays ALIVE but never emits `ready` must hit the LAUNCH_TIMEOUT deadline and
    BLOCK (controlled), NOT hang — the select()-guarded readline (codex PR review P2). Forced with
    a tiny launch-timeout override + a fake runner that sleeps without writing."""
    body = "import time\ntime.sleep(30)\n"  # alive, silent — never signals ready
    restore = _with_fake_runner(body)
    saved_to = os.environ.get("REVIEW_QA_EXT_LAUNCH_TIMEOUT_S")
    os.environ["REVIEW_QA_EXT_LAUNCH_TIMEOUT_S"] = "1"  # short deadline so the test is fast
    # The launch timeout is read at import time into a module constant; re-read it for this test.
    import importlib

    importlib.reload(eh)
    try:
        sess = eh.vscode_session(extension_path="/tmp/x", workspace=Path("/tmp"), exit_blocked=8)
        import time as _t

        start = _t.monotonic()
        try:
            sess.__enter__()
        except eh.ExtHarnessError as exc:
            elapsed = _t.monotonic() - start
            assert exc.exit_code == 8
            assert "did not become ready" in str(exc)
            assert elapsed < 10, f"timed out cleanly, not hung (took {elapsed:.1f}s)"
        else:
            sess.__exit__(None, None, None)
            raise AssertionError("a silent-but-alive runner must BLOCK on the launch timeout")
    finally:
        _restore_env("REVIEW_QA_EXT_LAUNCH_TIMEOUT_S", saved_to)
        restore()
        importlib.reload(eh)  # restore the module's real constants for later tests


# --- the 2-fixture DoD (deterministic, no VS Code): good -> PASS, buggy -> FAIL -------
def _run_fixture_deterministic(name: str) -> str:
    """Drive a fixture's extension through the REAL driver against a fake automation backed by the
    fixture's real behavior.json, returning the ## QA RESULTS transcript. No VS Code, no Electron —
    the fake reads the fixture's behavior map directly, so the DoD is about the DRIVER + the
    fixture's declared behavior, deterministic in normal CI. The behavior.json is kept in sync with
    src/extension.js so this verdicts the same thing the live VS Code leg does."""
    sut = _FIXTURES / name
    suite_files = sorted((sut / "docs" / "tests" / "suites").glob("*.md"))
    suite_text = load_suites_text(suite_files, max_cases=None)
    cases = ed.parse_ext_cases(suite_text)
    behavior = json.loads((sut / "behavior.json").read_text(encoding="utf-8"))
    auto = _BehaviorFakeAutomation(behavior)
    res = ed.run_ext_suite(cases=cases, automation=auto)
    return res.to_qa_results(sut_path=sut.resolve(), extension_path=str(sut.resolve()))


def test_dod_good_fixture_passes():
    transcript = _run_fixture_deterministic("ext-good")
    verdict, findings, _sev, cases = parse_qa_results(transcript)
    assert verdict == "PASS", transcript
    assert findings == 0
    assert cases == {"run": 2, "passed": 2, "failed": 0, "blocked": 0}
    assert verdict_to_exit_code(verdict, findings=findings, strict=False, exit_blocked=8) == 0


def test_dod_buggy_fixture_fails_with_a_finding():
    transcript = _run_fixture_deterministic("ext-buggy")
    verdict, findings, max_sev, cases = parse_qa_results(transcript)
    assert verdict == "FAIL", transcript
    assert findings >= 1 and max_sev == "P1"
    assert cases["failed"] >= 1
    # report-only: a found bug exits 0 (the report carries it); --strict flips it to 10.
    assert verdict_to_exit_code(verdict, findings=findings, strict=False, exit_blocked=8) == 0
    assert verdict_to_exit_code(verdict, findings=findings, strict=True, exit_blocked=8) == 10


# --- the LIVE-VS-Code DoD (gated; SKIPs without node + VS Code) ----------------------
def _vscode_ready() -> bool:
    """True only if REVIEW_QA_VSCODE is on AND a node runtime + a VS Code binary are present. Used
    to gate the live test so it never fails a normal CI. Best-effort — the actual launch can still
    BLOCK cleanly if VS Code is half-installed."""
    ok, _reason = eh.vscode_available()
    if not ok:
        return False
    import shutil

    vscode = os.environ.get("VSCODE_PATH") or shutil.which("code")
    has_app = Path("/Applications/Visual Studio Code.app/Contents/MacOS/Electron").exists()
    return bool(vscode or has_app)


def test_live_vscode_dod_good_passes():
    """The REAL isolated-VS-Code DoD: launch the good fixture extension in an isolated VS Code,
    drive the smoke suite over CDP, assert PASS. Gated on REVIEW_QA_VSCODE=1 + node + a VS Code
    binary; SKIPs otherwise so normal CI (no VS Code) is unaffected. This is the end-to-end proof
    that the launchVSCode-over-CDP runner + the JSON protocol + command-driving all work against a
    real VS Code."""
    if not _vscode_ready():
        _skip("REVIEW_QA_VSCODE=1 + node/tsx + a VS Code binary (set VSCODE_PATH or `code` on PATH)")
        return
    transcript = _run_fixture_live("ext-good")
    verdict, _findings, _sev, _cases = parse_qa_results(transcript)
    assert verdict == "PASS", transcript


def test_live_vscode_dod_buggy_fails():
    if not _vscode_ready():
        _skip("REVIEW_QA_VSCODE=1 + node/tsx + a VS Code binary (set VSCODE_PATH or `code` on PATH)")
        return
    transcript = _run_fixture_live("ext-buggy")
    verdict, findings, _sev, _cases = parse_qa_results(transcript)
    assert verdict == "FAIL", transcript
    assert findings >= 1


def _run_fixture_live(name: str) -> str:
    """Launch the fixture's extension in a real isolated VS Code, drive the smoke suite over CDP,
    and return the ## QA RESULTS transcript. Guaranteed VS Code teardown (the session context
    manager). Only reached when _vscode_ready()."""
    sut = (_FIXTURES / name).resolve()
    cfg = load_qa_config(sut, None)
    assert cfg is not None and cfg.ext is not None
    suite_files = sorted((sut / "docs" / "tests" / "suites").glob("*.md"))
    suite_text = load_suites_text(suite_files, max_cases=None)
    return ed.run_ext_test(
        suite_text=suite_text, extension_path=str(sut), sut_path=sut,
        automation_factory=eh.vscode_session(
            extension_path=str(sut), workspace=sut, exit_blocked=8),
    )


def _skip(reason: str) -> None:
    """SKIP under pytest (a real pytest skip) or print a SKIP line standalone."""
    try:
        import pytest

        if os.environ.get("PYTEST_CURRENT_TEST"):
            pytest.skip(reason)
    except ImportError:
        pass
    print(f"SKIP: {reason}")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok  {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    if failures:
        print(f"\n{failures} failure(s)")
        sys.exit(1)
    print("\nall ext-harness tests passed")
