#!/usr/bin/env python3
"""review qa — bot AGENT-SIDE tier (a bridge bot driven by the agent's hook client).

These pin the agent-side contract (docs/specs/review-qa.md §7.3, AGENT-SIDE tier) — the loop the
inbound inject/capture path cannot reach:

  * the SUITE PARSER turns a ``## Case:`` block's ``Ask-question:``/``Ask-permission:`` /
    ``Expect-card:`` / ``Tap:`` / ``Expect-answer:`` grammar into deterministic agent-side cases;
  * the CONFIG parser reads the new ``sut.bot`` knobs (``ask_command`` / ``seed`` / ``owner_id`` /
    ``sender_id`` / ``ready_file``) and rejects an agent-side block with no ``owner_id``;
  * the HARNESS SEAM emits a question via the hook client, filters the fake's outbound to inline
    cards, injects a tap as a synthetic ``callback_query`` from the owner id, and reads the answer
    off the hook client's stdout (a hang = the lost-answer / tap-loss bug);
  * the TEMPLATE/SEED layer substitutes the run's allocated paths into the seed/env/argv knobs;
  * **the 2-config DoD**: a faithful miniature bridge bot (``tgctl-agentside/sut.py``, an
    independent SUT-side impl, NOT a mock of the driver) verdicts PASS when it replays an answered
    re-fire, and FAILs when ``SUT_DUP_BUG=1`` makes it re-post a duplicate card (the tg-cli#98
    class) — both driven through the REAL harness (fake Telegram + a real subprocess daemon + a
    real Unix-socket hook client + emit/tap/await).

All deterministic: a stdlib HTTP fake + a stdlib-only subprocess daemon/hook-client fixture, no
network, no token, no model. The waits are shrunk via REVIEW_QA_BOT_*_S so the suite runs in
seconds. Runnable standalone (``python3 tests/test_qa_bot_agent_side.py``) or under pytest.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Shrink the agent-side waits BEFORE importing the harness (read at import time). The fixture
# daemon answers in milliseconds; the generous production defaults would make this take minutes.
os.environ.setdefault("REVIEW_QA_BOT_DAEMON_READY_TIMEOUT_S", "12")
os.environ.setdefault("REVIEW_QA_BOT_DAEMON_BOOT_GRACE_S", "1.5")
os.environ.setdefault("REVIEW_QA_BOT_CARD_TIMEOUT_S", "8")
os.environ.setdefault("REVIEW_QA_BOT_NO_CARD_TIMEOUT_S", "2")
os.environ.setdefault("REVIEW_QA_BOT_ANSWER_TIMEOUT_S", "10")

from reviewlib.qa import bot_driver as bd  # noqa: E402
from reviewlib.qa import bot_harness as bh  # noqa: E402
from reviewlib.qa.config import BotConfig, QaConfigError, SeedFile  # noqa: E402

_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "qa"
_AGENT_FIXTURE = _FIXTURES / "tgctl-agentside"


# --- the suite parser ----------------------------------------------------------------
def test_parse_agent_case_question_with_tap():
    """An Ask-question block parses into a runnable question case with the card/tap/answer."""
    suite = (
        "## Case: ship\n"
        'Ask-question: {"q": "Ship?"}\n'
        "Expect-card: 1\n"
        "Tap: Ship it\n"
        "Expect-answer: Ship it\n"
    )
    [case] = bd.parse_agent_cases(suite)
    assert case.runnable and case.kind == "question"
    assert case.payload == '{"q": "Ship?"}'
    assert case.expect_card == 1 and case.tap == "Ship it"
    assert case.expect_answer == ("Ship it",)


def test_parse_agent_case_permission_and_refire_zero_card():
    """Ask-permission sets the permission kind; a re-fire uses Expect-card: 0 with no Tap."""
    suite = (
        '## Case: perm\nAsk-permission: {"tool": "Bash"}\nExpect-card: 1\nTap: Allow\n\n'
        '## Case: refire\nAsk-question: {"q": "Ship?"}\nExpect-card: 0\nExpect-answer: Ship it\n'
    )
    perm, refire = bd.parse_agent_cases(suite)
    assert perm.kind == "permission" and perm.tap == "Allow"
    assert refire.expect_card == 0 and refire.tap is None
    assert refire.expect_answer == ("Ship it",)


def test_parse_agent_case_defaults_one_card_and_detects_suite():
    """Expect-card defaults to 1 when omitted; suite_has_agent_directives detects an Ask block."""
    suite = '## Case: q\nAsk-question: {"q": "x"}\nExpect-answer: ok\n'
    [case] = bd.parse_agent_cases(suite)
    assert case.expect_card == 1
    assert bd.suite_has_agent_directives(suite) is True
    assert (
        bd.suite_has_agent_directives("## Case: c\nSend: /start\nExpect: hi\n") is False
    )


def test_parse_agent_case_without_ask_is_not_runnable():
    """A block with no Ask-* directive is a non-runnable agent case (the driver BLOCKs it)."""
    [case] = bd.parse_agent_cases("## Case: inbound\nSend: /start\nExpect: hi\n")
    assert not case.runnable


# --- the config knobs ----------------------------------------------------------------
def test_config_agent_side_knobs_parse():
    """A sut.bot block with ask_command/seed/owner_id/sender_id/ready_file parses + is agent-side."""
    from reviewlib.qa.config import _bot_from

    cfg = _bot_from(
        {
            "driver": "mock",
            "command": ["bun", "tg-ctl", "run"],
            "ask_command": ["bun", "tg-ctl", "ask", "--agent", "claude"],
            "owner_id": 424242,
            "sender_id": 999,
            "ready_file": "{config_dir}/x.sock",
            "seed": [{"path": "config/reg.json", "content": '[{"cwd": "{cwd}"}]'}],
            "env": {"K": "v"},
        },
        Path("qa.yaml"),
    )
    assert (
        cfg.is_agent_side and cfg.owner_id == 424242 and cfg.effective_sender_id == 999
    )
    assert cfg.ready_file == "{config_dir}/x.sock"
    assert cfg.seed == (SeedFile(path="config/reg.json", content='[{"cwd": "{cwd}"}]'),)


def test_config_sender_defaults_to_owner():
    """sender_id defaults to owner_id (a bridge bot gates a tap on the owner)."""
    cfg = BotConfig(driver="mock", command=("x",), ask_command=("y",), owner_id=7)
    assert cfg.effective_sender_id == 7


def test_config_agent_side_requires_owner_id():
    """ask_command set without owner_id is a clean config error (the tap would be dropped)."""
    try:
        BotConfig(driver="mock", command=("x",), ask_command=("y",))
        raise AssertionError("expected QaConfigError for missing owner_id")
    except QaConfigError as exc:
        assert "owner_id" in str(exc)


def test_config_inbound_block_is_not_agent_side():
    """A plain inbound sut.bot (no ask_command) is NOT agent-side and needs no owner_id."""
    cfg = BotConfig(driver="mock", command=("python3", "bot.py"))
    assert not cfg.is_agent_side


def test_config_rejects_mtproto_with_ask_command():
    """driver: mtproto + ask_command is a config error — the agent-side loop is mock-only, and the
    live path would SILENTLY ignore ask_command (review finding)."""
    try:
        BotConfig(driver="mtproto", command=("x",), ask_command=("y",), owner_id=7)
        raise AssertionError("expected QaConfigError for mtproto + ask_command")
    except QaConfigError as exc:
        assert "driver: mock" in str(exc) and "ask_command" in str(exc)


def test_config_opt_int_rejects_non_numeric_owner_id():
    """A non-numeric owner_id is a clean QaConfigError (not a ValueError traceback)."""
    from reviewlib.qa.config import _bot_from

    try:
        _bot_from(
            {
                "driver": "mock",
                "command": ["x"],
                "ask_command": ["y"],
                "owner_id": "abc",
            },
            Path("qa.yaml"),
        )
        raise AssertionError("expected QaConfigError for a non-numeric owner_id")
    except QaConfigError as exc:
        assert "owner_id" in str(exc)


def test_bot_seed_from_rejects_bad_shapes():
    """_bot_seed_from rejects a non-list seed, a non-mapping entry, and a missing path (clean
    QaConfigError each, never a raw TypeError/KeyError)."""
    from reviewlib.qa.config import _bot_seed_from

    for bad, needle in (
        ({"path": "x"}, "must be a list"),  # a mapping, not a list
        ([42], "must be a mapping"),  # an entry that is not a mapping
        ([{"content": "x"}], "path is required"),  # an entry missing its path
    ):
        try:
            _bot_seed_from(bad, Path("qa.yaml"))
            raise AssertionError(f"expected QaConfigError for {bad!r}")
        except QaConfigError as exc:
            assert needle in str(exc), (bad, str(exc))
    assert _bot_seed_from(None, Path("qa.yaml")) == ()  # absent -> empty


# --- the harness seam ----------------------------------------------------------------
def _card(text: str, buttons: list[list[str]]) -> bh.OutboundCall:
    kb = [[{"text": b, "callback_data": f"cb:{b}"} for b in row] for row in buttons]
    return bh.OutboundCall(
        method="sendMessage",
        payload={"chat_id": 1, "text": text, "reply_markup": {"inline_keyboard": kb}},
        at=time.monotonic(),
    )


def test_cards_captured_filters_inline_keyboard():
    """cards_captured keeps only outbound that carry an inline-button keyboard."""
    fake = bh.FakeTelegram()
    fake.outbound.append(
        bh.OutboundCall("sendMessage", {"text": "plain"}, time.monotonic())
    )
    fake.outbound.append(_card("with buttons", [["A"], ["B"]]))
    cards = bh.cards_captured(fake)
    assert len(cards) == 1 and cards[0].payload["text"] == "with buttons"


def test_cards_captured_ignores_edits():
    """An editMessageReplyMarkup (a card MUTATION, not a new post) is NOT counted as a new card —
    else a bridge that edits its card after answering would false-FAIL the Expect-card: 0 re-fire."""
    fake = bh.FakeTelegram()
    fake.outbound.append(_card("posted", [["A"]]))
    fake.outbound.append(
        bh.OutboundCall(
            "editMessageReplyMarkup",
            {
                "reply_markup": {
                    "inline_keyboard": [[{"text": "A", "callback_data": "x"}]]
                }
            },
            time.monotonic(),
        )
    )
    assert len(bh.cards_captured(fake)) == 1  # only the sendMessage


def test_cards_captured_decodes_form_encoded_json_markup():
    """A bot posting via application/x-www-form-urlencoded leaves reply_markup as a JSON STRING (the
    fake's form decoder doesn't parse it). cards_captured + card_button_data must decode it, else a
    valid inline card reads as zero cards and its Tap: labels as missing."""
    markup_json = json.dumps(
        {"inline_keyboard": [[{"text": "Ship it", "callback_data": "cb:0"}]]}
    )
    fake = bh.FakeTelegram()
    fake.outbound.append(
        bh.OutboundCall(
            "sendMessage",
            {"chat_id": 1, "text": "q", "reply_markup": markup_json},
            time.monotonic(),
        )
    )
    cards = bh.cards_captured(fake)
    assert len(cards) == 1, (
        "a form-encoded (JSON-string) reply_markup must still count as a card"
    )
    assert bh.card_button_data(cards[0], "ship it") == "cb:0"
    assert bh.card_button_labels(cards[0]) == ["Ship it"]


def test_reply_markup_dict_rejects_broken_and_non_dict():
    """_reply_markup_dict returns None for a non-dict markup so a malformed value reads as 'no card'
    rather than crashing: a broken JSON string, a JSON value that decodes to a non-dict (a list), and
    an absent markup. (str covers both the form-urlencoded and multipart-text decoders; neither emits
    bytes.)"""
    assert bh._reply_markup_dict({"reply_markup": "{not json"}) is None
    assert (
        bh._reply_markup_dict({"reply_markup": "[1, 2, 3]"}) is None
    )  # valid JSON, not a dict
    assert bh._reply_markup_dict({"reply_markup": 42}) is None
    assert bh._reply_markup_dict({}) is None
    assert bh._reply_markup_dict({"reply_markup": {"inline_keyboard": []}}) == {
        "inline_keyboard": []
    }


def test_emit_question_missing_binary_is_blocked_not_traceback():
    """A typo'd / unavailable ask_command binary raises a controlled BotHarnessError (mapped to a
    BLOCKED case), never an uncaught OSError traceback that kills the whole agent-side run."""
    try:
        bh.emit_question(
            ask_command=["/no/such/hook-client-binary"],
            cwd=Path.cwd(),
            env=dict(os.environ),
            payload="{}",
            exit_boot_failed=8,
        )
        raise AssertionError(
            "expected a BotHarnessError for a missing ask_command binary"
        )
    except bh.BotHarnessError as exc:
        assert exc.exit_code == 8 and "hook client" in str(exc)


def test_emit_question_empty_argv_is_blocked_not_traceback():
    """An EMPTY ask_command (Popen([]) raises IndexError, not OSError) is still a controlled
    BotHarnessError — the defensive IndexError branch, so a degenerate argv never tracebacks."""
    try:
        bh.emit_question(
            ask_command=[],
            cwd=Path.cwd(),
            env=dict(os.environ),
            payload="{}",
            exit_boot_failed=8,
        )
        raise AssertionError("expected a BotHarnessError for an empty ask_command")
    except bh.BotHarnessError as exc:
        assert exc.exit_code == 8


def test_agent_side_missing_ask_command_blocks_the_case():
    """End-to-end: a daemon that boots fine but a MISSING ask_command yields a BLOCKED case (with the
    launch error), not a crash — the run stays honest about what it could not drive."""
    suite = (
        "## Case: q\n"
        'Ask-question: {"tool_input": {"questions": [{"question": "x", "options": [{"label": "A"}]}]}}\n'
        "Expect-card: 1\nTap: A\nExpect-answer: A\n"
    )
    cfg = BotConfig(
        driver="mock",
        command=("python3", "-c", "import time; time.sleep(5)"),
        ask_command=("/no/such/hook-client-binary",),
        owner_id=424242,
    )
    transcript = bd.run_hermetic_bot_test(
        suite_text=suite,
        bot_config=cfg,
        cwd=Path.cwd(),
        sut_path=Path.cwd(),
        exit_boot_failed=8,
    )
    assert "VERDICT: BLOCKED" in transcript, transcript
    assert "hook client" in transcript, transcript


def test_do_tap_with_no_card_fails_cleanly():
    """A Tap: with no card to tap (e.g. Expect-card: 0 + Tap:) FAILs honestly — no IndexError."""
    fake = bh.FakeTelegram()
    ctx = bd._AgentRunCtx(
        fake=fake,
        ask_command=[],
        cwd=Path.cwd(),
        env={},
        sender_id=1,
        handles=[],
        exit_boot_failed=8,
    )
    case = bd.AgentCase(title="t", payload="{}", expect_card=0, tap="Allow")
    result = bd._do_tap(case, ctx)
    assert (
        result is not None
        and result.status == bd.FAIL
        and "no card to tap" in result.detail
    )


def test_card_button_data_and_labels():
    """card_button_data resolves a label (case-insensitive) to its callback_data; labels lists all."""
    card = _card("q", [["Ship it"], ["Hold"]])
    assert bh.card_button_data(card, "ship IT") == "cb:Ship it"
    assert bh.card_button_data(card, "nope") is None
    assert bh.card_button_labels(card) == ["Ship it", "Hold"]


def test_tap_injects_callback_query_from_owner():
    """tap extracts the button's callback_data and injects a callback_query stamped with from_id."""
    fake = bh.FakeTelegram()
    card = _card("q", [["Ship it"], ["Hold"]])
    assert bh.tap(fake, card, "Ship it", from_id=424242) is True
    [update] = fake._updates
    cb = update["callback_query"]
    assert cb["data"] == "cb:Ship it" and cb["from"]["id"] == 424242
    assert (
        "message" not in cb
    )  # message-less so the daemon skips the host-message-id match


