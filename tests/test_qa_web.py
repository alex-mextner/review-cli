#!/usr/bin/env python3
"""review qa — web Tier-1 DETERMINISTIC harness (Playwright bring-up + DOM-driving driver).

These pin the web-kind contract (docs/specs/review-qa.md §7.1, Tier 1):

  * the CASE GRAMMAR parser turns a prose ``## Case:`` block with a
    ``Goto:``/``Click:``/``Fill:``/``Expect-text:``/``Expect-no:``/``Expect-url:`` grammar into
    ordered actions + DOM assertions (and BLOCKS a prose-only case it cannot drive);
  * the DRIVER runs the actions against a ``PageDriver`` and classifies PASS/FAIL with evidence,
    emitting the ``## QA RESULTS`` contract the executor's parser reads;
  * the PLAYWRIGHT GATE: with REVIEW_QA_PLAYWRIGHT off (the default) or the browser uninstalled,
    a web run is a controlled BLOCKED with the install command, never a crash;
  * the DEV-SERVER bring-up + health gate: a server that never answers is a BLOCKED, not a
    browser pointed at a dead address; teardown is guaranteed;
  * **the 2-fixture DoD**: the good app (landing says "Welcome") verdicts PASS; the buggy app
    (wrong landing text) verdicts FAIL with a finding — both driven through the REAL driver
    against the REAL fixture site served over a real stdlib HTTP server (no browser needed for
    the deterministic CI path; the in-memory page speaks the same PageDriver protocol).

The deterministic CI path needs NO browser: the driver speaks only the small ``PageDriver``
protocol, so an in-memory HTTP-backed fake page exercises the parser + action mapping + QA
RESULTS emission fully. A LIVE-browser variant of the DoD is gated on REVIEW_QA_PLAYWRIGHT=1 and
SKIPs when Chromium isn't installed (so it runs locally / in a browser-provisioned CI but never
blocks normal CI). Runnable standalone (``python3 tests/test_qa_web.py``) or under pytest.
"""

from __future__ import annotations

import os
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Shrink the harness's nav timeout BEFORE importing the modules (read at import time). The fake
# page resolves instantly; even the live browser drives a localhost fixture in milliseconds.
os.environ.setdefault("REVIEW_QA_WEB_NAV_TIMEOUT_S", "10")

from reviewlib.qa import web_driver as wd  # noqa: E402
from reviewlib.qa import web_harness as wh  # noqa: E402
from reviewlib.qa.config import QaConfigError, WebConfig, load_qa_config  # noqa: E402
from reviewlib.qa.executor import parse_qa_results, verdict_to_exit_code  # noqa: E402
from reviewlib.qa.suites import load_suites_text  # noqa: E402

_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "qa"


# --- the case-grammar parser (no browser) --------------------------------------------
def test_parse_actions_in_order():
    """Goto/Click/Fill parse into ordered actions; the order is preserved top-to-bottom."""
    suite = (
        "## Case: login\n"
        "Goto: /login\n"
        "Fill: #user = alice\n"
        "Click: button[type=submit]\n"
        "Expect-url: /dashboard\n"
    )
    cases = wd.parse_web_cases(suite)
    assert len(cases) == 1
    c = cases[0]
    assert [a.kind for a in c.actions] == ["goto", "fill", "click"]
    assert c.actions[0] == wd.WebAction("goto", "/login")
    assert c.actions[1] == wd.WebAction("fill", "#user", "alice")
    assert c.actions[2] == wd.WebAction("click", "button[type=submit]")
    assert c.expect_url == ("/dashboard",)


def test_parse_text_and_no_assertions():
    """Expect-text / Expect-no parse as substring assertions; Expect-no does not eat Expect-text."""
    suite = (
        "## Case: home\n"
        "Goto: /\n"
        "Expect-text: Welcome\n"
        "Expect-text: Sign in\n"
        "Expect-no: 404\n"
    )
    c = wd.parse_web_cases(suite)[0]
    assert c.expect_text == ("Welcome", "Sign in")
    assert c.expect_no == ("404",)


