"""The web Tier-1 DETERMINISTIC harness: bring a web app up, health-gate it reachable, then
drive it in a real headless browser (Playwright/Chromium) against a prose ``## Case:`` suite —
no flaky network, no un-caged agent (spec docs/specs/review-qa.md §7.1, Tier 1).

WHY A SEPARATE, DETERMINISTIC PATH (not the un-caged executor). The backend/ext testers need
an un-caged write/exec agent to drive a live system. A Tier-1 web test does NOT: "goto this
URL -> click this -> expect this text/url" is a fully mechanical assertion the moment the app
is reachable and a browser can drive the DOM. So web Tier-1 runs as DETERMINISTIC Python — it
boots the app's dev server, waits for it to answer, opens a headless Chromium page, and
classifies each ``## Case:`` block by running its actions and matching the resulting DOM. This
keeps the path off the blast radius of an un-caged agent entirely (the agent cage is a real
boundary the spec went to lengths to keep — web Tier-1 simply doesn't need to remove it), and
makes the run reproducible with zero model spend.

THE TWO-LAYER SPLIT (load-bearing for testability). Playwright is a heavy dependency that needs
a browser binary installed; requiring it in normal CI would make the harness's CORE logic
untestable without an install. So the driver is decoupled from Playwright behind a tiny
``PageDriver`` protocol (``goto`` / ``click`` / ``fill`` / ``text_content`` / ``current_url`` /
``screenshot``). The deterministic CASE-RUNNER + the prose-grammar PARSER + the ``## QA RESULTS``
EMITTER (in ``web_driver.py``) speak only that protocol, so they are unit-tested against an
IN-MEMORY fake page with no browser. ``PlaywrightPage`` is the real, browser-backed
implementation, gated behind ``REVIEW_QA_PLAYWRIGHT=1`` + an installed-browser check; when the
flag is off or the browser is missing, a web run SKIPs LOUDLY (a controlled BLOCKED with the
install command) rather than crashing.

SAFETY. The dev server is booted in its OWN process group so the whole tree can be reaped, and
the browser context is always closed (try/finally) — a web run leaks neither a server nor a
browser. The browser is launched headless with a short navigation timeout, so a hung page can
never wedge the run.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.error import URLError
from urllib.request import urlopen

# How much of the dev server's stdout tail to retain for a BLOCKED-report proof. A drain thread
# keeps the OS pipe empty (so a chatty server never blocks on a full pipe before it starts
# serving); we hold only the last few KiB, plenty for a crash/traceback tail.
_OUTPUT_TAIL_BYTES = 16384


def _env_float(name: str, default: float) -> float:
    """A float from ``name`` in the environment, falling back to ``default`` on absent OR a
    non-numeric value. A bad env value must NOT crash at module import (which would wedge the
    whole qa import chain on a typo — review finding); it degrades to the default."""
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


# How long a single browser navigation / action may take before it is abandoned. A hung page
# (an infinite redirect, a never-resolving fetch) must not wedge the whole run; the action
# fails the case with a clear timeout instead. Overridable so a slow app gets more room.
NAV_TIMEOUT_MS = int(_env_float("REVIEW_QA_WEB_NAV_TIMEOUT_S", 15.0) * 1000)
# Poll granularity while waiting for the dev server to answer the health gate.
_HEALTH_POLL_S = 0.1


# --- the PageDriver protocol (the seam the deterministic driver speaks) ----------------
class PageDriver(Protocol):
    """The minimal browser-page interface the deterministic web driver drives. Implemented by
    the real ``PlaywrightPage`` and by the in-memory test fake — so the case-runner logic is
    unit-testable with no browser. Every method is synchronous and either succeeds or raises a
    ``WebActionError`` the driver turns into a case FAIL.

    Selectors are Playwright selector strings (a CSS selector, or ``text=...``); ``goto`` takes
    an absolute or base-relative URL. ``text_content`` returns the visible text of the whole page
    (the body) so an ``Expect-text:`` substring match is a simple containment check."""

    def goto(self, url: str) -> None: ...
    def click(self, selector: str) -> None: ...
    def fill(self, selector: str, value: str) -> None: ...
    def text_content(self) -> str: ...
    def current_url(self) -> str: ...
    def screenshot(self, path: Path) -> bool: ...


class WebActionError(RuntimeError):
    """A browser action failed (selector not found, navigation timed out, …). The driver turns
    it into a case FAIL with the message as the proof line — never lets it escape as a
    traceback."""


class WebHarnessError(RuntimeError):
    """A controlled web-harness failure (could not boot the app / browser unavailable). Carries
    the qa exit class the handler should return so a harness infra failure maps to a stable code
    (NOT a found bug — that is report-only)."""

    def __init__(self, message: str, *, exit_code: int):
        super().__init__(message)
        self.exit_code = exit_code


# --- the real Playwright-backed page (gated; heavy) ------------------------------------
def playwright_available() -> tuple[bool, str]:
    """Whether a real Playwright run is GATED ON: the ``REVIEW_QA_PLAYWRIGHT`` flag is on AND the
    ``playwright`` package imports. Returns ``(ok, reason)`` — ``reason`` is the actionable
    skip/install message when ``ok`` is False, so a web run that cannot use a browser BLOCKS
    loudly with the fix rather than crashing.

    NOTE: this checks the flag + the PACKAGE, not the browser BINARY — a missing Chromium is
    caught later at ``chromium.launch`` and turned into a controlled ``WebHarnessError`` /
    BLOCKED (with the same install command), so a half-installed Playwright still fails cleanly,
    not with a traceback.

    The flag gate is first and deliberate: Playwright is heavy and pulls a browser binary, so a
    plain ``review qa --kind web`` does NOT silently try to launch a browser — the user opts in
    with ``REVIEW_QA_PLAYWRIGHT=1`` (mirroring how the bot harness keeps its heavy bits behind a
    clear switch). With the flag off, the harness's pure logic is still fully exercised by the
    unit tests against the fake page."""
    if not _flag_enabled("REVIEW_QA_PLAYWRIGHT"):
        return False, (
            "the web Tier-1 harness needs a real headless browser, which is OFF by default. "
            "Set REVIEW_QA_PLAYWRIGHT=1 to enable it, and install the browser once with: "
            "pip install playwright && python -m playwright install chromium"
        )
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False, (
            "REVIEW_QA_PLAYWRIGHT=1 is set but the `playwright` package is not installed. "
            "Install it with: pip install playwright && python -m playwright install chromium"
        )
    return True, ""


def _flag_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
        "off",
    )


class PlaywrightPage:
    """A real headless-Chromium page driven over Playwright, implementing ``PageDriver``.

    Lifecycle is owned by ``browser_session`` (a context manager) — it launches Chromium, opens
    a page, yields this driver, and ALWAYS closes the browser + the Playwright runtime. A short
    ``NAV_TIMEOUT_MS`` is set on the page so a hung navigation fails fast. ``base_url`` lets a
    case use a relative ``Goto: /login`` (resolved against it); an absolute URL is used as-is.

    Every method maps a high-level action to Playwright and raises ``WebActionError`` on failure
    (a selector miss, a navigation timeout) so the driver classifies the case honestly instead
    of leaking a Playwright exception."""

    def __init__(self, page: object, base_url: str):
        self._page = page
        self._base_url = base_url.rstrip("/")

    def _resolve(self, url: str) -> str:
        if url.startswith(("http://", "https://")):
            return url
        if not url.startswith("/"):
            url = "/" + url
        return f"{self._base_url}{url}"

    def goto(self, url: str) -> None:
        try:
            self._page.goto(
                self._resolve(url), wait_until="load", timeout=NAV_TIMEOUT_MS
            )
        except Exception as exc:  # noqa: BLE001 — any Playwright error becomes a case FAIL
            raise WebActionError(
                f"navigation to {url!r} failed: {_short(exc)}"
            ) from exc

    def click(self, selector: str) -> None:
        try:
            self._page.click(selector, timeout=NAV_TIMEOUT_MS)
            # A click that triggers navigation (a link, a submit) leaves the next read of
            # url/text racing the navigation commit (review finding — gated-live flake). Wait
            # for the page to settle after the click so a subsequent Expect-url / Expect-text
            # sees the post-navigation state, not the pre-click one. A click that does NOT
            # navigate settles immediately, so this is a no-op for it.
            self._page.wait_for_load_state("load", timeout=NAV_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001
            raise WebActionError(
                f"could not click selector {selector!r}: {_short(exc)}"
            ) from exc

    def fill(self, selector: str, value: str) -> None:
        try:
            self._page.fill(selector, value, timeout=NAV_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001
            raise WebActionError(
                f"could not fill selector {selector!r}: {_short(exc)}"
            ) from exc

    def text_content(self) -> str:
        try:
            return self._page.inner_text("body") or ""
        except Exception as exc:  # noqa: BLE001
            raise WebActionError(f"could not read page text: {_short(exc)}") from exc

    def current_url(self) -> str:
        try:
            return str(self._page.url)
        except Exception as exc:  # noqa: BLE001
            raise WebActionError(f"could not read page url: {_short(exc)}") from exc

    def screenshot(self, path: Path) -> bool:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._page.screenshot(path=str(path))
            return True
        except Exception:  # noqa: BLE001 — a screenshot is best-effort evidence, never fatal
            return False


def _short(exc: object, *, limit: int = 200) -> str:
    """A short, single-line form of an exception message for a proof line (Playwright errors are
    multi-line and verbose; the case proof wants the gist, not the stack)."""
    text = (
        str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    )
    return text[:limit]


class _PlaywrightSession:
    """Owns the Playwright runtime + browser + page for one web run. Created via
    ``browser_session``; ``__exit__`` closes everything (page-context, browser, runtime) so a run
    leaks no browser process. Launch failure raises ``WebHarnessError`` (the browser was meant to
    be available — ``playwright_available`` gated us here — so a launch crash is a controlled
    BLOCKED, not a traceback)."""

    def __init__(self, base_url: str, *, exit_blocked: int):
        self._base_url = base_url
        self._exit_blocked = exit_blocked
        self._pw = None
        self._browser = None
        self._context = None

    def __enter__(self) -> PlaywrightPage:
        from playwright.sync_api import sync_playwright

        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            self._context = self._browser.new_context()
            page = self._context.new_page()
            page.set_default_timeout(NAV_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001 — a browser-launch failure is a controlled BLOCKED
            self.__exit__(None, None, None)
            raise WebHarnessError(
                f"could not launch headless Chromium: {_short(exc)}. Install the browser with: "
                "python -m playwright install chromium",
                exit_code=self._exit_blocked,
            ) from exc
        return PlaywrightPage(page, self._base_url)

    def __exit__(self, *_exc) -> None:
        for closer in (
            lambda: self._context and self._context.close(),
            lambda: self._browser and self._browser.close(),
            lambda: self._pw and self._pw.stop(),
        ):
            try:
                closer()
            except Exception:  # noqa: BLE001 — teardown is best-effort, never raises
                pass
        self._context = self._browser = self._pw = None


def browser_session(base_url: str, *, exit_blocked: int) -> _PlaywrightSession:
    """A context manager yielding a real ``PlaywrightPage`` and guaranteeing browser teardown.
    Call ``playwright_available()`` FIRST — this assumes the runtime + browser are present (it
    is only reached on the real path)."""
    return _PlaywrightSession(base_url, exit_blocked=exit_blocked)


# --- bringing the web app's dev server up ----------------------------------------------
@dataclass
class WebServer:
    """A running dev-server subprocess (the SUT's web app), plus the plan to reap it.

    Reaping sends the process GROUP a SIGTERM then SIGKILL (a dev server commonly forks workers /
    a watcher child); idempotent and never raises so it is safe from a finally. A daemon thread
    drains stdout continuously into a bounded in-memory tail so the OS pipe never fills (a chatty
    server that out-logged the ~64 KiB pipe buffer before serving would otherwise block on its
    next write — a false BLOCKED). ``output_tail()`` returns that buffer for a boot-failure proof.
    """

    proc: subprocess.Popen
    _reaped: bool = False
    _reaper_handle: tuple[subprocess.Popen, int | None] | None = None

    def __post_init__(self) -> None:
        self._tail: deque[str] = deque()
        self._tail_len = 0
        self._tail_lock = threading.Lock()
        self._drain_thread: threading.Thread | None = None
        if self.proc.stdout is not None:
            self._drain_thread = threading.Thread(
                target=self._drain_stdout, daemon=True
            )
            self._drain_thread.start()

    def _drain_stdout(self) -> None:
        stream = self.proc.stdout
        if stream is None:
            return
        try:
            for (
                line
            ) in stream:  # blocks per line; EOF ends the loop when the server exits
                with self._tail_lock:
                    self._tail.append(line)
                    self._tail_len += len(line)
                    while self._tail_len > _OUTPUT_TAIL_BYTES and self._tail:
                        self._tail_len -= len(self._tail.popleft())
        except (OSError, ValueError):
            pass

    def output_tail(self, *, limit: int = 2000) -> str:
        with self._tail_lock:
            text = "".join(self._tail)
        return text[-limit:] or "(no output captured)"

    def reap(self) -> None:
        if self._reaped:
            return
        self._reaped = True
        _terminate_group(self.proc)
        if self._drain_thread is not None:
            self._drain_thread.join(timeout=2)
        if self._reaper_handle is not None:
            from ..process import unregister_external_child

            unregister_external_child(self._reaper_handle)
            self._reaper_handle = None


def _terminate_group(proc: subprocess.Popen) -> None:
    """SIGTERM then (after a grace) SIGKILL the dev server's whole process GROUP, so a forked
    worker/watcher can't outlive the run. Captures the pgid up front so a reaped leader doesn't
    make ``getpgid`` fail; signals the GROUP even after the leader exits (a wrapper that
    backgrounds the real server and exits would otherwise leak it). Best-effort; never raises."""
    pgid = _pgid_of(proc)
    leader_alive = proc.poll() is None
    if pgid is None and not leader_alive:
        return
    _signal_group_or_proc(proc, pgid, signal.SIGTERM)
    if leader_alive:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    else:
        time.sleep(0.2)
    _signal_group_or_proc(proc, pgid, signal.SIGKILL)


def _pgid_of(proc: subprocess.Popen) -> int | None:
    try:
        return os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        return proc.pid if proc.pid else None


def _signal_group_or_proc(proc: subprocess.Popen, pgid: int | None, sig: int) -> None:
    if pgid is not None:
        try:
            os.killpg(pgid, sig)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        if sig == signal.SIGKILL:
            proc.kill()
        else:
            proc.terminate()
    except (OSError, ValueError):
        pass


def boot_web_server(
    *,
    command: list[str],
    cwd: Path,
    extra_env: dict[str, str] | None = None,
    exit_boot_failed: int,
) -> WebServer:
    """Spawn the SUT's dev server in its OWN process group (so the whole tree can be reaped).
    Returns a ``WebServer`` whose ``reap`` tears it down. Raises ``WebHarnessError`` if the
    process cannot be launched at all (bad command / missing binary)."""
    env = dict(os.environ)
    env.update(extra_env or {})
    try:
        proc = subprocess.Popen(  # noqa: S603 — command resolved from the SUT's own qa.yaml
            command,
            cwd=str(cwd),
            env=env,
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except (OSError, ValueError) as exc:
        raise WebHarnessError(
            f"could not launch the web dev server {command!r} in {cwd}: {exc}",
            exit_code=exit_boot_failed,
        ) from exc
    # Registered with the SAME signal-reaper/backstop registry a backend model child
    # uses (codex review, review-cli#162 follow-up) — without this, an external
    # SIGTERM/SIGINT (or the internal backstop) reaped only `_run_streamed`'s children
    # and left this SUT process group behind, since `WebServer.reap()`'s own teardown
    # only runs from a live interpreter's normal control flow, not from a raw signal.
    from ..process import register_external_child

    handle = register_external_child(proc)
    server = WebServer(proc=proc)
    server._reaper_handle = handle
    return server


def wait_until_reachable(
    url: str, *, timeout_s: float, server: WebServer | None = None
) -> bool:
    """Poll ``url`` (bounded) until it answers an HTTP 2xx/3xx, returning True the moment it
    does. The health gate before any case runs: a dev server that never comes up makes the run a
    controlled BLOCKED instead of driving a browser at a dead address. If ``server`` is given and
    its process EXITS while we wait, return False early (the server crashed — no point polling a
    dead boot). A 4xx/5xx still counts as "reachable" (the server answered) — the app is up, the
    page just has its own status; the case assertions catch a real error page."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if server is not None and server.proc.poll() is not None:
            return False
        if _http_reachable(url):
            return True
        time.sleep(_HEALTH_POLL_S)
    return False


def _http_reachable(url: str) -> bool:
    """True if ``url`` answers any HTTP status (the server is up). A connection refused / DNS
    failure is "not yet up" (False); an HTTP error STATUS (4xx/5xx) still means the server
    answered, so it is up (True)."""
    try:
        with urlopen(url, timeout=3) as resp:  # noqa: S310 — health probe of the SUT's own server
            return 200 <= resp.status < 600
    except URLError as exc:
        # An HTTPError (a subclass of URLError) means the server ANSWERED with a status — it is up.
        return hasattr(exc, "status") or hasattr(exc, "code")
    except (OSError, ValueError):
        return False
