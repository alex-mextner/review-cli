#!/usr/bin/env python3
"""review qa — bot Tier-1 HERMETIC harness (fake Telegram + inject/capture driver).

These pin the bot-kind contract (docs/specs/review-qa.md §7.3, Tier 1):

  * the FAKE TELEGRAM server answers ``getUpdates`` with injected updates, captures outbound
    ``sendMessage`` calls, and stubs the handshake methods — deterministic, loopback-only;
  * the SUITE PARSER turns a prose ``## Case:`` block with a ``Send:``/``Expect:`` grammar into
    deterministic cases (and BLOCKS a prose-only case it cannot inject);
  * the CLASSIFIER matches a captured reply against ``Expect:`` / ``Expect-no:`` /
    ``Expect-silent`` and emits the ``## QA RESULTS`` contract the executor's parser reads;
  * the POSITIVE CAPABILITY PROBE turns an unwired sender (never reaches the fake) into a LOUD
    BLOCKED with the TG_API_BASE pointer, not a false pass on zero sends;
  * the SAFETY tripwire fails closed on a real TG_CHAT_ID in the environment;
  * **the 2-fixture DoD**: the good bot (replies "welcome" to /start) verdicts PASS; the buggy
    bot (wrong /start reply) verdicts FAIL with a finding — both driven through the REAL
    hermetic harness (fake server + a real subprocess bot + inject/capture + parse).

All deterministic: a stdlib HTTP fake + a stdlib-only subprocess bot fixture, no network, no
token, no model. The per-case waits are shrunk via the REVIEW_QA_BOT_*_TIMEOUT_S env so the
suite runs in seconds. Runnable standalone (``python3 tests/test_qa_bot.py``) or under pytest.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Shrink the harness's per-case / probe waits BEFORE importing the modules (the timeouts are
# read at import time). A fast local bot replies in milliseconds; the generous production
# defaults would make this suite take minutes.
os.environ.setdefault("REVIEW_QA_BOT_RESPONSE_TIMEOUT_S", "8")
os.environ.setdefault("REVIEW_QA_BOT_SILENT_TIMEOUT_S", "1.5")
os.environ.setdefault("REVIEW_QA_BOT_PROBE_TIMEOUT_S", "10")

from reviewlib.qa import bot_driver as bd  # noqa: E402
from reviewlib.qa import bot_harness as bh  # noqa: E402
from reviewlib.qa.config import BotConfig  # noqa: E402

_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "qa"


# --- the fake Telegram server ---------------------------------------------------------
def test_fake_get_updates_returns_injected_then_acks_offset():
    """getUpdates returns queued injected updates; a poller acking via offset = last+1 must not
    get them re-delivered (re-delivery would make a bot double-handle a case)."""
    fake = bh.FakeTelegram()
    fake.start()
    try:
        fake.inject(bh.make_text_update(5, "/start"))
        first = _post(fake, "getUpdates", {"offset": 0, "timeout": 0})
        assert [u["update_id"] for u in first["result"]] == [5], first
        # ack update 5 (offset 6) → it must not come back.
        again = _post(fake, "getUpdates", {"offset": 6, "timeout": 0})
        assert again["result"] == [], again
    finally:
        fake.stop()


def test_fake_captures_outbound_send_message():
    """A sendMessage POST is captured (method + payload) and answered with a plausible ok
    Message result, so a bot that reads the response doesn't crash."""
    fake = bh.FakeTelegram()
    fake.start()
    try:
        resp = _post(
            fake, "sendMessage", {"chat_id": bh.TEST_CHAT_ID, "text": "hi there"}
        )
        assert resp["ok"] is True
        assert resp["result"]["text"] == "hi there"
        assert len(fake.outbound) == 1
        assert fake.outbound[0].method == "sendMessage"
        assert fake.outbound[0].text == "hi there"
    finally:
        fake.stop()


def test_fake_stubs_get_me_handshake():
    """getMe returns a bot user so a bot's startup handshake succeeds against the fake."""
    fake = bh.FakeTelegram()
    fake.start()
    try:
        resp = _post(fake, "getMe", {})
        assert resp["ok"] is True and resp["result"]["is_bot"] is True
    finally:
        fake.stop()