def test_fill_splits_on_spaced_equals():
    """Fill: <sel> = <value> splits on the SPACE-PADDED ' = ', so a value containing a bare '='
    (a query string / equation) survives intact."""
    c = wd.parse_web_cases("## Case: x\nFill: input#q = a=b=c\n")[0]
    assert c.actions[0] == wd.WebAction("fill", "input#q", "a=b=c")


def test_fill_preserves_attribute_selector_equals():
    """A CSS attribute selector carries its own UNSPACED '=' (input[name=email]); the spaced
    separator must not cut it in two (review finding — a bare-'=' split mangled the selector)."""
    c = wd.parse_web_cases("## Case: x\nFill: input[name=email] = bob\n")[0]
    assert c.actions[0] == wd.WebAction("fill", "input[name=email]", "bob")
    # selector AND value both carrying '=' still resolve to the spaced separator.
    c2 = wd.parse_web_cases("## Case: y\nFill: input[data-x=y] = key=val\n")[0]
    assert c2.actions[0] == wd.WebAction("fill", "input[data-x=y]", "key=val")


def test_prose_only_case_is_not_runnable():
    """A case with no driveable directive is non-runnable (the driver BLOCKS it)."""
    c = wd.parse_web_cases(
        "## Case: just words\nThis is a description with no directives.\n"
    )[0]
    assert c.runnable is False


def test_case_title_extracted():
    c = wd.parse_web_cases("## Case: home page greets\nGoto: /\nExpect-text: hi\n")[0]
    assert c.title == "home page greets"


# --- classification against an in-memory page ----------------------------------------
def _body_text(html: str) -> str:
    """Approximate a browser's ``inner_text("body")`` from raw HTML: drop <head>/<script>/<style>
    and strip the remaining tags, leaving only the rendered-visible text.

    WHY (review finding). The REAL PlaywrightPage returns ``inner_text("body")`` (rendered visible
    text), so if the fake page matched on RAW HTML instead, an ``Expect-text:`` / ``Expect-no:``
    hitting a <title>/comment/attribute would pass in CI and fail on a live browser (or vice
    versa). Extracting body text here keeps the deterministic fake's text semantics aligned with
    the browser's, so the deterministic DoD validates the SAME thing the live DoD does."""
    import re as _re

    # Drop the whole <head>… and any <script>/<style> blocks (their text is not visible).
    cleaned = _re.sub(r"(?is)<head\b.*?</head>", " ", html)
    cleaned = _re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", cleaned)
    cleaned = _re.sub(r"(?is)<!--.*?-->", " ", cleaned)
    cleaned = _re.sub(r"(?s)<[^>]+>", " ", cleaned)  # strip remaining tags
    return _re.sub(r"\s+", " ", cleaned).strip()


class _RoutedFakePage:
    """An in-memory PageDriver: a url->raw-HTML map + a click->path navigation map. Speaks the
    same protocol the real PlaywrightPage does, so it exercises the WHOLE driver (parse -> run
    actions -> assert -> classify) with no browser. ``text_content`` returns the BODY text (tags
    stripped, head/script/style dropped) to match the real page's ``inner_text("body")`` — NOT
    raw HTML (review finding). base-relative goto resolves against ``base``."""

    def __init__(
        self, routes: dict, *, base: str = "http://fake", click_map: dict | None = None
    ):
        self.routes = routes
        self.base = base.rstrip("/")
        self.click_map = click_map or {}
        self._url = self.base + "/"
        self.fills: dict[str, str] = {}

    def goto(self, url: str) -> None:
        if not url.startswith(("http://", "https://")):
            url = self.base + "/" + url.lstrip("/")
        self._url = url

    def click(self, selector: str) -> None:
        if selector not in self.click_map:
            raise wh.WebActionError(f"no element matches {selector!r}")
        self._url = self.base + self.click_map[selector]

    def fill(self, selector: str, value: str) -> None:
        self.fills[selector] = value

    def text_content(self) -> str:
        return _body_text(self.routes.get(self._url[len(self.base) :], "404 not found"))

    def current_url(self) -> str:
        return self._url

    def screenshot(self, path: Path) -> bool:
        return False


def test_pass_when_all_assertions_hold():
    page = _RoutedFakePage({"/": "Welcome home"})
    res = wd.run_web_suite(
        cases=wd.parse_web_cases(
            "## Case: home\nGoto: /\nExpect-text: Welcome\nExpect-no: error\n"
        ),
        page=page,
    )
    assert res.verdict == "PASS"
    assert res.results[0].status == "PASS"


