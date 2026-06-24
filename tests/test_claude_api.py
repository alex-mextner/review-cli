#!/usr/bin/env python3
"""Unit tests for the claude backend's API variant + api/cli dispatch (backends.py).

review_claude() dispatches to review_claude_api (HTTP, no CLI needed) or
review_claude_cli (claude-p), chosen by REVIEW_CLAUDE_MODE or, automatically, by
whether an Anthropic-compatible key is configured. The API path POSTs the
Anthropic Messages format to {ANTHROPIC_BASE_URL}/v1/messages. These tests stub
urlopen and the sub-backends — no network, no claude binary, no real key.

Same harness style as tests/test_streaming.py: plain test_* run by __main__.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import backends as b  # noqa: E402
from reviewlib.backends import ReviewResult  # noqa: E402

_ANTHROPIC_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                  "ANTHROPIC_MODEL", "ANTHROPIC_MAX_TOKENS", "REVIEW_CLAUDE_MODE")


class _Env:
    """Isolate the Anthropic/dispatch env vars and neutralise the .env file
    fallback (_resolve_key reads ~/.config/review-cli/.env unless GEMINI_ENV_FILE
    points elsewhere), so the host's real config can't leak into a test."""
    def __init__(self, **vals):
        self.vals = vals

    def __enter__(self):
        self.saved = {k: os.environ.get(k) for k in (*_ANTHROPIC_ENV, "GEMINI_ENV_FILE")}
        for k in _ANTHROPIC_ENV:
            os.environ.pop(k, None)
        os.environ["GEMINI_ENV_FILE"] = "/nonexistent-review-test-env"
        for k, v in self.vals.items():
            os.environ[k] = v
        return self

    def __exit__(self, *a):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


class _FakeResp:
    def __init__(self, body: bytes):
        self._b = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b


def _stub(captured):
    saved_open = b.urllib.request.urlopen

    def fake(req, timeout=None):
        captured["req"] = req
        captured["timeout"] = timeout
        return _FakeResp(json.dumps(captured["body"]).encode("utf-8"))

    b.urllib.request.urlopen = fake
    return lambda: setattr(b.urllib.request, "urlopen", saved_open)


def _hdrs(req):
    return {k.lower(): v for k, v in req.header_items()}


# ---- dispatch -------------------------------------------------------------

def _which_cli(cli_present: bool):
    """A _which_optional stub: report the claude CLI binaries present-or-absent. The claude
    dispatch + availability resolve the CLI via backends._which_optional (review-cli#76), so
    the presence of a CLI seat is simulated here — patching the backends-local indirection,
    NOT the stdlib shutil.which globally."""
    def which(name):
        if name in ("claude", "claude-p"):
            return f"/bin/{name}" if cli_present else None
        return None
    return which


def _mark_dispatch(cli_present: bool):
    """Stub both sub-backends (return 'API'/'CLI' markers) AND claude CLI presence."""
    saved_api, saved_cli, saved_which = b.review_claude_api, b.review_claude_cli, b._which_optional
    b.review_claude_api = lambda *a, **k: ReviewResult("api", "api", 0, "API", "")
    b.review_claude_cli = lambda *a, **k: ReviewResult("cli", "cli", 0, "CLI", "")
    b._which_optional = _which_cli(cli_present)

    def restore():
        b.review_claude_api, b.review_claude_cli, b._which_optional = saved_api, saved_cli, saved_which
    return restore


def _dispatch(cli_present, **env):
    with _Env(**env):
        restore = _mark_dispatch(cli_present)
        try:
            return b.review_claude("claude:opus", "p", "", Path("."), 5).stdout
        finally:
            restore()


def test_dispatch_auto_uses_api_when_no_cli_but_key_set():
    assert _dispatch(cli_present=False, ANTHROPIC_API_KEY="user_x") == "API"


def test_dispatch_auto_prefers_cli_when_cli_present_even_with_key():
    # the regression guard: a key in the env must NOT silently switch a working
    # CLI host to the paid API.
    assert _dispatch(cli_present=True, ANTHROPIC_API_KEY="user_x") == "CLI"


def test_dispatch_auto_uses_cli_when_no_key():
    assert _dispatch(cli_present=True) == "CLI"


def test_dispatch_mode_cli_forces_cli_even_with_key_and_no_binary():
    assert _dispatch(cli_present=False, ANTHROPIC_API_KEY="user_x", REVIEW_CLAUDE_MODE="cli") == "CLI"


def test_dispatch_mode_api_forces_api_even_with_cli_present():
    assert _dispatch(cli_present=True, REVIEW_CLAUDE_MODE="api") == "API"


# ---- API request shape + parsing -----------------------------------------

def test_api_request_shape_and_parse_xapikey():
    captured = {"body": {"content": [{"type": "text", "text": "verdict here"}],
                         "usage": {"input_tokens": 11, "output_tokens": 7}}}
    with _Env(ANTHROPIC_API_KEY="user_x", ANTHROPIC_BASE_URL="https://api.commandcode.ai/provider"):
        undo = _stub(captured)
        try:
            res = b.review_claude_api("claude:claude-opus-4-8", "Question?", "the diff", Path("."), 30)
        finally:
            undo()
    req = captured["req"]
    assert req.full_url == "https://api.commandcode.ai/provider/v1/messages", req.full_url
    assert req.method == "POST"
    h = _hdrs(req)
    assert h["x-api-key"] == "user_x", h
    assert h["anthropic-version"] == "2023-06-01", h
    assert h.get("user-agent"), "must send a non-default UA (Cloudflare 1010 otherwise)"
    body = json.loads(req.data)
    assert body["model"] == "claude-opus-4-8"
    assert body["messages"][0]["role"] == "user"
    assert "the diff" in body["messages"][0]["content"]  # diff folded into the prompt
    assert "verdict here" in res.stdout and res.returncode == 0
    assert "output_tokens=7" in res.stdout


def test_api_uses_bearer_with_auth_token():
    captured = {"body": {"content": [{"type": "text", "text": "ok"}]}}
    with _Env(ANTHROPIC_AUTH_TOKEN="tok123"):
        undo = _stub(captured)
        try:
            b.review_claude_api("claude:opus", "p", "", Path("."), 30)
            req = captured["req"]
        finally:
            undo()
    h = _hdrs(req)
    assert h.get("authorization") == "Bearer tok123", h
    assert "x-api-key" not in h, h


def test_api_http_error_is_surfaced():
    with _Env(ANTHROPIC_API_KEY="user_x"):
        saved = b.urllib.request.urlopen

        def boom(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {},
                                         __import__("io").BytesIO(b'{"error":"bad key"}'))
        b.urllib.request.urlopen = boom
        try:
            res = b.review_claude_api("claude:opus", "p", "", Path("."), 30)
        finally:
            b.urllib.request.urlopen = saved
    assert res.returncode == 401 and "bad key" in res.stderr, res


def test_api_empty_text_is_failure():
    captured = {"body": {"content": [{"type": "text", "text": "   "}], "usage": {}}}
    with _Env(ANTHROPIC_API_KEY="user_x"):
        undo = _stub(captured)
        try:
            res = b.review_claude_api("claude:opus", "p", "", Path("."), 30)
        finally:
            undo()
    assert res.returncode != 0, res  # empty success -> failure, for moderator fallback


def test_api_no_key_returns_error_not_crash():
    with _Env(REVIEW_CLAUDE_MODE="api"):  # forced api, but no key configured
        res = b.review_claude_api("claude:opus", "p", "", Path("."), 30)
    assert res.returncode == 1 and "no ANTHROPIC" in res.stderr, res


def test_backend_available_with_key_and_no_cli():
    with _Env(ANTHROPIC_API_KEY="user_x"):
        saved = b._which_optional
        b._which_optional = _which_cli(cli_present=False)
        try:
            assert b.backend_available("claude:claude-opus-4-8") is True
        finally:
            b._which_optional = saved


def test_backend_available_false_without_key_or_cli():
    with _Env():
        saved = b._which_optional
        b._which_optional = _which_cli(cli_present=False)
        try:
            assert b.backend_available("claude:claude-opus-4-8") is False
        finally:
            b._which_optional = saved


def _avail_with(cli_present, **env):
    with _Env(**env):
        saved = b._which_optional
        b._which_optional = _which_cli(cli_present)
        try:
            return b.backend_available("claude:claude-opus-4-8")
        finally:
            b._which_optional = saved


def test_backend_available_mirrors_forced_mode():
    # mode=api: available only if a key exists, regardless of the CLI
    assert _avail_with(cli_present=True, REVIEW_CLAUDE_MODE="api") is False
    assert _avail_with(cli_present=True, REVIEW_CLAUDE_MODE="api", ANTHROPIC_API_KEY="user_x") is True
    # mode=cli: available only if the binary exists, regardless of a key
    assert _avail_with(cli_present=False, REVIEW_CLAUDE_MODE="cli", ANTHROPIC_API_KEY="user_x") is False
    assert _avail_with(cli_present=True, REVIEW_CLAUDE_MODE="cli") is True


def test_api_malformed_json_is_failure_not_crash():
    with _Env(ANTHROPIC_API_KEY="user_x"):
        saved = b.urllib.request.urlopen

        def fake(req, timeout=None):
            return _FakeResp(b"<html>not json</html>")
        b.urllib.request.urlopen = fake
        try:
            res = b.review_claude_api("claude:opus", "p", "", Path("."), 30)
        finally:
            b.urllib.request.urlopen = saved
    assert res.returncode == 1 and "malformed" in res.stderr.lower(), res


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failures else 0)
