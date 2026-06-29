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

import os
from dataclasses import dataclass
from pathlib import Path


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


# --- the live-driver SKELETONS (wired behind the Tier-1 protocol seams; live run = #82) -------
class LiveBotDriver:
    """The bot Tier-2 live driver SKELETON — drives a real test Telegram USER account over MTProto
    (Telethon) as the human caller, behind the same inbound/outbound seam the Tier-1 hermetic
    driver speaks. The live run (open the MTProto session, ``send``/``tap``/``expect`` against the
    real account) is implemented under #82; until then ``connect`` raises ``LiveTierUnavailable``
    so a misrouted call BLOCKS rather than silently doing nothing.

    The CONTRACT is fixed here (spec §7.3 Tier 2): ``send(text, reply_to, thread_id, media)``,
    ``tap(message, button)`` (callback queries — the faithful way to exercise q-buttons /
    plan-approval), ``expect(predicate, timeout)``. Pinning the contract now is what makes the
    live run a drop-in."""

    def __init__(self, *, exit_blocked: int):
        self._exit_blocked = exit_blocked

    def connect(self) -> None:
        raise LiveTierUnavailable(
            "the bot Tier-2 live MTProto run is not yet implemented (tracked in #82). The "
            "scaffolding (gate, config path, this driver seam) is in place; the live run lands "
            "once the test-account creds are provisioned and #82 is built.",
            exit_code=self._exit_blocked,
        )


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
    """The live-driver SKELETON for ``kind`` (bot/web/ext). Each driver's ``connect`` currently
    raises ``LiveTierUnavailable`` (the live run lands under #82); the factory exists so the
    dispatch layer can reach the seam uniformly and the live impl is a drop-in once built. The
    web/ext drivers read their target (site URL / baseline dir) from env at construction. Raises
    ``LiveTierUnavailable`` for a kind with no live tier (e.g. backend)."""
    if kind == "bot":
        return LiveBotDriver(exit_blocked=exit_blocked)
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