def test_fail_on_missing_text_with_proof():
    page = _RoutedFakePage({"/": "Hello stranger"})
    res = wd.run_web_suite(
        cases=wd.parse_web_cases("## Case: home\nGoto: /\nExpect-text: Welcome\n"),
        page=page,
    )
    assert res.verdict == "FAIL"
    assert res.results[0].status == "FAIL"
    assert "Welcome" in res.results[0].detail


def test_fail_on_forbidden_text():
    page = _RoutedFakePage({"/": "Fatal error: boom"})
    res = wd.run_web_suite(
        cases=wd.parse_web_cases("## Case: home\nGoto: /\nExpect-no: error\n"),
        page=page,
    )
    assert res.results[0].status == "FAIL"
    assert "forbidden" in res.results[0].detail


def test_fail_on_url_assertion():
    page = _RoutedFakePage(
        {"/": "x", "/dashboard": "Dashboard"}, click_map={"text=Login": "/dashboard"}
    )
    res = wd.run_web_suite(
        cases=wd.parse_web_cases(
            "## Case: redirect\nGoto: /\nClick: text=Login\nExpect-url: /settings\n"
        ),
        page=page,
    )
    assert res.results[0].status == "FAIL"
    assert "url" in res.results[0].detail.lower()


def test_pass_on_url_and_text_after_click():
    page = _RoutedFakePage(
        {"/": "x", "/dashboard": "Welcome back"}, click_map={"text=Login": "/dashboard"}
    )
    res = wd.run_web_suite(
        cases=wd.parse_web_cases(
            "## Case: login\nGoto: /\nClick: text=Login\nExpect-url: /dashboard\n"
            "Expect-text: Welcome back\n"
        ),
        page=page,
    )
    assert res.verdict == "PASS"


def test_action_failure_is_a_fail_not_a_crash():
    """A click on a missing selector raises WebActionError inside the driver — classified as a
    FAIL with the failing selector, never an escaping traceback."""
    page = _RoutedFakePage({"/": "x"})
    res = wd.run_web_suite(
        cases=wd.parse_web_cases("## Case: click missing\nGoto: /\nClick: #nope\n"),
        page=page,
    )
    assert res.results[0].status == "FAIL"
    assert "#nope" in res.results[0].detail


def test_prose_only_case_blocks_the_run():
    page = _RoutedFakePage({"/": "x"})
    res = wd.run_web_suite(
        cases=wd.parse_web_cases("## Case: prose\njust words, no directives\n"),
        page=page,
    )
    assert res.results[0].status == "BLOCKED"
    assert res.verdict == "BLOCKED"  # an unexercised authored case is not a green run


def test_mixed_pass_and_prose_block_is_blocked_not_pass():
    """A suite mixing a passing structured case with a prose-only case verdicts BLOCKED (the
    prose case was not exercised), never PASS — mirrors the bot driver's same invariant."""
    page = _RoutedFakePage({"/": "Welcome"})
    res = wd.run_web_suite(
        cases=wd.parse_web_cases(
            "## Case: ok\nGoto: /\nExpect-text: Welcome\n## Case: prose\njust words\n"
        ),
        page=page,
    )
    assert {r.status for r in res.results} == {"PASS", "BLOCKED"}
    assert res.verdict == "BLOCKED"


# --- the QA RESULTS contract round-trips through the executor parser -------------------
def test_qa_results_contract_parses():
    page = _RoutedFakePage({"/": "Hello stranger"})
    res = wd.run_web_suite(
        cases=wd.parse_web_cases("## Case: home\nGoto: /\nExpect-text: Welcome\n"),
        page=page,
    )
    transcript = res.to_qa_results(sut_path=Path("/tmp/sut"), base_url="http://fake")
    verdict, findings, max_sev, cases = parse_qa_results(transcript)
    assert verdict == "FAIL"
    assert findings == 1 and max_sev == "P1"
    assert cases == {"run": 1, "passed": 0, "failed": 1, "blocked": 0}
    # report-only FAIL is exit 0; under --strict it flips to 10.
    assert (
        verdict_to_exit_code(verdict, findings=findings, strict=False, exit_blocked=8)
        == 0
    )
    assert (
        verdict_to_exit_code(verdict, findings=findings, strict=True, exit_blocked=8)
        == 10
    )