def test_fake_decodes_form_urlencoded_body():
    """python-telegram-bot / a curl -d send arrives form-urlencoded, not JSON — the fake must
    still decode chat_id/text so the capture works regardless of the client's content-type."""
    import urllib.request

    fake = bh.FakeTelegram()
    fake.start()
    try:
        url = f"{fake.base_url()}/bottoken/sendMessage"
        body = b"chat_id=-100&text=form+encoded+reply"
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        urllib.request.urlopen(req, timeout=5).read()  # noqa: S310 — loopback fake
        assert fake.outbound[0].text == "form encoded reply", fake.outbound
    finally:
        fake.stop()


def test_fake_binds_loopback_only():
    """The fake must bind 127.0.0.1 — a hermetic run never exposes the capture surface to the
    network."""
    fake = bh.FakeTelegram()
    fake.start()
    try:
        assert fake.base_url().startswith("http://127.0.0.1:")
    finally:
        fake.stop()


# --- the suite parser -----------------------------------------------------------------
def test_parse_send_expect_grammar():
    suite = (
        "# Suite: s\n"
        "## Case: greet\nSend: /start\nExpect: welcome\nExpect-no: error\n"
        "## Case: tap\nSend-callback: confirm:1\nExpect: done\n"
        "## Case: quiet\nSend: chatter\nExpect-silent\n"
    )
    cases = bd.parse_bot_cases(suite)
    assert [c.title for c in cases] == ["greet", "tap", "quiet"]
    assert (
        cases[0].send == "/start"
        and cases[0].expect == ("welcome",)
        and cases[0].expect_no == ("error",)
    )
    assert cases[1].send_callback == "confirm:1" and cases[1].runnable
    assert cases[2].expect_silent is True


def test_prose_only_case_is_not_runnable():
    """A ``## Case:`` with no Send/Send-callback is a prose-only case the hermetic driver can't
    inject — it must parse as non-runnable so the driver BLOCKS it (not a silent skip)."""
    cases = bd.parse_bot_cases(
        "## Case: manual thing\nSteps:\n- do it by hand\nExpected:\n- ok\n"
    )
    assert cases[0].runnable is False


# --- the classifier -------------------------------------------------------------------
def _call(text: str) -> bh.OutboundCall:
    return bh.OutboundCall(method="sendMessage", payload={"text": text}, at=0.0)


def test_classify_pass_on_matching_reply():
    case = bd.BotCase(
        title="t", send="/start", expect=("welcome",), expect_no=("error",)
    )
    r = bd._classify(case, [_call("Welcome aboard!")])
    assert r.status == bd.PASS, r


def test_classify_fail_on_missing_substring():
    case = bd.BotCase(title="t", send="/start", expect=("welcome",))
    r = bd._classify(case, [_call("hello")])
    assert r.status == bd.FAIL and "welcome" in r.detail and r.severity == "P1"


def test_classify_fail_on_forbidden_substring():
    case = bd.BotCase(title="t", send="/x", expect=("ok",), expect_no=("error",))
    r = bd._classify(case, [_call("ok but error happened")])
    assert r.status == bd.FAIL and "forbidden" in r.detail


def test_classify_fail_on_silence_when_reply_expected():
    case = bd.BotCase(title="t", send="/start", expect=("welcome",))
    r = bd._classify(case, [])
    assert r.status == bd.FAIL and "NO reply" in r.detail


def test_classify_silent_case():
    silent = bd.BotCase(title="t", send="chatter", expect_silent=True)
    assert bd._classify(silent, []).status == bd.PASS
    assert bd._classify(silent, [_call("unexpected")]).status == bd.FAIL


def test_classify_multi_message_reply_concatenated():
    """A bot that splits its answer across two sends still satisfies the expectations
    collectively (the captured texts are concatenated for the match)."""
    case = bd.BotCase(title="t", send="/help", expect=("commands", "echo"))
    r = bd._classify(case, [_call("Available commands:"), _call("/echo <text>")])
    assert r.status == bd.PASS, r


def test_expectations_met_predicate():
    """The wait predicate is True only once ALL Expects are present across the captured calls —
    so the driver keeps the window open for a delayed later message (no early-return false-fail)."""
    case = bd.BotCase(title="t", send="/help", expect=("commands", "echo"))
    assert bd._expectations_met(case, []) is False
    assert (
        bd._expectations_met(case, [_call("Available commands:")]) is False
    )  # 'echo' not yet
    assert (
        bd._expectations_met(case, [_call("Available commands:"), _call("/echo")])
        is True
    )
    # an Expect-no substring keeps it unsatisfied even if Expects are present
    case2 = bd.BotCase(title="t", send="/x", expect=("ok",), expect_no=("error",))
    assert bd._expectations_met(case2, [_call("ok but error")]) is False


