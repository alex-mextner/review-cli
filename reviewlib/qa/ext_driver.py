"""The deterministic VS Code extension Tier-1 DRIVER: parse a prose ``## Case:`` suite into
run-command / open-file steps + UI assertions, drive them against an ``ExtAutomation`` (a real
isolated VS Code over CDP, or a test fake), and emit the machine-parsed ``## QA RESULTS``
contract the executor's parser reads (spec docs/specs/review-qa.md §7.1, Tier 1, ext kind).

This is the ext-kind counterpart to the un-caged executor and to the hermetic bot / web
drivers: same OUTPUT contract (``parse_qa_results`` reads it unchanged), but a fully
deterministic, no-model run. A case is "run this VS Code command / open this file -> assert
the resulting notification / editor text / webview body", which an isolated VS Code instance
driven over CDP makes mechanical. The driver therefore:

  1. for each ``## Case:`` block: parses its ordered ACTION lines (``Command:`` / ``Open:``)
     and its ASSERTION lines (``Expect-notification:`` / ``Expect-editor-text:`` /
     ``Expect-webview:`` / ``Expect-no:``);
  2. runs the actions in order against the VS Code automation, then checks the assertions
     against the resulting notification messages, the active editor's text, and the webview
     frame body;
  3. classifies PASS / FAIL (with the failing command/text as the proof, plus an optional
     ``window.screenshot`` path) / BLOCKED (a prose-only case with no driveable action);
  4. tallies and writes the ``## QA RESULTS`` block (verdict FAIL on any failed case, BLOCKED
     if the extension could not be launched at all).

THE CASE GRAMMAR (a thin, validated superset of the free-form ``## Case:`` markdown). Inside a
case block, ordered directive lines drive the deterministic run:

    ## Case: hello command greets the user
    Command: myext.hello
    Expect-notification: Hello
    Expect-no: error

  * ``Command: <id>``  — run a VS Code command id via ``executeCommand`` (e.g.
    ``myext.hello``, ``workbench.action.files.newUntitledFile``). ACTION.
  * ``Open: <path>``   — open a file (path relative to the SUT workspace) in the editor.
    ACTION.
  * ``Expect-notification: <s>`` — a notification/message toast MUST contain substring
    ``<s>`` (case-insensitive). This is the most-asserted ext outcome (``showInformationMessage``).
  * ``Expect-editor-text: <s>`` — the ACTIVE editor's text MUST contain substring ``<s>``.
  * ``Expect-webview: <s>``     — the extension's webview frame body MUST contain ``<s>`` (an
    HTML/text substring of the rendered panel).
  * ``Expect-no: <s>``          — none of notifications / editor text / webview body may contain
    ``<s>`` (a forbidden-string check across every observed surface).

Actions run in the ORDER written (so ``Open:`` then ``Command:`` then ``Expect-editor-text:``
reads top-to-bottom); assertions are all checked after the actions, against the post-action
window state. A case with NO action and NO assertion is BLOCKED (the deterministic driver has
nothing to do) — a prose-only case is for the un-caged tester, not this hermetic path. A suite
mixing the two is fine: the driver runs the structured cases and BLOCKS the rest with a clear
reason, so the report is honest about what the deterministic run did and didn't cover.

SESSION CONTINUITY (by design). All cases in a suite share ONE VS Code instance, run in order.
A case that omits an action therefore asserts against the window state the PREVIOUS case left —
this is intentional (it lets a multi-step flow span cases: open a file in one case, run a
command on it in the next). Start each independent case with its own ``Open:``/``Command:`` to
make it self-contained.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# The case heading — kept identical to modes/qa.py's _CASE_HEADING_RE (same thing counted),
# imported there so the two never drift.
from ..modes.qa import _CASE_HEADING_RE
from . import ext_harness as eh

# Directive lines inside a case block. Case-insensitive; value is everything after the colon.
_COMMAND_RE = re.compile(r"^\s*command\s*:\s*(.+?)\s*$", re.IGNORECASE)
_OPEN_RE = re.compile(r"^\s*open\s*:\s*(.+?)\s*$", re.IGNORECASE)
_EXPECT_NOTIFICATION_RE = re.compile(r"^\s*expect-notification\s*:\s*(.+?)\s*$", re.IGNORECASE)
_EXPECT_EDITOR_TEXT_RE = re.compile(r"^\s*expect-editor-text\s*:\s*(.+?)\s*$", re.IGNORECASE)
_EXPECT_WEBVIEW_RE = re.compile(r"^\s*expect-webview\s*:\s*(.+?)\s*$", re.IGNORECASE)
_EXPECT_NO_RE = re.compile(r"^\s*expect-no\s*:\s*(.+?)\s*$", re.IGNORECASE)


# --- the parsed case model ------------------------------------------------------------
@dataclass(frozen=True)
class ExtAction:
    """One ordered VS Code action. ``kind`` is ``command`` / ``open``; ``arg`` is the command
    id / file path."""

    kind: str
    arg: str


@dataclass(frozen=True)
class ExtCase:
    """One parsed deterministic case. ``title`` labels it in the report. ``actions`` are the
    ordered command/open steps; ``expect_notification`` / ``expect_editor_text`` /
    ``expect_webview`` are surface-specific substring assertions; ``expect_no`` are
    forbidden-substring assertions checked across EVERY observed surface. ``runnable`` is False
    for a prose-only case (no action AND no assertion) — those BLOCK."""

    title: str
    actions: tuple[ExtAction, ...] = ()
    expect_notification: tuple[str, ...] = ()
    expect_editor_text: tuple[str, ...] = ()
    expect_webview: tuple[str, ...] = ()
    expect_no: tuple[str, ...] = ()

    @property
    def runnable(self) -> bool:
        return bool(
            self.actions
            or self.expect_notification
            or self.expect_editor_text
            or self.expect_webview
            or self.expect_no
        )


@dataclass(frozen=True)
class CaseResult:
    """The verdict for one driven case. ``status`` is PASS / FAIL / BLOCKED; ``detail`` is the
    human reason (the proof line in the report); ``severity`` is the finding tag for a FAIL (P1
    by default — a wrong/absent notification or editor state is a real bug); ``screenshot`` is
    an optional evidence path (a ``window.screenshot`` of the failing state)."""

    title: str
    status: str
    detail: str
    severity: str | None = None
    screenshot: Path | None = None


PASS = "PASS"
FAIL = "FAIL"
BLOCKED = "BLOCKED"


# --- parsing the prose suite into deterministic cases --------------------------------
def parse_ext_cases(suite_text: str) -> list[ExtCase]:
    """Split a suite's concatenated markdown into ``ExtCase`` objects, one per ``## Case:``
    block. Each block's directive lines become the case's actions + assertions; a block with no
    driveable directive parses to a non-runnable (prose-only) case the driver BLOCKS. Order is
    preserved so ``--max-cases`` and the report read left-to-right."""
    return [_parse_one_case(title, body) for title, body in _split_into_case_blocks(suite_text)]


def _split_into_case_blocks(text: str) -> list[tuple[str, str]]:
    """Cut ``text`` at each ``## Case:`` heading into (title, body) pairs. The title is the
    heading's trailing text; the body is everything up to the next ``## Case:`` (or EOF)."""
    matches = list(_CASE_HEADING_RE.finditer(text))
    blocks: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        # End-of-heading-line from m.end() (just past the colon): the heading regex's leading
        # ``^\s*`` can swallow the preceding newline into the match, so searching from m.start()
        # would land on that newline and leave the title empty.
        heading_line_end = text.find("\n", m.end())
        if heading_line_end == -1:
            heading_line_end = len(text)
        title = text[m.end():heading_line_end].strip() or f"case-{i + 1}"
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[heading_line_end + 1:body_end]
        blocks.append((title, body))
    return blocks


