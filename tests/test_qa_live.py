#!/usr/bin/env python3
"""review qa — Tier-2 (the LIVE tier) SCAFFOLDING (docs/specs/review-qa.md §7.3/§7.4, #82/#84).

Tier-2 is the LIVE tier: a real test Telegram account over MTProto (bot), a real browser against
a deployed site (web), a real VS Code with window-screenshot visual diffing (ext). The LIVE RUN
itself needs creds/infra the CTO provisions (the live run lands under #82); these tests pin the
SCAFFOLDING that is buildable WITHOUT creds:

  * the per-SUT availability GATE returns ``(ok, reason)`` and, when creds are absent, names the
    EXACT missing env var / dep / infra (the SKIP-LOUD message) — never a fake pass, never a crash;
  * a set-but-EMPTY credential var is treated as MISSING (an empty api hash is no api hash);
  * the bot gate fails CLOSED when the test chat equals the real ``TG_CHAT_ID`` (safety);
  * the config layer ACCEPTS the Tier-2 driver values (``mtproto``/``agent-browser``/
    ``vscode-visual``) and flags them ``is_live``, while still rejecting garbage and still
    requiring a Tier-1 web ``base_url``;
  * the live-driver SKELETON ``connect`` raises ``LiveTierUnavailable`` (the live run = #82) so a
    misrouted call BLOCKS rather than silently no-op'ing;
  * the DISPATCH routes a ``tier: live`` block to a controlled BLOCKED (the gate's missing-creds
    message) and NEVER to the un-caged executor or a green pass;
  * the creds DOC names the same env vars the gates name (no drift between the SKIP message and
    the docs the CTO reads).

ALL deterministic — NO creds, NO Telegram, NO browser, NO VS Code, NO network. Runnable
standalone (``python3 tests/test_qa_live.py``) or under pytest.
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib.qa import live_tier as lt  # noqa: E402
from reviewlib.qa.config import BotConfig, ExtConfig, QaConfigError, WebConfig  # noqa: E402


# --- a small env sandbox so a test never leaks creds into the process or another test ----------
@contextmanager
def _env(**overrides: str | None):
    """Set/clear env vars for the body, restoring the prior values on exit. A value of ``None``
    DELETES the var (so a test can assert the absent path even if the dev has it set)."""
    saved: dict[str, str | None] = {k: os.environ.get(k) for k in overrides}
    try:
        for k, v in overrides.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# Every cred/flag var the live tiers touch — cleared by default so a gate test sees the absent
# path regardless of the dev's own environment.
_ALL_LIVE_VARS = {
    "REVIEW_QA_BOT_LIVE": None, "TG_TEST_API_ID": None, "TG_TEST_API_HASH": None,
    "TG_TEST_SESSION": None, "TG_TEST_CHAT_ID": None, "TG_CHAT_ID": None,
    "REVIEW_QA_WEB_LIVE": None, "REVIEW_QA_WEB_BASE_URL": None,
    "REVIEW_QA_EXT_LIVE": None, "REVIEW_QA_VSCODE": None, "REVIEW_QA_EXT_BASELINE_DIR": None,
}


# --- the bot gate ---------------------------------------------------------------------------
def test_bot_gate_off_by_default_names_flag_and_creds():
    """With nothing set, the bot gate is not ok and the reason names the opt-in flag + the creds."""
    with _env(**_ALL_LIVE_VARS):
        gate = lt.bot_live_available()
    assert not gate.ok
    assert "REVIEW_QA_BOT_LIVE" in gate.reason
    for cred in lt.BOT_LIVE_CREDS:
        assert cred in gate.reason, f"{cred} not named in the SKIP message"


def test_bot_gate_flag_on_missing_creds_names_exact_missing():
    """Flag on + telethon present but creds absent → the reason names the SPECIFIC missing creds
    (so the CTO knows exactly what to provision), not a generic 'creds missing'. Telethon is
    stubbed present so the test ISOLATES the creds branch deterministically — without the stub the
    gate would take the telethon-missing branch on CI and the creds-missing branch on a dev box
    with telethon installed (host-dependent coverage)."""
    with _env(**{**_ALL_LIVE_VARS, "REVIEW_QA_BOT_LIVE": "1"}), _stub_telethon():
        gate = lt.bot_live_available()
    assert not gate.ok
    # The creds branch is now isolated — every required cred must be named in the SKIP message.
    for cred in lt.BOT_LIVE_CREDS:
        assert cred in gate.reason, f"{cred} not named in the SKIP message"


def test_bot_gate_empty_cred_is_treated_as_missing():
    """A set-but-BLANK credential var counts as missing — an empty api hash must not pass the
    gate. Telethon stubbed present so the creds check is the only thing under test."""
    env = {**_ALL_LIVE_VARS, "REVIEW_QA_BOT_LIVE": "1",
           "TG_TEST_API_ID": "123", "TG_TEST_API_HASH": "   ",  # blank
           "TG_TEST_SESSION": "sess", "TG_TEST_CHAT_ID": "-100"}
    with _env(**env), _stub_telethon():
        gate = lt.bot_live_available()
    assert not gate.ok
    assert "TG_TEST_API_HASH" in gate.reason


def test_bot_gate_fails_closed_when_test_chat_is_real_chat():
    """SAFETY: even with the flag + all creds, the gate refuses if the test chat id equals the
    real TG_CHAT_ID — driving the real chat would spam a real human. Fail closed."""
    env = {**_ALL_LIVE_VARS, "REVIEW_QA_BOT_LIVE": "1", "TG_TEST_API_ID": "1",
           "TG_TEST_API_HASH": "h", "TG_TEST_SESSION": "s",
           "TG_TEST_CHAT_ID": "999", "TG_CHAT_ID": "999"}
    with _env(**env), _stub_telethon():
        gate = lt.bot_live_available()
    assert not gate.ok
    assert "SAFETY" in gate.reason
    assert "TG_TEST_CHAT_ID" in gate.reason


def test_bot_gate_all_green_when_flag_creds_and_safe_chat():
    """With the flag, every cred, telethon present, and a test chat distinct from the real one,
    the gate is ok — proving the gate's positive path is reachable (the live RUN is still #82)."""
    env = {**_ALL_LIVE_VARS, "REVIEW_QA_BOT_LIVE": "1", "TG_TEST_API_ID": "1",
           "TG_TEST_API_HASH": "h", "TG_TEST_SESSION": "s",
           "TG_TEST_CHAT_ID": "-100", "TG_CHAT_ID": "-200"}
    with _env(**env), _stub_telethon():
        gate = lt.bot_live_available()
    assert gate.ok, gate.reason
    assert gate.reason == ""