def test_delayed_multi_message_reply_passes():
    """A bot whose expected substring arrives in a LATER message (after the first) must PASS, not
    false-fail on a 50ms grace — the case waits until expectations are met (review finding). Run
    against a real bot that sends an ack first, then the answer after a short delay."""
    import shutil
    import tempfile

    from reviewlib.qa.executor import parse_qa_results

    d = Path(tempfile.mkdtemp(prefix="qa-delayed-"))
    try:
        (d / "bot.py").write_text(
            "import json, os, time, urllib.request\n"
            "API = os.environ['TG_API_BASE'].rstrip('/'); TOK = os.environ.get('BOT_TOKEN','t')\n"
            "def api(m, p):\n"
            "    r = urllib.request.Request(f'{API}/bot{TOK}/{m}', data=json.dumps(p).encode(),\n"
            "                               headers={'Content-Type':'application/json'})\n"
            "    return json.loads(urllib.request.urlopen(r, timeout=10).read())\n"
            "off = 0; deadline = time.monotonic() + 30\n"
            "while time.monotonic() < deadline:\n"
            "    try: resp = api('getUpdates', {'offset': off, 'timeout': 1})\n"
            "    except Exception: time.sleep(0.1); continue\n"
            "    for u in resp.get('result', []):\n"
            "        off = max(off, int(u['update_id'])+1)\n"
            "        msg = u.get('message', {})\n"
            "        if (msg.get('text') or '').startswith('/start'):\n"
            "            cid = msg['chat']['id']\n"
            "            api('sendMessage', {'chat_id': cid, 'text': 'one moment...'})\n"
            "            time.sleep(0.6)  # the EXPECTED text arrives only in this later message\n"
            "            api('sendMessage', {'chat_id': cid, 'text': 'here is your welcome'})\n"
            "    time.sleep(0.05)\n"
        )
        cfg = BotConfig(driver="mock", command=("python3", "bot.py"))
        transcript = bd.run_hermetic_bot_test(
            suite_text="# S\n## Case: start\nSend: /start\nExpect: welcome\n",
            bot_config=cfg,
            cwd=d,
            sut_path=Path("/sut/delayed"),
            exit_boot_failed=8,
        )
        verdict, _f, _s, _c = parse_qa_results(transcript)
        assert verdict == "PASS", transcript  # the later message's 'welcome' is matched
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --- the QA RESULTS rendering ---------------------------------------------------------
def test_run_result_renders_parseable_contract():
    """The bot run's ## QA RESULTS block must be readable by the executor's own parser — the
    bot path and the un-caged path share one machine-parsed contract."""
    from reviewlib.qa.executor import parse_qa_results

    result = bd.BotRunResult(
        results=[
            bd.CaseResult("a", bd.PASS, "ok"),
            bd.CaseResult("b", bd.FAIL, "missing 'x'", "P1"),
        ]
    )
    text = result.to_qa_results(sut_path=Path("/sut"))
    verdict, findings, max_sev, cases = parse_qa_results(text)
    assert verdict == "FAIL" and findings == 1 and max_sev == "P1"
    assert cases == {"run": 2, "passed": 1, "failed": 1, "blocked": 0}


def test_run_result_blocked_when_no_runnable_cases():
    assert bd.BotRunResult(results=[]).verdict == bd.BLOCKED


def test_run_result_pass_plus_blocked_is_not_pass():
    """A mix of a PASSing case and a BLOCKED case (an authored case the driver couldn't
    exercise) must roll up to BLOCKED, NOT a silent PASS — an unexercised authored case is not a
    green run (review finding)."""
    result = bd.BotRunResult(
        results=[
            bd.CaseResult("ran", bd.PASS, "ok"),
            bd.CaseResult("prose-only", bd.BLOCKED, "no Send: directive"),
        ]
    )
    assert result.verdict == bd.BLOCKED, result
    # and it surfaces in the report's verdict line, so verdict_to_exit_code treats it as infra.
    from reviewlib.qa.executor import parse_qa_results, verdict_to_exit_code

    verdict, findings, _s, cases = parse_qa_results(
        result.to_qa_results(sut_path=Path("/s"))
    )
    assert verdict == "BLOCKED" and cases == {
        "run": 2,
        "passed": 1,
        "failed": 0,
        "blocked": 1,
    }
    assert (
        verdict_to_exit_code(verdict, findings=findings, strict=False, exit_blocked=8)
        == 8
    )