def test_blocked_transcript_maps_to_boot_failed():
    res = wd.WebRunResult(blocked_reason="REVIEW_QA_PLAYWRIGHT off")
    transcript = res.to_qa_results(sut_path=Path("/tmp/sut"), base_url="http://x")
    verdict, findings, _sev, _cases = parse_qa_results(transcript)
    assert verdict == "BLOCKED"
    assert (
        verdict_to_exit_code(verdict, findings=findings, strict=False, exit_blocked=8)
        == 8
    )


def test_text_assertions_match_body_not_head_or_attributes():
    """The fake (like the real browser's inner_text) matches BODY text only — a string present
    ONLY in <title>/<head>/a comment/an attribute must NOT satisfy Expect-text, and an Expect-no
    on such a string must NOT fire. This pins the fidelity gap the review flagged (raw-HTML vs
    rendered-text), so the deterministic DoD validates the same semantics the live browser does."""
    html = (
        "<html><head><title>SECRETTITLE</title></head>"
        "<body><!-- HIDDENCOMMENT --><p data-x='ATTRVAL'>Visible hello</p></body></html>"
    )
    page = _RoutedFakePage({"/": html})
    # Expect-text on a head/comment/attribute string -> NOT found in body text -> FAIL.
    res = wd.run_web_suite(
        cases=wd.parse_web_cases(
            "## Case: head text invisible\nGoto: /\nExpect-text: SECRETTITLE\n"
        ),
        page=page,
    )
    assert res.results[0].status == "FAIL"
    # Expect-no on the same head string -> body has no such text -> the forbidden string is
    # absent -> PASS (a raw-HTML match would have wrongly FAILed this).
    res2 = wd.run_web_suite(
        cases=wd.parse_web_cases(
            "## Case: forbidden only in head\nGoto: /\nExpect-no: HIDDENCOMMENT\n"
            "Expect-text: Visible hello\n"
        ),
        page=page,
    )
    assert res2.results[0].status == "PASS"


# --- the config block -----------------------------------------------------------------
def test_web_config_requires_base_url():
    try:
        WebConfig(base_url="")
    except QaConfigError:
        pass
    else:
        raise AssertionError("empty base_url must raise QaConfigError")


def test_web_config_accepts_live_agent_browser_driver_flags_is_live():
    """The agent-browser Tier-2 LIVE driver is now ACCEPTED (the scaffolding landed — #84) and
    flagged is_live (and may omit base_url, which it reads from REVIEW_QA_WEB_BASE_URL at the
    gate); the live RUN is still gated behind creds at dispatch. A genuinely-unknown driver is
    still rejected loud."""
    cfg = WebConfig(
        driver="agent-browser"
    )  # no base_url -> allowed for the live driver
    assert cfg.is_live
    try:
        WebConfig(base_url="http://x", driver="garbage")
    except QaConfigError:
        pass
    else:
        raise AssertionError("an unknown driver must raise QaConfigError")


def test_web_config_parsed_from_fixture_yaml():
    cfg = load_qa_config(_FIXTURES / "web-good", None)
    assert cfg is not None and cfg.kind == "web"
    assert cfg.web is not None
    assert cfg.web.driver == "playwright"
    assert cfg.web.base_url == "http://127.0.0.1:8099"
    assert cfg.web.command == ("python3", "serve.py")
    assert cfg.web.env.get("PORT") == "8099"


# --- the Playwright gate --------------------------------------------------------------
def test_playwright_off_by_default_is_a_clear_skip():
    """With REVIEW_QA_PLAYWRIGHT unset, playwright_available returns (False, <install hint>)."""
    saved = os.environ.pop("REVIEW_QA_PLAYWRIGHT", None)
    try:
        ok, reason = wh.playwright_available()
        assert ok is False
        assert "REVIEW_QA_PLAYWRIGHT=1" in reason and "playwright install" in reason
    finally:
        if saved is not None:
            os.environ["REVIEW_QA_PLAYWRIGHT"] = saved