def _parse_one_case(title: str, body: str) -> ExtCase:
    actions: list[ExtAction] = []
    expect_notification: list[str] = []
    expect_editor_text: list[str] = []
    expect_webview: list[str] = []
    expect_no: list[str] = []
    for line in body.splitlines():
        if m := _COMMAND_RE.match(line):
            actions.append(ExtAction("command", m.group(1)))
        elif m := _OPEN_RE.match(line):
            actions.append(ExtAction("open", m.group(1)))
        elif m := _EXPECT_NO_RE.match(line):  # check Expect-no first (no overlap, but explicit)
            expect_no.append(m.group(1))
        elif m := _EXPECT_NOTIFICATION_RE.match(line):
            expect_notification.append(m.group(1))
        elif m := _EXPECT_EDITOR_TEXT_RE.match(line):
            expect_editor_text.append(m.group(1))
        elif m := _EXPECT_WEBVIEW_RE.match(line):
            expect_webview.append(m.group(1))
    return ExtCase(
        title=title,
        actions=tuple(actions),
        expect_notification=tuple(expect_notification),
        expect_editor_text=tuple(expect_editor_text),
        expect_webview=tuple(expect_webview),
        expect_no=tuple(expect_no),
    )


# --- driving the parsed cases against a VS Code automation ----------------------------
@dataclass
class ExtRunResult:
    """The outcome of a full ext run: the per-case results + the rolled-up verdict.
    ``to_qa_results`` renders the ``## QA RESULTS`` block the executor's ``parse_qa_results``
    reads, so the ext path and the un-caged path produce the SAME machine-parsed contract."""

    results: list[CaseResult] = field(default_factory=list)
    blocked_reason: str | None = None  # set when VS Code / the extension itself couldn't launch

    @property
    def verdict(self) -> str:
        """The rolled-up verdict. A FAIL anywhere fails the run; otherwise ANY blocked case (an
        authored case the deterministic driver could NOT exercise — e.g. a prose-only case)
        makes the run BLOCKED, NOT PASS — an unexercised authored case is not a green run. PASS
        only when EVERY case ran and passed."""
        if self.blocked_reason is not None:
            return BLOCKED
        if not self.results:
            return BLOCKED
        if any(r.status == FAIL for r in self.results):
            return FAIL
        if any(r.status == BLOCKED for r in self.results):
            return BLOCKED
        return PASS

    def to_qa_results(self, *, sut_path: Path, extension_path: str) -> str:
        run = len(self.results)
        passed = sum(1 for r in self.results if r.status == PASS)
        failed = sum(1 for r in self.results if r.status == FAIL)
        blocked = sum(1 for r in self.results if r.status == BLOCKED)
        return "\n".join([
            "## QA RESULTS",
            f"SUT: {sut_path}   KIND: ext   BRING-UP: {extension_path or 'vscode (isolated)'}",
            f"CASES: {run} run, {passed} passed, {failed} failed, {blocked} blocked",
            "",
            "### FINDINGS",
            *self._finding_lines(),
            "",
            "### BLOCKED",
            *self._blocked_lines(),
            "",
            f"VERDICT: {self.verdict}",
        ]) + "\n"

    def _finding_lines(self) -> list[str]:
        lines = []
        for r in self.results:
            if r.status != FAIL:
                continue
            proof = f" — proof: {r.screenshot}" if r.screenshot else ""
            lines.append(f"- [{r.severity or 'P1'}] {r.title} — {r.detail}{proof}")
        if self.blocked_reason is not None:
            lines.insert(0, f"- [P0] ext bring-up — {self.blocked_reason}")
        return lines or ["no findings"]

    def _blocked_lines(self) -> list[str]:
        lines = [f"- {r.title} — {r.detail}" for r in self.results if r.status == BLOCKED]
        return lines or ["none"]