# --- safety ---------------------------------------------------------------------------
def test_boot_refuses_real_chat_id():
    """A hermetic run with a real-looking TG_CHAT_ID in the env must fail closed — a bot that
    ignores TG_API_BASE could otherwise reach the user's real chat."""
    try:
        bh.boot_bot(
            command=["true"],
            cwd=Path("/tmp"),
            api_base="http://127.0.0.1:1",
            extra_env={"TG_CHAT_ID": "123456789"},
            exit_boot_failed=8,
        )
        raise AssertionError("expected BotHarnessError for a real chat id")
    except bh.BotHarnessError as exc:
        assert "hermetic" in str(exc) and exc.exit_code == 8


def test_boot_allows_synthetic_test_chat_id():
    """A synthetic -100999… test chat id is fine (it can't be a real human)."""
    bot = bh.boot_bot(
        command=["true"],
        cwd=Path("/tmp"),
        api_base="http://127.0.0.1:1",
        extra_env={"TG_CHAT_ID": "-1009990001"},
        exit_boot_failed=8,
    )
    bot.reap()


def test_boot_bot_registers_with_the_signal_reaper():
    """review-cli#162 follow-up (codex review): `boot_bot` must register the SUT bot
    process with `process._LIVE_CHILDREN` — the same registry `install_signal_reaper`'s
    SIGTERM/SIGINT handler and the internal backstop's `kill_live_children()` sweep —
    so an external signal reaps this SUT process too, not only `_run_streamed`'s own
    backend children. `sleep 5` (not `true`) so the pid is still live long enough to
    assert on before `reap()` tears it down."""
    from reviewlib import process as proc_mod

    bot = bh.boot_bot(
        command=["sleep", "5"],
        cwd=Path("/tmp"),
        api_base="http://127.0.0.1:1",
        extra_env={"TG_CHAT_ID": "-1009990001"},
        exit_boot_failed=8,
    )
    try:
        with proc_mod._LIVE_CHILDREN_LOCK:
            live_pids = {p.pid for p, _pgid in proc_mod._LIVE_CHILDREN}
        assert bot.proc.pid in live_pids
    finally:
        bot.reap()
    with proc_mod._LIVE_CHILDREN_LOCK:
        live_pids_after = {p.pid for p, _pgid in proc_mod._LIVE_CHILDREN}
    assert bot.proc.pid not in live_pids_after


def test_reap_kills_forked_child_after_leader_exits():
    """A wrapper command that forks a long-lived child then EXITS leaves the child in the
    bot's process group; reap() must still SIGKILL the group even though the leader is already
    dead — otherwise the forked poller leaks (review finding)."""
    import shutil
    import subprocess
    import tempfile

    d = Path(tempfile.mkdtemp(prefix="qa-fork-"))
    child_pid: int | None = None
    try:
        # A wrapper: spawn a child that sleeps 60s, write its pid, then the wrapper EXITS.
        (d / "wrap.py").write_text(
            "import subprocess, sys, pathlib\n"
            "child = subprocess.Popen(['sleep', '60'])\n"
            "pathlib.Path(sys.argv[1]).write_text(str(child.pid))\n"
            "sys.exit(0)\n"
        )
        pidfile = d / "child.pid"
        bot = bh.boot_bot(
            command=["python3", "wrap.py", str(pidfile)],
            cwd=d,
            api_base="http://127.0.0.1:1",
            exit_boot_failed=8,
        )
        # wait for the wrapper to exit and record the child pid
        bot.proc.wait(timeout=10)
        for _ in range(50):
            if pidfile.exists():
                break
            time.sleep(0.1)
        child_pid = int(pidfile.read_text().strip())
        assert _pid_alive(child_pid), "the forked child should be alive before reap"
        bot.reap()
        # the child must be gone after reap (give the SIGKILL a beat to land)
        for _ in range(50):
            if not _pid_alive(child_pid):
                break
            time.sleep(0.1)
        assert not _pid_alive(child_pid), "the forked child leaked past reap()"
    finally:
        # Belt-and-suspenders cleanup in the FINALLY (not after the assertion) — if reap()
        # regresses the assertion raises first, so the leaked `sleep 60` MUST still be killed
        # here or it survives in the test environment (review finding).
        if child_pid is not None and _pid_alive(child_pid):
            subprocess.run(["kill", "-9", str(child_pid)], capture_output=True)
        shutil.rmtree(d, ignore_errors=True)