# --- the health gate against a real stdlib HTTP server --------------------------------
def _serve_dir(directory: Path) -> tuple[ThreadingHTTPServer, str]:
    """Start a real loopback HTTP server for ``directory`` on an ephemeral port; return
    (server, base_url). Lets the reachability tests exercise the REAL HTTP probe path."""

    def handler(*a, **k):
        return SimpleHTTPRequestHandler(*a, directory=str(directory), **k)

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


def test_wait_until_reachable_true_for_a_live_server():
    server, base = _serve_dir(_FIXTURES / "web-good" / "site")
    try:
        assert wh.wait_until_reachable(base + "/index.html", timeout_s=5) is True
    finally:
        server.shutdown()


def test_wait_until_reachable_false_for_a_dead_address():
    # An unused loopback port: nothing listening -> not reachable within a short timeout.
    assert wh.wait_until_reachable("http://127.0.0.1:1", timeout_s=1) is False


def test_boot_server_health_gate_and_reap_no_browser():
    """Boot the fixture's REAL dev server (a stdlib serve.py) on a free port, health-gate it
    reachable, then reap it — the bring-up/health/teardown path exercised deterministically with
    NO browser (review finding: this path was only covered in the gated live test)."""
    import socket
    import time

    sut = (_FIXTURES / "web-good").resolve()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = wh.boot_web_server(
        command=["python3", "serve.py"],
        cwd=sut,
        extra_env={"PORT": str(port)},
        exit_boot_failed=8,
    )
    try:
        ready = f"http://127.0.0.1:{port}/index.html"
        assert wh.wait_until_reachable(ready, timeout_s=10, server=server) is True
        assert server.proc.poll() is None  # still running
    finally:
        server.reap()
    # After reap the process is gone (idempotent: a second reap is a no-op).
    time.sleep(0.3)
    assert server.proc.poll() is not None
    server.reap()


def test_boot_server_registers_with_the_signal_reaper():
    """review-cli#162 follow-up (codex review): `boot_web_server` must register the dev
    server with `process._LIVE_CHILDREN` — the same registry `install_signal_reaper`'s
    SIGTERM/SIGINT handler and the internal backstop's `kill_live_children()` sweep —
    so an external signal (or a wedged run) reaps this SUT process too, not only
    `_run_streamed`'s own backend children. Before this fix, a QA web run's dev server
    was invisible to that registry entirely."""
    from reviewlib import process as proc_mod

    sut = (_FIXTURES / "web-good").resolve()
    server = wh.boot_web_server(
        command=["python3", "serve.py"],
        cwd=sut,
        extra_env={"PORT": "0"},
        exit_boot_failed=8,
    )
    try:
        with proc_mod._LIVE_CHILDREN_LOCK:
            live_pids = {p.pid for p, _pgid in proc_mod._LIVE_CHILDREN}
        assert server.proc.pid in live_pids
    finally:
        server.reap()
    with proc_mod._LIVE_CHILDREN_LOCK:
        live_pids_after = {p.pid for p, _pgid in proc_mod._LIVE_CHILDREN}
    assert server.proc.pid not in live_pids_after


def test_boot_server_bad_command_is_a_controlled_blocked():
    """A dev-server command that cannot be launched raises WebHarnessError carrying the boot-failed
    exit class — a controlled BLOCKED, never a raw OSError traceback."""
    try:
        wh.boot_web_server(
            command=["this-binary-does-not-exist-xyz"],
            cwd=_FIXTURES,
            exit_boot_failed=8,
        )
    except wh.WebHarnessError as exc:
        assert exc.exit_code == 8
    else:
        raise AssertionError("a missing dev-server binary must raise WebHarnessError")


# --- the fast-path routing in modes/qa.py --------------------------------------------
class _Args:
    """A minimal stand-in for the parsed argparse namespace the routing reads (kind + config)."""

    def __init__(self, kind="auto", config=None):
        self.kind = kind
        self.config = config


class _Ctx:
    """A minimal ModeContext stand-in: the web routing only reads ctx.args."""

    def __init__(self, args):
        self.args = args