def test_tap_missing_button_returns_false():
    """A tap on a label the card lacks returns False (the driver then FAILs with the labels)."""
    fake = bh.FakeTelegram()
    assert bh.tap(fake, _card("q", [["A"]]), "Z", from_id=1) is False


def test_ask_handle_reads_answer_then_caches():
    """await_answer returns the hook client's stdout and is idempotent (caches the first read)."""
    handle = bh.emit_question(
        ask_command=[
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('ANSWER:'+sys.stdin.read())",
        ],
        cwd=Path.cwd(),
        env=dict(os.environ),
        payload="Ship it",
    )
    # emit_question detaches the closed stdin (proc.stdin = None) so await_answer's communicate()
    # never flushes a closed stream — a version-independent pin for the CPython <3.13 crash the CI
    # matrix caught (the flush-on-closed ValueError), which a 3.13+ dev box would not reproduce.
    assert handle.proc.stdin is None
    assert handle.await_answer(timeout=10) == "ANSWER:Ship it"
    assert handle.await_answer(timeout=10) == "ANSWER:Ship it"  # cached, no re-read


def test_ask_handle_registers_with_the_signal_reaper_then_unregisters():
    """review-cli#162 follow-up (codex review): `emit_question` must register the hook
    client with `process._LIVE_CHILDREN` — the same registry `install_signal_reaper`'s
    SIGTERM/SIGINT handler and the internal backstop's `kill_live_children()` sweep —
    so an external signal reaps it too, not only `_run_streamed`'s own backend
    children. And it must UNREGISTER once `await_answer` observes a normal exit —
    otherwise the registry would grow forever across many QA test cases."""
    from reviewlib import process as proc_mod

    handle = bh.emit_question(
        ask_command=[
            sys.executable,
            "-c",
            "import sys; sys.stdout.write(sys.stdin.read())",
        ],
        cwd=Path.cwd(),
        env=dict(os.environ),
        payload="hi",
    )
    assert handle._reaper_handle is not None
    with proc_mod._LIVE_CHILDREN_LOCK:
        live_pids = {p.pid for p, _pgid in proc_mod._LIVE_CHILDREN}
    assert handle.proc.pid in live_pids
    handle.await_answer(timeout=10)
    with proc_mod._LIVE_CHILDREN_LOCK:
        live_pids_after = {p.pid for p, _pgid in proc_mod._LIVE_CHILDREN}
    assert handle.proc.pid not in live_pids_after