# --- the web gate ---------------------------------------------------------------------------
def test_web_gate_off_by_default_names_flag_and_url():
    with _env(**_ALL_LIVE_VARS):
        gate = lt.web_live_available()
    assert not gate.ok
    assert "REVIEW_QA_WEB_LIVE" in gate.reason
    assert "REVIEW_QA_WEB_BASE_URL" in gate.reason


def test_web_gate_flag_on_no_url_names_missing_url():
    """Flag on + a browser runtime present but no site URL → names REVIEW_QA_WEB_BASE_URL. A
    browser runtime is stubbed so the test isolates the URL check."""
    with _env(**{**_ALL_LIVE_VARS, "REVIEW_QA_WEB_LIVE": "1"}), _stub_playwright():
        gate = lt.web_live_available()
    assert not gate.ok
    assert "REVIEW_QA_WEB_BASE_URL" in gate.reason


def test_web_gate_all_green_with_flag_runtime_and_url():
    with _env(**{**_ALL_LIVE_VARS, "REVIEW_QA_WEB_LIVE": "1",
                 "REVIEW_QA_WEB_BASE_URL": "https://stage.example.test"}), _stub_playwright():
        gate = lt.web_live_available()
    assert gate.ok, gate.reason


# --- the ext gate ---------------------------------------------------------------------------
def test_ext_gate_off_by_default_names_flag_and_baseline():
    with _env(**_ALL_LIVE_VARS):
        gate = lt.ext_live_available()
    assert not gate.ok
    assert "REVIEW_QA_EXT_LIVE" in gate.reason
    assert "REVIEW_QA_EXT_BASELINE_DIR" in gate.reason


def test_ext_gate_flag_on_needs_underlying_vscode_gate():
    """Flag on but the underlying Tier-1 VS Code gate unsatisfied → the reason surfaces the VS
    Code gate's own message (REVIEW_QA_VSCODE off), proving the gate composes, not duplicates."""
    with _env(**{**_ALL_LIVE_VARS, "REVIEW_QA_EXT_LIVE": "1"}):
        gate = lt.ext_live_available()
    assert not gate.ok
    assert "REVIEW_QA_VSCODE" in gate.reason


def test_ext_gate_missing_baseline_dir_named():
    """Flag on + the VS Code gate stubbed satisfied, but no baseline dir → names
    REVIEW_QA_EXT_BASELINE_DIR (the thick ext-specific branch the off-by-default test skips over)."""
    with _env(**{**_ALL_LIVE_VARS, "REVIEW_QA_EXT_LIVE": "1"}), _stub_vscode_gate(True), \
            _stub_magick(True):
        gate = lt.ext_live_available()
    assert not gate.ok
    assert "REVIEW_QA_EXT_BASELINE_DIR" in gate.reason


def test_ext_gate_missing_magick_named():
    """Flag + VS Code gate + baseline dir present, but no `magick` → names the perceptual-diff
    tool (the last ext-specific branch)."""
    import tempfile

    with tempfile.TemporaryDirectory() as base:
        env = {**_ALL_LIVE_VARS, "REVIEW_QA_EXT_LIVE": "1", "REVIEW_QA_EXT_BASELINE_DIR": base}
        with _env(**env), _stub_vscode_gate(True), _stub_magick(False):
            gate = lt.ext_live_available()
    assert not gate.ok
    assert "magick" in gate.reason.lower()


def test_ext_gate_all_green_when_everything_present():
    """The ext gate's positive path: flag + VS Code gate satisfied + a baseline dir + magick → ok
    (the live RUN is still #82). Symmetric with the bot/web all-green tests."""
    import tempfile

    with tempfile.TemporaryDirectory() as base:
        env = {**_ALL_LIVE_VARS, "REVIEW_QA_EXT_LIVE": "1", "REVIEW_QA_EXT_BASELINE_DIR": base}
        with _env(**env), _stub_vscode_gate(True), _stub_magick(True):
            gate = lt.ext_live_available()
    assert gate.ok, gate.reason


# --- the baseline-dir VALIDATION (codex P2): a non-empty value is not enough — it must be a
#     writable dir OR a creatable path, else a controlled BLOCKED, not an ok gate that explodes
#     on the first baseline write outside the availability gate ----------------------------------
def _ext_gate_with_baseline(baseline: str):
    """Run ext_live_available with the flag on, the VS Code gate + magick stubbed present, and
    REVIEW_QA_EXT_BASELINE_DIR=baseline — so the baseline-dir branch is the only thing under test."""
    env = {**_ALL_LIVE_VARS, "REVIEW_QA_EXT_LIVE": "1", "REVIEW_QA_EXT_BASELINE_DIR": baseline}
    with _env(**env), _stub_vscode_gate(True), _stub_magick(True):
        return lt.ext_live_available()