def run_ext_suite(
    *,
    cases: list[ExtCase],
    automation: eh.ExtAutomation,
    out_dir: Path | None = None,
) -> ExtRunResult:
    """Drive every ``runnable`` case against the already-launched ``automation``, classify, and
    return the rolled-up result. The automation is owned by the caller (so VS Code launch /
    teardown is handled once around the whole suite). ``out_dir`` is where a FAIL's screenshot is
    written (skipped when ``None`` or when the automation's screenshot is best-effort-unavailable).
    """
    results = [_drive_one_case(c, automation, out_dir) for c in cases]
    return ExtRunResult(results=results)


def _drive_one_case(
    case: ExtCase, automation: eh.ExtAutomation, out_dir: Path | None,
) -> CaseResult:
    """Run the case's actions in order, then check its assertions against the resulting window
    state (notifications, active editor text, webview body)."""
    if not case.runnable:
        return CaseResult(
            title=case.title, status=BLOCKED,
            detail="no Command:/Open:/Expect-* directive — a prose-only case the deterministic "
                   "driver cannot drive; author a Command:/Expect-notification: pair to run it.",
        )
    try:
        _run_actions(case, automation)
    except eh.ExtActionError as exc:
        return _fail(case, f"action failed: {exc}", automation, out_dir)
    return _check_assertions(case, automation, out_dir)


def _run_actions(case: ExtCase, automation: eh.ExtAutomation) -> None:
    """Execute the case's ordered actions against the VS Code automation (raises
    ``ExtActionError`` on the first failure, which the caller turns into a FAIL with that action
    as the proof)."""
    for action in case.actions:
        if action.kind == "command":
            automation.run_command(action.arg)
        elif action.kind == "open":
            automation.open_file(action.arg)