def test_ask_handle_hang_returns_none():
    """A hook client that never exits (the tap-loss bug) yields None within the timeout + is reaped."""
    handle = bh.emit_question(
        ask_command=[sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=Path.cwd(),
        env=dict(os.environ),
        payload="",
    )
    assert handle.await_answer(timeout=1.0) is None
    handle.reap()
    assert handle.proc.poll() is not None  # reaped, not leaked


def test_ask_handle_survives_a_stderr_flood():
    """A hook client that writes MORE than the ~64 KiB pipe buffer to stderr before printing the
    answer must NOT deadlock — await_answer drains both channels (a real bun/node tg-ctl logs to
    stderr). The naive wait-then-read would hang here and false-FAIL as a lost answer."""
    handle = bh.emit_question(
        ask_command=[
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('E'*200000); sys.stdout.write('Ship it')",
        ],
        cwd=Path.cwd(),
        env=dict(os.environ),
        payload="",
    )
    assert handle.await_answer(timeout=10) == "Ship it"


def test_wait_for_file():
    """wait_for_file returns True once the readiness file appears, False on timeout."""
    import tempfile

    d = Path(tempfile.mkdtemp())
    target = d / "ready.sock"
    assert bh.wait_for_file(target, timeout=0.2) is False
    target.write_text("x")
    assert bh.wait_for_file(target, timeout=0.2) is True


def test_build_agent_env_overrides_and_strips_tmux():
    """build_agent_env pins the hermetic core vars and strips TMUX so a question stays unscoped."""
    env = bh.build_agent_env(
        api_base="http://127.0.0.1:5000",
        owner_id=424242,
        token="123:tok",
        config_dir=Path("/c"),
        home=Path("/h"),
        extra_env={"TG_BOT_TOKEN": "should-be-overridden", "K": "v", "TMUX": "leaked"},
    )
    assert env["TG_API_BASE"] == "http://127.0.0.1:5000"
    assert env["TG_CHAT_ID"] == "424242" and env["TG_BOT_TOKEN"] == "123:tok"
    assert env["TG_CTL_CONFIG_DIR"] == "/c" and env["HOME"] == "/h"
    assert env["K"] == "v" and "TMUX" not in env


def test_boot_agent_daemon_refuses_non_loopback():
    """The daemon boot refuses a non-loopback TG_API_BASE so a misbuilt env can't reach real TG."""
    env = bh.build_agent_env(
        api_base="http://198.51.100.7:443",
        owner_id=1,
        token="1:t",
        config_dir=Path("/c"),
        home=Path("/h"),
    )
    try:
        bh.boot_agent_daemon(
            command=["true"], cwd=Path.cwd(), env=env, exit_boot_failed=8
        )
        raise AssertionError("expected a refusal for a non-loopback api_base")
    except bh.BotHarnessError as exc:
        assert "loopback" in str(exc) and exc.exit_code == 8


# --- template substitution + token/bot-id --------------------------------------------
def test_substitute_leaves_json_braces_untouched():
    """A token replace (not str.format) leaves JSON's literal braces alone."""
    out = bd._substitute('[{"cwd": "{cwd}", "x": 1}]', {"cwd": "/p"})
    assert out == '[{"cwd": "/p", "x": 1}]'
    assert json.loads(out) == [{"cwd": "/p", "x": 1}]


def test_bot_id_from_token_and_default():
    """The bot id is the numeric head of the token; a non-numeric head yields ''."""
    assert bd._bot_id_from_token("123456:secret") == "123456"
    assert bd._bot_id_from_token("notnumeric:x") == ""
    assert bd._resolve_token({}) == bd._DEFAULT_HERMETIC_TOKEN
    assert bd._resolve_token({"TG_BOT_TOKEN": "9:z"}) == "9:z"


def test_write_seed_substitutes_and_writes():
    """_write_seed resolves a templated path/content relative to the workspace and writes it."""
    ws = bd._AgentWorkspace.create()
    try:
        variables = {"bot_id": "777", "cwd": "/proj"}
        bd._write_seed(
            (
                SeedFile(
                    path="config/tg-ctl.{bot_id}.registration.json",
                    content='[{"cwd": "{cwd}"}]',
                ),
            ),
            ws,
            variables,
            exit_boot_failed=8,
        )
        written = (ws.config_dir / "tg-ctl.777.registration.json").read_text()
        assert json.loads(written) == [{"cwd": "/proj"}]
    finally:
        ws.cleanup()


def test_write_seed_rejects_escape_outside_workdir():
    """A seed path that escapes the throwaway workdir (a ``..`` climb) is a BotHarnessError, not a
    silent out-of-tree write — the run's 'nothing leaks' guarantee (review finding)."""
    ws = bd._AgentWorkspace.create()
    try:
        bd._write_seed(
            (SeedFile(path="../escaped.json", content="x"),), ws, {}, exit_boot_failed=8
        )
        raise AssertionError(
            "expected a BotHarnessError for a workdir-escaping seed path"
        )
    except bh.BotHarnessError as exc:
        assert "OUTSIDE" in str(exc) and not (ws.root.parent / "escaped.json").exists()
    finally:
        ws.cleanup()


# --- the 2-config DoD: the fixture bridge bot through the REAL harness ----------------
def _run_agent_fixture(*, dup_bug: bool) -> str:
    """Drive the tgctl-agentside fixture through the real agent-side harness. ``dup_bug`` flips on
    the #98 duplicate-card regression via the SUT's env toggle."""
    from reviewlib.qa.suites import load_suites_text

    suite_files = sorted((_AGENT_FIXTURE / "docs" / "tests" / "suites").glob("*.md"))
    suite_text = load_suites_text(suite_files, max_cases=None)
    env = {"SUT_MAX_RUNTIME_S": "60"}
    if dup_bug:
        env["SUT_DUP_BUG"] = "1"
    cfg = BotConfig(
        driver="mock",
        command=("python3", "{sut_dir}/sut.py", "run"),
        ask_command=("python3", "{sut_dir}/sut.py", "ask"),
        owner_id=424242,
        ready_file="{config_dir}/tg-ctl.{bot_id}.sock",
        seed=(
            SeedFile(
                path="config/tg-ctl.{bot_id}.registration.json",
                content='[{"cwd": "{cwd}", "registeredAt": 0}]',
            ),
        ),
        env=env,
    )
    return bd.run_hermetic_bot_test(
        suite_text=suite_text,
        bot_config=cfg,
        cwd=_AGENT_FIXTURE.resolve(),
        sut_path=_AGENT_FIXTURE.resolve(),
        exit_boot_failed=8,
    )


def test_agent_side_routing_blocks_an_inbound_suite():
    """ask_command set (agent-side tier) but the suite is inbound-only (Send:/Expect:) → a clear
    BLOCKED with the mismatch pointer, NOT a daemon boot. suite_has_agent_directives is the gate."""
    cfg = BotConfig(
        driver="mock",
        command=("python3", "x"),
        ask_command=("python3", "ask"),
        owner_id=424242,
    )
    transcript = bd.run_hermetic_bot_test(
        suite_text="## Case: c\nSend: /start\nExpect: hi\n",
        bot_config=cfg,
        cwd=Path.cwd(),
        sut_path=Path.cwd(),
        exit_boot_failed=8,
    )
    assert "VERDICT: BLOCKED" in transcript and "ask_command is set" in transcript


def test_dod_fixed_bridge_bot_passes():
    """The fixed bridge bot: one card per question, the tap reaches the agent, and an answered
    re-fire replays the answer with NO second card → VERDICT PASS."""
    transcript = _run_agent_fixture(dup_bug=False)
    assert "VERDICT: PASS" in transcript, transcript
    assert "2 run, 2 passed" in transcript, transcript


def test_dod_duplicate_card_bug_fails():
    """SUT_DUP_BUG=1 re-posts a duplicate card on the answered re-fire (the tg-cli#98 class) →
    VERDICT FAIL with the duplicate-card finding."""
    transcript = _run_agent_fixture(dup_bug=True)
    assert "VERDICT: FAIL" in transcript, transcript
    assert "duplicate" in transcript.lower(), transcript


def test_agent_side_daemon_crash_is_blocked_with_output_tail():
    """A daemon that EXITS on boot (rather than long-polling) yields VERDICT BLOCKED with its output
    tail as the proof — the harness is honest that it never drove a case, not a silent pass and not a
    traceback. (No ready_file → the short boot grace then the crash check catches the dead daemon.)"""
    suite = (
        "## Case: q\n"
        'Ask-question: {"tool_input": {"questions": [{"question": "x", "options": [{"label": "A"}]}]}}\n'
        "Expect-card: 1\nTap: A\nExpect-answer: A\n"
    )
    cfg = BotConfig(
        driver="mock",
        command=("python3", "-c", "print('DAEMON-BOOM'); raise SystemExit(3)"),
        ask_command=("python3", "-c", "pass"),
        owner_id=424242,
    )
    transcript = bd.run_hermetic_bot_test(
        suite_text=suite,
        bot_config=cfg,
        cwd=Path.cwd(),
        sut_path=Path.cwd(),
        exit_boot_failed=8,
    )
    assert "VERDICT: BLOCKED" in transcript, transcript
    assert "DAEMON-BOOM" in transcript, transcript  # the output tail is the proof


def test_agent_side_seed_escape_is_blocked():
    """A seed path that escapes the throwaway workdir BLOCKS the run with the containment message,
    rather than writing out of tree or crashing (review finding, end-to-end through the orchestrator)."""
    suite = (
        "## Case: q\n"
        'Ask-question: {"tool_input": {"questions": [{"question": "x", "options": [{"label": "A"}]}]}}\n'
        "Expect-card: 1\n"
    )
    cfg = BotConfig(
        driver="mock",
        command=("python3", "-c", "import time; time.sleep(5)"),
        ask_command=("python3", "-c", "pass"),
        owner_id=424242,
        seed=(SeedFile(path="../escaped-by-qa.json", content="x"),),
    )
    transcript = bd.run_hermetic_bot_test(
        suite_text=suite,
        bot_config=cfg,
        cwd=Path.cwd(),
        sut_path=Path.cwd(),
        exit_boot_failed=8,
    )
    assert "VERDICT: BLOCKED" in transcript, transcript
    assert "OUTSIDE" in transcript, transcript


# --- the REAL tg-ctl proof (GATED on REVIEW_QA_TGCTL_DIR + bun; SKIPs in CI) ----------
def test_real_tgctl_agent_side_proof():
    """Drive the tgctl-real suite against a REAL tg-ctl checkout and assert the expected verdict.

    SKIPs unless ``REVIEW_QA_TGCTL_DIR`` (a tg-cli checkout) and ``bun`` are present — tg-cli is a
    separate repo, so this never runs in review-cli CI. ``REVIEW_QA_TGCTL_EXPECT`` (PASS default,
    or FAIL) is the verdict asserted, so the SAME test pins both ends of the RED→GREEN proof: PASS
    on a fixed tg-ctl (>=1.19.2), FAIL on pre-#98 tg-ctl (the duplicate-card bug). See
    tests/fixtures/qa/tgctl-real/README.md."""
    import shutil

    tgctl_dir = os.environ.get("REVIEW_QA_TGCTL_DIR")
    if not tgctl_dir or not shutil.which("bun"):
        print(
            "   (skipped: set REVIEW_QA_TGCTL_DIR to a tg-cli checkout + install bun)"
        )
        return
    from reviewlib.qa.config import load_qa_config
    from reviewlib.qa.suites import load_suites_text

    fixture = _FIXTURES / "tgctl-real"
    sut = load_qa_config(fixture, None)
    suite_text = load_suites_text(
        sorted((fixture / "docs" / "tests" / "suites").glob("*.md")), max_cases=None
    )
    transcript = bd.run_hermetic_bot_test(
        suite_text=suite_text,
        bot_config=sut.bot,
        cwd=Path(tgctl_dir).resolve(),
        sut_path=Path(tgctl_dir).resolve(),
        exit_boot_failed=8,
    )
    expect = os.environ.get("REVIEW_QA_TGCTL_EXPECT", "PASS").upper()
    assert f"VERDICT: {expect}" in transcript, transcript
    print(f"   real tg-ctl proof: got VERDICT {expect} (as expected) for {tgctl_dir}")


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
