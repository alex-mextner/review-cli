"""review-qa Tier-2 (the LIVE tier) SCAFFOLDING: the gated live-driver seams for the bot, web,
and ext harnesses — the structure that, GIVEN credentials/infra via env, would drive the REAL
System-Under-Test (a real test Telegram account over MTProto, a live site in a real browser, a
live VS Code), and that SKIPs LOUDLY with the exact missing-creds message when those creds are
absent (spec docs/specs/review-qa.md §7.3 Tier 2, §7.4 the live run, issue #82/#84).

WHAT IS HERE (no live creds needed) vs WHAT NEEDS CREDS (the live run, #82).
The Tier-1 harnesses (bot_harness / web_harness / ext_harness) drive a HERMETIC or local SUT
deterministically. Tier-2 swaps the in-process fake / local boot for a REAL external system:

  - bot:  a DEDICATED test Telegram USER account over MTProto (Telethon) — the only faithful
          way to exercise real delivery, voice notes, inline-button callbacks, forum topics.
  - web:  a real browser (agent-browser / Playwright) against a LIVE deployed site URL — visual
          correctness an in-memory page can't show.
  - ext:  a real isolated VS Code with window-screenshot visual diffing (baseline + perceptual
          diff + threshold gate) layered on the Tier-1 CDP runner.

The LIVE RUN itself needs credentials/infra the CTO must provision (per SUT — see
``creds_doc()`` and ``docs/specs/review-qa.md`` §7.4). This module is the SCAFFOLDING: the
``tier: live`` config path, the per-SUT availability GATE that names the exact missing creds,
and the live-driver SKELETON wired behind the SAME protocol seam the Tier-1 driver speaks — so
the live run is a drop-in once creds land, NOT a rewrite. Everything here is exercised by unit
tests with NO creds, NO network, NO browser, NO Telegram: the gate's creds-detection and the
SKIP-LOUD path are pure logic.

THE GATE CONTRACT (mirrors web_harness.playwright_available / ext_harness.vscode_available).
Each ``*_live_available()`` returns ``(ok, reason)``. ``ok`` is True ONLY when the live tier's
opt-in flag is on AND every required credential env var is present AND the driver's heavy dep
imports. When False, ``reason`` is the ACTIONABLE message naming EXACTLY which env var / dep /
infra is missing and how to provide it — so a ``tier: live`` run with no creds BLOCKS loudly
with the fix rather than crashing OR (worse) silently faking a pass. A Tier-2 run is NEVER a
green pass on zero work: absent creds is a controlled BLOCKED, never SUCCESS.

SAFETY (Tier-2 drives a REAL account / site — fail-closed BEFORE any live action). The bot
gate additionally refuses to run if the configured live chat targets the user's REAL
``TG_CHAT_ID`` (the value that would spam a real human) and requires the explicit
``REVIEW_QA_BOT_LIVE`` opt-in on top of the creds — one missing cred OR a real-chat match
fails the gate closed, before MTProto ever connects. The skeletons here do NOT open a live
connection; they raise ``LiveTierUnavailable`` until the live run is implemented under #82.
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# How the live bot driver waits for the bot's reply: poll the chat for a NEW inbound message
# every ``_LIVE_REPLY_POLL_S`` until the predicate matches or the caller's timeout elapses, and
# fetch at most ``_LIVE_FETCH_LIMIT`` messages per poll (newest-first). A bot reply is one or a few
# messages; the limit bounds the read. A burst LARGER than the limit between two polls would drop
# its oldest messages (newest-first + the limit), so keep the limit comfortably above any realistic
# single-reply burst.
_LIVE_REPLY_POLL_S = 0.7
_LIVE_FETCH_LIMIT = 100
# A network-bounded ceiling on the MTProto connect itself, so a live run can't hang forever on an
# unreachable Telegram (Telethon's own retries are otherwise unbounded at this layer).
_LIVE_CONNECT_TIMEOUT_S = 30.0
# The same ceiling on every other discrete op (auth check, entity resolve, send, tap, cleanup) so a
# network stall on any single call surfaces as a controlled BLOCKED rather than a silent hang.
# ``expect`` bounds itself via its own polling deadline, so it does NOT use this.
_LIVE_OP_TIMEOUT_S = 30.0
# Telethon's flood_sleep_threshold (default 60s) SILENTLY sleeps through a flood-wait up to that
# many seconds — which to the QA run looks exactly like a hang. We set a SMALL positive threshold:
# a trivial sub-threshold wait (the common case for a handful of quick sends across a suite) sleeps
# silently and the run proceeds, while a genuinely long flood-wait still RAISES FloodWaitError (→ a
# controlled per-case BLOCKED). A threshold of 0 would flap EVERY trivial wait into a BLOCKED and
# make live runs flaky; 60s (the default) would silently stall the suite.
_LIVE_FLOOD_SLEEP_THRESHOLD_S = 5


# --- the live-tier driver-name constants (the qa.yaml `driver:` values that select Tier-2) ---
# A SUT block selects Tier-2 by naming its live driver. These sit alongside the Tier-1 default
# (`mock` / `playwright` / `vscode`); the config layer accepts them and routes to the gate here.
BOT_LIVE_DRIVER = "mtproto"
WEB_LIVE_DRIVER = "agent-browser"
EXT_LIVE_DRIVER = "vscode-visual"

# The opt-in flag each SUT's live tier gates on FIRST (mirrors REVIEW_QA_PLAYWRIGHT /
# REVIEW_QA_VSCODE). A `driver: <live>` in qa.yaml is necessary but NOT sufficient — the live
# tier is heavy and drives a real external system, so the operator must ALSO opt in explicitly.
BOT_LIVE_FLAG = "REVIEW_QA_BOT_LIVE"
WEB_LIVE_FLAG = "REVIEW_QA_WEB_LIVE"
EXT_LIVE_FLAG = "REVIEW_QA_EXT_LIVE"

# The credential / infra env vars each live tier REQUIRES (the exact set the CTO must provision).
# Kept as data so the gate, the docs generator, and the tests all read the SAME source of truth.
BOT_LIVE_CREDS = ("TG_TEST_API_ID", "TG_TEST_API_HASH", "TG_TEST_SESSION", "TG_TEST_CHAT_ID")
WEB_LIVE_CREDS = ("REVIEW_QA_WEB_BASE_URL",)
EXT_LIVE_CREDS: tuple[str, ...] = ()  # ext live reuses REVIEW_QA_VSCODE + a baseline dir (below)


def _flag_enabled(name: str) -> bool:
    """True when env ``name`` is set to anything truthy (mirrors the Tier-1 harness flag check)."""
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no", "off")


def _missing(creds: tuple[str, ...]) -> list[str]:
    """The subset of ``creds`` that is absent OR blank in the environment (a set-but-empty var is
    treated as missing — an empty api hash is no api hash)."""
    return [name for name in creds if not os.environ.get(name, "").strip()]


@dataclass(frozen=True)
class LiveTierGate:
    """The result of a live-tier availability check: ``ok`` plus the actionable ``reason`` when
    not ok. ``as_tuple`` matches the ``(ok, reason)`` shape of the Tier-1 ``*_available`` gates so
    the dispatch layer treats every gate uniformly."""

    ok: bool
    reason: str

    def as_tuple(self) -> tuple[bool, str]:
        return self.ok, self.reason


class LiveTierUnavailable(RuntimeError):
    """Raised when a live-tier driver is asked to drive the SUT but its gate is not satisfied (no
    opt-in flag, missing creds, or the live run is not yet implemented). The dispatch layer turns
    it into a controlled BLOCKED with the gate's ``reason`` — never a traceback, never a fake pass.
    Carries the qa exit class so a not-provisioned live run maps to the same stable BLOCKED code a
    Tier-1 harness-infra failure uses."""

    def __init__(self, message: str, *, exit_code: int):
        super().__init__(message)
        self.exit_code = exit_code


# --- the per-SUT availability gates (each names the EXACT missing creds) ---------------------
def bot_live_available() -> LiveTierGate:
    """Whether the bot Tier-2 (real-Telegram MTProto) live run is GATED ON. Order of checks (each
    fails the gate with the precise fix): (1) the ``REVIEW_QA_BOT_LIVE`` opt-in flag; (2) the
    Telethon dep imports (a new qa-harness dep, NOT a tg-cli dep); (3) every test-account
    credential is present (api id/hash, a session string for the DEDICATED test USER account, and
    the test chat id); (4) SAFETY — the configured test chat is NOT the user's real ``TG_CHAT_ID``
    (driving a real chat would spam a real human; fail closed). Only all four green → ``ok``."""
    if not _flag_enabled(BOT_LIVE_FLAG):
        return LiveTierGate(False, (
            "the bot Tier-2 LIVE run drives a REAL test Telegram account over MTProto, which is "
            f"OFF by default. Set {BOT_LIVE_FLAG}=1 to opt in, AND provision a DEDICATED throwaway "
            "test account: " + _creds_phrase(BOT_LIVE_CREDS) + ". See docs/specs/review-qa.md §7.4."
        ))
    if not _telethon_importable():
        return LiveTierGate(False, (
            f"{BOT_LIVE_FLAG}=1 is set but the `telethon` MTProto client is not installed. "
            "Install it (a qa-harness dep, NOT a tg-cli dep): pip install telethon"
        ))
    missing = _missing(BOT_LIVE_CREDS)
    if missing:
        return LiveTierGate(False, (
            "the bot Tier-2 LIVE run needs a DEDICATED test Telegram account; missing creds: "
            f"{', '.join(missing)}. Provision a throwaway account and set: "
            + _creds_phrase(BOT_LIVE_CREDS) + ". See docs/specs/review-qa.md §7.4."
        ))
    safety = _bot_live_safety_block()
    if safety is not None:
        return LiveTierGate(False, safety)
    return LiveTierGate(True, "")


def _bot_live_safety_block() -> str | None:
    """The fail-closed safety check for the bot live run: refuse if the configured test chat id
    equals the real ``TG_CHAT_ID`` (the value that targets a real human). Returns the refusal
    message, or ``None`` when safe. This is the LAST line — the gate already required an explicit
    opt-in + a dedicated test account; this stops a misconfigured test chat from spamming a real
    chat even when the operator opted in.

    BEST-EFFORT: the comparison is exact-string equality, so it catches the common
    ``TG_TEST_CHAT_ID == TG_CHAT_ID`` mistake but NOT an aliased form (a ``@username`` vs the same
    chat's numeric id, or whitespace/format variance). It is a guard, not a proof — the PRIMARY
    safety is using a DEDICATED test account in a DEDICATED test chat (spec §7.3), which this
    backstops; it does not replace it."""
    test_chat = os.environ.get("TG_TEST_CHAT_ID", "").strip()
    real_chat = os.environ.get("TG_CHAT_ID", "").strip()
    if test_chat and real_chat and test_chat == real_chat:
        return (
            "SAFETY: TG_TEST_CHAT_ID equals the real TG_CHAT_ID — the bot Tier-2 live run would "
            "drive your REAL chat. Point TG_TEST_CHAT_ID at a DEDICATED test chat containing only "
            "the test account + test bot, never your real chat. Refusing to run (fail-closed)."
        )
    return None


def web_live_available() -> LiveTierGate:
    """Whether the web Tier-2 (real browser against a LIVE site) live run is GATED ON. Checks: the
    ``REVIEW_QA_WEB_LIVE`` opt-in flag, a Playwright/agent-browser runtime, and the live site base
    URL (``REVIEW_QA_WEB_BASE_URL`` — the deployed test site to drive, distinct from the Tier-1
    locally-booted dev server). Each missing piece fails the gate with the precise fix."""
    if not _flag_enabled(WEB_LIVE_FLAG):
        return LiveTierGate(False, (
            "the web Tier-2 LIVE run drives a REAL browser against a deployed test SITE, which is "
            f"OFF by default. Set {WEB_LIVE_FLAG}=1 to opt in, AND provision a test site URL: "
            + _creds_phrase(WEB_LIVE_CREDS) + ". See docs/specs/review-qa.md §7.4."
        ))
    if not _playwright_importable():
        return LiveTierGate(False, (
            f"{WEB_LIVE_FLAG}=1 is set but no browser runtime is installed. Install one: "
            "pip install playwright && python -m playwright install chromium "
            "(or have the `agent-browser` CLI on PATH)."
        ))
    missing = _missing(WEB_LIVE_CREDS)
    if missing:
        return LiveTierGate(False, (
            "the web Tier-2 LIVE run needs a deployed test site to drive; missing: "
            f"{', '.join(missing)}. Set REVIEW_QA_WEB_BASE_URL to the test site URL "
            "(e.g. https://stage.example.test). See docs/specs/review-qa.md §7.4."
        ))
    return LiveTierGate(True, "")


def ext_live_available() -> LiveTierGate:
    """Whether the ext Tier-2 (real VS Code + window-screenshot visual diffing) live run is GATED
    ON. The ext live tier LAYERS visual diffing on the Tier-1 CDP runner, so it reuses the Tier-1
    ``REVIEW_QA_VSCODE`` gate (real VS Code + node/tsx) and ADDS a baseline directory
    (``REVIEW_QA_EXT_BASELINE_DIR``) + the perceptual-diff tooling (the cvGate's ImageMagick v7
    ``magick``, shared with the visual-verification suite). Checks: the ``REVIEW_QA_EXT_LIVE``
    opt-in, the underlying VS Code gate, a writable baseline dir, and the diff tool."""
    if not _flag_enabled(EXT_LIVE_FLAG):
        return LiveTierGate(False, (
            "the ext Tier-2 LIVE run launches a real VS Code and DIFFS window screenshots against "
            f"a baseline, which is OFF by default. Set {EXT_LIVE_FLAG}=1 to opt in. It also needs "
            "the Tier-1 ext gate (REVIEW_QA_VSCODE=1 + node/tsx + a VS Code binary), a baseline "
            "dir (REVIEW_QA_EXT_BASELINE_DIR), and ImageMagick v7 (`magick`). "
            "See docs/specs/review-qa.md §7.4."
        ))
    vscode_ok, vscode_reason = _vscode_gate()
    if not vscode_ok:
        return LiveTierGate(False, (
            f"{EXT_LIVE_FLAG}=1 is set but the underlying VS Code gate is not satisfied: "
            f"{vscode_reason}"
        ))
    baseline = os.environ.get("REVIEW_QA_EXT_BASELINE_DIR", "").strip()
    if not baseline:
        return LiveTierGate(False, (
            "the ext Tier-2 LIVE run needs a baseline-screenshot directory to diff against; set "
            "REVIEW_QA_EXT_BASELINE_DIR to a writable dir (first run records baselines, later runs "
            "diff against them). See docs/specs/review-qa.md §7.4."
        ))
    baseline_problem = _baseline_dir_problem(baseline)
    if baseline_problem is not None:
        return LiveTierGate(False, baseline_problem)
    if not _perceptual_diff_available():
        return LiveTierGate(False, (
            "the ext Tier-2 LIVE run needs ImageMagick v7 (`magick`) for the perceptual diff "
            "(same tool the visual-verification suite uses). Install ImageMagick 7."
        ))
    return LiveTierGate(True, "")


# --- the heavy-dep / tool probes (a missing dep is a clean False, not a crash) ----------------
# NOTE: these catch ImportError (the dep is absent). A package that imports but raises a DIFFERENT
# exception (a broken half-install) is rare and still surfaces — deliberately not swallowed here,
# so a corrupt install fails loud at the import site rather than being silently reported "absent".
def _telethon_importable() -> bool:
    try:
        import telethon  # noqa: F401
    except ImportError:
        return False
    return True


def _playwright_importable() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        # agent-browser (a separate CLI) is the alternate runtime; treat its presence on PATH as
        # an acceptable browser backend so a Playwright-less but agent-browser-equipped host gates
        # in. The actual driver picks whichever is present.
        import shutil

        return shutil.which("agent-browser") is not None
    return True


def _vscode_gate() -> tuple[bool, str]:
    """Defer to the Tier-1 ext gate (real VS Code + node/tsx). Imported lazily so this module
    stays import-light and a circular import is avoided."""
    from .ext_harness import vscode_available

    return vscode_available()


def _perceptual_diff_available() -> bool:
    """Whether ImageMagick v7's ``magick`` is on PATH (the perceptual-diff tool the cvGate uses).
    A missing tool is a clean False (a gate fail with the install hint), never a crash."""
    import shutil

    return shutil.which("magick") is not None


def _baseline_dir_problem(baseline: str) -> str | None:
    """Why ``REVIEW_QA_EXT_BASELINE_DIR`` can't serve as the baseline-screenshot store, or ``None``
    when it can. The ext live run WRITES baselines on the first run and DIFFS on later runs, so the
    dir must be writable BEFORE the live screenshot path is reached — otherwise the availability
    gate reports ok and the first baseline write blows up OUTSIDE the gate (codex P2). Usable means
    an existing WRITABLE directory, OR a not-yet-existing path whose PARENT exists and is writable
    (so the first run can create it). Anything else — a path that is a FILE, a DANGLING symlink, an
    UNWRITABLE dir, or a path whose parent is missing/unwritable — is a controlled BLOCKED with the
    actionable fix.

    BEST-EFFORT: the writability check is ``os.access(.., W_OK)`` (the same idiom as the rest of
    this gate), which is judged by the real uid/gid and so can't see a read-only mount, a restrictive
    ACL, or an immutable flag — those would still fail at the actual write. This catches the common
    cases (a file, a 0-perm dir, a missing parent) at the gate; it is a guard, not a proof that the
    first write WILL succeed."""
    path = Path(baseline)
    if path.is_symlink() and not path.exists():
        return (
            f"REVIEW_QA_EXT_BASELINE_DIR ({baseline!r}) is a dangling symlink (its target is "
            "missing); the path is occupied, so the first baseline write would fail. Point it at an "
            "existing writable dir or a creatable path. See docs/specs/review-qa.md §7.4."
        )
    if path.exists():
        if not path.is_dir():
            return (
                f"REVIEW_QA_EXT_BASELINE_DIR ({baseline!r}) is not a directory; it must be a "
                "writable directory for baseline screenshots (first run records, later runs diff). "
                "Point it at a directory or a creatable path. See docs/specs/review-qa.md §7.4."
            )
        if not os.access(path, os.W_OK):
            return (
                f"REVIEW_QA_EXT_BASELINE_DIR ({baseline!r}) is not writable; the ext live run "
                "records/updates baseline screenshots there. Make it writable or point it at a "
                "writable dir. See docs/specs/review-qa.md §7.4."
            )
        return None
    parent = path.parent
    if not parent.is_dir() or not os.access(parent, os.W_OK):
        return (
            f"REVIEW_QA_EXT_BASELINE_DIR ({baseline!r}) does not exist and can't be created: its "
            f"parent ({str(parent)!r}) is missing or not writable. Point it at an existing writable "
            "dir or a creatable path (a writable parent). See docs/specs/review-qa.md §7.4."
        )
    return None


# --- the live-driver SKELETONS (wired behind the Tier-1 protocol seams; live run = #82) -------
class LiveBotDriver:
    """The bot Tier-2 LIVE driver — drives a real test Telegram USER account over MTProto
    (Telethon) as the human caller, behind the same send/expect/tap seam the Tier-1 hermetic
    driver speaks (spec §7.3 Tier 2). ``connect`` opens the MTProto session against a DEDICATED
    test account, resolves the test chat, and marks the high-water message id so ``expect`` only
    sees replies that arrive AFTER connect (chat history is ignored).

    THE CONTRACT (spec §7.3 Tier 2): ``send(text, reply_to)`` (deliver as the human caller),
    ``tap(message, button)`` (callback queries — the faithful way to exercise q-buttons /
    plan-approval), ``expect(predicate, timeout)`` (wait for the bot's next INBOUND reply matching
    the predicate; our own outbound is skipped). Telethon's client is async; this driver holds its
    own event loop and runs each coroutine synchronously, so the deterministic case-runner drives
    it with the same straight-line code the hermetic path uses.

    TESTABILITY. The Telethon client is built lazily inside ``connect`` (the dep is a qa-harness
    extra, NOT installed in CI), so a unit test injects a ``client_factory`` (``loop -> client``)
    that returns an in-memory fake transport — exercising this driver's REAL translation logic
    (min-id polling, outbound filtering, the predicate gate, button resolution) with no Telegram,
    no network, no telethon. A missing/old telethon or a failed connect raises
    ``LiveTierUnavailable`` (the controlled BLOCKED), never a raw traceback."""

    def __init__(
        self,
        *,
        api_id: str,
        api_hash: str,
        session: str,
        chat: str,
        exit_blocked: int,
        client_factory: Callable[[asyncio.AbstractEventLoop], Any] | None = None,
        poll_interval_s: float = _LIVE_REPLY_POLL_S,
        connect_timeout_s: float = _LIVE_CONNECT_TIMEOUT_S,
        op_timeout_s: float = _LIVE_OP_TIMEOUT_S,
    ):
        self._api_id = api_id
        self._api_hash = api_hash
        self._session = session
        self._chat = chat
        self._exit_blocked = exit_blocked
        self._client_factory = client_factory
        self._poll_interval_s = poll_interval_s
        self._connect_timeout_s = connect_timeout_s
        self._op_timeout_s = op_timeout_s
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: Any = None
        self._entity: Any = None
        self._last_seen_id = 0
        self._sent_ids: list[int] = []  # ids of messages WE sent, deleted on disconnect (cleanup)

    # --- lifecycle -------------------------------------------------------------------------
    def connect(self) -> None:
        """Open the MTProto session, verify the account is authorized, resolve the test chat, and
        record the high-water message id. Any failure (telethon absent, not authorized, the chat
        can't be resolved, the network is down, a timeout) is a controlled ``LiveTierUnavailable``
        carrying the boot-failed exit class — the dispatch turns it into a BLOCKED transcript, never
        a fake pass. The loop/client are always torn down on a failed connect (no leaked socket).

        THREADING ASSUMPTION: this drives a dedicated event loop and makes it the thread's current
        loop, so it assumes the QA run is on the main/a dedicated thread WITHOUT a foreign running
        asyncio loop (the synchronous CLI case). It is not safe to call from inside another running
        loop."""
        # A second connect() on the SAME driver (misuse — the suite runner builds a FRESH driver
        # per run) would orphan the prior loop/client; tear any prior session down first so connect
        # is re-entrant-safe instead of leaking the first loop.
        if self._loop is not None:
            self.disconnect()
        loop = self._loop = asyncio.new_event_loop()
        # Make this loop the thread's current loop BEFORE building the client: Telethon binds its
        # internal asyncio objects to ``get_event_loop()`` at construct/connect time, so without
        # this they could attach to a different loop ("got Future attached to a different loop") on
        # the first live call. The public methods then drive coroutines on THIS loop explicitly.
        asyncio.set_event_loop(loop)
        try:
            client = self._client = self._build_client(loop)
            # Cap how long a flood-wait may SILENTLY sleep inside send/tap (see the constant): a
            # short wait passes quietly, a long one RAISES FloodWaitError so the run reports it
            # rather than appearing to hang. (0 would flap even trivial waits into a BLOCKED.)
            if hasattr(client, "flood_sleep_threshold"):
                client.flood_sleep_threshold = _LIVE_FLOOD_SLEEP_THRESHOLD_S
            # Bound the connect itself — an unreachable Telegram must not hang the whole QA run.
            self._await(client.connect(), self._connect_timeout_s)
            if not self._await(client.is_user_authorized(), self._op_timeout_s):
                raise LiveTierUnavailable(
                    "the bot Tier-2 live run connected to Telegram but the test account is NOT "
                    "authorized — TG_TEST_SESSION is empty, expired, or revoked. Regenerate a "
                    "Telethon StringSession for the DEDICATED test account. See "
                    "docs/specs/review-qa.md §7.4.",
                    exit_code=self._exit_blocked,
                )
            self._entity = self._await(client.get_entity(self._target_chat()), self._op_timeout_s)
            self._last_seen_id = self._await(self._latest_message_id(), self._op_timeout_s)
        except LiveTierUnavailable:
            self.disconnect()
            raise
        except Exception as exc:  # noqa: BLE001 — any transport/resolve failure → controlled BLOCKED
            self.disconnect()
            raise LiveTierUnavailable(
                "the bot Tier-2 live run could not open the MTProto session / resolve the test "
                f"chat ({type(exc).__name__}: {exc}). Check TG_TEST_API_ID/HASH/SESSION and that "
                "TG_TEST_CHAT_ID is a chat the test account is a member of. See "
                "docs/specs/review-qa.md §7.4.",
                exit_code=self._exit_blocked,
            ) from exc

    def _build_client(self, loop: asyncio.AbstractEventLoop) -> Any:
        """Build the Telethon client (or the injected fake). Telethon is imported HERE (lazy) — it
        is an opt-in qa-harness dep, not installed in CI — so a missing/old install is a clean
        ``LiveTierUnavailable`` with the install hint, never an import crash at module load."""
        if self._client_factory is not None:
            return self._client_factory(loop)
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
        except ImportError as exc:  # the dep is absent OR too old to expose StringSession
            raise LiveTierUnavailable(
                "the bot Tier-2 live run needs the `telethon` MTProto client (a qa-harness dep, "
                "NOT a tg-cli dep): pip install -e '.[live]' (or pip install telethon). "
                f"(import failed: {exc})",
                exit_code=self._exit_blocked,
            ) from exc
        try:
            api_id = int(self._api_id)
        except (TypeError, ValueError) as exc:
            # Do NOT echo the configured api_id value: it is an account credential (the pair to the
            # secret api_hash) and this message lands in a SAVED BLOCKED artifact. Name the var, not
            # the value.
            raise LiveTierUnavailable(
                "TG_TEST_API_ID must be the NUMERIC app id from my.telegram.org (the configured "
                "value is not an integer). See docs/specs/review-qa.md §7.4.",
                exit_code=self._exit_blocked,
            ) from exc
        # Telethon 1.x binds to the thread's current loop (set in connect); it no longer accepts a
        # ``loop=`` kwarg, so do NOT pass one.
        return TelegramClient(StringSession(self._session), api_id, self._api_hash)

    def _target_chat(self) -> Any:
        """The chat to drive: a numeric id as ``int`` (the common ``-100…`` supergroup / a user
        id), else the raw string (an ``@username``) for Telethon to resolve."""
        chat = self._chat.strip()
        try:
            return int(chat)
        except ValueError:
            return chat

    async def _latest_message_id(self) -> int:
        """The newest existing message id in the chat (the high-water mark) so ``expect`` ignores
        history and only matches replies that land AFTER connect. Empty chat → 0."""
        messages = await self._client.get_messages(self._entity, limit=1)
        return max((m.id for m in messages), default=0)

    def disconnect(self) -> None:
        """Tear the session down idempotently — delete the messages WE created (spec §7.3 cleanup),
        disconnect the client, close the loop. Never raises (teardown runs in a ``finally`` and on a
        failed connect)."""
        client, loop = self._client, self._loop
        if client is not None and loop is not None and not loop.is_closed():
            self._cleanup_sent(client, loop)
            try:
                loop.run_until_complete(client.disconnect())
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
        if loop is not None and not loop.is_closed():
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass
        if loop is not None:
            # Drop the (now-closed) loop as the thread's current loop so a later driver / caller
            # never inherits a closed loop from us.
            try:
                asyncio.set_event_loop(None)
            except Exception:  # noqa: BLE001
                pass
        self._client = self._loop = self._entity = None
        self._sent_ids = []

    def _cleanup_sent(self, client: Any, loop: asyncio.AbstractEventLoop) -> None:
        """Best-effort delete of the messages WE sent (spec §7.3: 'Cleanup created test messages
        after a run') so a dedicated test chat doesn't accumulate run-over-run. Never raises — a bot
        whose replies we can't delete, or a client without ``delete_messages``, is silently skipped;
        the run already produced its verdict before teardown."""
        if not self._sent_ids or self._entity is None or not hasattr(client, "delete_messages"):
            return
        try:
            loop.run_until_complete(asyncio.wait_for(
                client.delete_messages(self._entity, self._sent_ids), timeout=self._op_timeout_s))
        except Exception:  # noqa: BLE001 — cleanup is best-effort, never fails the run
            pass

    # --- the send / expect / tap contract --------------------------------------------------
    def _require_connected(self) -> None:
        """Guard the send/expect/tap ops: a call before ``connect`` (no loop/client) is a controlled
        ``LiveTierUnavailable``, not an ``AttributeError`` on a ``None`` client."""
        if self._loop is None or self._client is None:
            raise LiveTierUnavailable(
                "the bot Tier-2 live driver was driven before connect() — call connect() first.",
                exit_code=self._exit_blocked,
            )

    def send(self, text: str, *, reply_to: int | None = None) -> Any:
        """Deliver ``text`` to the test chat AS the human caller and return the sent message. The
        sent id is recorded so disconnect can delete our messages (cleanup).

        Re-base the high-water mark to the chat's CURRENT newest message before sending, so each
        ``Send:`` opens a fresh reply window. Without this, a bot that answers one ``Send:`` with
        MULTIPLE messages (e.g. a greeting AND a separate menu card) leaves the un-matched extras
        behind — ``expect`` returns on the FIRST match and only advances the high-water to it — and
        those leftovers would then be consumed by the NEXT case's ``expect`` (the runner always uses
        the any-reply predicate), producing a false match / FAIL that flaps between runs."""
        self._require_connected()
        self._last_seen_id = self._run_op(self._latest_message_id())
        message = self._run_op(self._client.send_message(self._entity, text, reply_to=reply_to))
        mid = getattr(message, "id", None)
        if isinstance(mid, int):
            self._sent_ids.append(mid)
        return message

    def expect(self, predicate: Callable[[Any], bool], timeout: float) -> Any | None:
        """Wait up to ``timeout`` seconds for the bot's next INBOUND reply that satisfies
        ``predicate``, polling the chat for messages newer than the high-water mark. Our OWN
        outbound (``message.out``) is skipped — only the bot's replies count. Returns the matching
        message, or ``None`` on timeout (the case classifier reports the honest miss)."""
        self._require_connected()
        return self._run(self._await_reply(predicate, timeout))

    async def _await_reply(self, predicate: Callable[[Any], bool], timeout: float) -> Any | None:
        loop = self._loop
        assert loop is not None
        deadline = loop.time() + timeout
        seen = self._last_seen_id
        while loop.time() < deadline:
            # Bound EACH poll's network fetch at the FIXED op ceiling (the same 30s as send/tap/
            # connect) so a genuinely hung get_messages surfaces as a controlled TimeoutError →
            # BLOCKED, instead of blowing past the window on Telethon's own unbounded retries. We do
            # NOT shrink this cap to the time-left: a HEALTHY fetch that merely runs past a tiny
            # remaining must still complete and have its messages processed — then the deadline check
            # returns the honest None. Shrinking to `remaining` would cut a normal sub-second
            # round-trip near the deadline into a SPURIOUS transport BLOCKED (flapping a real "no
            # reply" FAIL / an Expect-silent PASS into BLOCKED). The deadline loop bounds the TOTAL
            # wait; this per-fetch cap only catches a true stall.
            messages = await asyncio.wait_for(
                self._client.get_messages(self._entity, min_id=seen, limit=_LIVE_FETCH_LIMIT),
                timeout=self._op_timeout_s)
            for message in sorted(messages, key=lambda m: m.id):
                if message.id <= seen:
                    continue
                seen = self._last_seen_id = message.id
                if getattr(message, "out", False):
                    continue  # our own send — advance past it, never match it
                if getattr(message, "action", None) is not None:
                    continue  # a Telegram SERVICE message (join/pin/…), not a bot reply
                if predicate(message):
                    return message
            await asyncio.sleep(self._poll_interval_s)
        # The window elapsed with no matching inbound reply — the honest miss (the classifier turns
        # it into a FAIL / a satisfied Expect-silent), never a transport BLOCKED.
        self._last_seen_id = seen
        return None

    def tap(self, message: Any, button: int | str) -> Any:
        """Tap an inline button on ``message`` (a callback query — the faithful way to exercise a
        bot's q-buttons / plan-approval). ``button`` is the button's LABEL (text) or its 0-based
        index. Returns Telethon's callback result (its ``.message`` is the toast/alert text)."""
        self._require_connected()
        return self._run_op(self._tap(message, button))

    async def _tap(self, message: Any, button: int | str) -> Any:
        if isinstance(button, int):
            return await message.click(i=button)
        return await message.click(text=button)

    def _run(self, coro: Awaitable[Any]) -> Any:
        """Run one client coroutine to completion on the driver's own loop — used only by ``expect``,
        whose ``_await_reply`` bounds itself two ways: it never polls past the caller's deadline AND
        caps each individual network fetch at the op timeout (a hung fetch raises a controlled
        TimeoutError, never a silent hang)."""
        loop = self._loop
        if loop is None:
            raise LiveTierUnavailable(
                "the bot Tier-2 live driver was driven before connect() — call connect() first.",
                exit_code=self._exit_blocked,
            )
        return loop.run_until_complete(coro)

    def _run_op(self, coro: Awaitable[Any]) -> Any:
        """Run one discrete client op (send/tap) bounded by the op timeout, so a network stall on a
        single call can't hang the QA run forever."""
        return self._await(coro, self._op_timeout_s)

    def _await(self, coro: Awaitable[Any], timeout: float) -> Any:
        """Run ``coro`` on the driver's loop with a hard ``timeout`` (a ``TimeoutError`` propagates,
        which connect/the suite runner turn into a controlled BLOCKED)."""
        loop = self._loop
        if loop is None:
            raise LiveTierUnavailable(
                "the bot Tier-2 live driver was driven before connect() — call connect() first.",
                exit_code=self._exit_blocked,
            )
        return loop.run_until_complete(asyncio.wait_for(coro, timeout=timeout))


class LiveWebDriver:
    """The web Tier-2 live driver SKELETON — implements the Tier-1 ``web_harness.PageDriver``
    protocol (``goto``/``click``/``fill``/``text_content``/``current_url``/``screenshot``) against
    a real browser pointed at a LIVE deployed site, so the existing deterministic case-runner
    drives it unchanged. The live wiring (launch agent-browser/Playwright against
    ``REVIEW_QA_WEB_BASE_URL`` + visual capture) lands under #82; until then ``connect`` raises
    ``LiveTierUnavailable``."""

    def __init__(self, base_url: str, *, exit_blocked: int):
        self._base_url = base_url
        self._exit_blocked = exit_blocked

    def connect(self) -> None:
        raise LiveTierUnavailable(
            "the web Tier-2 live-site run is not yet implemented (tracked in #82). The scaffolding "
            "(gate, config path, this PageDriver-compatible seam) is in place; the live run lands "
            "once a test site is provisioned and #82 is built.",
            exit_code=self._exit_blocked,
        )


class LiveExtDriver:
    """The ext Tier-2 live driver SKELETON — implements the Tier-1 ``ext_harness.ExtAutomation``
    protocol and ADDS window-screenshot visual diffing (baseline capture + perceptual diff +
    threshold gate, issue #82's core ask), so the existing case-runner drives a real VS Code and
    the visual op compares against a baseline. The live wiring (CDP screenshot + cvGate diff
    against ``REVIEW_QA_EXT_BASELINE_DIR``) lands under #82; until then ``connect`` raises
    ``LiveTierUnavailable``."""

    def __init__(self, baseline_dir: Path, *, exit_blocked: int):
        self._baseline_dir = baseline_dir
        self._exit_blocked = exit_blocked

    def connect(self) -> None:
        raise LiveTierUnavailable(
            "the ext Tier-2 live VS Code + visual-diff run is not yet implemented (tracked in "
            "#82). The scaffolding (gate, config path, this ExtAutomation-compatible seam) is in "
            "place; the live run lands once #82 is built.",
            exit_code=self._exit_blocked,
        )


# --- the kind -> gate routing (the single seam the dispatch layer calls) ---------------------
_GATES = {
    "bot": bot_live_available,
    "web": web_live_available,
    "ext": ext_live_available,
}

_LIVE_DRIVERS = {
    "bot": BOT_LIVE_DRIVER,
    "web": WEB_LIVE_DRIVER,
    "ext": EXT_LIVE_DRIVER,
}


def is_live_driver(kind: str, driver: str) -> bool:
    """Whether ``driver`` is the Tier-2 LIVE driver for ``kind`` (vs the Tier-1 default). The
    config layer uses this to route a ``tier: live`` block to the gate here instead of the Tier-1
    harness."""
    return _LIVE_DRIVERS.get(kind) == driver


def live_gate_for(kind: str) -> LiveTierGate:
    """The live-tier availability gate for ``kind`` (bot/web/ext). A kind with no live tier
    (backend — its Tier-2 is just the existing un-caged executor against a real stage) returns a
    not-ok gate naming that, so a stray ``tier: live`` on backend fails loud rather than silently."""
    gate = _GATES.get(kind)
    if gate is None:
        return LiveTierGate(False, (
            f"kind={kind!r} has no Tier-2 live driver — only bot/web/ext have a distinct LIVE "
            "tier. (A backend's live run is the existing un-caged executor against a real stage.)"
        ))
    return gate()


def live_driver_for(kind: str, *, exit_blocked: int):
    """The live driver for ``kind`` (bot/web/ext). The BOT driver is the real MTProto driver
    (Telethon) — its ``connect`` opens a live session against the test account read from env
    (``TG_TEST_API_ID/HASH/SESSION/CHAT_ID``); the web/ext drivers are still SKELETONS whose
    ``connect`` raises ``LiveTierUnavailable`` (their live run lands under #82). Each reads its
    target from env at construction. Raises ``LiveTierUnavailable`` for a kind with no live tier
    (e.g. backend)."""
    if kind == "bot":
        return LiveBotDriver(
            api_id=os.environ.get("TG_TEST_API_ID", ""),
            api_hash=os.environ.get("TG_TEST_API_HASH", ""),
            session=os.environ.get("TG_TEST_SESSION", ""),
            chat=os.environ.get("TG_TEST_CHAT_ID", ""),
            exit_blocked=exit_blocked,
        )
    if kind == "web":
        return LiveWebDriver(os.environ.get("REVIEW_QA_WEB_BASE_URL", ""), exit_blocked=exit_blocked)
    if kind == "ext":
        baseline = Path(os.environ.get("REVIEW_QA_EXT_BASELINE_DIR", ""))
        return LiveExtDriver(baseline, exit_blocked=exit_blocked)
    raise LiveTierUnavailable(
        f"kind={kind!r} has no Tier-2 live driver (only bot/web/ext).", exit_code=exit_blocked,
    )


# --- the creds documentation (the SAME source of truth the docs + issue read) ----------------
def _creds_phrase(creds: tuple[str, ...]) -> str:
    """A human phrase listing the required creds for a gate message (kept consistent with the
    docs so a skip message and the docs name the same vars)."""
    return "set " + ", ".join(creds) if creds else "(no extra creds)"


def creds_doc() -> str:
    """The canonical, per-SUT list of the creds/infra the CTO must provision for the live run,
    returned as a markdown block for a ``--help``-style dump. It names the SAME env vars the gates
    name; consistency with ``docs/specs/review-qa.md`` §7.4 is ENFORCED by
    ``tests/test_qa_live.py::test_creds_doc_consistent_with_spec`` (the spec is hand-written, so a
    test pins the two together rather than relying on the prose claim alone)."""
    return _CREDS_DOC


_CREDS_DOC = """\
### bot (real Telegram, MTProto)
- A DEDICATED throwaway test Telegram USER account (its own phone/virtual number), NEVER your
  real account — MTProto automation risks a Telegram ToS ban; burn a throwaway, not your account.
- A test BOT (its own token from @BotFather) and a DEDICATED test chat containing ONLY the test
  account + test bot.
- Env to set:
  - `REVIEW_QA_BOT_LIVE=1`            — opt in to the live tier.
  - `TG_TEST_API_ID` / `TG_TEST_API_HASH` — the test account's app credentials (my.telegram.org).
  - `TG_TEST_SESSION`                 — a Telethon StringSession for the test USER account.
  - `TG_TEST_CHAT_ID`                 — the dedicated test chat id (MUST NOT equal real TG_CHAT_ID;
                                         the gate fails closed if it does).
- Dep: `pip install telethon` (a qa-harness dep, not a tg-cli dep).

### web (real browser, live site)
- A deployed test SITE URL to drive (a stage, not production).
- Env to set:
  - `REVIEW_QA_WEB_LIVE=1`            — opt in to the live tier.
  - `REVIEW_QA_WEB_BASE_URL`          — the test site URL (e.g. https://stage.example.test).
- Runtime: Playwright + a browser (`pip install playwright && python -m playwright install
  chromium`) OR the `agent-browser` CLI on PATH.

### ext (real VS Code, window-screenshot visual diffing)
- Env to set:
  - `REVIEW_QA_EXT_LIVE=1`            — opt in to the live tier.
  - `REVIEW_QA_VSCODE=1`              — the underlying Tier-1 ext gate (real VS Code).
  - `REVIEW_QA_EXT_BASELINE_DIR`      — a writable dir for baseline screenshots (first run records,
                                         later runs diff against them).
- Runtime: node/tsx (NOT bun — bun hangs Electron launch on macOS), a VS Code binary
  (`VSCODE_PATH` or `code` on PATH), and ImageMagick v7 (`magick`) for the perceptual diff.
"""