def _pid_alive(pid: int) -> bool:
    import os as _os

    try:
        _os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --- the 2-fixture DoD (the headline) -------------------------------------------------
def test_dod_good_bot_verdicts_pass():
    """The good bot (replies 'welcome' to /start) driven through the REAL hermetic harness
    verdicts PASS with no findings — the must-pass half of the DoD."""
    transcript = _run_fixture("bot-good")
    from reviewlib.qa.executor import parse_qa_results

    verdict, findings, _sev, _cases = parse_qa_results(transcript)
    assert verdict == "PASS", transcript
    assert findings == 0, transcript


def test_dod_buggy_bot_verdicts_fail_with_finding():
    """The buggy bot (wrong /start reply, missing 'welcome') verdicts FAIL with a P1 finding
    that cites the actual captured reply as proof — the must-fail half of the DoD."""
    transcript = _run_fixture("bot-buggy")
    from reviewlib.qa.executor import parse_qa_results, verdict_to_exit_code

    verdict, findings, max_sev, _cases = parse_qa_results(transcript)
    assert verdict == "FAIL", transcript
    assert findings >= 1 and max_sev == "P1", transcript
    assert "welcome" in transcript  # the missing-substring proof
    # report-only by default (a found bug exits 0); --strict flips it to 10.
    assert (
        verdict_to_exit_code(verdict, findings=findings, strict=False, exit_blocked=8)
        == 0
    )
    assert (
        verdict_to_exit_code(verdict, findings=findings, strict=True, exit_blocked=8)
        == 10
    )


def test_dod_teardown_leaves_no_bot_process():
    """After a run the booted bot subprocess must be reaped — the hermetic harness leaks
    nothing (the guaranteed-teardown guarantee, on the normal path). The match is scoped to the
    FIXTURE path (not a bare 'bot.py' that would match any unrelated bot.py on a shared CI)."""
    before = _fixture_bot_pids()
    _run_fixture("bot-good")
    after = _fixture_bot_pids()
    leaked = after - before
    assert not leaked, f"a fixture bot subprocess survived the run: {leaked}"


