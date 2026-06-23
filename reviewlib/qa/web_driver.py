"""The deterministic web Tier-1 DRIVER: parse a prose ``## Case:`` suite into goto/click/fill
steps + DOM assertions, drive them against a ``PageDriver`` (real Playwright or a test fake),
and emit the machine-parsed ``## QA RESULTS`` contract the executor's parser reads (spec §7.1).

This is the web-kind counterpart to the un-caged executor and to the hermetic bot driver: same
OUTPUT contract (``parse_qa_results`` reads it unchanged), but a fully deterministic, no-model
run. A case is "drive these actions -> assert the resulting DOM/url", which a headless browser
makes mechanical. The driver therefore:

  1. for each ``## Case:`` block: parses its ordered ACTION lines (``Goto:`` / ``Click:`` /
     ``Fill:``) and its ASSERTION lines (``Expect-text:`` / ``Expect-no:`` / ``Expect-url:``);
  2. runs the actions in order against the page, then checks the assertions against the page's
     text + url;
  3. classifies PASS / FAIL (with the failing selector/text/url as the proof, plus an optional
     screenshot path) / BLOCKED (a prose-only case with no driveable action);
  4. tallies and writes the ``## QA RESULTS`` block (verdict FAIL on any failed case, BLOCKED if
     the app could not be brought up at all).

THE CASE GRAMMAR (a thin, validated superset of the free-form ``## Case:`` markdown). Inside a
case block, ordered directive lines drive the deterministic run:

    ## Case: home page greets the visitor
    Goto: /
    Expect-text: Welcome
    Expect-no: error

  * ``Goto: <url>``   — navigate (absolute, or relative to the SUT's base_url). ACTION.
  * ``Click: <sel>``  — click a Playwright selector (CSS or ``text=...``). ACTION.
  * ``Fill: <sel> = <value>`` — type ``<value>`` into the field ``<sel>``. ACTION. The separator
    is a SPACE-PADDED ``=`` (` = `), so a CSS attribute selector (``input[name=email]``) keeps its
    own unspaced ``=`` and the value may contain a bare ``=``.
  * ``Expect-text: <s>`` — the page body MUST contain substring ``<s>`` (case-insensitive).
  * ``Expect-no: <s>``   — the page body must NOT contain ``<s>``.
  * ``Expect-url: <s>``  — the current url MUST contain substring ``<s>`` (a redirect/route check).

Actions run in the ORDER written (so ``Goto`` then ``Click`` then ``Expect-text`` reads
top-to-bottom); assertions are all checked after the actions. A case with NO action and NO
assertion is BLOCKED (the deterministic driver has nothing to do) — a prose-only case is for the
un-caged tester, not this hermetic path. A suite mixing the two is fine: the driver runs the
structured cases and BLOCKS the rest with a clear reason, so the report is honest about what the
deterministic run did and didn't cover.

SESSION CONTINUITY (by design). All cases in a suite share ONE browser page, run in order. A
case that omits ``Goto:`` therefore asserts against the page state the PREVIOUS case left — this
is intentional (it lets a multi-step flow span cases: log in in one case, assert the dashboard in
the next). The flip side: a case that forgot its ``Goto:`` silently inspects the prior page. Start
each independent case with its own ``Goto:`` to make it self-contained.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import web_harness as wh

# The case heading — kept identical to modes/qa.py's _CASE_HEADING_RE (same thing counted),
# imported there so the two never drift.
from ..modes.qa import _CASE_HEADING_RE

# Directive lines inside a case block. Case-insensitive; value is everything after the colon.
_GOTO_RE = re.compile(r"^\s*goto\s*:\s*(.+?)\s*$", re.IGNORECASE)
_CLICK_RE = re.compile(r"^\s*click\s*:\s*(.+?)\s*$", re.IGNORECASE)
# Fill is "Fill: <selector> = <value>". The separator is a SPACE-PADDED ``=`` (` = `), NOT a
# bare ``=`` — a CSS attribute selector (``input[name=email]``) has an UNSPACED ``=`` inside it,
# so a bare-``=`` split would wrongly cut the selector in two (``input[name`` / ``email]=…``,
# review finding). Requiring the spaced separator lets an attribute selector keep its own ``=``
# while the value side may still contain a bare ``=`` (a query string, an equation). The
# selector is the lazy ``(.+?)`` before the FIRST ` = `; the value is everything after.
_FILL_RE = re.compile(r"^\s*fill\s*:\s*(.+?)\s+=\s+(.*?)\s*$", re.IGNORECASE)
_EXPECT_TEXT_RE = re.compile(r"^\s*expect-text\s*:\s*(.+?)\s*$", re.IGNORECASE)
_EXPECT_NO_RE = re.compile(r"^\s*expect-no\s*:\s*(.+?)\s*$", re.IGNORECASE)
_EXPECT_URL_RE = re.compile(r"^\s*expect-url\s*:\s*(.+?)\s*$", re.IGNORECASE)


# --- the parsed case model ------------------------------------------------------------
@dataclass(frozen=True)
class WebAction:
    """One ordered browser action. ``kind`` is ``goto`` / ``click`` / ``fill``; ``arg`` is the
    url / selector; ``value`` is the fill text (only for ``fill``)."""

    kind: str
    arg: str
    value: str | None = None


@dataclass(frozen=True)
class WebCase:
    """One parsed deterministic case. ``title`` labels it in the report. ``actions`` are the
    ordered goto/click/fill steps; ``expect_text`` / ``expect_no`` are page-body substring
    assertions; ``expect_url`` are current-url substring assertions. ``runnable`` is False for a
    prose-only case (no action AND no assertion) — those BLOCK."""

    title: str
    actions: tuple[WebAction, ...] = ()
    expect_text: tuple[str, ...] = ()
    expect_no: tuple[str, ...] = ()
    expect_url: tuple[str, ...] = ()

    @property
    def runnable(self) -> bool:
        return bool(self.actions or self.expect_text or self.expect_no or self.expect_url)


@dataclass(frozen=True)
class CaseResult:
    """The verdict for one driven case. ``status`` is PASS / FAIL / BLOCKED; ``detail`` is the
    human reason (the proof line in the report); ``severity`` is the finding tag for a FAIL (P1
    by default — a wrong/absent DOM is a real bug); ``screenshot`` is an optional evidence path."""

    title: str
    status: str
    detail: str
    severity: str | None = None
    screenshot: Path | None = None


PASS = "PASS"
FAIL = "FAIL"
BLOCKED = "BLOCKED"


# --- parsing the prose suite into deterministic cases --------------------------------
def parse_web_cases(suite_text: str) -> list[WebCase]:
    """Split a suite's concatenated markdown into ``WebCase`` objects, one per ``## Case:``
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