def _check_assertions(
    case: ExtCase, automation: eh.ExtAutomation, out_dir: Path | None,
) -> CaseResult:
    """Check the case's notification / editor-text / webview / forbidden assertions against the
    post-action window state; PASS only if all hold."""
    try:
        notifications = automation.notifications()
        editor_text = automation.editor_text()
        webview = automation.webview_text()
    except eh.ExtActionError as exc:
        return _fail(case, f"could not read the window state after actions: {exc}",
                     automation, out_dir)

    notif_blob = "\n".join(notifications).lower()
    editor_lower = editor_text.lower()
    webview_lower = webview.lower()
    everything = "\n".join([notif_blob, editor_lower, webview_lower])

    missing_notif = [s for s in case.expect_notification if s.lower() not in notif_blob]
    missing_editor = [s for s in case.expect_editor_text if s.lower() not in editor_lower]
    missing_webview = [s for s in case.expect_webview if s.lower() not in webview_lower]
    present_forbidden = [s for s in case.expect_no if s.lower() in everything]

    if missing_notif or missing_editor or missing_webview or present_forbidden:
        detail = _mismatch_detail(
            missing_notif, missing_editor, missing_webview, present_forbidden, notifications)
        return _fail(case, detail, automation, out_dir)
    return CaseResult(
        case.title, PASS,
        f"all assertions held ({len(case.expect_notification)} notification + "
        f"{len(case.expect_editor_text)} editor + {len(case.expect_webview)} webview checks "
        "passed)",
    )


def _mismatch_detail(
    missing_notif: list[str], missing_editor: list[str], missing_webview: list[str],
    forbidden: list[str], notifications: list[str],
) -> str:
    parts = []
    if missing_notif:
        seen = notifications or ["(no notifications)"]
        parts.append(f"no notification contained {missing_notif!r} (saw: {seen!r})")
    if missing_editor:
        parts.append(f"active editor is missing expected text {missing_editor!r}")
    if missing_webview:
        parts.append(f"webview body is missing expected text {missing_webview!r}")
    if forbidden:
        parts.append(f"a surface contains forbidden text {forbidden!r}")
    return "; ".join(parts)


def _fail(
    case: ExtCase, detail: str, automation: eh.ExtAutomation, out_dir: Path | None,
) -> CaseResult:
    """Build a FAIL result, capturing a ``window.screenshot`` into ``out_dir`` as evidence when
    possible (the spec's CDP-over-Playwright screenshot, the only reliable VS Code capture)."""
    shot = _capture_screenshot(case, automation, out_dir)
    return CaseResult(case.title, FAIL, detail, severity="P1", screenshot=shot)


def _capture_screenshot(
    case: ExtCase, automation: eh.ExtAutomation, out_dir: Path | None,
) -> Path | None:
    """Best-effort ``window.screenshot`` of the failing VS Code window for the finding's proof.
    ``None`` when no ``out_dir`` is given or the automation can't screenshot (a fake, a dead
    instance)."""
    if out_dir is None:
        return None
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in case.title)[:60] or "case"
    path = out_dir / f"fail-{safe}.png"
    return path if automation.screenshot(path) else None


# --- the top-level deterministic orchestrator (the qa handler's entrypoint) -----------
def run_ext_test(
    *,
    suite_text: str,
    extension_path: str,
    sut_path: Path,
    out_dir: Path | None = None,
    automation_factory=None,
) -> str:
    """Run the full deterministic ext Tier-1 flow against an ALREADY-LAUNCHED isolated VS Code
    (the ``automation_factory`` opens it) and return the ``## QA RESULTS`` transcript the qa
    handler feeds to ``parse_qa_results`` / ``verdict_to_exit_code`` (the SAME contract the
    un-caged executor + the bot/web drivers emit).

    ``automation_factory`` is a context manager yielding an ``ExtAutomation`` — the real isolated
    VS Code session in production, or a test fake. It is the seam that keeps this function
    VS-Code-free for the unit tests: the parser, action-mapping, and QA-RESULTS emission are all
    exercised against an in-memory automation with no VS Code launch. ALWAYS closes the instance
    (the factory's ``__exit__``), so a run leaks no Electron process.

    The VS-Code-availability gate is the CALLER's job (the qa handler) — this function assumes a
    factory can open an instance; an ``ExtHarnessError`` from the factory (launch failure) becomes
    a BLOCKED transcript here, never a traceback."""
    cases = parse_ext_cases(suite_text)
    if automation_factory is None:
        automation_factory = _default_automation_factory(extension_path, sut_path)
    try:
        with automation_factory as automation:
            result = run_ext_suite(cases=cases, automation=automation, out_dir=out_dir)
    except eh.ExtHarnessError as exc:
        result = ExtRunResult(blocked_reason=str(exc))
    return result.to_qa_results(sut_path=sut_path, extension_path=extension_path)


def _default_automation_factory(extension_path: str, sut_path: Path):
    """The real isolated-VS-Code automation context manager (used when the caller passes no fake).
    Reached only on the live path — the caller has already confirmed VS Code is available, so here
    we just open the session. The exit code on a launch failure is the SUT-boot-failed class (the
    harness couldn't launch the extension)."""
    from ..cli import EXIT_QA_SUT_BOOT_FAILED

    return eh.vscode_session(
        extension_path=extension_path, workspace=sut_path, exit_blocked=EXIT_QA_SUT_BOOT_FAILED)