def test_unwired_sender_blocks_with_tg_api_base_pointer():
    """THE load-bearing footgun (spec §7.3): a bot that never calls the fake (ignores
    TG_API_BASE / hardcodes api.telegram.org) must yield a BLOCKED verdict with the TG_API_BASE
    pointer — NOT a false PASS on zero captured sends. End-to-end through run_hermetic_bot_test."""
    import shutil
    import tempfile

    from reviewlib.qa.executor import parse_qa_results

    d = Path(tempfile.mkdtemp(prefix="qa-unwired-"))
    try:
        # An 'unwired' bot: it just sleeps, never polling the fake (the worst case the probe
        # exists to catch). Keep its runtime short so reap is instant.
        (d / "bot.py").write_text(
            "import time\nfor _ in range(200): time.sleep(0.05)\n"
        )
        cfg = BotConfig(driver="mock", command=("python3", "bot.py"))
        transcript = bd.run_hermetic_bot_test(
            suite_text="# S\n## Case: start\nSend: /start\nExpect: welcome\n",
            bot_config=cfg,
            cwd=d,
            sut_path=Path("/sut/unwired"),
            exit_boot_failed=8,
        )
        verdict, _f, _s, _c = parse_qa_results(transcript)
        assert verdict == "BLOCKED", transcript
        assert "TG_API_BASE" in transcript, transcript
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_dead_bot_blocks_with_crash_output():
    """A bot that crashes on startup (exits before polling) must yield BLOCKED with its output
    tail as proof — distinct from the unwired-sender case, and never a traceback."""
    import shutil
    import tempfile

    from reviewlib.qa.executor import parse_qa_results

    d = Path(tempfile.mkdtemp(prefix="qa-deadbot-"))
    try:
        (d / "bot.py").write_text(
            "import sys\nprint('boom: missing config', file=sys.stderr)\n"
            "print('boom: missing config')\nsys.exit(3)\n"
        )
        cfg = BotConfig(driver="mock", command=("python3", "bot.py"))
        transcript = bd.run_hermetic_bot_test(
            suite_text="# S\n## Case: start\nSend: /start\nExpect: hi\n",
            bot_config=cfg,
            cwd=d,
            sut_path=Path("/sut/dead"),
            exit_boot_failed=8,
        )
        verdict, _f, _s, _c = parse_qa_results(transcript)
        assert verdict == "BLOCKED", transcript
        # the crash is reported as a boot crash (not misattributed to an unwired sender).
        assert "crashed on startup" in transcript or "exited" in transcript, transcript
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_launch_failure_blocks():
    """A bot command that cannot be launched at all (a non-existent binary) yields BLOCKED via
    BotHarnessError, mapped to the boot-failed class — not a traceback."""
    import shutil
    import tempfile

    from reviewlib.qa.executor import parse_qa_results

    d = Path(tempfile.mkdtemp(prefix="qa-nolaunch-"))
    try:
        cfg = BotConfig(driver="mock", command=("this-binary-does-not-exist-xyz",))
        transcript = bd.run_hermetic_bot_test(
            suite_text="# S\n## Case: start\nSend: /start\nExpect: hi\n",
            bot_config=cfg,
            cwd=d,
            sut_path=Path("/sut/nolaunch"),
            exit_boot_failed=8,
        )
        verdict, _f, _s, _c = parse_qa_results(transcript)
        assert verdict == "BLOCKED", transcript
        assert "could not launch" in transcript, transcript
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_chatty_bot_does_not_deadlock_on_full_pipe():
    """A bot that logs MORE than the OS pipe buffer (~64 KiB) BEFORE it starts polling must not
    deadlock on a full stdout pipe — the BotProcess drain thread keeps the pipe empty so the bot
    reaches its poll and replies (review finding). Without the drain, the bot would block on
    print() forever and the run would false-BLOCK."""
    import shutil
    import tempfile

    from reviewlib.qa.executor import parse_qa_results

    d = Path(tempfile.mkdtemp(prefix="qa-chatty-"))
    try:
        # A self-contained chatty bot: flood stdout with ~200 KiB BEFORE polling, then behave
        # like the good bot (reply 'welcome' to /start). If stdout isn't drained, the bot blocks
        # on the print loop and never polls → false BLOCKED. Written standalone (not spliced) so
        # `from __future__` stays at the top.
        (d / "bot.py").write_text(
            "import json, os, sys, time, urllib.request\n"
            "for i in range(4000): print('noise line %d ' % i + 'x'*50)\n"
            "sys.stdout.flush()\n"
            "API = os.environ['TG_API_BASE'].rstrip('/'); TOK = os.environ.get('BOT_TOKEN','t')\n"
            "def api(m, p):\n"
            "    r = urllib.request.Request(f'{API}/bot{TOK}/{m}', data=json.dumps(p).encode(),\n"
            "                               headers={'Content-Type':'application/json'})\n"
            "    return json.loads(urllib.request.urlopen(r, timeout=10).read())\n"
            "off = 0; deadline = time.monotonic() + 30\n"
            "while time.monotonic() < deadline:\n"
            "    try: resp = api('getUpdates', {'offset': off, 'timeout': 1})\n"
            "    except Exception: time.sleep(0.1); continue\n"
            "    for u in resp.get('result', []):\n"
            "        off = max(off, int(u['update_id'])+1)\n"
            "        msg = u.get('message', {})\n"
            "        if (msg.get('text') or '').startswith('/start'):\n"
            "            api('sendMessage', {'chat_id': msg['chat']['id'], 'text': 'Welcome!'})\n"
            "    time.sleep(0.05)\n"
        )
        cfg = BotConfig(driver="mock", command=("python3", "bot.py"))
        transcript = bd.run_hermetic_bot_test(
            suite_text="# S\n## Case: start\nSend: /start\nExpect: welcome\n",
            bot_config=cfg,
            cwd=d,
            sut_path=Path("/sut/chatty"),
            exit_boot_failed=8,
        )
        verdict, _f, _s, _c = parse_qa_results(transcript)
        assert verdict == "PASS", transcript  # reached the poll despite the log flood
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_long_poll_holds_empty_queue():
    """An empty getUpdates HOLDS server-side up to the cap instead of returning instantly — the
    fix that stops a real timeout=30 poller from busy-looping. A bare drain returns [] at once;
    the long-poll waits ~the cap then returns []."""
    import time as _t

    fake = bh.FakeTelegram()
    fake.start()
    try:
        t0 = _t.monotonic()
        result = fake._long_poll_updates(0, client_timeout=30.0)
        held = _t.monotonic() - t0
        assert result == []
        # held at least most of the cap (allowing scheduler slop), and never the full 30s.
        assert held >= bh._GET_UPDATES_HOLD_CAP_S * 0.5, held
        assert held < 5.0, held
        # an already-queued update returns immediately (no hold).
        fake.inject(bh.make_text_update(1, "x"))
        t1 = _t.monotonic()
        assert fake._long_poll_updates(0, client_timeout=30.0)
        assert _t.monotonic() - t1 < 0.5
    finally:
        fake.stop()