def test_routing_web_kind_with_config_takes_deterministic_path():
    """--kind web (or sut.kind: web) + a sut.web config resolves to the WebConfig — the
    deterministic fast path. The fixture declares both, so explicit and auto both route."""
    from reviewlib.modes.qa import _resolve_deterministic_web

    sut = _FIXTURES / "web-good"
    cfg = _resolve_deterministic_web(_Ctx(_Args(kind="web")), sut)
    assert cfg is not None and cfg.driver == "playwright"
    # under --kind auto, the qa.yaml's sut.kind: web still routes.
    cfg_auto = _resolve_deterministic_web(_Ctx(_Args(kind="auto")), sut)
    assert cfg_auto is not None


def test_routing_web_kind_without_config_falls_through():
    """--kind web on a SUT with NO sut.web config returns None (falls through to the un-caged
    executor path, where the prose web runbook drives an agent by hand)."""
    from reviewlib.modes.qa import _resolve_deterministic_web

    # sut-good is a backend/CLI fixture (no sut.web) — --kind web must NOT route to the web path.
    sut = _FIXTURES / "sut-good"
    assert _resolve_deterministic_web(_Ctx(_Args(kind="web")), sut) is None


def test_routing_non_web_kind_does_not_take_web_path():
    """A non-web kind never takes the web path, even on a SUT that happens to have a sut.web."""
    from reviewlib.modes.qa import _resolve_deterministic_web

    sut = _FIXTURES / "web-good"
    assert _resolve_deterministic_web(_Ctx(_Args(kind="backend")), sut) is None


def test_command_omitted_unreachable_target_blocks_not_fails():
    """The command-omitted 'already-running base_url' path MUST health-gate the target first: a
    DOWN target BLOCKS (infra, exit 8), it does NOT silently drive the browser into a report-only
    navigation FAIL (exit 0) that callers can't tell from a found bug (codex PR review P1)."""
    from reviewlib.modes.qa import _bring_up_and_drive_web
    from reviewlib.qa.config import WebConfig

    # No command + a base_url nothing listens on -> the gate must fail and BLOCK.
    cfg = WebConfig(
        base_url="http://127.0.0.1:1", command=(), ready_path="/", ready_timeout_s=1
    )
    transcript = _bring_up_and_drive_web(
        cwd=_FIXTURES,
        sut_path=_FIXTURES,
        suite_text="## Case: x\nGoto: /\nExpect-text: hi\n",
        web_config=cfg,
        out_dir=None,
        exit_blocked=8,
    )
    verdict, _findings, _sev, _cases = parse_qa_results(transcript)
    assert verdict == "BLOCKED", transcript
    assert "already-running" in transcript or "did not answer" in transcript
    assert verdict_to_exit_code(verdict, findings=0, strict=False, exit_blocked=8) == 8


# --- the 2-fixture DoD (deterministic, no browser): good -> PASS, buggy -> FAIL -------
def _run_fixture_deterministic(name: str) -> str:
    """Drive a fixture's site through the REAL driver against a fake page backed by the fixture's
    real HTML, returning the ## QA RESULTS transcript. No browser, no dev-server subprocess — the
    fake page reads the fixture files directly, so the DoD is about the DRIVER + the fixture's
    rendered text, deterministic in normal CI."""
    sut = _FIXTURES / name
    suite_files = sorted((sut / "docs" / "tests" / "suites").glob("*.md"))
    suite_text = load_suites_text(suite_files, max_cases=None)
    cases = wd.parse_web_cases(suite_text)
    site = sut / "site"
    routes = {
        "/index.html": (site / "index.html").read_text(encoding="utf-8"),
        "/about.html": (site / "about.html").read_text(encoding="utf-8"),
    }
    page = _RoutedFakePage(routes, click_map={"text=About": "/about.html"})
    res = wd.run_web_suite(cases=cases, page=page)
    return res.to_qa_results(sut_path=sut.resolve(), base_url="http://fixture")


def test_dod_good_fixture_passes():
    transcript = _run_fixture_deterministic("web-good")
    verdict, findings, _sev, cases = parse_qa_results(transcript)
    assert verdict == "PASS", transcript
    assert findings == 0
    assert cases == {"run": 2, "passed": 2, "failed": 0, "blocked": 0}
    assert (
        verdict_to_exit_code(verdict, findings=findings, strict=False, exit_blocked=8)
        == 0
    )