def test_ext_gate_baseline_is_a_file_blocked():
    """REVIEW_QA_EXT_BASELINE_DIR pointing at an existing FILE (not a dir) → controlled BLOCKED
    naming the var, never an ok gate (the first baseline write would fail outside the gate)."""
    import tempfile

    with tempfile.TemporaryDirectory() as parent:
        f = Path(parent) / "baseline.png"
        f.write_text("not a dir", encoding="utf-8")
        gate = _ext_gate_with_baseline(str(f))
    assert not gate.ok
    assert "REVIEW_QA_EXT_BASELINE_DIR" in gate.reason


def test_ext_gate_baseline_unwritable_dir_blocked():
    """An existing-but-UNWRITABLE baseline dir → controlled BLOCKED (the first run could not record
    a baseline there). ``os.access`` is stubbed to deny write so the branch fires on ANY uid — a real
    chmod can't revoke write from root, which would silently no-op this case in a root CI env."""
    import tempfile

    with tempfile.TemporaryDirectory() as d, _deny_write_access():
        gate = _ext_gate_with_baseline(d)
    assert not gate.ok
    assert "REVIEW_QA_EXT_BASELINE_DIR" in gate.reason


def test_ext_gate_baseline_dangling_symlink_blocked():
    """A baseline path that is a DANGLING symlink (target missing) → BLOCKED: ``exists()`` is False
    but the path is occupied, so the first-run ``mkdir`` would fail — exactly the explode-outside-
    the-gate class the validation closes."""
    import tempfile

    with tempfile.TemporaryDirectory() as parent:
        link = Path(parent) / "link"
        os.symlink(Path(parent) / "nonexistent-target", link)
        gate = _ext_gate_with_baseline(str(link))
    assert not gate.ok
    assert "REVIEW_QA_EXT_BASELINE_DIR" in gate.reason


def test_ext_gate_baseline_symlink_to_writable_dir_ok():
    """A baseline path that is a symlink to an EXISTING writable directory is ACCEPTED — it goes
    through the ``exists()``/``is_dir()`` branch (not the dangling-symlink branch), so a live
    symlink is NOT blocked. Locks the distinction from test_ext_gate_baseline_dangling_symlink."""
    import tempfile

    with tempfile.TemporaryDirectory() as parent:
        target = Path(parent) / "real"
        target.mkdir()
        link = Path(parent) / "link"
        os.symlink(target, link)
        gate = _ext_gate_with_baseline(str(link))
    assert gate.ok, gate.reason


def test_ext_gate_baseline_uncreatable_parent_blocked():
    """A not-yet-existing baseline path whose PARENT is also missing → can't be created on the
    first run → controlled BLOCKED, not an ok gate."""
    import tempfile

    with tempfile.TemporaryDirectory() as parent:
        target = Path(parent) / "missing" / "nested" / "baselines"  # parent dir does not exist
        gate = _ext_gate_with_baseline(str(target))
    assert not gate.ok
    assert "REVIEW_QA_EXT_BASELINE_DIR" in gate.reason


def test_ext_gate_baseline_unwritable_parent_blocked():
    """A not-yet-existing baseline path whose PARENT exists but is NOT writable → can't be created
    → BLOCKED. Covers the ``os.access(parent, W_OK)`` sub-branch — the uncreatable-parent test
    above uses a MISSING parent, which only exercises the ``not parent.is_dir()`` half."""
    import tempfile

    with tempfile.TemporaryDirectory() as parent:
        target = Path(parent) / "baselines"  # parent exists; denied write makes it uncreatable
        with _deny_write_access():
            gate = _ext_gate_with_baseline(str(target))
    assert not gate.ok
    assert "REVIEW_QA_EXT_BASELINE_DIR" in gate.reason


def test_ext_gate_baseline_creatable_path_ok():
    """A not-yet-existing baseline dir whose PARENT exists and is writable is ACCEPTED (the first
    run creates it) → the gate is ok (the live RUN is still #82). The other half of the happy path
    alongside test_ext_gate_all_green_when_everything_present (which uses an existing dir)."""
    import tempfile

    with tempfile.TemporaryDirectory() as parent:
        target = Path(parent) / "baselines"  # parent exists + writable, target absent → creatable
        gate = _ext_gate_with_baseline(str(target))
    assert gate.ok, gate.reason


# --- the heavy-dep-ABSENT SKIP branches (the ones that actually fire in CI) ------------------
def test_bot_gate_telethon_absent_names_the_dep():
    """Flag on + telethon NOT importable → the SKIP message names `telethon` + the install. This
    is the branch that fires in CI (telethon is not installed there); pin its message."""
    with _env(**{**_ALL_LIVE_VARS, "REVIEW_QA_BOT_LIVE": "1"}), _force_import_error("telethon"):
        gate = lt.bot_live_available()
    assert not gate.ok
    assert "telethon" in gate.reason.lower()


def test_web_gate_browser_runtime_absent_names_the_fix():
    """Flag on + no playwright AND no agent-browser → the SKIP message names the install/PATH fix.
    This is the branch that fires on a host without a browser runtime."""
    import shutil as _sh

    with _env(**{**_ALL_LIVE_VARS, "REVIEW_QA_WEB_LIVE": "1"}), _force_import_error("playwright"):
        # also ensure agent-browser is not found on PATH for this assertion
        real_which = _sh.which
        _sh.which = lambda name: None if name == "agent-browser" else real_which(name)
        try:
            gate = lt.web_live_available()
        finally:
            _sh.which = real_which
    assert not gate.ok
    assert "playwright" in gate.reason.lower() or "agent-browser" in gate.reason.lower()