# --- the sut.bot config parse ---------------------------------------------------------
def test_bot_config_parses_from_qa_yaml():
    """A sut.bot mock block parses into a typed BotConfig with the command argv + env."""
    import shutil
    import tempfile

    from reviewlib.qa.config import load_qa_config

    sut = Path(tempfile.mkdtemp(prefix="qa-bot-cfg-"))
    try:
        cfg_dir = sut / "docs" / "tests"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "qa.yaml").write_text(
            "sut:\n  kind: bot\n  bot:\n    driver: mock\n"
            "    command:\n      - python3\n      - bot.py\n"
            "    env:\n      FEATURE_X: '1'\n    skip_probe: true\n"
        )
        config = load_qa_config(sut, None)
        assert config is not None and config.bot is not None
        assert config.bot.driver == "mock"
        assert config.bot.command == ("python3", "bot.py")
        assert config.bot.env == {"FEATURE_X": "1"}
        assert config.bot.skip_probe is True
    finally:
        shutil.rmtree(sut, ignore_errors=True)


def test_bot_config_rejects_missing_command():
    """A mock bot block with no command is a clean QaConfigError (the harness needs an argv to
    boot the bot), not a silent empty run."""
    from reviewlib.qa.config import BotConfig, QaConfigError

    try:
        BotConfig(driver="mock", command=())
        raise AssertionError("expected QaConfigError for an empty command")
    except QaConfigError as exc:
        assert "command is required" in str(exc)


def test_bot_config_skip_probe_string_false_is_false():
    """A SAFETY flag must not be misparsed: bool('false') is True in Python, so a quoted
    skip_probe: 'false' would silently DISABLE the unwired-sender probe. The strict coercion
    reads it as False; a garbage value is a clean config error, not a silent on."""
    import shutil
    import tempfile

    from reviewlib.qa.config import QaConfigError, load_qa_config

    sut = Path(tempfile.mkdtemp(prefix="qa-bot-sp-"))
    try:
        cfg_dir = sut / "docs" / "tests"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "qa.yaml").write_text(
            "sut:\n  kind: bot\n  bot:\n    command: [python3, bot.py]\n    skip_probe: 'false'\n"
        )
        config = load_qa_config(sut, None)
        assert config.bot.skip_probe is False
        # a garbage value is a clean error, not a silent True
        (cfg_dir / "qa.yaml").write_text(
            "sut:\n  kind: bot\n  bot:\n    command: [python3, bot.py]\n    skip_probe: maybe\n"
        )
        try:
            load_qa_config(sut, None)
            raise AssertionError("expected QaConfigError for a non-boolean skip_probe")
        except QaConfigError as exc:
            assert "boolean" in str(exc)
    finally:
        shutil.rmtree(sut, ignore_errors=True)


def test_bot_config_accepts_live_mtproto_driver_flags_is_live():
    """The mtproto Tier-2 LIVE driver is now ACCEPTED (the scaffolding landed — #84) and flagged
    is_live; the live RUN is still gated behind creds at dispatch. A genuinely-unknown driver is
    still rejected loud."""
    from reviewlib.qa.config import BotConfig, QaConfigError

    cfg = BotConfig(driver="mtproto", command=("python3", "bot.py"))
    assert cfg.is_live
    try:
        BotConfig(driver="garbage", command=("python3", "bot.py"))
        raise AssertionError("expected QaConfigError for an unknown driver")
    except QaConfigError as exc:
        assert "not supported" in str(exc)


# --- the handler routing (_resolve_hermetic_bot) --------------------------------------
def test_resolve_routes_bot_with_mock_config():
    """--kind bot + a sut.bot mock config routes to the hermetic path; a missing config or a
    non-bot kind does not."""
    from reviewlib.modes.qa import _resolve_hermetic_bot

    sut = _FIXTURES / "bot-good"
    # explicit --kind bot, config present → routes
    ctx = _fake_ctx(kind="bot", config=None)
    assert _resolve_hermetic_bot(ctx, sut) is not None
    # a non-bot kind never routes here even with a bot config on disk
    ctx_backend = _fake_ctx(kind="backend", config=None)
    assert _resolve_hermetic_bot(ctx_backend, sut) is None