def test_dod_buggy_fixture_fails_with_a_finding():
    transcript = _run_fixture_deterministic("web-buggy")
    verdict, findings, max_sev, cases = parse_qa_results(transcript)
    assert verdict == "FAIL", transcript
    assert findings >= 1 and max_sev == "P1"
    assert cases["failed"] >= 1
    # report-only: a found bug exits 0 (the report carries it); --strict flips it to 10.
    assert (
        verdict_to_exit_code(verdict, findings=findings, strict=False, exit_blocked=8)
        == 0
    )
    assert (
        verdict_to_exit_code(verdict, findings=findings, strict=True, exit_blocked=8)
        == 10
    )


# --- the LIVE-browser DoD (gated; SKIPs without Chromium) -----------------------------
def _playwright_browser_ready() -> bool:
    """True only if REVIEW_QA_PLAYWRIGHT is on AND playwright + a Chromium browser are installed
    AND a browser actually launches. Used to gate the live test so it never fails a normal CI."""
    ok, _reason = wh.playwright_available()
    if not ok:
        return False
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception:  # noqa: BLE001 — browser not installed / cannot launch -> skip the live test
        return False


def test_live_browser_dod_good_passes():
    """The REAL headless-Chromium DoD: boot the fixture's dev server, drive the good fixture in a
    live browser, assert PASS. Gated on REVIEW_QA_PLAYWRIGHT=1 + an installed Chromium; SKIPs
    otherwise so normal CI (no browser) is unaffected. This is the end-to-end proof that the
    PlaywrightPage + dev-server bring-up + health gate all work against a real browser."""
    if not _playwright_browser_ready():
        _skip(
            "REVIEW_QA_PLAYWRIGHT=1 + a Chromium browser (python -m playwright install chromium)"
        )
        return
    transcript = _run_fixture_live("web-good")
    verdict, _findings, _sev, _cases = parse_qa_results(transcript)
    assert verdict == "PASS", transcript


def test_live_browser_dod_buggy_fails():
    if not _playwright_browser_ready():
        _skip(
            "REVIEW_QA_PLAYWRIGHT=1 + a Chromium browser (python -m playwright install chromium)"
        )
        return
    transcript = _run_fixture_live("web-buggy")
    verdict, findings, _sev, _cases = parse_qa_results(transcript)
    assert verdict == "FAIL", transcript
    assert findings >= 1


def _free_port() -> int:
    """An OS-assigned free loopback port, so the live DoD never collides with a hardcoded
    fixture port (a lingering prior server) and stays flake-free under repeats."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_fixture_live(name: str) -> str:
    """Boot the fixture's real dev server on a FRESH free port, health-gate it, drive the suite
    in a live headless browser, and return the ## QA RESULTS transcript. Guaranteed server
    teardown. The free port (not the fixture's hardcoded one) keeps the live DoD robust to a
    lingering prior server / parallel runs."""
    sut = _FIXTURES / name
    cfg = load_qa_config(sut, None)
    assert cfg is not None and cfg.web is not None
    suite_files = sorted((sut / "docs" / "tests" / "suites").glob("*.md"))
    suite_text = load_suites_text(suite_files, max_cases=None)
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    server = None
    try:
        server = wh.boot_web_server(
            command=list(cfg.web.command),
            cwd=sut.resolve(),
            extra_env={**cfg.web.env, "PORT": str(port)},
            exit_boot_failed=8,
        )
        ready = base_url + cfg.web.ready_path
        assert wh.wait_until_reachable(
            ready, timeout_s=cfg.web.ready_timeout_s, server=server
        ), f"dev server never reachable at {ready}\n{server.output_tail()}"
        return wd.run_web_test(
            suite_text=suite_text, base_url=base_url, sut_path=sut.resolve()
        )
    finally:
        if server is not None:
            server.reap()


def _skip(reason: str) -> None:
    """SKIP under pytest (a real pytest skip) or print a SKIP line standalone."""
    try:
        import pytest

        if os.environ.get("PYTEST_CURRENT_TEST"):
            pytest.skip(reason)
    except ImportError:
        pass
    print(f"SKIP (web live browser): {reason}")


if __name__ == "__main__":
    failures = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"ok   {_name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {_name}: {exc}")
    sys.exit(1 if failures else 0)