def _parse_one_case(title: str, body: str) -> WebCase:
    actions: list[WebAction] = []
    expect_text: list[str] = []
    expect_no: list[str] = []
    expect_url: list[str] = []
    for line in body.splitlines():
        if m := _GOTO_RE.match(line):
            actions.append(WebAction("goto", m.group(1)))
        elif m := _CLICK_RE.match(line):
            actions.append(WebAction("click", m.group(1)))
        elif m := _FILL_RE.match(line):
            actions.append(WebAction("fill", m.group(1).strip(), m.group(2)))
        elif m := _EXPECT_NO_RE.match(line):  # check Expect-no BEFORE Expect-text (no overlap, but explicit)
            expect_no.append(m.group(1))
        elif m := _EXPECT_TEXT_RE.match(line):
            expect_text.append(m.group(1))
        elif m := _EXPECT_URL_RE.match(line):
            expect_url.append(m.group(1))
    return WebCase(
        title=title, actions=tuple(actions),
        expect_text=tuple(expect_text), expect_no=tuple(expect_no), expect_url=tuple(expect_url),
    )


# --- driving the parsed cases against a page -----------------------------------------
@dataclass
class WebRunResult:
    """The outcome of a full web run: the per-case results + the rolled-up verdict.
    ``to_qa_results`` renders the ``## QA RESULTS`` block the executor's ``parse_qa_results``
    reads, so the web path and the un-caged path produce the SAME machine-parsed contract."""

    results: list[CaseResult] = field(default_factory=list)
    blocked_reason: str | None = None  # set when the app/browser itself couldn't be brought up

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

    def to_qa_results(self, *, sut_path: Path, base_url: str) -> str:
        run = len(self.results)
        passed = sum(1 for r in self.results if r.status == PASS)
        failed = sum(1 for r in self.results if r.status == FAIL)
        blocked = sum(1 for r in self.results if r.status == BLOCKED)
        return "\n".join([
            "## QA RESULTS",
            f"SUT: {sut_path}   KIND: web   BRING-UP: {base_url or 'playwright (headless)'}",
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
            lines.insert(0, f"- [P0] web bring-up — {self.blocked_reason}")
        return lines or ["no findings"]

    def _blocked_lines(self) -> list[str]:
        lines = [f"- {r.title} — {r.detail}" for r in self.results if r.status == BLOCKED]
        return lines or ["none"]


def run_web_suite(
    *,
    cases: list[WebCase],
    page: wh.PageDriver,
    out_dir: Path | None = None,
) -> WebRunResult:
    """Drive every ``runnable`` case against the already-open ``page``, classify, and return the
    rolled-up result. The page is owned by the caller (so browser/teardown is handled once around
    the whole suite). ``out_dir`` is where a FAIL's screenshot is written (skipped when ``None``
    or when the page's screenshot is best-effort-unavailable)."""
    results = [_drive_one_case(c, page, out_dir) for c in cases]
    return WebRunResult(results=results)


def _drive_one_case(case: WebCase, page: wh.PageDriver, out_dir: Path | None) -> CaseResult:
    """Run the case's actions in order, then check its assertions against the resulting DOM/url."""
    if not case.runnable:
        return CaseResult(
            title=case.title, status=BLOCKED,
            detail="no Goto:/Click:/Fill:/Expect-* directive — a prose-only case the "
                   "deterministic driver cannot drive; author a Goto:/Expect-text: pair to run it.",
        )
    try:
        _run_actions(case, page)
    except wh.WebActionError as exc:
        return _fail(case, f"action failed: {exc}", page, out_dir)
    return _check_assertions(case, page, out_dir)


def _run_actions(case: WebCase, page: wh.PageDriver) -> None:
    """Execute the case's ordered actions against the page (raises ``WebActionError`` on the
    first failure, which the caller turns into a FAIL with that action as the proof)."""
    for action in case.actions:
        if action.kind == "goto":
            page.goto(action.arg)
        elif action.kind == "click":
            page.click(action.arg)
        elif action.kind == "fill":
            page.fill(action.arg, action.value or "")


def _check_assertions(case: WebCase, page: wh.PageDriver, out_dir: Path | None) -> CaseResult:
    """Check the case's text + url assertions against the page; PASS only if all hold."""
    try:
        body = page.text_content()
        url = page.current_url()
    except wh.WebActionError as exc:
        return _fail(case, f"could not read the page after actions: {exc}", page, out_dir)

    body_lower = body.lower()
    missing_text = [s for s in case.expect_text if s.lower() not in body_lower]
    present_forbidden = [s for s in case.expect_no if s.lower() in body_lower]
    missing_url = [s for s in case.expect_url if s.lower() not in url.lower()]
    if missing_text or present_forbidden or missing_url:
        return _fail(
            case, _mismatch_detail(missing_text, present_forbidden, missing_url, url), page, out_dir)
    return CaseResult(
        case.title, PASS,
        f"all assertions held (url={url!r}, "
        f"{len(case.expect_text)} text + {len(case.expect_url)} url checks passed)",
    )


def _mismatch_detail(
    missing_text: list[str], forbidden: list[str], missing_url: list[str], url: str,
) -> str:
    parts = []
    if missing_text:
        parts.append(f"page is missing expected text {missing_text!r}")
    if forbidden:
        parts.append(f"page contains forbidden text {forbidden!r}")
    if missing_url:
        parts.append(f"url {url!r} is missing expected substring(s) {missing_url!r}")
    return "; ".join(parts)


def _fail(case: WebCase, detail: str, page: wh.PageDriver, out_dir: Path | None) -> CaseResult:
    """Build a FAIL result, capturing a screenshot into ``out_dir`` as evidence when possible."""
    shot = _capture_screenshot(case, page, out_dir)
    return CaseResult(case.title, FAIL, detail, severity="P1", screenshot=shot)


def _capture_screenshot(case: WebCase, page: wh.PageDriver, out_dir: Path | None) -> Path | None:
    """Best-effort screenshot of the failing page for the finding's proof. ``None`` when no
    ``out_dir`` is given or the page can't screenshot (a fake page, a closed browser)."""
    if out_dir is None:
        return None
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in case.title)[:60] or "case"
    path = out_dir / f"fail-{safe}.png"
    return path if page.screenshot(path) else None


# --- the top-level deterministic orchestrator (the qa handler's entrypoint) -----------
def run_web_test(
    *,
    suite_text: str,
    base_url: str,
    sut_path: Path,
    out_dir: Path | None = None,
    page_factory=None,
) -> str:
    """Run the full deterministic web Tier-1 flow against an ALREADY-REACHABLE ``base_url`` and
    return the ``## QA RESULTS`` transcript the qa handler feeds to ``parse_qa_results`` /
    ``verdict_to_exit_code`` (the SAME contract the un-caged executor + the bot driver emit).

    ``page_factory`` is a context manager yielding a ``PageDriver`` — the real Playwright session
    in production, or a test fake. It is the seam that keeps this function browser-free for the
    unit tests: the parser, action-mapping, and QA-RESULTS emission are all exercised against an
    in-memory page with no Playwright. ALWAYS closes the page (the factory's ``__exit__``), so a
    run leaks no browser.

    The dev-server bring-up + the Playwright-availability gate are the CALLER's job (the qa
    handler) — this function assumes the app is up and a page can be opened; a ``WebHarnessError``
    from the factory (browser launch failure) becomes a BLOCKED transcript here, never a
    traceback."""
    cases = parse_web_cases(suite_text)
    if page_factory is None:
        page_factory = _default_page_factory(base_url)
    try:
        with page_factory as page:
            result = run_web_suite(cases=cases, page=page, out_dir=out_dir)
    except wh.WebHarnessError as exc:
        result = WebRunResult(blocked_reason=str(exc))
    return result.to_qa_results(sut_path=sut_path, base_url=base_url)


def _default_page_factory(base_url: str):
    """The real Playwright page-session context manager (used when the caller passes no fake).
    Reached only on the live path — the caller has already confirmed Playwright is available, so
    here we just open the browser session. The exit code on a browser-launch failure is the
    SUT-boot-failed class (the harness couldn't drive the app)."""
    from ..cli import EXIT_QA_SUT_BOOT_FAILED

    return wh.browser_session(base_url, exit_blocked=EXIT_QA_SUT_BOOT_FAILED)