def test_resolve_no_route_without_bot_config():
    """A bot-kind SUT with no sut.bot config falls through to the normal flow (where the prose
    runbook tells an agent to stand a mock up by hand) — _resolve returns None."""
    from reviewlib.modes.qa import _resolve_hermetic_bot

    sut = _FIXTURES / "sut-good"  # a backend fixture with no bot config
    ctx = _fake_ctx(kind="bot", config=None)
    assert _resolve_hermetic_bot(ctx, sut) is None


def test_resolve_routes_on_qa_yaml_kind_under_auto():
    """Under the DEFAULT --kind auto, a SUT whose qa.yaml declares `sut.kind: bot` (with a
    sut.bot mock config) routes to the hermetic path even with NO telegram dependency marker
    that package-detection would catch (review finding). The fixture has kind: bot in its yaml
    but is a plain python bot (no telegraf/grammy dep), so package detection alone returns
    'backend' — only honoring sut.kind activates the path."""
    from reviewlib.modes.qa import _detect_kind, _resolve_hermetic_bot

    sut = _FIXTURES / "bot-good"
    assert (
        _detect_kind(sut) == "backend"
    )  # no dep marker → package detection says backend
    ctx = _fake_ctx(kind="auto", config=None)
    assert _resolve_hermetic_bot(ctx, sut) is not None  # but sut.kind: bot routes it


def test_effective_kind_honors_qa_yaml_kind_in_fallback():
    """The EXECUTOR fallback path's kind resolution honors sut.kind from qa.yaml under --kind
    auto — so a YAML-declared bot with NO sut.bot mock config still gets the BOT runbook (not the
    backend one from package-marker detection alone). review finding: the fallback ignored
    sut.kind."""
    import shutil
    import tempfile

    from reviewlib.modes.qa import _detect_kind, _effective_kind

    sut = Path(tempfile.mkdtemp(prefix="qa-kind-"))
    try:
        cfg_dir = sut / "docs" / "tests"
        cfg_dir.mkdir(parents=True)
        # sut.kind: bot but NO sut.bot block (so it would take the executor fallback, not hermetic)
        (cfg_dir / "qa.yaml").write_text("sut:\n  kind: bot\n")
        assert _detect_kind(sut) == "backend"  # no dep marker
        ctx = _fake_ctx(kind="auto", config=None)
        assert (
            _effective_kind(ctx, sut) == "bot"
        )  # but the executor path now honors sut.kind
    finally:
        shutil.rmtree(sut, ignore_errors=True)


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _fake_ctx(*, kind: str, config: str | None):
    """A minimal stand-in for ModeContext carrying just the args _resolve_hermetic_bot reads."""
    return _Args(args=_Args(kind=kind, config=config))


# --- helpers --------------------------------------------------------------------------
def _post(fake: bh.FakeTelegram, method: str, payload: dict) -> dict:
    import json
    import urllib.request

    url = f"{fake.base_url()}/bottoken/{method}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 — loopback fake
        return json.loads(resp.read().decode())


def _run_fixture(name: str) -> str:
    """Drive a fixture bot through the real hermetic harness in its fixture dir, return the
    ## QA RESULTS transcript. No worktree isolation here (the handler adds that); this drives
    the harness core directly so the DoD is about the harness, not git plumbing."""
    from reviewlib.qa.suites import load_suites_text

    sut = _FIXTURES / name
    suite_files = sorted((sut / "docs" / "tests" / "suites").glob("*.md"))
    suite_text = load_suites_text(suite_files, max_cases=None)
    cfg = BotConfig(driver="mock", command=("python3", "bot.py"))
    return bd.run_hermetic_bot_test(
        suite_text=suite_text,
        bot_config=cfg,
        cwd=sut.resolve(),
        sut_path=sut.resolve(),
        exit_boot_failed=8,
    )


def _fixture_bot_pids() -> set[str]:
    """PIDs of any running FIXTURE bot (scoped to the fixtures dir, so an unrelated bot.py on a
    shared CI host never matches). pgrep is Unix-only; on a host without it, return empty (the
    test then only asserts no NEW pid, which is vacuously fine)."""
    import subprocess

    pattern = str(_FIXTURES / "bot-")  # matches bot-good/bot.py and bot-buggy/bot.py
    try:
        out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
    except (OSError, FileNotFoundError):
        return set()
    return set(out.stdout.split()) if out.returncode == 0 else set()


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