# --- driver-name routing --------------------------------------------------------------------
def test_is_live_driver_routing():
    assert lt.is_live_driver("bot", lt.BOT_LIVE_DRIVER)
    assert lt.is_live_driver("web", lt.WEB_LIVE_DRIVER)
    assert lt.is_live_driver("ext", lt.EXT_LIVE_DRIVER)
    assert not lt.is_live_driver("bot", "mock")
    assert not lt.is_live_driver("web", "playwright")
    assert not lt.is_live_driver("ext", "vscode")


def test_live_gate_for_unknown_kind_is_not_ok():
    """A stray tier:live on a kind with no live tier (backend) is a not-ok gate, not a crash."""
    gate = lt.live_gate_for("backend")
    assert not gate.ok
    assert "no Tier-2 live driver" in gate.reason


# --- the live-driver skeletons --------------------------------------------------------------
def test_live_drivers_connect_block_until_82():
    """Each live-driver skeleton's connect raises LiveTierUnavailable (the live run is #82),
    carrying the boot-failed exit class — a misrouted call BLOCKS, never silently no-ops."""
    for kind in ("bot", "web", "ext"):
        driver = lt.live_driver_for(kind, exit_blocked=8)
        try:
            driver.connect()
            raise AssertionError(f"{kind} connect should have raised")
        except lt.LiveTierUnavailable as exc:
            assert exc.exit_code == 8
            assert "#82" in str(exc)


def test_live_driver_for_unknown_kind_raises():
    try:
        lt.live_driver_for("backend", exit_blocked=8)
        raise AssertionError("backend should have no live driver")
    except lt.LiveTierUnavailable as exc:
        assert exc.exit_code == 8


# --- config acceptance ----------------------------------------------------------------------
def test_config_accepts_live_drivers_and_flags_is_live():
    bot = BotConfig(driver=lt.BOT_LIVE_DRIVER, command=("python", "bot.py"))
    assert bot.is_live
    web = WebConfig(driver=lt.WEB_LIVE_DRIVER)  # no base_url -> allowed for the live driver
    assert web.is_live
    ext = ExtConfig(driver=lt.EXT_LIVE_DRIVER)
    assert ext.is_live


def test_config_tier1_drivers_not_live():
    assert not BotConfig(driver="mock", command=("x",)).is_live
    assert not WebConfig(driver="playwright", base_url="http://127.0.0.1:8080").is_live
    assert not ExtConfig(driver="vscode").is_live


def test_config_tier1_web_still_requires_base_url():
    """The relaxation that lets the LIVE web driver omit base_url must NOT relax the Tier-1
    playwright driver — it still requires base_url (regression guard)."""
    try:
        WebConfig(driver="playwright")
        raise AssertionError("playwright must still require base_url")
    except QaConfigError as exc:
        assert "base_url" in str(exc)


def test_config_still_rejects_garbage_driver():
    for make in (
        lambda: BotConfig(driver="garbage", command=("x",)),
        lambda: WebConfig(driver="garbage", base_url="http://x"),
        lambda: ExtConfig(driver="garbage"),
    ):
        try:
            make()
            raise AssertionError("garbage driver must be rejected")
        except QaConfigError:
            pass


# --- the dispatch routes tier:live to a controlled BLOCKED, never the executor --------------
@contextmanager
def _scratch():
    """A throwaway tempdir for a dispatch test's report — never writes into the repo tree."""
    import shutil
    import tempfile

    d = Path(tempfile.mkdtemp(prefix="qa-live-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_dispatch_live_bot_emits_blocked_not_executor():
    """A `driver: mtproto` bot block (creds absent) routes to a controlled BLOCKED transcript
    naming the missing creds — NOT the un-caged executor, NOT a green pass."""
    from reviewlib.modes import qa as qamode

    with _scratch() as out, _env(**_ALL_LIVE_VARS):
        report = out / "report.md"
        code = qamode._run_bot_live(report, out, strict=False, exit_blocked=8, in_place=False)
        text = report.read_text(encoding="utf-8")
    assert "## QA RESULTS" in text
    assert "BLOCKED" in text
    assert "REVIEW_QA_BOT_LIVE" in text
    # The report FOOTER carries the live backend label, not the bot Tier-1 hermetic default.
    assert "backend: bot-live" in text and "hermetic-bot" not in text
    # BLOCKED bring-up maps to the boot-failed exit class, not 0 (a clean pass).
    assert code == 8, code


def test_dispatch_live_web_emits_blocked():
    from reviewlib.modes import qa as qamode

    cfg = WebConfig(driver=lt.WEB_LIVE_DRIVER)
    with _scratch() as out, _env(**_ALL_LIVE_VARS):
        report = out / "report.md"
        code = qamode._run_web_live(report, out, cfg, strict=False, exit_blocked=8, in_place=False)
        text = report.read_text(encoding="utf-8")
    assert "## QA RESULTS" in text and "BLOCKED" in text
    assert "REVIEW_QA_WEB_LIVE" in text
    assert "backend: web-live" in text and "hermetic-bot" not in text
    assert code == 8


def test_dispatch_live_ext_emits_blocked():
    from reviewlib.modes import qa as qamode

    cfg = ExtConfig(driver=lt.EXT_LIVE_DRIVER)
    with _scratch() as out, _env(**_ALL_LIVE_VARS):
        report = out / "report.md"
        code = qamode._run_ext_live(report, out, cfg, strict=False, exit_blocked=8, in_place=False)
        text = report.read_text(encoding="utf-8")
    assert "## QA RESULTS" in text and "BLOCKED" in text
    assert "REVIEW_QA_EXT_LIVE" in text
    assert "backend: ext-live" in text and "hermetic-bot" not in text
    assert code == 8


def test_tier1_web_ext_blocked_report_backend_label():
    """The report FOOTER ``backend:`` label must match the run's actual backend: a Tier-1 web
    BLOCKED report reads ``playwright-web`` and a Tier-1 ext BLOCKED report reads ``vscode-ext``,
    never the bot default ``hermetic-bot``. Guards completion of the label fix — the LIVE paths
    were relabelled first, but these Tier-1 web/ext sites shared the same mislabel (the saved
    footer contradicted the run's own ``backend=…`` stderr until fixed)."""
    from reviewlib.modes.qa import _emit_ext_blocked, _emit_web_blocked

    web_cfg = WebConfig(driver="playwright", base_url="http://127.0.0.1:8080")
    ext_cfg = ExtConfig(driver="vscode")
    with _scratch() as out:
        wreport = out / "w.md"
        _emit_web_blocked(wreport, out, web_cfg, "playwright off", 8, strict=False, in_place=False)
        wtext = wreport.read_text(encoding="utf-8")
        ereport = out / "e.md"
        _emit_ext_blocked(ereport, out, ext_cfg, "vscode off", 8, strict=False, in_place=False)
        etext = ereport.read_text(encoding="utf-8")
    assert "backend: playwright-web" in wtext and "hermetic-bot" not in wtext, wtext
    assert "backend: vscode-ext" in etext and "hermetic-bot" not in etext, etext


def test_tier1_pass_report_backend_label():
    """The HAPPY-path (non-blocked) Tier-1 report footer carries the run's real backend — bot
    ``hermetic-bot``, web ``playwright-web``, ext ``vscode-ext`` — and never another kind's label.
    For each kind: stub the availability gate present and the isolation-drive to return a PASS
    transcript (no real bot / browser / VS Code), then assert the drive was ACTUALLY reached — a
    per-kind called-flag + ``code == 0`` + the PASS marker — so the footer assertion can't be
    satisfied by an accidental fall-through to the BLOCKED helper (which now writes the same web/ext
    labels — codex P2). Covers the non-blocked label sites for all three kinds (claude #2); the
    BLOCKED helpers are covered by ``test_tier1_web_ext_blocked_report_backend_label``."""
    import reviewlib.modes.qa as qamode
    import reviewlib.qa.ext_harness as ext_harness
    import reviewlib.qa.web_harness as web_harness

    pass_tail = (
        "ran it.\n## QA RESULTS\nSUT: /s   KIND: x   BRING-UP: local\n"
        "CASES: 1 run, 1 passed, 0 failed, 0 blocked\n\n"
        "### FINDINGS\nno findings\n\n### BLOCKED\nnone\n\nVERDICT: PASS\n"
    )
    called = {"bot": False, "web": False, "ext": False}

    def _stub_drive(kind):
        def _drive(**_k):
            called[kind] = True
            return pass_tail
        return _drive

    saved = {
        (web_harness, "playwright_available"): web_harness.playwright_available,
        (ext_harness, "vscode_available"): ext_harness.vscode_available,
        (qamode, "_drive_bot_in_isolation"): qamode._drive_bot_in_isolation,
        (qamode, "_drive_web_in_isolation"): qamode._drive_web_in_isolation,
        (qamode, "_drive_ext_in_isolation"): qamode._drive_ext_in_isolation,
    }
    web_harness.playwright_available = lambda: (True, "")
    ext_harness.vscode_available = lambda: (True, "")
    qamode._drive_bot_in_isolation = _stub_drive("bot")
    qamode._drive_web_in_isolation = _stub_drive("web")
    qamode._drive_ext_in_isolation = _stub_drive("ext")

    # Tier-1 configs (is_live is False — driver-determined, not env-determined — so no live env is
    # needed or set here).
    runs = [
        ("bot", qamode._run_bot_hermetic, "bot_config",
         BotConfig(driver="mock", command=("x",)), "hermetic-bot"),
        ("web", qamode._run_web_deterministic, "web_config",
         WebConfig(driver="playwright", base_url="http://127.0.0.1:8080"), "playwright-web"),
        ("ext", qamode._run_ext_deterministic, "ext_config",
         ExtConfig(driver="vscode"), "vscode-ext"),
    ]
    all_labels = {"hermetic-bot", "playwright-web", "vscode-ext"}
    try:
        for kind, handler, cfg_kw, cfg, label in runs:
            with _scratch() as out:
                args = _FullArgs(kind=kind)
                args.report = str(out / "r.md")
                code = handler(_Ctx(args), out, [], exit_blocked=8, **{cfg_kw: cfg})
                text = Path(args.report).read_text(encoding="utf-8")
            assert called[kind], f"{kind} happy-path drive not reached (fell through to BLOCKED?)"
            assert code == 0, (kind, code)
            assert "ran it." in text and "VERDICT: PASS" in text, kind  # the PASS path, not BLOCKED
            assert f"backend: {label}" in text, (kind, text)
            for other in all_labels - {label}:
                assert f"backend: {other}" not in text, (kind, other)
    finally:
        for (mod, name), real in saved.items():
            setattr(mod, name, real)


# --- the SELECTOR routes a tier:live block to the live path (not None → not the executor) ------
class _Args:
    """A minimal stand-in for the parsed argparse namespace the routing reads (kind + config)."""

    def __init__(self, kind="auto", config=None):
        self.kind = kind
        self.config = config


class _Ctx:
    """A minimal ModeContext stand-in: the fast-path routing only reads ctx.args."""

    def __init__(self, args):
        self.args = args


def _sut_with_qa_yaml(out: Path, body: str) -> Path:
    """Write a ``docs/tests/qa.yaml`` (the default config path) under ``out`` and return ``out``."""
    cfg = out / "docs" / "tests" / "qa.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(body, encoding="utf-8")
    return out


def test_selector_routes_live_bot_block_not_to_executor():
    """A `driver: mtproto` bot block resolves to the live BotConfig (is_live) — proving the
    selector does NOT return None (which would fall through to the un-caged executor). This is the
    end-to-end half of the routing the direct _run_bot_live tests don't cover."""
    from reviewlib.modes.qa import _resolve_hermetic_bot

    body = "sut:\n  kind: bot\n  bot:\n    driver: mtproto\n    command: [python3, bot.py]\n"
    with _scratch() as out:
        sut = _sut_with_qa_yaml(out, body)
        cfg = _resolve_hermetic_bot(_Ctx(_Args(kind="bot")), sut)
    assert cfg is not None, "a tier:live bot block must route to the bot path, not fall through"
    assert cfg.is_live


def test_selector_routes_live_web_block_not_to_executor():
    from reviewlib.modes.qa import _resolve_deterministic_web

    body = "sut:\n  kind: web\n  web:\n    driver: agent-browser\n"
    with _scratch() as out:
        sut = _sut_with_qa_yaml(out, body)
        cfg = _resolve_deterministic_web(_Ctx(_Args(kind="web")), sut)
    assert cfg is not None, "a tier:live web block must route to the web path, not fall through"
    assert cfg.is_live


def test_selector_routes_live_ext_block_not_to_executor():
    from reviewlib.modes.qa import _resolve_deterministic_ext

    body = "sut:\n  kind: ext\n  ext:\n    driver: vscode-visual\n"
    with _scratch() as out:
        sut = _sut_with_qa_yaml(out, body)
        cfg = _resolve_deterministic_ext(_Ctx(_Args(kind="ext")), sut)
    assert cfg is not None, "a tier:live ext block must route to the ext path, not fall through"
    assert cfg.is_live


# --- the HANDLER takes the `if config.is_live` branch (the literal dispatch line) ------------
class _FullArgs:
    """A fuller args stub for the HANDLER entry points (``_run_bot_hermetic`` etc.), which read
    report/max_cases/strict/in_place beyond what the lighter selector stub (``_Args``) carries.
    ``report`` is set per-test so ``_report_path`` returns it directly (no log-dir dependency)."""

    def __init__(self, kind="auto"):
        self.kind = kind
        self.config = None
        self.report: str | None = None
        self.max_cases: int | None = None
        self.strict = False
        self.in_place = False


def test_handler_routes_live_bot_to_blocked_not_executor():
    """The ``_run_bot_hermetic`` HANDLER, given an is_live bot block and no creds, takes the
    ``if bot_config.is_live`` branch → a controlled BLOCKED (exit_blocked), NEVER the isolation /
    un-caged executor path. Covers the literal dispatch LINE that the selector + ``_run_bot_live``
    tests leave uncovered — deleting it would otherwise pass green (the exact fall-through the
    change exists to prevent)."""
    from reviewlib.modes.qa import _run_bot_hermetic

    cfg = BotConfig(driver=lt.BOT_LIVE_DRIVER, command=("python3", "bot.py"))
    with _scratch() as out, _env(**_ALL_LIVE_VARS):
        args = _FullArgs(kind="bot")
        args.report = str(out / "r.md")
        code = _run_bot_hermetic(_Ctx(args), out, [], bot_config=cfg, exit_blocked=8)
        text = Path(args.report).read_text(encoding="utf-8")
    assert code == 8, code
    assert "BLOCKED" in text and "REVIEW_QA_BOT_LIVE" in text


def test_handler_routes_live_web_to_blocked_not_executor():
    """The ``_run_web_deterministic`` HANDLER takes the ``if web_config.is_live`` branch → BLOCKED,
    never the bring-up/browser path."""
    from reviewlib.modes.qa import _run_web_deterministic

    cfg = WebConfig(driver=lt.WEB_LIVE_DRIVER)
    with _scratch() as out, _env(**_ALL_LIVE_VARS):
        args = _FullArgs(kind="web")
        args.report = str(out / "r.md")
        code = _run_web_deterministic(_Ctx(args), out, [], web_config=cfg, exit_blocked=8)
        text = Path(args.report).read_text(encoding="utf-8")
    assert code == 8, code
    assert "BLOCKED" in text and "REVIEW_QA_WEB_LIVE" in text


def test_handler_routes_live_ext_to_blocked_not_executor():
    """The ``_run_ext_deterministic`` HANDLER takes the ``if ext_config.is_live`` branch → BLOCKED,
    never the VS Code launch path."""
    from reviewlib.modes.qa import _run_ext_deterministic

    cfg = ExtConfig(driver=lt.EXT_LIVE_DRIVER)
    with _scratch() as out, _env(**_ALL_LIVE_VARS):
        args = _FullArgs(kind="ext")
        args.report = str(out / "r.md")
        code = _run_ext_deterministic(_Ctx(args), out, [], ext_config=cfg, exit_blocked=8)
        text = Path(args.report).read_text(encoding="utf-8")
    assert code == 8, code
    assert "BLOCKED" in text and "REVIEW_QA_EXT_LIVE" in text


def test_handler_live_short_circuits_before_suite_load():
    """Finding #4 regression: each tier:live HANDLER must take its ``is_live`` short-circuit BEFORE
    loading the Tier-1 suite. The live path never reads the suite, so a suite-stage failure must
    not be mis-attributed to (or block) a live run that was going to SKIP-LOUD on creds. We patch
    ``load_suites_text`` to EXPLODE and pass a bogus suite path: on the correct order the loader is
    never reached and the handler still BLOCKs cleanly; on the old order (suite-load first) the
    handler would blow up on the loader before ever seeing ``is_live``.

    The handlers import ``load_suites_text`` IN-FUNCTION (``from ..qa.suites import …`` inside each
    def), so they resolve it off ``reviewlib.qa.suites`` at call time — patching that source binding
    is what makes ``_boom`` fire. We ALSO patch any module-level alias on ``reviewlib.modes.qa``
    defensively, so this guard survives a future hoist of the import to module scope (whichever
    binding the handler reads, the explode is wired). Verified to FAIL on the pre-fix order."""
    import reviewlib.modes.qa as qamode
    import reviewlib.qa.suites as suites_mod
    from reviewlib.modes.qa import (
        _run_bot_hermetic,
        _run_ext_deterministic,
        _run_web_deterministic,
    )

    cases = [
        (_run_bot_hermetic, "bot_config",
         BotConfig(driver=lt.BOT_LIVE_DRIVER, command=("python3", "b.py")), "REVIEW_QA_BOT_LIVE"),
        (_run_web_deterministic, "web_config",
         WebConfig(driver=lt.WEB_LIVE_DRIVER), "REVIEW_QA_WEB_LIVE"),
        (_run_ext_deterministic, "ext_config",
         ExtConfig(driver=lt.EXT_LIVE_DRIVER), "REVIEW_QA_EXT_LIVE"),
    ]

    def _boom(*_a, **_k):
        raise AssertionError("suite-load ran before the is_live short-circuit (finding #4)")

    # Patch every binding the handler could resolve: the source module (in-function import, current)
    # plus a module-level alias on reviewlib.modes.qa if one ever exists (hoisted import, future).
    targets = [suites_mod] + ([qamode] if hasattr(qamode, "load_suites_text") else [])
    saved = {mod: mod.load_suites_text for mod in targets}
    for mod in targets:
        mod.load_suites_text = _boom
    try:
        for handler, cfg_kw, cfg, flag in cases:
            with _scratch() as out, _env(**_ALL_LIVE_VARS):
                args = _FullArgs(kind="auto")
                args.report = str(out / "r.md")
                code = handler(_Ctx(args), out, [Path("does-not-exist.md")],
                               exit_blocked=8, **{cfg_kw: cfg})
                text = Path(args.report).read_text(encoding="utf-8")
            assert code == 8, (handler.__name__, code)
            assert "BLOCKED" in text and flag in text, handler.__name__
    finally:
        for mod, real in saved.items():
            mod.load_suites_text = real


def test_dispatch_creds_present_blocks_on_82_not_fake_pass():
    """When the gate is SATISFIED (flag + all creds + a safe chat, telethon stubbed present), the
    dispatch does NOT fake a pass — it reaches ``live_driver_for(...).connect()`` which raises the
    not-yet-implemented ``#82`` message, and that surfaces as the BLOCKED reason. Exercises the
    ``_live_blocked_reason`` gate-ok branch (every other dispatch test runs creds-absent)."""
    from reviewlib.modes.qa import _run_bot_live

    env = {**_ALL_LIVE_VARS, "REVIEW_QA_BOT_LIVE": "1", "TG_TEST_API_ID": "1",
           "TG_TEST_API_HASH": "h", "TG_TEST_SESSION": "s",
           "TG_TEST_CHAT_ID": "-100", "TG_CHAT_ID": "-200"}
    with _scratch() as out, _env(**env), _stub_telethon():
        report = out / "r.md"
        code = _run_bot_live(report, out, strict=False, exit_blocked=8, in_place=False)
        text = report.read_text(encoding="utf-8")
    assert code == 8, code
    assert "BLOCKED" in text
    assert "#82" in text, "a creds-present live run must BLOCK on the #82 not-yet-implemented path"


def test_dispatch_web_creds_present_blocks_on_82_not_fake_pass():
    """Web analogue of the bot #82 test (closes the web half of the creds-present gap): flag + a
    base URL + a browser runtime stubbed present → the web gate is SATISFIED, so the dispatch
    reaches ``live_driver_for("web").connect()`` which raises the not-yet-implemented ``#82``
    message — a BLOCKED reason, NOT a fake pass."""
    from reviewlib.modes.qa import _run_web_live

    cfg = WebConfig(driver=lt.WEB_LIVE_DRIVER)
    env = {**_ALL_LIVE_VARS, "REVIEW_QA_WEB_LIVE": "1",
           "REVIEW_QA_WEB_BASE_URL": "https://stage.example.test"}
    with _scratch() as out, _env(**env), _stub_playwright():
        report = out / "r.md"
        code = _run_web_live(report, out, cfg, strict=False, exit_blocked=8, in_place=False)
        text = report.read_text(encoding="utf-8")
    assert code == 8, code
    assert "BLOCKED" in text
    assert "#82" in text, "a creds-present web live run must BLOCK on the #82 not-yet-implemented path"


def test_dispatch_ext_creds_present_blocks_on_82_not_fake_pass():
    """Ext analogue (closes the ext half of the gap): flag + the VS Code gate stubbed satisfied +
    a baseline dir + ``magick`` stubbed present → the ext gate is SATISFIED, so the dispatch
    reaches ``live_driver_for("ext").connect()`` which raises the ``#82`` message — BLOCKED, not a
    fake pass."""
    import tempfile

    from reviewlib.modes.qa import _run_ext_live

    cfg = ExtConfig(driver=lt.EXT_LIVE_DRIVER)
    with tempfile.TemporaryDirectory() as base:
        env = {**_ALL_LIVE_VARS, "REVIEW_QA_EXT_LIVE": "1", "REVIEW_QA_EXT_BASELINE_DIR": base}
        with _scratch() as out, _env(**env), _stub_vscode_gate(True), _stub_magick(True):
            report = out / "r.md"
            code = _run_ext_live(report, out, cfg, strict=False, exit_blocked=8, in_place=False)
            text = report.read_text(encoding="utf-8")
    assert code == 8, code
    assert "BLOCKED" in text
    assert "#82" in text, "a creds-present ext live run must BLOCK on the #82 not-yet-implemented path"


# --- creds doc / gate-message consistency ---------------------------------------------------
def test_creds_doc_names_every_gate_var():
    """The docs the CTO reads must name the SAME env vars the gates name — no drift between the
    SKIP message and the provisioning doc."""
    doc = lt.creds_doc()
    for var in (
        "REVIEW_QA_BOT_LIVE", *lt.BOT_LIVE_CREDS,
        "REVIEW_QA_WEB_LIVE", *lt.WEB_LIVE_CREDS,
        "REVIEW_QA_EXT_LIVE", "REVIEW_QA_VSCODE", "REVIEW_QA_EXT_BASELINE_DIR",
    ):
        assert var in doc, f"{var} missing from creds_doc()"


def test_creds_doc_consistent_with_spec():
    """ENFORCE the ``creds_doc()`` 'single source' claim: every per-SUT cred/flag var the gate
    code names must ALSO appear in the spec §7.4 the CTO reads — so the two can't silently drift
    (the docstring claims they're consistent; this is what makes that true rather than aspirational)."""
    spec = (REPO_ROOT / "docs" / "specs" / "review-qa.md").read_text(encoding="utf-8")
    for var in (
        "REVIEW_QA_BOT_LIVE", *lt.BOT_LIVE_CREDS,
        "REVIEW_QA_WEB_LIVE", *lt.WEB_LIVE_CREDS,
        "REVIEW_QA_EXT_LIVE", "REVIEW_QA_VSCODE", "REVIEW_QA_EXT_BASELINE_DIR",
    ):
        assert var in spec, f"{var} named by the gates/creds_doc() but missing from spec §7.4"


def test_spec_documents_tier2_live_run():
    """The spec must carry a §7.4 Tier-2 live-run section listing the creds — so the doc is the
    durable source, not just a chat message (regression guard against the docs being dropped)."""
    spec = (REPO_ROOT / "docs" / "specs" / "review-qa.md").read_text(encoding="utf-8")
    assert "7.4" in spec
    assert "TG_TEST_API_ID" in spec
    assert "REVIEW_QA_WEB_BASE_URL" in spec
    assert "REVIEW_QA_EXT_BASELINE_DIR" in spec


# --- stubs: make a heavy dep "present" so a gate test can reach the next check ----------------
@contextmanager
def _stub_telethon():
    """Insert a fake ``telethon`` module so ``_telethon_importable`` returns True without the real
    dep (not installed in CI). Removed on exit so it never leaks into another test."""
    import types

    had = "telethon" in sys.modules
    prev = sys.modules.get("telethon")
    sys.modules["telethon"] = types.ModuleType("telethon")
    try:
        yield
    finally:
        if had:
            sys.modules["telethon"] = prev
        else:
            sys.modules.pop("telethon", None)


@contextmanager
def _stub_playwright():
    """Insert a fake ``playwright`` module so the web gate's runtime check passes without the real
    browser dep. Removed on exit."""
    import types

    had = "playwright" in sys.modules
    prev = sys.modules.get("playwright")
    sys.modules["playwright"] = types.ModuleType("playwright")
    try:
        yield
    finally:
        if had:
            sys.modules["playwright"] = prev
        else:
            sys.modules.pop("playwright", None)


@contextmanager
def _force_import_error(name: str):
    """Force ``import <name>`` to raise ImportError inside the body (regardless of whether the dep
    is installed on this host) — so the dep-ABSENT SKIP branch is exercised deterministically.
    Implemented via a meta-path finder that vetoes the module + a sys.modules block."""
    import builtins

    had = name in sys.modules
    prev = sys.modules.get(name)
    sys.modules.pop(name, None)
    real_import = builtins.__import__

    def _blocked(modname, *args, **kwargs):
        if modname == name or modname.startswith(name + "."):
            raise ImportError(f"forced-absent: {name}")
        return real_import(modname, *args, **kwargs)

    builtins.__import__ = _blocked
    try:
        yield
    finally:
        builtins.__import__ = real_import
        if had:
            sys.modules[name] = prev
        else:
            sys.modules.pop(name, None)


@contextmanager
def _stub_vscode_gate(ok: bool):
    """Stub the ext gate's underlying VS Code gate to ``(ok, reason)`` so the ext-specific branches
    (baseline dir, magick) can be reached without a real VS Code / node runtime."""
    real = lt._vscode_gate
    lt._vscode_gate = lambda: (ok, "" if ok else "stubbed VS Code gate off")
    try:
        yield
    finally:
        lt._vscode_gate = real


@contextmanager
def _deny_write_access():
    """Make ``os.access(path, W_OK)`` report False (other modes pass through) for the body, so the
    gate's writability branches fire deterministically regardless of the test user's uid — a real
    ``chmod`` can't revoke write from root, so this is the uid-independent way to exercise the
    unwritable-dir/parent BLOCKED paths. ``lt.os`` is the shared ``os`` module, so this swaps
    ``os.access`` PROCESS-WIDE for the body (fine for this short, single-threaded test, which
    restores the original immediately on exit)."""
    real = lt.os.access
    lt.os.access = lambda path, mode, *a, **k: False if mode == os.W_OK else real(path, mode, *a, **k)
    try:
        yield
    finally:
        lt.os.access = real


@contextmanager
def _stub_magick(present: bool):
    """Stub the perceptual-diff (`magick`) probe so the ext gate's tool-present/absent branches are
    exercised without ImageMagick installed."""
    real = lt._perceptual_diff_available
    lt._perceptual_diff_available = lambda: present
    try:
        yield
    finally:
        lt._perceptual_diff_available = real


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
