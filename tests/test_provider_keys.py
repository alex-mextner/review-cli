#!/usr/bin/env python3
"""Unit tests for the keyed OpenAI-compatible provider backends (HYP-741).

Covers the two new backends added to reviewlib.backends:
  * z.ai (Zhipu / GLM) — OpenAI-compatible /chat/completions, keyed via
    ZAI_API_KEY / ZHIPU_API_KEY.
  * commandcode — Command Code's OpenAI-compatible Provider API
    (https://api.commandcode.ai/provider/v1/chat/completions), keyed via
    COMMANDCODE_API_KEY ONLY (a `user_...` token; a DeepSeek/legacy key must NOT
    resolve here — it would leak that credential to the wrong host).

Proven here, all offline (urllib.request.urlopen faked — NO real network):
  (a) resolve_backend routes zai/glm/z.ai/zhipu + zai:<model> + glm: → review_zai,
      and commandcode (+ legacy common-code/common_code) + commandcode:<model> →
      review_commandcode;
  (b) the request body is the OpenAI shape ({"model","messages":[{role,content}]})
      with an Authorization: Bearer header — NOT the gemini contents/parts shape;
  (c) the key resolves from a temp .env via GEMINI_ENV_FILE (the shared
      ~/.config/review-cli/.env surface), reusing _resolve_key;
  (d) backend_available reflects key presence (True with key, False without),
      never crashing.

These tests run as a plain script (mirroring tests/test_streaming.py): each
`test_*` function is invoked by the __main__ block below, no pytest required.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import io
import urllib.request
import urllib.error
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Make the in-repo package importable without an install (mirrors the bin/review shim).
sys.path.insert(0, str(REPO_ROOT))

import reviewlib.backends as backends  # noqa: E402
from reviewlib.config import (  # noqa: E402
    ASTRA_SEAT,
    SOL_SEAT,
    SONNET_SEAT,
    TERRA_SEAT,
    BoardReviewer,
    split_pool_reserve,
    _expand_alias,
)

_KEY_ENV_NAMES = (
    "ZAI_API_KEY",
    "ZHIPU_API_KEY",
    "COMMANDCODE_API_KEY",
    "COMMON_CODE_API_KEY",
    "DEEPSEEK_API_KEY",
    "ZAI_MODEL",
    "ZAI_BASE_URL",
    "COMMANDCODE_MODEL",
    "COMMANDCODE_BASE_URL",
    "REVIEW_COMMANDCODE_MODE",
    "REVIEW_ZAI_MODE",
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_MODEL",
    "OPENROUTER_BASE_URL",
    "OPENROUTER_HTTP_REFERER",
    "OPENROUTER_X_TITLE",
    "REVIEW_OPENROUTER_MODE",
    "GEMINI_ENV_FILE",
    # opencode per-provider probe (review-cli#94)
    "ANTHROPIC_API_KEY",
    "GOOGLE_AI_API_KEY",
    "GOOGLE_GENERATIVE_AI_API_KEY",
    "GROQ_API_KEY",
    "MISTRAL_API_KEY",
    "XAI_API_KEY",
    "TOGETHER_API_KEY",
    "OC_AUTH_FILE",
    "OC_CONFIG_FILE",
    "FIREWORKS_API_KEY",
    "FIREWORKS_BASE_URL",
    "REVIEW_UNPAID_PROVIDERS",
)


class _EnvSandbox:
    """Snapshot + restore the provider-related env vars so tests don't leak into
    one another (or pick up a real key from the dev machine)."""

    def __enter__(self):
        self._saved = {name: os.environ.get(name) for name in _KEY_ENV_NAMES}
        self._saved_config_unpaid = backends._CONFIG_UNPAID_PROVIDERS
        for name in _KEY_ENV_NAMES:
            os.environ.pop(name, None)
        # Point the .env fallback at a path that does not exist so, by default,
        # no key resolves unless a test sets one explicitly.
        os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
        backends.configure_unpaid_providers(None)
        with backends._PAYMENT_PREFLIGHT_CACHE_LOCK:
            backends._PAYMENT_PREFLIGHT_CACHE.clear()
        return self

    def __exit__(self, *exc):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        backends.configure_unpaid_providers(self._saved_config_unpaid)
        with backends._PAYMENT_PREFLIGHT_CACHE_LOCK:
            backends._PAYMENT_PREFLIGHT_CACHE.clear()
        return False


class _FakeResp:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _fake_urlopen(captured: dict, payload: dict):
    def _inner(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _FakeResp(payload)

    return _inner


def _http_error(url: str, code: int, body: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url, code, "preflight failed", {}, io.BytesIO(body.encode("utf-8"))
    )


def _allow_preflight(req, timeout=None):
    if req.full_url.endswith("/models"):
        return _FakeResp({"data": []})
    raise AssertionError(
        f"unexpected provider dispatch in preflight stub: {req.full_url}"
    )


# === resolve_backend routing ====================================================
def test_resolve_backend_routes_zai():
    for name in ("zai", "z.ai", "zhipu", "glm", "ZAI", "Glm"):
        assert backends.resolve_backend(name) is backends.review_zai, name
    assert backends.resolve_backend("zai:glm-4.6") is backends.review_zai
    # The dotted spelling with a suffix must route too (codex P2 regression).
    assert backends.resolve_backend("z.ai:glm-4.6") is backends.review_zai
    assert backends.resolve_backend("glm:glm-4.5") is backends.review_zai
    assert backends.resolve_backend("zhipu:glm-4.6") is backends.review_zai


def test_resolve_backend_routes_commandcode():
    for name in ("commandcode", "command-code", "command_code", "CommandCode"):
        assert backends.resolve_backend(name) is backends.review_commandcode, name
    assert (
        backends.resolve_backend("commandcode:deepseek/deepseek-coder")
        is backends.review_commandcode
    )
    # Legacy common-code spellings still resolve (back-compat alias on resolve_backend).
    for legacy in ("common-code", "common_code", "commoncode", "Common-Code"):
        assert backends.resolve_backend(legacy) is backends.review_commandcode, legacy
    assert (
        backends.resolve_backend("common-code:deepseek-coder")
        is backends.review_commandcode
    )


def test_existing_routes_unchanged():
    # The new prefixes must not steal codex/gemini/claude/opencode routing.
    assert backends.resolve_backend("codex") is backends.review_codex
    assert backends.resolve_backend("gemini") is backends.review_gemini
    assert backends.resolve_backend("claude:claude-fable-5") is backends.review_claude
    assert backends.resolve_backend("oc:fireworks/x") is backends.review_opencode
    # An unknown model still defaults to opencode (unchanged behaviour).
    assert backends.resolve_backend("totally-unknown") is backends.review_opencode


def test_aliases_expand():
    # `glm` and `glm52` point at the NEWEST GLM (glm-5.2, reachable on the Coding-Plan
    # endpoint); the rest pin specific older releases.
    assert _expand_alias("glm") == "zai:glm-5.2"
    assert _expand_alias("glm52") == "zai:glm-5.2"
    assert _expand_alias("glm51") == "zai:glm-5.1"
    assert _expand_alias("glm47") == "zai:glm-4.7"
    assert _expand_alias("glm46") == "zai:glm-4.6"
    assert _expand_alias("glm45") == "zai:glm-4.5"
    # commandcode aliases (incl. the short `cc` and the legacy `commoncode`).
    assert _expand_alias("commandcode") == "commandcode"
    assert _expand_alias("commoncode") == "commandcode"
    assert _expand_alias("cc") == "commandcode"
    # Pre-existing aliases survive.
    assert _expand_alias("fable5") == "claude:claude-fable-5"
    # Sol/Astra: pinned so a bare `-m sol`/`-m astra` doesn't fall through
    # `_match_named_backend` to the opencode catch-all (the same failure class `opus`
    # was pinned against, config.py's MODEL_ALIASES comment).
    assert _expand_alias("sol") == SOL_SEAT
    assert _expand_alias("gpt56sol") == SOL_SEAT
    assert _expand_alias("astra") == ASTRA_SEAT
    assert _expand_alias("gpt6astra") == ASTRA_SEAT
    # Terra/Sonnet (review-cli#382): pinned for the same reason as Sol/Astra above.
    assert _expand_alias("terra") == TERRA_SEAT
    assert _expand_alias("gpt56terra") == TERRA_SEAT
    assert _expand_alias("sonnet") == SONNET_SEAT
    assert _expand_alias("sonnet5") == SONNET_SEAT


# === z.ai request shape (OpenAI-compatible, NOT gemini contents/parts) ===========
def test_zai_request_is_openai_shape():
    captured: dict = {}
    payload = {
        "choices": [{"message": {"content": "hi from glm"}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3},
    }
    old_open = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen(captured, payload)
    with _EnvSandbox():
        os.environ["ZAI_API_KEY"] = "zai-secret"
        try:
            res = backends.review_zai("zai", "say hi", "", REPO_ROOT, 30)
        finally:
            urllib.request.urlopen = old_open
    # Default endpoint = the GLM Coding-Plan base (serves glm-5.2) + /chat/completions.
    assert captured["url"] == "https://api.z.ai/api/coding/paas/v4/chat/completions", (
        captured["url"]
    )
    assert captured["method"] == "POST"
    # OpenAI request shape — NOT the gemini contents/parts shape.
    body = captured["body"]
    assert "messages" in body and "contents" not in body, body
    assert body["model"] == "glm-5.2", (
        body
    )  # bare `zai` → ZAI_DEFAULT_MODEL (newest GLM)
    assert body["messages"][0]["role"] == "user"
    assert body["messages"][0]["content"] == "say hi"
    assert body["stream"] is False
    # Bearer auth header (NOT gemini's x-goog-api-key).
    assert captured["headers"].get("authorization") == "Bearer zai-secret"
    assert "x-goog-api-key" not in captured["headers"]
    # Response parsed from choices[0].message.content + usage echoed.
    assert res.returncode == 0
    assert "hi from glm" in res.stdout
    assert "prompt_tokens=7 output_tokens=3" in res.stdout


def test_zai_model_suffix_and_env_overrides():
    captured: dict = {}
    payload = {"choices": [{"message": {"content": "x"}}], "usage": {}}
    old_open = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen(captured, payload)
    with _EnvSandbox():
        os.environ["ZAI_API_KEY"] = "k"
        os.environ["ZAI_BASE_URL"] = "https://api.z.ai/api/coding/paas/v4"
        try:
            # Explicit suffix wins over ZAI_MODEL.
            os.environ["ZAI_MODEL"] = "glm-4.5"
            backends.review_zai("zai:glm-4.6", "q", "", REPO_ROOT, 10)
        finally:
            urllib.request.urlopen = old_open
    assert captured["body"]["model"] == "glm-4.6", captured["body"]
    assert captured["url"] == "https://api.z.ai/api/coding/paas/v4/chat/completions", (
        captured["url"]
    )


def test_zai_diff_is_fenced_in_message():
    captured: dict = {}
    payload = {"choices": [{"message": {"content": "ok"}}], "usage": {}}
    old_open = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen(captured, payload)
    with _EnvSandbox():
        os.environ["ZAI_API_KEY"] = "k"
        try:
            backends.review_zai("zai", "review this", "+added line", REPO_ROOT, 10)
        finally:
            urllib.request.urlopen = old_open
    content = captured["body"]["messages"][0]["content"]
    assert "review this" in content
    assert "```diff" in content and "+added line" in content


def test_zai_result_model_is_requested_string_not_provider_id():
    """REGRESSION (codex P1): ReviewResult.model must be the REQUESTED backend string
    (`zai`), NOT the resolved provider id (`glm-4.6`). mode_review keys results by the
    requested string, so substituting the provider id KeyErrors after a successful call."""
    payload = {"choices": [{"message": {"content": "ok"}}], "usage": {}}
    old_open = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen({}, payload)
    with _EnvSandbox():
        os.environ["ZAI_API_KEY"] = "k"
        try:
            res = backends.review_zai("zai", "q", "+x", REPO_ROOT, 10)
            res2 = backends.review_zai("zai:glm-4.5", "q", "+x", REPO_ROOT, 10)
        finally:
            urllib.request.urlopen = old_open
    assert res.model == "zai", res.model
    assert res2.model == "zai:glm-4.5", res2.model
    # The api_model still appears in the command label (the resolved id).
    assert "glm-5.2" in res.command  # bare `zai` → ZAI_DEFAULT_MODEL (newest GLM)
    assert "glm-4.5" in res2.command


def test_mode_review_keys_by_requested_model_without_keyerror():
    """End-to-end (codex P1): the plain diff-review mode must format z.ai/commandcode
    results without KeyError — proves ReviewResult.model matches the request string."""
    from reviewlib.modes.review import mode_review

    payload = {"choices": [{"message": {"content": "looks fine"}}], "usage": {}}
    old_open = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen({}, payload)
    with _EnvSandbox():
        os.environ["ZAI_API_KEY"] = "k"
        os.environ["COMMANDCODE_API_KEY"] = "k"
        try:
            # staged=False so it doesn't write a review stamp; returns 0 on success.
            rc = mode_review(
                ["zai", "commandcode"], "review", "+added", REPO_ROOT, 10, False
            )
        finally:
            urllib.request.urlopen = old_open
    assert rc == 0, rc


# === commandcode request shape ==================================================
def test_commandcode_request_is_openai_shape():
    captured: dict = {}
    payload = {
        "choices": [{"message": {"content": "cc says hi"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }
    old_open = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen(captured, payload)
    with _EnvSandbox():
        os.environ["COMMANDCODE_API_KEY"] = "cc-secret"
        try:
            res = backends.review_commandcode("commandcode", "hello", "", REPO_ROOT, 30)
        finally:
            urllib.request.urlopen = old_open
    # Default endpoint = the verified Command Code Provider API + /chat/completions.
    assert (
        captured["url"] == "https://api.commandcode.ai/provider/v1/chat/completions"
    ), captured["url"]
    body = captured["body"]
    assert "messages" in body and "contents" not in body
    # Bare `commandcode` → COMMANDCODE_DEFAULT_MODEL (an OpenAI-shape, provider-prefixed id).
    assert body["model"] == "deepseek/deepseek-v4-flash", body
    assert body["messages"][0]["content"] == "hello"
    assert captured["headers"].get("authorization") == "Bearer cc-secret"
    assert captured["headers"].get("accept") == "application/json"
    assert captured["headers"].get("user-agent") == "review-cli/0.1"
    # The default gateway path must NOT carry the raw-DeepSeek `thinking` field.
    assert "thinking" not in body, body
    assert res.returncode == 0 and "cc says hi" in res.stdout
    assert "prompt_tokens=5 output_tokens=2" in res.stdout


def test_commandcode_key_is_not_a_deepseek_or_legacy_key():
    """SECURITY (codex P1): commandcode must require COMMANDCODE_API_KEY ONLY. A
    DEEPSEEK_API_KEY / legacy COMMON_CODE_API_KEY must NOT resolve here — otherwise a
    DeepSeek credential would be POSTed to api.commandcode.ai (wrong host, key leak)."""
    payload = {"choices": [{"message": {"content": "ok"}}], "usage": {}}
    old_open = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen({}, payload)
    with _EnvSandbox():
        # Only the foreign/legacy names present → unavailable, must NOT auth.
        os.environ["DEEPSEEK_API_KEY"] = "deepseek-secret"
        os.environ["COMMON_CODE_API_KEY"] = "legacy-secret"
        try:
            assert backends.backend_available("commandcode") is False
            raised = False
            try:
                backends._commandcode_key()
            except RuntimeError:
                raised = True
            assert raised, "commandcode must not resolve a DeepSeek/legacy key"
        finally:
            urllib.request.urlopen = old_open


def test_commandcode_base_url_and_model_override():
    captured: dict = {}
    payload = {"choices": [{"message": {"content": "x"}}], "usage": {}}
    old_open = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen(captured, payload)
    with _EnvSandbox():
        os.environ["COMMANDCODE_API_KEY"] = "user_secret"
        os.environ["COMMANDCODE_BASE_URL"] = "https://example.test/v1"
        os.environ["COMMANDCODE_MODEL"] = "deepseek/deepseek-coder"
        try:
            backends.review_commandcode("commandcode", "q", "", REPO_ROOT, 10)
        finally:
            urllib.request.urlopen = old_open
    assert captured["url"] == "https://example.test/v1/chat/completions", captured[
        "url"
    ]
    assert captured["body"]["model"] == "deepseek/deepseek-coder"


def test_commandcode_glm_seat_posts_the_byte_exact_gateway_id():
    """The priority-3 GLM-5.2 board seat (`commandcode:zai-org/GLM-5.2`) must POST the
    byte-exact gateway model id `zai-org/GLM-5.2` — INCLUDING the embedded slash. The id has
    TWO `/`-free segments around a single `/` plus the `commandcode:` provider prefix, so a
    naive split could truncate it; this pins that `review_commandcode` strips ONLY the
    provider prefix (`split(":", 1)[1]`) and sends the whole `zai-org/GLM-5.2` selector. The
    'byte-exact against the gateway /models catalog' claim in the board comments/CHANGELOG is
    only load-bearing if the wire actually carries it (review of #57)."""
    captured: dict = {}
    payload = {"choices": [{"message": {"content": "x"}}], "usage": {}}
    old_open = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen(captured, payload)
    with _EnvSandbox():
        os.environ["COMMANDCODE_API_KEY"] = "user_secret"
        os.environ.pop("COMMANDCODE_BASE_URL", None)
        os.environ.pop("COMMANDCODE_MODEL", None)
        try:
            backends.review_commandcode(
                "commandcode:zai-org/GLM-5.2", "q", "", REPO_ROOT, 10
            )
        finally:
            urllib.request.urlopen = old_open
    assert captured["body"]["model"] == "zai-org/GLM-5.2", captured["body"]
    # And it goes to the default Command Code gateway, not z.ai's host.
    assert (
        captured["url"] == "https://api.commandcode.ai/provider/v1/chat/completions"
    ), captured["url"]


def test_commandcode_glm_seat_id_beats_commandcode_model_env():
    """The priority-3 GLM-cc seat id WINS over a `COMMANDCODE_MODEL` env override — so a host
    that exports `COMMANDCODE_MODEL` (a legitimate override for the bare `-m cc` path) can NOT
    silently hijack the default-board seat into POSTing a different model. `review_commandcode`
    only consults `COMMANDCODE_MODEL` for a BARE `commandcode` id (no suffix); a suffixed seat
    like `commandcode:zai-org/GLM-5.2` takes `model.split(':',1)[1]` unconditionally. This pins
    the production case (env PRESENT, NOT popped) — refuting the concern that the seat's
    byte-exact id could be overridden by env on a real host (review of #57)."""
    captured: dict = {}
    payload = {"choices": [{"message": {"content": "x"}}], "usage": {}}
    old_open = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen(captured, payload)
    with _EnvSandbox():
        os.environ["COMMANDCODE_API_KEY"] = "user_secret"
        # Hostile env: a COMMANDCODE_MODEL that MUST be ignored for the suffixed seat id.
        os.environ["COMMANDCODE_MODEL"] = "deepseek/deepseek-v4-flash"
        os.environ.pop("COMMANDCODE_BASE_URL", None)
        try:
            backends.review_commandcode(
                "commandcode:zai-org/GLM-5.2", "q", "", REPO_ROOT, 10
            )
        finally:
            urllib.request.urlopen = old_open
    assert captured["body"]["model"] == "zai-org/GLM-5.2", captured["body"]
    # The env value did NOT leak into the wire payload.
    assert captured["body"]["model"] != "deepseek/deepseek-v4-flash", captured["body"]


# === API-only mode contract (a forced cli mode is a hard error) =================
def test_commandcode_forced_cli_mode_is_a_dead_backend_not_a_silent_post():
    """REVIEW_COMMANDCODE_MODE=cli is a config error (no commandcode CLI exists). It
    must surface as a non-zero ReviewResult and must NOT fall through to the api POST."""
    posted = {"called": False}

    def _should_not_be_called(
        req, timeout=None
    ):  # pragma: no cover - asserted unreached
        posted["called"] = True
        raise AssertionError("api path POSTed despite a forced cli mode")

    old_open = urllib.request.urlopen
    urllib.request.urlopen = _should_not_be_called
    with _EnvSandbox():
        os.environ["COMMANDCODE_API_KEY"] = "k"
        os.environ["REVIEW_COMMANDCODE_MODE"] = "cli"
        try:
            res = backends.review_commandcode("commandcode", "q", "", REPO_ROOT, 10)
        finally:
            urllib.request.urlopen = old_open
    assert posted["called"] is False
    assert res.returncode == 1, res
    assert "cli" in res.stderr and "commandcode" in res.stderr


def test_commandcode_forced_api_mode_is_accepted():
    """An explicit REVIEW_COMMANDCODE_MODE=api is the supported mode — it runs normally."""
    captured: dict = {}
    payload = {"choices": [{"message": {"content": "ok"}}], "usage": {}}
    old_open = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen(captured, payload)
    with _EnvSandbox():
        os.environ["COMMANDCODE_API_KEY"] = "k"
        os.environ["REVIEW_COMMANDCODE_MODE"] = "api"
        try:
            res = backends.review_commandcode("commandcode", "q", "", REPO_ROOT, 10)
        finally:
            urllib.request.urlopen = old_open
    assert res.returncode == 0, res
    assert captured["url"].endswith("/chat/completions")


def test_zai_forced_cli_mode_is_a_dead_backend():
    """z.ai is api-only too: REVIEW_ZAI_MODE=cli must fail loudly, not POST."""

    def _should_not_be_called(
        req, timeout=None
    ):  # pragma: no cover - asserted unreached
        raise AssertionError("api path POSTed despite a forced cli mode")

    old_open = urllib.request.urlopen
    urllib.request.urlopen = _should_not_be_called
    with _EnvSandbox():
        os.environ["ZAI_API_KEY"] = "k"
        os.environ["REVIEW_ZAI_MODE"] = "cli"
        try:
            res = backends.review_zai("zai", "q", "", REPO_ROOT, 10)
        finally:
            urllib.request.urlopen = old_open
    assert res.returncode == 1, res
    assert "cli" in res.stderr


def test_resolve_backend_mode_helper():
    """The shared mode resolver: unset -> default, supported -> the forced value,
    unsupported -> RuntimeError (used by the api-only guards above)."""
    with _EnvSandbox():
        assert backends.resolve_backend_mode("commandcode", ("api",), "api") == "api"
        os.environ["REVIEW_COMMANDCODE_MODE"] = "api"
        assert backends.resolve_backend_mode("commandcode", ("api",), "api") == "api"
        os.environ["REVIEW_COMMANDCODE_MODE"] = "cli"
        raised = False
        try:
            backends.resolve_backend_mode("commandcode", ("api",), "api")
        except RuntimeError:
            raised = True
        assert raised
        # A both-modes backend resolves either forced value.
        os.environ["REVIEW_CLAUDE_MODE"] = "cli"
        assert backends.resolve_backend_mode("claude", ("api", "cli"), "api") == "cli"


# === thinking-mode default: only the RAW DeepSeek base injects `thinking` ========
def test_commandcode_default_gateway_omits_thinking():
    """The bare `commandcode` default goes through the Command Code gateway, which must
    NOT receive the raw-DeepSeek `thinking` field (an unknown body field can 400 it)."""
    captured: dict = {}
    payload = {"choices": [{"message": {"content": "ok"}}], "usage": {}}
    old_open = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen(captured, payload)
    with _EnvSandbox():
        os.environ["COMMANDCODE_API_KEY"] = "k"
        try:
            backends.review_commandcode("commandcode", "q", "", REPO_ROOT, 10)
        finally:
            urllib.request.urlopen = old_open
    assert captured["body"]["model"] == "deepseek/deepseek-v4-flash", captured["body"]
    assert "thinking" not in captured["body"], captured["body"]


def test_zai_does_not_send_thinking_field():
    """z.ai (GLM) shares the request builder but must NOT carry any extra body field —
    the OpenAI wire shape stays generic (model + messages + stream only)."""
    captured: dict = {}
    payload = {"choices": [{"message": {"content": "ok"}}], "usage": {}}
    old_open = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen(captured, payload)
    with _EnvSandbox():
        os.environ["ZAI_API_KEY"] = "k"
        try:
            backends.review_zai("zai", "q", "", REPO_ROOT, 10)
        finally:
            urllib.request.urlopen = old_open
    assert "thinking" not in captured["body"], captured["body"]


# === coding-endpoint default + reasoning-model handling (glm-5.2) ================
def test_zai_default_base_url_is_the_coding_plan_endpoint():
    """The DEFAULT z.ai base must be the GLM Coding-Plan endpoint (the only one that
    serves the flagship glm-5.2). A standard-plan user overrides via ZAI_BASE_URL."""
    assert backends.ZAI_DEFAULT_BASE_URL == "https://api.z.ai/api/coding/paas/v4", (
        backends.ZAI_DEFAULT_BASE_URL
    )
    assert backends.ZAI_DEFAULT_MODEL == "glm-5.2", backends.ZAI_DEFAULT_MODEL
    # End-to-end: bare `zai` with no overrides hits the coding endpoint.
    captured: dict = {}
    payload = {"choices": [{"message": {"content": "ok"}}], "usage": {}}
    old_open = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen(captured, payload)
    with _EnvSandbox():
        os.environ["ZAI_API_KEY"] = "k"
        try:
            backends.review_zai("zai", "q", "", REPO_ROOT, 10)
        finally:
            urllib.request.urlopen = old_open
    assert captured["url"] == "https://api.z.ai/api/coding/paas/v4/chat/completions", (
        captured["url"]
    )


def test_zai_base_url_env_overrides_to_standard_endpoint():
    """A standard-plan user points ZAI_BASE_URL at the general /api/paas/v4 endpoint."""
    captured: dict = {}
    payload = {"choices": [{"message": {"content": "ok"}}], "usage": {}}
    old_open = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen(captured, payload)
    with _EnvSandbox():
        os.environ["ZAI_API_KEY"] = "k"
        os.environ["ZAI_BASE_URL"] = "https://api.z.ai/api/paas/v4"
        os.environ["ZAI_MODEL"] = "glm-5.1"
        try:
            backends.review_zai("zai", "q", "", REPO_ROOT, 10)
        finally:
            urllib.request.urlopen = old_open
    assert captured["url"] == "https://api.z.ai/api/paas/v4/chat/completions", captured[
        "url"
    ]
    assert captured["body"]["model"] == "glm-5.1", captured["body"]


def test_zai_reasoning_content_fallback_when_content_empty():
    """REASONING MODEL (glm-5.2): a 2xx whose message.content is empty/missing but
    carries message.reasoning_content must NOT fail-closed as "no assistant content".
    Surface the reasoning text (rc=0) so a low-output-budget reasoning reply is usable."""
    cases = (
        {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "reasoning_content": "I think the diff is fine.",
                    }
                }
            ],
            "usage": {},
        },
        {
            "choices": [
                {
                    "message": {
                        "reasoning_content": "Only reasoning here, no content key."
                    }
                }
            ],
            "usage": {},
        },
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "reasoning_content": "null content, reasoning present.",
                    }
                }
            ],
            "usage": {},
        },
        {
            "choices": [
                {
                    "message": {
                        "content": "   ",
                        "reasoning_content": "whitespace content, reasoning present.",
                    }
                }
            ],
            "usage": {},
        },
    )
    for payload in cases:
        captured: dict = {}
        old_open = urllib.request.urlopen
        urllib.request.urlopen = _fake_urlopen(captured, payload)
        with _EnvSandbox():
            os.environ["ZAI_API_KEY"] = "k"
            try:
                res = backends.review_zai("zai", "q", "+x", REPO_ROOT, 10)
            finally:
                urllib.request.urlopen = old_open
        assert res.returncode == 0, (payload, res)
        assert "reasoning" in res.stdout.lower(), (payload, res.stdout)


def test_zai_prefers_content_over_reasoning_when_both_present():
    """When BOTH content and reasoning_content are present, the final answer (content)
    wins — the reasoning is the chain of thought, not the review."""
    captured: dict = {}
    payload = {
        "choices": [
            {
                "message": {
                    "content": "FINAL: the change looks correct.",
                    "reasoning_content": "step 1 ... step 2 ...",
                }
            }
        ],
        "usage": {},
    }
    old_open = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen(captured, payload)
    with _EnvSandbox():
        os.environ["ZAI_API_KEY"] = "k"
        try:
            res = backends.review_zai("zai", "q", "+x", REPO_ROOT, 10)
        finally:
            urllib.request.urlopen = old_open
    assert res.returncode == 0, res
    assert "FINAL: the change looks correct." in res.stdout
    assert (
        "step 1" not in res.stdout
    )  # reasoning not surfaced when a final answer exists


def test_zai_empty_with_no_reasoning_still_fails_closed():
    """The fallback must not weaken the empty-output guard: NO content AND NO usable
    reasoning_content must still map to a non-zero dead-backend result."""
    cases = (
        {"choices": [{"message": {"content": ""}}], "usage": {}},
        {
            "choices": [{"message": {"content": "", "reasoning_content": ""}}],
            "usage": {},
        },
        {
            "choices": [{"message": {"content": "", "reasoning_content": "   "}}],
            "usage": {},
        },
        {
            "choices": [{"message": {"content": "", "reasoning_content": 42}}],
            "usage": {},
        },
    )
    for payload in cases:
        old_open = urllib.request.urlopen
        urllib.request.urlopen = _fake_urlopen({}, payload)
        with _EnvSandbox():
            os.environ["ZAI_API_KEY"] = "k"
            try:
                res = backends.review_zai("zai", "q", "+x", REPO_ROOT, 10)
            finally:
                urllib.request.urlopen = old_open
        assert res.returncode == 1, (payload, res)
        assert "no assistant content" in res.stderr, (payload, res.stderr)


def test_zai_empty_content_failure_still_carries_spent_prompt_tokens():
    """Codex review finding (review-cli#200): a 2xx with a valid `usage` object but no
    assistant content still fails closed (rc=1) -- but the prompt tokens it SPENT must
    ride along on the failed result, so provider failover's per-attempt tally (and
    `run-stats.jsonl`) does not silently lose them. Mirrors the Anthropic path."""
    payload = {
        "choices": [{"message": {"content": ""}}],
        "usage": {"prompt_tokens": 1234, "completion_tokens": 0},
    }
    old_open = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen({}, payload)
    with _EnvSandbox():
        os.environ["ZAI_API_KEY"] = "k"
        try:
            res = backends.review_zai("zai", "q", "+x", REPO_ROOT, 10)
        finally:
            urllib.request.urlopen = old_open
    assert res.returncode == 1 and "no assistant content" in res.stderr, res
    assert (res.prompt_tokens, res.output_tokens) == (1234, 0), res


def test_validated_usage_pair_is_all_or_nothing():
    """Unit rule: a usage reading is a measurement only as a complete, non-negative int
    pair; a completed call additionally cannot have a zero in either field. Anything
    else collapses to 0/0 ("unknown") -- the shape `dashboard/tokenstats.py` already
    rejects, so the two stores agree."""
    parse = backends._parse_openai_usage
    assert parse({"usage": {"prompt_tokens": 500}}) == (0, 0)
    assert parse({"usage": {"completion_tokens": 7}}) == (0, 0)
    assert parse({"usage": {"prompt_tokens": 500, "completion_tokens": "7"}}) == (0, 0)
    assert parse({"usage": {"prompt_tokens": True, "completion_tokens": 7}}) == (0, 0)
    assert parse({"usage": {"prompt_tokens": -1, "completion_tokens": 7}}) == (0, 0)
    assert parse({"usage": {"prompt_tokens": 500, "completion_tokens": 7}}) == (500, 7)
    assert parse({"usage": {"prompt_tokens": 500, "completion_tokens": 0}}) == (0, 0)
    assert parse(
        {"usage": {"prompt_tokens": 500, "completion_tokens": 0}}, completed=False
    ) == (500, 0)
    assert parse([]) == (0, 0) and parse({"usage": None}) == (0, 0)
    assert backends._validated_usage_pair(1234, 0, completed=False) == (1234, 0)
    assert backends._validated_usage_pair(1234, 0, completed=True) == (0, 0)
    assert backends._validated_usage_pair(1234, None, completed=False) == (0, 0)


def test_openai_compatible_success_carries_validated_tokens_on_the_result():
    """Integration: the RESULT OBJECT's fields are what the panel tally reads -- not
    just the stdout footer -- so a complete pair lands on `ReviewResult`, and a
    partial or zero-output pair on a completed call is recorded as unknown."""
    for usage, expected in (
        ({"prompt_tokens": 500}, (0, 0)),
        ({"prompt_tokens": 500, "completion_tokens": 0}, (0, 0)),
        ({"prompt_tokens": 500, "completion_tokens": 7}, (500, 7)),
    ):
        payload = {"choices": [{"message": {"content": "looks fine"}}], "usage": usage}
        old_open = urllib.request.urlopen
        urllib.request.urlopen = _fake_urlopen({}, payload)
        with _EnvSandbox():
            os.environ["ZAI_API_KEY"] = "k"
            try:
                res = backends.review_zai("zai", "q", "+x", REPO_ROOT, 10)
            finally:
                urllib.request.urlopen = old_open
        assert res.returncode == 0, res
        assert (res.prompt_tokens, res.output_tokens) == expected, (usage, res)
        assert res.stdout.rstrip().endswith(
            f"prompt_tokens={expected[0]} output_tokens={expected[1]}"
        ), res.stdout


def test_gemini_applies_the_same_usage_validity_rule_and_fails_closed_when_empty():
    """The Gemini REST path shares `_validated_usage_pair` AND now fails closed on a
    2xx with no candidate text (previously it returned rc=0 with only the token
    footer, which `result_is_usable()` would accept as review content) -- while
    still carrying the prompt tokens that empty call spent."""
    cases = (
        ("fine", {"promptTokenCount": 1234}, 0, (0, 0)),
        ("fine", {"promptTokenCount": 1234, "candidatesTokenCount": 56}, 0, (1234, 56)),
        ("fine", {"promptTokenCount": 1234, "candidatesTokenCount": 0}, 0, (0, 0)),
        ("", {"promptTokenCount": 1234, "candidatesTokenCount": 0}, 1, (1234, 0)),
    )
    for text, usage, rc, expected in cases:
        payload = {
            "candidates": [{"content": {"parts": [{"text": text}]}}],
            "usageMetadata": usage,
        }
        old_open = urllib.request.urlopen
        urllib.request.urlopen = _fake_urlopen({}, payload)
        with _EnvSandbox():
            os.environ["GEMINI_API_KEY"] = "k"
            try:
                res = backends.review_gemini("gemini", "q", "+x", REPO_ROOT, 10)
            finally:
                urllib.request.urlopen = old_open
        assert res.returncode == rc, (text, usage, res)
        assert (res.prompt_tokens, res.output_tokens) == expected, (text, usage, res)
        if rc:
            assert res.stdout == "" and "no candidate text" in res.stderr, res
    # `{"candidates": []}` -- the exact shape Codex flagged -- must also fail closed.
    old_open = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen({}, {"candidates": [], "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 0}})
    with _EnvSandbox():
        os.environ["GEMINI_API_KEY"] = "k"
        try:
            res = backends.review_gemini("gemini", "q", "+x", REPO_ROOT, 10)
        finally:
            urllib.request.urlopen = old_open
    assert res.returncode == 1 and (res.prompt_tokens, res.output_tokens) == (9, 0), res


def test_anthropic_api_applies_the_same_usage_validity_rule():
    """The Anthropic REST path shares `_validated_usage_pair`: a partial `usage`
    (`input_tokens` only) collapses to 0/0 exactly like the OpenAI-compatible path,
    a complete pair on a completed call is kept, and an empty-content failure keeps
    the prompt spend."""
    cases = (
        ("fine", {"input_tokens": 1234}, 0, (0, 0)),
        ("fine", {"input_tokens": 1234, "output_tokens": 56}, 0, (1234, 56)),
        ("fine", {"input_tokens": 1234, "output_tokens": 0}, 0, (0, 0)),
        ("", {"input_tokens": 1234, "output_tokens": 0}, 1, (1234, 0)),
    )
    for text, usage, rc, expected in cases:
        payload = {"content": [{"type": "text", "text": text}], "usage": usage}
        old_open = urllib.request.urlopen
        urllib.request.urlopen = _fake_urlopen({}, payload)
        with _EnvSandbox():
            os.environ["ANTHROPIC_API_KEY"] = "k"
            try:
                res = backends.review_claude_api("claude:claude-opus-4-8", "q", "+x", REPO_ROOT, 10)
            finally:
                urllib.request.urlopen = old_open
        assert res.returncode == rc, (text, usage, res)
        assert (res.prompt_tokens, res.output_tokens) == expected, (text, usage, res)


def test_commandcode_never_injects_thinking_field():
    """The commandcode backend speaks the generic OpenAI shape and never injects the
    DeepSeek-specific `thinking` field — not on the default gateway, an explicit model,
    nor a custom base URL. (The old `common-code` placeholder injected it; the real
    Command Code gateway need not accept an unknown body field, so it is never sent.)"""
    cases = (
        {},  # bare default
        {"COMMANDCODE_MODEL": "deepseek/deepseek-v4-pro"},  # explicit model
        {"COMMANDCODE_BASE_URL": "https://api.deepseek.com"},  # raw DeepSeek base
        {"COMMANDCODE_BASE_URL": "https://some-gateway.test/v1"},  # foreign gateway
    )
    for extra_env in cases:
        captured: dict = {}
        payload = {"choices": [{"message": {"content": "ok"}}], "usage": {}}
        old_open = urllib.request.urlopen
        urllib.request.urlopen = _fake_urlopen(captured, payload)
        with _EnvSandbox():
            os.environ["COMMANDCODE_API_KEY"] = "k"
            os.environ.update(extra_env)
            try:
                backends.review_commandcode("commandcode", "q", "", REPO_ROOT, 10)
            finally:
                urllib.request.urlopen = old_open
        assert "thinking" not in captured["body"], (extra_env, captured["body"])


# === valid-but-wrong-shape JSON must not crash AND must fail-closed ==============
def test_commandcode_no_content_2xx_fails_closed():
    """A 2xx response with NO assistant content — whether wrong-shaped JSON
    (`{"choices":[null]}`, `usage` a list), an HTTP-200 error envelope, or an empty
    completion — must (a) NOT crash the type-guarded parse, and (b) map to a NON-zero
    ReviewResult. rc=0 here would let mode_review write a 'reviewed' stamp and satisfy
    the commit gate with an empty review."""
    for payload in (
        [],  # top-level not a dict
        {"choices": []},  # empty choices
        {"choices": [None]},  # choice not a dict
        {"choices": [{"message": []}]},  # message not a dict
        {"choices": [{"message": {"content": None}}]},  # content not a string
        {"choices": [{"message": {"content": ""}}]},  # empty completion
        {"choices": [{"message": {"content": "   "}}]},  # whitespace-only
        {"error": {"message": "rate limited"}},  # 200 error envelope, no choices
    ):
        captured: dict = {}
        old_open = urllib.request.urlopen
        urllib.request.urlopen = _fake_urlopen(captured, payload)
        with _EnvSandbox():
            os.environ["COMMANDCODE_API_KEY"] = "k"
            try:
                res = backends.review_commandcode("commandcode", "q", "", REPO_ROOT, 10)
            finally:
                urllib.request.urlopen = old_open
        assert res.returncode == 1, (payload, res)
        assert res.stdout == "", (payload, res.stdout)
        assert "no assistant content" in res.stderr, (payload, res.stderr)


def test_commandcode_usage_wrong_shape_does_not_crash_on_valid_content():
    """When there IS content but `usage` is the wrong type, the token parse must still
    degrade to 0/0 (rc=0, content preserved) rather than raising."""
    captured: dict = {}
    payload = {"choices": [{"message": {"content": "real review text"}}], "usage": []}
    old_open = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen(captured, payload)
    with _EnvSandbox():
        os.environ["COMMANDCODE_API_KEY"] = "k"
        try:
            res = backends.review_commandcode("commandcode", "q", "", REPO_ROOT, 10)
        finally:
            urllib.request.urlopen = old_open
    assert res.returncode == 0, res
    assert "real review text" in res.stdout
    assert "prompt_tokens=0 output_tokens=0" in res.stdout


# === key resolution from a temp .env (the shared config surface) ================
def test_zai_key_resolves_from_temp_env_file():
    import tempfile

    with _EnvSandbox():
        with tempfile.TemporaryDirectory() as d:
            env_path = Path(d) / ".env"
            env_path.write_text('ZAI_API_KEY="from-env-file"\n', encoding="utf-8")
            os.environ["GEMINI_ENV_FILE"] = str(env_path)
            assert backends._zai_key() == "from-env-file"


def test_commandcode_key_resolves_from_temp_env_file():
    import tempfile

    with _EnvSandbox():
        with tempfile.TemporaryDirectory() as d:
            env_path = Path(d) / ".env"
            # COMMANDCODE_API_KEY is the primary fallback var name.
            env_path.write_text("COMMANDCODE_API_KEY=cc-from-file\n", encoding="utf-8")
            os.environ["GEMINI_ENV_FILE"] = str(env_path)
            assert backends._commandcode_key() == "cc-from-file"


def test_env_var_takes_precedence_over_file():
    import tempfile

    with _EnvSandbox():
        with tempfile.TemporaryDirectory() as d:
            env_path = Path(d) / ".env"
            env_path.write_text("ZAI_API_KEY=file-key\n", encoding="utf-8")
            os.environ["GEMINI_ENV_FILE"] = str(env_path)
            os.environ["ZAI_API_KEY"] = "env-key"
            assert backends._zai_key() == "env-key"


def test_key_name_precedence_beats_file_order():
    """REGRESSION (codex P2): key-name precedence must be DETERMINISTIC across .env
    files. The canonical/primary key name (fallback_var) must win over an alias even
    when the alias lives in an EARLIER fallback file. A path-first loop would let the
    earlier file's alias shadow the later file's primary key — surprising precedence.

    Exercised via z.ai (ZAI_API_KEY primary / ZHIPU_API_KEY alias) — commandcode now
    has NO alias key names (codex P1: a foreign key must not resolve), so the multi-name
    _resolve_key path is covered through the provider that legitimately has aliases."""
    import tempfile

    with _EnvSandbox():
        with tempfile.TemporaryDirectory() as d:
            early = Path(d) / "early.env"  # holds only an ALIAS key
            late = Path(d) / "late.env"  # holds the PRIMARY key
            early.write_text("ZHIPU_API_KEY=alias-in-early-file\n", encoding="utf-8")
            late.write_text("ZAI_API_KEY=primary-in-late-file\n", encoding="utf-8")
            old_fallbacks = backends.GEMINI_ENV_FALLBACKS
            backends.GEMINI_ENV_FALLBACKS = (early, late)
            os.environ.pop(
                "GEMINI_ENV_FILE", None
            )  # use GEMINI_ENV_FALLBACKS, not override
            try:
                # Primary name (ZAI_API_KEY) in the LATER file must beat the
                # alias (ZHIPU_API_KEY) in the EARLIER file.
                assert backends._zai_key() == "primary-in-late-file"
            finally:
                backends.GEMINI_ENV_FALLBACKS = old_fallbacks


def test_alias_resolves_when_primary_absent():
    """Cross-file companion to the precedence test: with NO primary key anywhere, an
    alias key still resolves (name-priority-first must fall through to the aliases)."""
    import tempfile

    with _EnvSandbox():
        with tempfile.TemporaryDirectory() as d:
            early = Path(d) / "early.env"
            late = Path(d) / "late.env"
            early.write_text("# nothing useful here\n", encoding="utf-8")
            late.write_text("ZHIPU_API_KEY=only-alias\n", encoding="utf-8")
            old_fallbacks = backends.GEMINI_ENV_FALLBACKS
            backends.GEMINI_ENV_FALLBACKS = (early, late)
            os.environ.pop("GEMINI_ENV_FILE", None)
            try:
                assert backends._zai_key() == "only-alias"
            finally:
                backends.GEMINI_ENV_FALLBACKS = old_fallbacks


# === backend_available reflects key presence ====================================
def test_backend_available_reflects_zai_key():
    with _EnvSandbox():
        # No key anywhere → unavailable, never a crash.
        assert backends.backend_available("zai") is False
        os.environ["ZAI_API_KEY"] = "present"
        assert backends.backend_available("zai") is True
        assert backends.backend_available("glm") is True
        assert backends.backend_available("zai:glm-4.6") is True


def test_backend_available_reflects_commandcode_key():
    old_open = urllib.request.urlopen
    urllib.request.urlopen = lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("backend_available made network")
    )
    with _EnvSandbox():
        try:
            assert backends.backend_available("commandcode") is False
            os.environ["COMMANDCODE_API_KEY"] = "user_present"
            assert backends.backend_available("commandcode") is True
            assert (
                backends.backend_available("commandcode:deepseek/deepseek-coder")
                is True
            )
            # The legacy model-name spelling routes to the same backend (key present).
            assert backends.backend_available("common-code") is True
        finally:
            urllib.request.urlopen = old_open


def test_commandcode_payment_preflight_skips_unpaid_provider_without_chat_post():
    """A Command Code key can exist while the provider is unpaid/unavailable.

    The cheap availability probe stays offline; the backend itself must check entitlement
    before falling through to /chat/completions.
    """
    captured: list[tuple[str, str]] = []

    def _preflight_only(req, timeout=None):
        captured.append((req.get_method(), req.full_url))
        if req.full_url.endswith("/models"):
            raise _http_error(req.full_url, 402, "insufficient balance")
        raise AssertionError(
            f"unexpected Command Code chat POST after failed preflight: {req.full_url}"
        )

    old_open = urllib.request.urlopen
    urllib.request.urlopen = _preflight_only
    with _EnvSandbox():
        os.environ["COMMANDCODE_API_KEY"] = "user_present"
        try:
            assert (
                backends.backend_available("commandcode:deepseek/deepseek-v4-pro")
                is True
            )
            res = backends.review_commandcode(
                "commandcode:deepseek/deepseek-v4-pro", "q", "", REPO_ROOT, 10
            )
        finally:
            urllib.request.urlopen = old_open
    assert captured == [
        ("GET", "https://api.commandcode.ai/provider/v1/models"),
    ], captured
    assert res.returncode == 1, res
    assert "preflight" in res.stderr and "skipping" in res.stderr, res.stderr


def test_fireworks_payment_preflight_removes_seat_before_opencode_spawn():
    """Fireworks stays selectable, but runtime preflight must skip before opencode spawn."""
    captured: list[tuple[str, str]] = []

    def _preflight_only(req, timeout=None):
        captured.append((req.get_method(), req.full_url))
        if req.full_url.endswith("/models"):
            raise _http_error(req.full_url, 403, "account suspended")
        raise AssertionError(
            f"unexpected Fireworks model dispatch after failed preflight: {req.full_url}"
        )

    old_open = urllib.request.urlopen
    old_which = backends._which
    old_ensure = backends._ensure_opencode_readonly_agent
    urllib.request.urlopen = _preflight_only
    backends._which = lambda name: f"/fake/bin/{name}"
    backends._ensure_opencode_readonly_agent = lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("opencode setup ran")
    )
    with _EnvSandbox():
        os.environ["FIREWORKS_API_KEY"] = "fw_present"
        try:
            assert (
                backends.backend_available(
                    "oc:fireworks/accounts/fireworks/models/kimi"
                )
                is True
            )
            res = backends.review_opencode(
                "oc:fireworks/accounts/fireworks/models/kimi", "q", "", REPO_ROOT, 10
            )
        finally:
            urllib.request.urlopen = old_open
            backends._which = old_which
            backends._ensure_opencode_readonly_agent = old_ensure
    assert captured == [
        ("GET", "https://api.fireworks.ai/inference/v1/models"),
    ], captured
    assert res.returncode == 1, res
    assert "preflight" in res.stderr and "skipping" in res.stderr, res.stderr


def test_opencode_missing_binary_does_not_run_payment_preflight():
    def _network_should_not_run(_req, timeout=None):
        raise AssertionError("payment preflight ran before opencode binary check")

    def _no_opencode(name):
        raise RuntimeError(f"{name} CLI not found")

    old_open = urllib.request.urlopen
    old_which = backends._which
    urllib.request.urlopen = _network_should_not_run
    backends._which = _no_opencode
    with _EnvSandbox():
        os.environ["FIREWORKS_API_KEY"] = "fw_present"
        try:
            raised = False
            try:
                backends.review_opencode(
                    "oc:fireworks/accounts/fireworks/models/kimi",
                    "q",
                    "",
                    REPO_ROOT,
                    10,
                )
            except RuntimeError:
                raised = True
        finally:
            urllib.request.urlopen = old_open
            backends._which = old_which
    assert raised


def test_payment_preflight_ignores_transient_network_failures():
    calls = {"transient": 0, "ok": 0}

    def _transient(_req, timeout=None):
        calls["transient"] += 1
        raise urllib.error.URLError("temporary dns failure")

    def _ok(_req, timeout=None):
        calls["ok"] += 1
        return _FakeResp({"data": []})

    old_open = urllib.request.urlopen
    urllib.request.urlopen = _transient
    with _EnvSandbox():
        os.environ["COMMANDCODE_API_KEY"] = "user_present"
        try:
            assert (
                backends._provider_payment_preflight_unavailable_reason(
                    "commandcode:deepseek/deepseek-v4-pro"
                )
                is None
            )
            urllib.request.urlopen = _ok
            assert (
                backends._provider_payment_preflight_unavailable_reason(
                    "commandcode:moonshotai/Kimi-K2.7-Code"
                )
                is None
            )
        finally:
            urllib.request.urlopen = old_open
    assert calls == {"transient": 1, "ok": 1}, calls


def test_payment_preflight_403_without_billing_marker_is_not_authoritative():
    def _waf(_req, timeout=None):
        raise _http_error(
            "https://api.commandcode.ai/provider/v1/models", 403, "cloudflare challenge"
        )

    old_open = urllib.request.urlopen
    urllib.request.urlopen = _waf
    with _EnvSandbox():
        os.environ["COMMANDCODE_API_KEY"] = "user_present"
        try:
            assert (
                backends._provider_payment_preflight_unavailable_reason(
                    "commandcode:deepseek/deepseek-v4-pro"
                )
                is None
            )
        finally:
            urllib.request.urlopen = old_open


def test_payment_preflight_401_without_billing_marker_is_not_authoritative():
    def _auth_scope(_req, timeout=None):
        raise _http_error(
            "https://api.commandcode.ai/provider/v1/models",
            401,
            "model listing auth scope denied",
        )

    old_open = urllib.request.urlopen
    urllib.request.urlopen = _auth_scope
    with _EnvSandbox():
        os.environ["COMMANDCODE_API_KEY"] = "user_present"
        try:
            assert (
                backends._provider_payment_preflight_unavailable_reason(
                    "commandcode:deepseek/deepseek-v4-pro"
                )
                is None
            )
        finally:
            urllib.request.urlopen = old_open


def test_payment_preflight_generic_500_billing_text_is_not_authoritative():
    def _generic_500(_req, timeout=None):
        raise _http_error(
            "https://api.commandcode.ai/provider/v1/models",
            500,
            "billing service disabled",
        )

    old_open = urllib.request.urlopen
    urllib.request.urlopen = _generic_500
    with _EnvSandbox():
        os.environ["COMMANDCODE_API_KEY"] = "user_present"
        try:
            assert (
                backends._provider_payment_preflight_unavailable_reason(
                    "commandcode:deepseek/deepseek-v4-pro"
                )
                is None
            )
        finally:
            urllib.request.urlopen = old_open


def test_payment_preflight_specific_billing_marker_denies_http_402():
    def _unpaid(_req, timeout=None):
        raise _http_error(
            "https://api.commandcode.ai/provider/v1/models", 402, "insufficient credits"
        )

    old_open = urllib.request.urlopen
    urllib.request.urlopen = _unpaid
    with _EnvSandbox():
        os.environ["COMMANDCODE_API_KEY"] = "user_present"
        try:
            reason = backends._provider_payment_preflight_unavailable_reason(
                "commandcode:deepseek/deepseek-v4-pro"
            )
        finally:
            urllib.request.urlopen = old_open
    assert reason is not None and "preflight" in reason, reason


def test_payment_preflight_specific_billing_marker_denies_http_400():
    def _unpaid(_req, timeout=None):
        raise _http_error(
            "https://api.commandcode.ai/provider/v1/models", 400, "insufficient credits"
        )

    old_open = urllib.request.urlopen
    urllib.request.urlopen = _unpaid
    with _EnvSandbox():
        os.environ["COMMANDCODE_API_KEY"] = "user_present"
        try:
            reason = backends._provider_payment_preflight_unavailable_reason(
                "commandcode:deepseek/deepseek-v4-pro"
            )
        finally:
            urllib.request.urlopen = old_open
    assert reason is not None and "HTTP 400" in reason, reason


def test_commandcode_chat_billing_marker_caches_provider_skip_for_next_seat():
    calls: list[str] = []

    def _models_ok_then_chat_unpaid(req, timeout=None):
        calls.append(req.full_url)
        if req.full_url.endswith("/models"):

            class _Resp:
                def __enter__(self):
                    return self

                def __exit__(self, *_exc):
                    return False

                def read(self):
                    return b'{"data":[]}'

            return _Resp()
        raise _http_error(req.full_url, 400, "insufficient credits")

    old_open = urllib.request.urlopen
    urllib.request.urlopen = _models_ok_then_chat_unpaid
    with _EnvSandbox():
        os.environ["COMMANDCODE_API_KEY"] = "user_present"
        try:
            first = backends.review_commandcode(
                "commandcode:deepseek/deepseek-v4-pro", "q", "", REPO_ROOT, 10
            )
            second = backends.review_commandcode(
                "commandcode:Qwen/Qwen3.7-Max", "q", "", REPO_ROOT, 10
            )
        finally:
            urllib.request.urlopen = old_open
    assert first.returncode == 400
    assert second.returncode == 1
    assert "preflight" in second.stderr and "skipping" in second.stderr, second.stderr
    assert len([url for url in calls if url.endswith("/chat/completions")]) == 1, calls


def test_commandcode_model_subscription_denial_only_caches_that_model():
    calls: list[str] = []

    def _models_ok_then_chat_subscription_denied(req, timeout=None):
        calls.append(req.full_url)
        if req.full_url.endswith("/models"):
            return _FakeResp({"data": []})
        raise _http_error(req.full_url, 400, "subscription required for this model")

    old_open = urllib.request.urlopen
    urllib.request.urlopen = _models_ok_then_chat_subscription_denied
    with _EnvSandbox():
        os.environ["COMMANDCODE_API_KEY"] = "user_present"
        try:
            first = backends.review_commandcode(
                "commandcode:deepseek/deepseek-v4-pro", "q", "", REPO_ROOT, 10
            )
            same_model = backends.review_commandcode(
                "commandcode:deepseek/deepseek-v4-pro", "q", "", REPO_ROOT, 10
            )
            different_model = backends.review_commandcode(
                "commandcode:Qwen/Qwen3.7-Max", "q", "", REPO_ROOT, 10
            )
        finally:
            urllib.request.urlopen = old_open
    assert first.returncode == 400
    assert same_model.returncode == 1
    assert "preflight" in same_model.stderr and "skipping" in same_model.stderr, (
        same_model.stderr
    )
    assert different_model.returncode == 400
    assert len([url for url in calls if url.endswith("/chat/completions")]) == 2, calls


def test_commandcode_chat_nonbilling_http_400_does_not_cache_provider_skip():
    calls: list[str] = []

    def _models_ok_then_chat_bad_request(req, timeout=None):
        calls.append(req.full_url)
        if req.full_url.endswith("/models"):
            return _FakeResp({"data": []})
        raise _http_error(req.full_url, 400, "context length exceeded")

    old_open = urllib.request.urlopen
    urllib.request.urlopen = _models_ok_then_chat_bad_request
    with _EnvSandbox():
        os.environ["COMMANDCODE_API_KEY"] = "user_present"
        try:
            first = backends.review_commandcode(
                "commandcode:deepseek/deepseek-v4-pro", "q", "", REPO_ROOT, 10
            )
            second = backends.review_commandcode(
                "commandcode:Qwen/Qwen3.7-Max", "q", "", REPO_ROOT, 10
            )
        finally:
            urllib.request.urlopen = old_open
    assert first.returncode == 400
    assert second.returncode == 400
    assert "preflight" not in second.stderr and "skipping" not in second.stderr, (
        second.stderr
    )
    assert len([url for url in calls if url.endswith("/chat/completions")]) == 2, calls


def test_successful_payment_preflight_does_not_overwrite_existing_provider_denial():
    def _models_ok(req, timeout=None):
        if req.full_url.endswith("/models"):
            return _FakeResp({"data": []})
        raise AssertionError(
            f"unexpected chat dispatch in preflight stub: {req.full_url}"
        )

    old_open = urllib.request.urlopen
    urllib.request.urlopen = _models_ok
    with _EnvSandbox():
        os.environ["COMMANDCODE_API_KEY"] = "user_present"
        provider = "commandcode"
        key, url = backends._payment_preflight_credentials(
            "commandcode:deepseek/deepseek-v4-pro", provider
        )
        assert key and url
        cache_key = backends._payment_preflight_cache_key(provider, url, key)
        with backends._PAYMENT_PREFLIGHT_CACHE_LOCK:
            backends._PAYMENT_PREFLIGHT_CACHE[cache_key] = (True, 402)

        # Simulate the write phase of a slower concurrent successful /models probe that
        # began before the denial was cached. It must not clear the denial.
        with urllib.request.urlopen(
            urllib.request.Request(url, method="GET"),
            timeout=backends._PROVIDER_PREFLIGHT_TIMEOUT_SECONDS,
        ):
            with backends._PAYMENT_PREFLIGHT_CACHE_LOCK:
                cached = backends._PAYMENT_PREFLIGHT_CACHE.get(cache_key)
                if cached is None or cached[0] is False:
                    backends._PAYMENT_PREFLIGHT_CACHE[cache_key] = (False, None)

        try:
            reason = backends._provider_payment_preflight_unavailable_reason(
                "commandcode:Qwen/Qwen3.7-Max"
            )
        finally:
            urllib.request.urlopen = old_open
    assert reason is not None and "HTTP 402" in reason, reason


def test_successful_payment_preflight_returns_concurrent_provider_denial():
    """A slower successful /models probe must notice a denial cached while it was in flight."""
    calls = {"n": 0}

    def _models_ok_after_denial(req, timeout=None):
        calls["n"] += 1
        if req.full_url.endswith("/models"):
            provider = "commandcode"
            key, url = backends._payment_preflight_credentials(
                "commandcode:deepseek/deepseek-v4-pro", provider
            )
            assert key and url
            with backends._PAYMENT_PREFLIGHT_CACHE_LOCK:
                backends._PAYMENT_PREFLIGHT_CACHE[
                    backends._payment_preflight_cache_key(provider, url, key)
                ] = (True, 402)
            return _FakeResp({"data": []})
        raise AssertionError(
            f"unexpected chat dispatch in preflight stub: {req.full_url}"
        )

    old_open = urllib.request.urlopen
    urllib.request.urlopen = _models_ok_after_denial
    with _EnvSandbox():
        os.environ["COMMANDCODE_API_KEY"] = "user_present"
        try:
            reason = backends._provider_payment_preflight_unavailable_reason(
                "commandcode:deepseek/deepseek-v4-pro"
            )
        finally:
            urllib.request.urlopen = old_open
    assert reason is not None and "HTTP 402" in reason, reason
    assert calls["n"] == 1, calls


def test_payment_preflight_billing_marker_on_auth_status_denies():
    def _auth_with_marker(_req, timeout=None):
        raise _http_error(
            "https://api.commandcode.ai/provider/v1/models",
            401,
            "payment required for model list scope",
        )

    old_open = urllib.request.urlopen
    urllib.request.urlopen = _auth_with_marker
    with _EnvSandbox():
        os.environ["COMMANDCODE_API_KEY"] = "user_present"
        try:
            reason = backends._provider_payment_preflight_unavailable_reason(
                "commandcode:deepseek/deepseek-v4-pro"
            )
        finally:
            urllib.request.urlopen = old_open
    assert reason is not None and "HTTP 401" in reason, reason


def test_payment_preflight_http_402_denies_even_with_empty_body():
    def _empty_payment_required(_req, timeout=None):
        raise _http_error("https://api.commandcode.ai/provider/v1/models", 402, "")

    old_open = urllib.request.urlopen
    urllib.request.urlopen = _empty_payment_required
    with _EnvSandbox():
        os.environ["COMMANDCODE_API_KEY"] = "user_present"
        try:
            reason = backends._provider_payment_preflight_unavailable_reason(
                "commandcode:deepseek/deepseek-v4-pro"
            )
        finally:
            urllib.request.urlopen = old_open
    assert reason is not None and "HTTP 402" in reason, reason


def test_payment_preflight_success_is_cached_for_dispatch():
    calls = {"n": 0}

    def _ok(req, timeout=None):
        calls["n"] += 1
        return _FakeResp({"data": []})

    old_open = urllib.request.urlopen
    urllib.request.urlopen = _ok
    with _EnvSandbox():
        os.environ["COMMANDCODE_API_KEY"] = "user_present"
        try:
            assert (
                backends.backend_available("commandcode:deepseek/deepseek-v4-pro")
                is True
            )
            assert (
                backends.provider_preflight_result(
                    "commandcode:deepseek/deepseek-v4-pro",
                    backend="commandcode",
                    command="commandcode API x",
                )
                is None
            )
            assert (
                backends.provider_preflight_result(
                    "commandcode:moonshotai/Kimi-K2.7-Code",
                    backend="commandcode",
                    command="commandcode API y",
                )
                is None
            )
        finally:
            urllib.request.urlopen = old_open
    assert calls["n"] == 1, calls


def test_backend_available_uses_cached_payment_denial_without_network():
    def _no_network(req, timeout=None):
        raise AssertionError(f"backend_available made network: {req.full_url}")

    old_open = urllib.request.urlopen
    urllib.request.urlopen = _no_network
    with _EnvSandbox():
        os.environ["COMMANDCODE_API_KEY"] = "user_present"
        provider = "commandcode"
        key, url = backends._payment_preflight_credentials(
            "commandcode:deepseek/deepseek-v4-pro", provider
        )
        assert key and url
        with backends._PAYMENT_PREFLIGHT_CACHE_LOCK:
            backends._PAYMENT_PREFLIGHT_CACHE[
                backends._payment_preflight_cache_key(provider, url, key)
            ] = (True, 402)
        try:
            assert (
                backends.backend_available("commandcode:deepseek/deepseek-v4-pro")
                is False
            )
        finally:
            urllib.request.urlopen = old_open


def test_visual_opencode_availability_does_not_use_payment_preflight():
    from reviewlib.features.visual import vision_client

    old_open = urllib.request.urlopen
    old_which = vision_client.shutil.which
    urllib.request.urlopen = lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("vision availability made network")
    )
    vision_client.shutil.which = lambda name: f"/fake/bin/{name}"
    with _EnvSandbox():
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["OC_AUTH_FILE"] = _write_oc_auth(tmpdir, {})
            os.environ["OC_CONFIG_FILE"] = _write_oc_config(
                tmpdir,
                {"fireworks": {"options": {"apiKey": "fw_present"}}},
            )
            try:
                assert (
                    vision_client.vision_backend_available(
                        "oc:fireworks/accounts/fireworks/models/gpt-4o"
                    )
                    is True
                )
            finally:
                urllib.request.urlopen = old_open
                vision_client.shutil.which = old_which


def test_visual_opencode_call_returns_preflight_unavailable_without_spawn():
    from reviewlib.features.visual import vision_client

    def _unpaid(req, timeout=None):
        raise _http_error(req.full_url, 402, "insufficient balance")

    old_open = urllib.request.urlopen
    old_which = vision_client.shutil.which
    urllib.request.urlopen = _unpaid
    vision_client.shutil.which = lambda name: f"/fake/bin/{name}"
    with _EnvSandbox():
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["OC_AUTH_FILE"] = _write_oc_auth(tmpdir, {})
            os.environ["OC_CONFIG_FILE"] = _write_oc_config(
                tmpdir,
                {"fireworks": {"options": {"apiKey": "fw_present"}}},
            )
            try:
                verdict = vision_client.call_ai_vision(
                    "oc:fireworks/accounts/fireworks/models/gpt-4o",
                    blocks=[],
                )
            finally:
                urllib.request.urlopen = old_open
                vision_client.shutil.which = old_which
    assert verdict.available is False, verdict
    assert verdict.error and "preflight" in verdict.error, verdict.error


def test_payment_preflight_uses_opencode_config_base_url_with_config_key():
    captured: dict = {}

    def _capture(req, timeout=None):
        captured["url"] = req.full_url
        captured["auth"] = req.headers.get("Authorization")
        return _FakeResp({"data": []})

    old_open = urllib.request.urlopen
    urllib.request.urlopen = _capture
    with _EnvSandbox():
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["OC_AUTH_FILE"] = _write_oc_auth(tmpdir, {})
            os.environ["OC_CONFIG_FILE"] = _write_oc_config(
                tmpdir,
                {
                    "fireworks": {
                        "options": {
                            "baseURL": "https://proxy.example.test/fireworks/v1",
                            "apiKey": "fw_config_key",
                        }
                    }
                },
            )
            try:
                assert (
                    backends._provider_payment_preflight_unavailable_reason(
                        "oc:fireworks/accounts/fireworks/models/gpt-4o"
                    )
                    is None
                )
            finally:
                urllib.request.urlopen = old_open
    assert captured["url"] == "https://proxy.example.test/fireworks/v1/models", captured
    assert captured["auth"] == "Bearer fw_config_key", captured


def test_payment_preflight_uses_opencode_config_base_url_with_auth_key():
    captured: dict = {}

    def _capture(req, timeout=None):
        captured["url"] = req.full_url
        captured["auth"] = req.headers.get("Authorization")
        return _FakeResp({"data": []})

    old_open = urllib.request.urlopen
    urllib.request.urlopen = _capture
    with _EnvSandbox():
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["OC_AUTH_FILE"] = _write_oc_auth(
                tmpdir, {"fireworks": {"key": "fw_auth_key"}}
            )
            os.environ["OC_CONFIG_FILE"] = _write_oc_config(
                tmpdir,
                {
                    "fireworks": {
                        "options": {
                            "baseURL": "https://proxy.example.test/fireworks/v1",
                        }
                    }
                },
            )
            try:
                assert (
                    backends._provider_payment_preflight_unavailable_reason(
                        "oc:fireworks/accounts/fireworks/models/gpt-4o"
                    )
                    is None
                )
            finally:
                urllib.request.urlopen = old_open
    assert captured["url"] == "https://proxy.example.test/fireworks/v1/models", captured
    assert captured["auth"] == "Bearer fw_auth_key", captured


def test_payment_preflight_uses_opencode_config_base_url_with_env_key():
    captured: dict = {}

    def _capture(req, timeout=None):
        captured["url"] = req.full_url
        captured["auth"] = req.headers.get("Authorization")
        return _FakeResp({"data": []})

    old_open = urllib.request.urlopen
    urllib.request.urlopen = _capture
    with _EnvSandbox():
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["FIREWORKS_API_KEY"] = "fw_env_key"
            os.environ["OC_AUTH_FILE"] = _write_oc_auth(tmpdir, {})
            os.environ["OC_CONFIG_FILE"] = _write_oc_config(
                tmpdir,
                {
                    "fireworks": {
                        "options": {
                            "baseURL": "https://proxy.example.test/fireworks/v1",
                        }
                    }
                },
            )
            try:
                assert (
                    backends._provider_payment_preflight_unavailable_reason(
                        "oc:fireworks/accounts/fireworks/models/gpt-4o"
                    )
                    is None
                )
            finally:
                urllib.request.urlopen = old_open
    assert captured["url"] == "https://proxy.example.test/fireworks/v1/models", captured
    assert captured["auth"] == "Bearer fw_env_key", captured


def test_payment_preflight_denial_is_cached_for_provider_key_url():
    calls = {"n": 0}

    def _unpaid(req, timeout=None):
        calls["n"] += 1
        raise _http_error(req.full_url, 402, "insufficient credits")

    old_open = urllib.request.urlopen
    urllib.request.urlopen = _unpaid
    with _EnvSandbox():
        os.environ["COMMANDCODE_API_KEY"] = "user_present"
        try:
            first = backends._provider_payment_preflight_unavailable_reason(
                "commandcode:deepseek/deepseek-v4-pro"
            )
            second = backends._provider_payment_preflight_unavailable_reason(
                "commandcode:moonshotai/Kimi-K2.7-Code"
            )
        finally:
            urllib.request.urlopen = old_open
    assert first and "deepseek" in first, first
    assert second and "Kimi" in second, second
    assert calls["n"] == 1, calls


def test_preflight_failed_provider_does_not_make_startup_split_do_network():
    """Startup failover is a cheap offline check; runtime dispatch owns payment preflight."""

    def _no_network(req, timeout=None):
        raise AssertionError(f"startup availability made network: {req.full_url}")

    old_open = urllib.request.urlopen
    old_which = backends._which
    urllib.request.urlopen = _no_network
    backends._which = lambda name: f"/fake/bin/{name}"
    with _EnvSandbox():
        os.environ["FIREWORKS_API_KEY"] = "fw_present"
        try:
            board = [
                BoardReviewer(
                    "oc:fireworks/accounts/fireworks/models/kimi", "tests", "Fireworks"
                ),
                BoardReviewer("codex", "correctness", "Codex"),
            ]
            pool, reserve = split_pool_reserve(
                board, 1, lambda reviewer: backends.backend_available(reviewer.model)
            )
        finally:
            urllib.request.urlopen = old_open
            backends._which = old_which
    assert [seat.model for seat in pool] == [
        "oc:fireworks/accounts/fireworks/models/kimi"
    ], pool
    assert [seat.model for seat in reserve] == ["codex"], reserve


def test_backend_available_skips_unpaid_provider_before_key_or_cli_checks():
    """A provider marked unpaid/disabled is skipped immediately for direct and oc seats."""
    old_open = urllib.request.urlopen
    urllib.request.urlopen = _allow_preflight
    with _EnvSandbox():
        try:
            os.environ["COMMANDCODE_API_KEY"] = "user_present"
            assert (
                backends.backend_available("commandcode:deepseek/deepseek-v4-pro")
                is True
            )
            os.environ["REVIEW_UNPAID_PROVIDERS"] = " commandcode, fireworks "
            assert backends.backend_available("commandcode") is False
            assert (
                backends.backend_available("commandcode:deepseek/deepseek-v4-pro")
                is False
            )
            # These return False without needing opencode on PATH because the entitlement
            # denylist is checked before the backend's CLI/auth probe.
            assert (
                backends.backend_available("oc:commandcode/deepseek/deepseek-v4-pro")
                is False
            )
            assert (
                backends.backend_available(
                    "oc:fireworks/accounts/fireworks/models/fable-5"
                )
                is False
            )
        finally:
            urllib.request.urlopen = old_open


def test_unpaid_oc_provider_skip_wins_even_when_opencode_auth_exists():
    """Pin the unpaid branch for `oc:` seats, not merely a missing-opencode false result."""
    old_which = backends._which
    old_open = urllib.request.urlopen
    backends._which = lambda name: (
        "/fake/bin/opencode" if name == "opencode" else old_which(name)
    )
    urllib.request.urlopen = _allow_preflight
    try:
        with _EnvSandbox():
            with tempfile.TemporaryDirectory() as tmp:
                auth = Path(tmp) / "auth.json"
                auth.write_text(
                    json.dumps({"commandcode": {"key": "opencode-key"}}),
                    encoding="utf-8",
                )
                os.environ["OC_AUTH_FILE"] = str(auth)
                assert (
                    backends.backend_available(
                        "oc:commandcode/deepseek/deepseek-v4-pro"
                    )
                    is True
                )
                os.environ["REVIEW_UNPAID_PROVIDERS"] = "commandcode"
                assert (
                    backends.backend_available(
                        "oc:commandcode/deepseek/deepseek-v4-pro"
                    )
                    is False
                )
    finally:
        backends._which = old_which
        urllib.request.urlopen = old_open


def test_oc_zai_requires_opencode_auth_not_zai_rest_key():
    old_which = backends._which
    backends._which = lambda name: (
        "/fake/bin/opencode" if name == "opencode" else old_which(name)
    )
    try:
        with _EnvSandbox():
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["OC_AUTH_FILE"] = str(Path(tmp) / "auth.json")
                os.environ["OC_CONFIG_FILE"] = str(Path(tmp) / "opencode.json")
                os.environ["ZAI_API_KEY"] = "direct-rest-key-only"
                assert backends.backend_available("zai:glm-5.2") is True
                assert backends.backend_available("oc:zai/glm-5.2") is False
    finally:
        backends._which = old_which


def test_configured_unpaid_provider_skips_without_env():
    """CLI-loaded config.yaml unpaid_providers feeds the same availability gate as env."""
    saved = backends._CONFIG_UNPAID_PROVIDERS
    with _EnvSandbox():
        os.environ["COMMANDCODE_API_KEY"] = "user_present"
        try:
            backends.configure_unpaid_providers(["commandcode"])
            assert (
                backends.backend_available("commandcode:deepseek/deepseek-v4-pro")
                is False
            )
        finally:
            backends._CONFIG_UNPAID_PROVIDERS = saved


def test_configure_unpaid_providers_accepts_saved_frozenset():
    saved = backends._CONFIG_UNPAID_PROVIDERS
    with _EnvSandbox():
        try:
            backends.configure_unpaid_providers(frozenset({"commandcode"}))
            assert (
                backends.provider_marked_unpaid("commandcode:deepseek/deepseek-v4-pro")
                is True
            )
        finally:
            backends._CONFIG_UNPAID_PROVIDERS = saved


def test_commandcode_unpaid_provider_does_not_post():
    """Explicit `-m commandcode:*` fails fast when billing is disabled."""

    def _should_not_post(
        req, timeout=None
    ):  # pragma: no cover - asserted by not raising
        raise AssertionError("commandcode POSTed despite REVIEW_UNPAID_PROVIDERS")

    old_open = urllib.request.urlopen
    urllib.request.urlopen = _should_not_post
    with _EnvSandbox():
        os.environ["COMMANDCODE_API_KEY"] = "user_present"
        os.environ["REVIEW_UNPAID_PROVIDERS"] = "commandcode"
        try:
            res = backends.review_commandcode(
                "commandcode:deepseek/deepseek-v4-pro", "q", "", REPO_ROOT, 10
            )
        finally:
            urllib.request.urlopen = old_open
    assert res.returncode == 1, res
    assert "unpaid/disabled" in res.stderr, res.stderr


def test_codex_unpaid_provider_does_not_spawn_cli():
    """Agent CLI providers are also gated before PATH lookup or subprocess spawn."""
    old_which = backends._which
    backends._which = lambda _name: (_ for _ in ()).throw(
        AssertionError("codex CLI was probed")
    )
    with _EnvSandbox():
        os.environ["REVIEW_UNPAID_PROVIDERS"] = "codex"
        try:
            res = backends.review_codex("codex", "q", "", REPO_ROOT, 10)
        finally:
            backends._which = old_which
    assert res.returncode == 1, res
    assert "unpaid/disabled" in res.stderr, res.stderr


def test_opencode_unpaid_provider_does_not_spawn_cli():
    """`oc:provider/model` seats are skipped before read-only agent setup or launch."""
    old_ensure = backends._ensure_opencode_readonly_agent
    backends._ensure_opencode_readonly_agent = lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("opencode setup ran")
    )
    with _EnvSandbox():
        os.environ["REVIEW_UNPAID_PROVIDERS"] = "commandcode"
        try:
            res = backends.review_opencode(
                "oc:commandcode/deepseek/deepseek-v4-pro", "q", "", REPO_ROOT, 10
            )
        finally:
            backends._ensure_opencode_readonly_agent = old_ensure
    assert res.returncode == 1, res
    assert "unpaid/disabled" in res.stderr, res.stderr


def test_claude_unpaid_alias_provider_does_not_spawn_cli():
    """Unexpanded Claude aliases must be skipped before CLI probing or launch."""
    old_which = backends._which_optional
    backends._which_optional = lambda _name: (_ for _ in ()).throw(
        AssertionError("claude CLI was probed")
    )
    with _EnvSandbox():
        os.environ["REVIEW_UNPAID_PROVIDERS"] = "claude"
        try:
            res = backends.review_claude("fable5", "q", "", REPO_ROOT, 10)
        finally:
            backends._which_optional = old_which
    assert res.returncode == 1, res
    assert "unpaid/disabled" in res.stderr, res.stderr


def test_unpaid_provider_uses_canonical_commandcode_aliases():
    """Legacy command/common-code spellings must hit the same payment gate."""
    with _EnvSandbox():
        os.environ["REVIEW_UNPAID_PROVIDERS"] = "commandcode"
        assert (
            backends.effective_provider("command-code:deepseek/deepseek-v4-pro")
            == "commandcode"
        )
        assert (
            backends.effective_provider("common-code:deepseek/deepseek-v4-pro")
            == "commandcode"
        )
        assert (
            backends.provider_marked_unpaid("command-code:deepseek/deepseek-v4-pro")
            is True
        )
        assert (
            backends.provider_marked_unpaid("common-code:deepseek/deepseek-v4-pro")
            is True
        )


def test_unpaid_provider_uses_canonical_zai_aliases():
    """Every accepted z.ai/Zhipu/GLM spelling must hit the same payment gate."""
    with _EnvSandbox():
        os.environ["REVIEW_UNPAID_PROVIDERS"] = "zai"
        assert backends.effective_provider("z.ai:glm-5.2") == "zai"
        assert backends.effective_provider("zhipu:glm-5.2") == "zai"
        assert backends.effective_provider("glm:glm-5.2") == "zai"
        assert backends.effective_provider("oc:z.ai/glm-5.2") == "zai"
        assert backends.provider_marked_unpaid("z.ai:glm-5.2") is True
        assert backends.provider_marked_unpaid("zhipu:glm-5.2") is True
        assert backends.provider_marked_unpaid("glm:glm-5.2") is True
        assert backends.backend_available("oc:z.ai/glm-5.2") is False


def test_unpaid_provider_uses_canonical_gemini_and_claude_aliases():
    """Every named Gemini/Claude alias must hit the same payment gate."""
    with _EnvSandbox():
        os.environ["REVIEW_UNPAID_PROVIDERS"] = "gemini"
        assert backends.effective_provider("gemini-api") == "gemini"
        assert backends.provider_marked_unpaid("gemini-api") is True
        assert backends.backend_available("gemini-api") is False

    with _EnvSandbox():
        os.environ["REVIEW_UNPAID_PROVIDERS"] = "claude"
        assert backends.effective_provider("claude-p") == "claude"
        assert backends.effective_provider("claude-fable-5") == "claude"
        assert backends.effective_provider("fable5") == "claude"
        assert backends.provider_marked_unpaid("claude-p") is True
        assert backends.provider_marked_unpaid("claude-fable-5") is True
        assert backends.provider_marked_unpaid("fable5") is True


def test_unpaid_provider_env_uses_canonical_commandcode_aliases():
    """Aliases are normalized in the env/config value, not only in model ids."""
    with _EnvSandbox():
        os.environ["REVIEW_UNPAID_PROVIDERS"] = "common-code"
        assert (
            backends.provider_marked_unpaid("commandcode:deepseek/deepseek-v4-pro")
            is True
        )
        os.environ["REVIEW_UNPAID_PROVIDERS"] = "cc"
        assert (
            backends.provider_marked_unpaid("commandcode:deepseek/deepseek-v4-pro")
            is True
        )


def test_zai_unpaid_provider_does_not_post():
    """Every direct REST provider must fail fast when billing is disabled, not only commandcode."""

    def _should_not_post(
        req, timeout=None
    ):  # pragma: no cover - asserted by not raising
        raise AssertionError("z.ai POSTed despite REVIEW_UNPAID_PROVIDERS")

    old_open = urllib.request.urlopen
    urllib.request.urlopen = _should_not_post
    with _EnvSandbox():
        os.environ["ZAI_API_KEY"] = "zai_present"
        os.environ["REVIEW_UNPAID_PROVIDERS"] = "zai"
        try:
            res = backends.review_zai("zai:glm-5.2", "q", "", REPO_ROOT, 10)
        finally:
            urllib.request.urlopen = old_open
    assert res.returncode == 1, res
    assert "unpaid/disabled" in res.stderr, res.stderr


def test_zai_unpaid_alias_provider_does_not_post():
    """Accepted z.ai aliases are gated before the REST request, not only the zai prefix."""

    def _should_not_post(
        req, timeout=None
    ):  # pragma: no cover - asserted by not raising
        raise AssertionError("z.ai alias POSTed despite REVIEW_UNPAID_PROVIDERS")

    old_open = urllib.request.urlopen
    urllib.request.urlopen = _should_not_post
    with _EnvSandbox():
        os.environ["ZAI_API_KEY"] = "zai_present"
        os.environ["REVIEW_UNPAID_PROVIDERS"] = "zai"
        try:
            res = backends.review_zai("z.ai:glm-5.2", "q", "", REPO_ROOT, 10)
        finally:
            urllib.request.urlopen = old_open
    assert res.returncode == 1, res
    assert "unpaid/disabled" in res.stderr, res.stderr


def test_unpaid_provider_logs_use_model_specific_headers():
    """Unpaid sidecar logs still need exact model argv0s for dashboard attribution."""
    emitted = []
    old_emit = backends._emit_rest_log
    backends._emit_rest_log = lambda backend, command, **kwargs: emitted.append(
        (backend, command)
    )
    with _EnvSandbox():
        try:
            os.environ["REVIEW_UNPAID_PROVIDERS"] = (
                "zai,commandcode,openrouter,claude,gemini"
            )
            backends.review_zai("z.ai:glm-5.2", "q", "", REPO_ROOT, 10)
            backends.review_commandcode(
                "commandcode:deepseek/deepseek-v4-pro", "q", "", REPO_ROOT, 10
            )
            backends.review_openrouter(
                "openrouter:anthropic/claude-3.5-sonnet", "q", "", REPO_ROOT, 10
            )
            backends.review_claude("claude:claude-fable-5", "q", "", REPO_ROOT, 10)
            backends.review_gemini("gemini:gemini-3.5-flash", "q", "", REPO_ROOT, 10)
        finally:
            backends._emit_rest_log = old_emit
    assert emitted == [
        ("z.ai", "z.ai API glm-5.2"),
        ("commandcode", "commandcode API deepseek/deepseek-v4-pro"),
        ("openrouter", "openrouter API anthropic/claude-3.5-sonnet"),
        ("claude", "Anthropic API claude-fable-5"),
        ("gemini", "Gemini API gemini-3.5-flash"),
    ]


def test_gemini_bare_seat_resolves_to_current_default_model():
    """The bare `gemini` seat must POST to a CURRENT, non-retired model.

    `gemini-2.5-flash` (the old default) shuts down 2026-10-16 and was already
    404ing the review pool's gemini seat (issue #139); `gemini-3.5-flash` is the
    GA successor Google names as its replacement (no shutdown date). This guards
    the default so a stale id can't silently 404 every gemini review again -- a
    dead seat that would inflate the self-merge quorum's distinct-model count.
    """
    captured: dict = {}
    payload = {
        "candidates": [{"content": {"parts": [{"text": "looks good"}]}}],
        "usageMetadata": {},
    }
    old_open = urllib.request.urlopen
    old_key = backends._gemini_key
    old_model = os.environ.pop("GEMINI_MODEL", None)
    backends._gemini_key = lambda: "fake-key"
    urllib.request.urlopen = _fake_urlopen(captured, payload)
    try:
        with _EnvSandbox():
            result = backends.review_gemini("gemini", "q", "", REPO_ROOT, 10)
    finally:
        urllib.request.urlopen = old_open
        backends._gemini_key = old_key
        if old_model is not None:
            os.environ["GEMINI_MODEL"] = old_model
    assert "models/gemini-3.5-flash:generateContent" in captured["url"], captured.get(
        "url"
    )
    assert result.command == "Gemini API gemini-3.5-flash", result.command
    assert result.returncode == 0, result.stderr


def test_gemini_model_env_override_still_honored():
    """An explicit $GEMINI_MODEL must still win over the default -- the fix only
    moves the FALLBACK, it must not hardcode the model."""
    captured: dict = {}
    payload = {
        "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
        "usageMetadata": {},
    }
    old_open = urllib.request.urlopen
    old_key = backends._gemini_key
    old_model = os.environ.get("GEMINI_MODEL")
    backends._gemini_key = lambda: "fake-key"
    urllib.request.urlopen = _fake_urlopen(captured, payload)
    try:
        with _EnvSandbox():
            os.environ["GEMINI_MODEL"] = "gemini-3.1-flash-lite"
            result = backends.review_gemini("gemini", "q", "", REPO_ROOT, 10)
    finally:
        urllib.request.urlopen = old_open
        backends._gemini_key = old_key
        if old_model is None:
            os.environ.pop("GEMINI_MODEL", None)
        else:
            os.environ["GEMINI_MODEL"] = old_model
    assert "models/gemini-3.1-flash-lite:generateContent" in captured["url"], (
        captured.get("url")
    )
    assert result.command == "Gemini API gemini-3.1-flash-lite", result.command


def test_unpaid_provider_log_failure_still_returns_skip_result():
    """A sidecar write failure must not turn a deliberate unpaid skip into an internal crash."""
    old_emit = backends._emit_rest_log

    def _raise_oserror(*args, **kwargs):
        raise OSError("disk full")

    backends._emit_rest_log = _raise_oserror
    with _EnvSandbox():
        try:
            os.environ["REVIEW_UNPAID_PROVIDERS"] = "zai"
            res = backends.review_zai("zai:glm-5.2", "q", "", REPO_ROOT, 10)
        finally:
            backends._emit_rest_log = old_emit
    assert res.returncode == 1, res
    assert "provider 'zai'" in res.stderr, res.stderr


def test_claude_with_images_unpaid_provider_does_not_spawn_cli():
    """The Claude raw-image special case must not bypass the unpaid-provider gate."""
    old_which = backends._which_optional
    backends._which_optional = lambda _name: (_ for _ in ()).throw(
        AssertionError("claude CLI was probed")
    )
    with _EnvSandbox():
        os.environ["REVIEW_UNPAID_PROVIDERS"] = "claude"
        try:
            res = backends.review_with_images(
                "claude:claude-opus-4-8",
                "q",
                "",
                REPO_ROOT,
                10,
                images=(Path("shot.png"),),
            )
        finally:
            backends._which_optional = old_which
    assert res.returncode == 1, res
    assert "unpaid/disabled" in res.stderr, res.stderr


def test_backend_available_false_for_forced_api_only_cli_mode():
    """Codex P2: with REVIEW_COMMANDCODE_MODE=cli (an unrunnable forced mode), the
    availability probe must report False so the moderator/brainstorm filter never
    selects a backend that can only return a dead-backend result — even with a key."""
    old_open = urllib.request.urlopen
    urllib.request.urlopen = _allow_preflight
    with _EnvSandbox():
        try:
            os.environ["COMMANDCODE_API_KEY"] = "user_present"
            assert backends.backend_available("commandcode") is True
            os.environ["REVIEW_COMMANDCODE_MODE"] = "cli"
            assert backends.backend_available("commandcode") is False
            # z.ai is api-only too: a forced cli mode makes it unavailable.
            os.environ["ZAI_API_KEY"] = "k"
            assert backends.backend_available("zai") is True
            os.environ["REVIEW_ZAI_MODE"] = "cli"
            assert backends.backend_available("zai") is False
        finally:
            urllib.request.urlopen = old_open


def test_zai_key_missing_raises():
    with _EnvSandbox():
        raised = False
        try:
            backends._zai_key()
        except RuntimeError:
            raised = True
        assert raised, "_zai_key must raise RuntimeError when no key is configured"


# === HTTP error → non-zero returncode (panel treats it as a dead backend) =======
def test_zai_http_error_maps_to_returncode():
    import urllib.error

    def _raise(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url,
            401,
            "Unauthorized",
            hdrs=None,
            fp=__import__("io").BytesIO(b'{"error":"bad key"}'),
        )

    old_open = urllib.request.urlopen
    urllib.request.urlopen = _raise
    with _EnvSandbox():
        os.environ["ZAI_API_KEY"] = "k"
        try:
            res = backends.review_zai("zai", "q", "", REPO_ROOT, 10)
        finally:
            urllib.request.urlopen = old_open
    assert res.returncode == 401
    assert "bad key" in res.stderr
    assert res.stdout == ""


def test_zai_connection_refused_maps_to_returncode():
    """URLError (connection refused / DNS failure) must normalise to a non-zero
    ReviewResult, not escape — the docstring promises transport errors are caught."""
    import urllib.error

    def _raise(req, timeout=None):
        raise urllib.error.URLError("Connection refused")

    old_open = urllib.request.urlopen
    urllib.request.urlopen = _raise
    with _EnvSandbox():
        os.environ["ZAI_API_KEY"] = "k"
        try:
            res = backends.review_zai("zai", "q", "", REPO_ROOT, 10)
        finally:
            urllib.request.urlopen = old_open
    assert res.returncode == 1, res.returncode
    assert res.stdout == ""
    assert "Connection refused" in res.stderr


def test_commandcode_socket_timeout_maps_to_returncode():
    """A socket timeout (TimeoutError, an OSError subclass) must be caught and mapped to
    the TIMEOUT code 124 — not a generic rc=1 — so the dashboard counts it as a timeout,
    consistent with review_gemini and the subprocess backends (HYP-742)."""

    def _raise(req, timeout=None):
        raise TimeoutError("timed out")

    old_open = urllib.request.urlopen
    urllib.request.urlopen = _raise
    with _EnvSandbox():
        os.environ["COMMANDCODE_API_KEY"] = "k"
        try:
            res = backends.review_commandcode("commandcode", "q", "", REPO_ROOT, 10)
        finally:
            urllib.request.urlopen = old_open
    assert res.returncode == 124, res.returncode
    assert res.stdout == ""
    assert "timed out" in res.stderr


def test_zai_malformed_json_maps_to_returncode():
    """A 2xx response with a non-JSON / truncated body must map to a non-zero result,
    not raise JSONDecodeError out of the backend."""

    class _GarbageResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"<html>not json</html>"

    def _fake(req, timeout=None):
        return _GarbageResp()

    old_open = urllib.request.urlopen
    urllib.request.urlopen = _fake
    with _EnvSandbox():
        os.environ["ZAI_API_KEY"] = "k"
        try:
            res = backends.review_zai("zai", "q", "", REPO_ROOT, 10)
        finally:
            urllib.request.urlopen = old_open
    assert res.returncode == 1, res.returncode
    assert res.stdout == ""
    assert "malformed" in res.stderr.lower()


# === OpenRouter (OpenAI-compatible aggregator) ==================================
def test_resolve_backend_routes_openrouter():
    for name in ("openrouter", "OpenRouter", "OPENROUTER"):
        assert backends.resolve_backend(name) is backends.review_openrouter, name
    assert (
        backends.resolve_backend("openrouter:anthropic/claude-3.5-sonnet")
        is backends.review_openrouter
    )
    assert (
        backends.resolve_backend("openrouter:openai/gpt-4o")
        is backends.review_openrouter
    )
    # OpenRouter must NOT steal opencode's `oc:`/`opencode:` agentic routing.
    assert backends.resolve_backend("oc:fireworks/x") is backends.review_opencode
    assert (
        backends.resolve_backend("opencode:provider/model") is backends.review_opencode
    )


def test_openrouter_request_is_openai_shape():
    captured: dict = {}
    payload = {
        "choices": [{"message": {"content": "review from openrouter"}}],
        "usage": {"prompt_tokens": 9, "completion_tokens": 4},
    }
    old_open = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen(captured, payload)
    with _EnvSandbox():
        os.environ["OPENROUTER_API_KEY"] = "sk-or-v1-secret"
        try:
            res = backends.review_openrouter("openrouter", "say hi", "", REPO_ROOT, 30)
        finally:
            urllib.request.urlopen = old_open
    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions", captured[
        "url"
    ]
    assert captured["method"] == "POST"
    body = captured["body"]
    assert "messages" in body and "contents" not in body, body
    # Bare `openrouter` → OPENROUTER_DEFAULT_MODEL (the never-stale auto-router).
    assert body["model"] == "openrouter/auto", body
    assert body["messages"][0]["role"] == "user"
    assert body["messages"][0]["content"] == "say hi"
    assert body["stream"] is False
    assert captured["headers"].get("authorization") == "Bearer sk-or-v1-secret"
    assert "x-goog-api-key" not in captured["headers"]
    assert res.returncode == 0
    assert "review from openrouter" in res.stdout
    assert "prompt_tokens=9 output_tokens=4" in res.stdout


def test_openrouter_model_suffix_preserves_slug_and_variant():
    """`split(':',1)[1]` must strip ONLY the `openrouter:` prefix — preserving the slug's
    embedded `/` AND any trailing `:variant` colon (OpenRouter's :free/:beta/:nitro)."""
    cases = (
        ("openrouter:anthropic/claude-3.5-sonnet", "anthropic/claude-3.5-sonnet"),
        ("openrouter:openai/gpt-4o", "openai/gpt-4o"),
        (
            "openrouter:anthropic/claude-3.5-sonnet:beta",
            "anthropic/claude-3.5-sonnet:beta",
        ),
        (
            "openrouter:meta-llama/llama-3.1-70b-instruct:free",
            "meta-llama/llama-3.1-70b-instruct:free",
        ),
    )
    for seat, wire in cases:
        captured: dict = {}
        payload = {"choices": [{"message": {"content": "x"}}], "usage": {}}
        old_open = urllib.request.urlopen
        urllib.request.urlopen = _fake_urlopen(captured, payload)
        with _EnvSandbox():
            os.environ["OPENROUTER_API_KEY"] = "k"
            try:
                res = backends.review_openrouter(seat, "q", "", REPO_ROOT, 10)
            finally:
                urllib.request.urlopen = old_open
        assert captured["body"]["model"] == wire, (seat, captured["body"])
        # ReviewResult.model is the REQUESTED string (mode_review keys on it), not the wire id.
        assert res.model == seat, res.model
        assert wire in res.command, res.command


def test_openrouter_diff_is_fenced_in_message():
    captured: dict = {}
    payload = {"choices": [{"message": {"content": "ok"}}], "usage": {}}
    old_open = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen(captured, payload)
    with _EnvSandbox():
        os.environ["OPENROUTER_API_KEY"] = "k"
        try:
            backends.review_openrouter(
                "openrouter", "review this", "+added line", REPO_ROOT, 10
            )
        finally:
            urllib.request.urlopen = old_open
    content = captured["body"]["messages"][0]["content"]
    assert "review this" in content
    assert "```diff" in content and "+added line" in content


def test_openrouter_base_url_and_model_env_override():
    captured: dict = {}
    payload = {"choices": [{"message": {"content": "x"}}], "usage": {}}
    old_open = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen(captured, payload)
    with _EnvSandbox():
        os.environ["OPENROUTER_API_KEY"] = "k"
        os.environ["OPENROUTER_BASE_URL"] = "https://proxy.example.test/v1"
        os.environ["OPENROUTER_MODEL"] = "google/gemini-flash-1.5"
        try:
            backends.review_openrouter("openrouter", "q", "", REPO_ROOT, 10)
        finally:
            urllib.request.urlopen = old_open
    assert captured["url"] == "https://proxy.example.test/v1/chat/completions", (
        captured["url"]
    )
    assert captured["body"]["model"] == "google/gemini-flash-1.5", captured["body"]


def test_openrouter_suffix_beats_model_env():
    """A `openrouter:<slug>` seat takes the suffix UNCONDITIONALLY — a host's
    OPENROUTER_MODEL (a legit override for the bare seat) must NOT hijack a suffixed seat."""
    captured: dict = {}
    payload = {"choices": [{"message": {"content": "x"}}], "usage": {}}
    old_open = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen(captured, payload)
    with _EnvSandbox():
        os.environ["OPENROUTER_API_KEY"] = "k"
        os.environ["OPENROUTER_MODEL"] = "should/be-ignored"
        try:
            backends.review_openrouter(
                "openrouter:openai/gpt-4o", "q", "", REPO_ROOT, 10
            )
        finally:
            urllib.request.urlopen = old_open
    assert captured["body"]["model"] == "openai/gpt-4o", captured["body"]


def test_openrouter_optional_attribution_headers_from_env():
    """HTTP-Referer / X-Title are sent ONLY when their env vars are set, and are absent by
    default — they affect openrouter.ai leaderboard attribution, never the review."""
    # Unset → absent.
    captured: dict = {}
    payload = {"choices": [{"message": {"content": "ok"}}], "usage": {}}
    old_open = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen(captured, payload)
    with _EnvSandbox():
        os.environ["OPENROUTER_API_KEY"] = "k"
        try:
            backends.review_openrouter("openrouter", "q", "", REPO_ROOT, 10)
        finally:
            urllib.request.urlopen = old_open
    assert "http-referer" not in captured["headers"], captured["headers"]
    assert "x-title" not in captured["headers"], captured["headers"]
    # Set → present, verbatim.
    captured2: dict = {}
    urllib.request.urlopen = _fake_urlopen(captured2, payload)
    with _EnvSandbox():
        os.environ["OPENROUTER_API_KEY"] = "k"
        os.environ["OPENROUTER_HTTP_REFERER"] = "https://review.example"
        os.environ["OPENROUTER_X_TITLE"] = "review-cli"
        try:
            backends.review_openrouter("openrouter", "q", "", REPO_ROOT, 10)
        finally:
            urllib.request.urlopen = old_open
    assert captured2["headers"].get("http-referer") == "https://review.example", (
        captured2["headers"]
    )
    assert captured2["headers"].get("x-title") == "review-cli", captured2["headers"]
    # The bearer auth is untouched by the optional headers.
    assert captured2["headers"].get("authorization") == "Bearer k"


def test_openrouter_extra_headers_cannot_shadow_authorization():
    """A hostile/stray Authorization in extra_headers must NOT override the real bearer key —
    _openai_compatible_request drops any case-insensitive authorization/content-type from
    extra_headers before writing the canonical pair. Covers BOTH a capitalized `Authorization`
    AND a lower-cased `authorization` (a distinct dict key that the explicit filter must catch,
    not a reliance on urllib's header capitalization)."""
    for stray in ("Authorization", "authorization", "AUTHORIZATION"):
        captured: dict = {}
        payload = {"choices": [{"message": {"content": "ok"}}], "usage": {}}
        old_open = urllib.request.urlopen
        urllib.request.urlopen = _fake_urlopen(captured, payload)
        try:
            backends._openai_compatible_request(
                model="openrouter",
                api_model="openrouter/auto",
                label="openrouter",
                base_url="https://openrouter.ai/api/v1",
                key="real-key",
                prompt="q",
                diff="",
                timeout=10,
                backend="openrouter",
                extra_headers={
                    stray: "Bearer STOLEN",
                    "content-type": "text/evil",
                    "X-Title": "t",
                },
            )
        finally:
            urllib.request.urlopen = old_open
        # _fake_urlopen lower-cases every captured header name, so a single normalized
        # `authorization`/`content-type` entry proves the stray was dropped (no duplicate).
        assert captured["headers"].get("authorization") == "Bearer real-key", (
            stray,
            captured["headers"],
        )
        assert captured["headers"].get("content-type") == "application/json", (
            stray,
            captured["headers"],
        )
        assert captured["headers"].get("x-title") == "t", (stray, captured["headers"])


def test_openrouter_empty_suffix_falls_back_to_default():
    """An EMPTY suffix (`openrouter:` / `openrouter: `) must NOT POST `model: ""` (a 400) —
    it falls back to OPENROUTER_MODEL, then the auto-router default."""
    # Empty suffix, no env → auto-router default.
    for seat in ("openrouter:", "openrouter: "):
        captured: dict = {}
        payload = {"choices": [{"message": {"content": "ok"}}], "usage": {}}
        old_open = urllib.request.urlopen
        urllib.request.urlopen = _fake_urlopen(captured, payload)
        with _EnvSandbox():
            os.environ["OPENROUTER_API_KEY"] = "k"
            try:
                backends.review_openrouter(seat, "q", "", REPO_ROOT, 10)
            finally:
                urllib.request.urlopen = old_open
        assert captured["body"]["model"] == "openrouter/auto", (seat, captured["body"])
    # Empty suffix WITH OPENROUTER_MODEL set → the env model (not "" and not the default).
    captured2: dict = {}
    payload2 = {"choices": [{"message": {"content": "ok"}}], "usage": {}}
    urllib.request.urlopen = _fake_urlopen(captured2, payload2)
    with _EnvSandbox():
        os.environ["OPENROUTER_API_KEY"] = "k"
        os.environ["OPENROUTER_MODEL"] = "openai/gpt-4o"
        try:
            backends.review_openrouter("openrouter:", "q", "", REPO_ROOT, 10)
        finally:
            urllib.request.urlopen = old_open
    assert captured2["body"]["model"] == "openai/gpt-4o", captured2["body"]


def test_openrouter_whitespace_env_falls_back_to_default():
    """Symmetric with the empty-suffix guard: a whitespace-only OPENROUTER_MODEL /
    OPENROUTER_BASE_URL must NOT win (it would POST `model:"   "` / build a broken URL) — it
    falls through to the default, never POSTing the blank value."""
    captured: dict = {}
    payload = {"choices": [{"message": {"content": "ok"}}], "usage": {}}
    old_open = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen(captured, payload)
    with _EnvSandbox():
        os.environ["OPENROUTER_API_KEY"] = "k"
        os.environ["OPENROUTER_MODEL"] = "   "
        os.environ["OPENROUTER_BASE_URL"] = "   "
        try:
            backends.review_openrouter("openrouter", "q", "", REPO_ROOT, 10)
        finally:
            urllib.request.urlopen = old_open
    assert captured["body"]["model"] == "openrouter/auto", captured["body"]
    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions", captured[
        "url"
    ]


def test_openrouter_control_char_attribution_header_is_dropped():
    """A CR/LF in OPENROUTER_HTTP_REFERER / OPENROUTER_X_TITLE must be DROPPED, not sent —
    it would be a header-injection vector and would make http.client raise mid-send. The
    request still goes out (the headers are optional); only the unsafe header is omitted."""
    captured: dict = {}
    payload = {"choices": [{"message": {"content": "ok"}}], "usage": {}}
    old_open = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen(captured, payload)
    with _EnvSandbox():
        os.environ["OPENROUTER_API_KEY"] = "k"
        os.environ["OPENROUTER_HTTP_REFERER"] = "https://evil\r\nX-Injected: 1"
        os.environ["OPENROUTER_X_TITLE"] = "ok-title"  # clean → kept
        try:
            res = backends.review_openrouter("openrouter", "q", "", REPO_ROOT, 10)
        finally:
            urllib.request.urlopen = old_open
    assert res.returncode == 0, res
    assert "http-referer" not in captured["headers"], captured["headers"]
    assert "x-injected" not in captured["headers"], captured["headers"]
    assert captured["headers"].get("x-title") == "ok-title", captured["headers"]


def test_openrouter_non_latin1_attribution_header_is_dropped():
    """A non-latin-1 value (emoji/CJK) must be DROPPED, not sent — http.client encodes
    header values as latin-1, so it would raise UnicodeEncodeError mid-send. The request
    still succeeds (the attribution header is optional); only the unencodable header is
    omitted. A latin-1-printable accented value (é) is still allowed."""
    captured: dict = {}
    payload = {"choices": [{"message": {"content": "ok"}}], "usage": {}}
    old_open = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen(captured, payload)
    with _EnvSandbox():
        os.environ["OPENROUTER_API_KEY"] = "k"
        os.environ["OPENROUTER_X_TITLE"] = "review-cli ☕"  # ☕ — not latin-1
        os.environ["OPENROUTER_HTTP_REFERER"] = "https://café.example"  # é — latin-1 OK
        try:
            res = backends.review_openrouter("openrouter", "q", "", REPO_ROOT, 10)
        finally:
            urllib.request.urlopen = old_open
    assert res.returncode == 0, res
    assert "x-title" not in captured["headers"], captured["headers"]
    assert captured["headers"].get("http-referer") == "https://café.example", captured[
        "headers"
    ]


def test_openrouter_missing_key_is_a_dead_backend_result():
    """The no-key path through review_openrouter itself (the `except RuntimeError` branch):
    a missing OPENROUTER_API_KEY must yield a NON-zero ReviewResult (not raise out of the
    panel as an internal 127), and must not POST."""

    def _should_not_be_called(
        req, timeout=None
    ):  # pragma: no cover - asserted unreached
        raise AssertionError("api path POSTed despite a missing key")

    old_open = urllib.request.urlopen
    urllib.request.urlopen = _should_not_be_called
    with _EnvSandbox():
        # No OPENROUTER_API_KEY anywhere (sandbox points the .env fallback at /nonexistent).
        try:
            res = backends.review_openrouter("openrouter", "q", "", REPO_ROOT, 10)
        finally:
            urllib.request.urlopen = old_open
    assert res.returncode == 1, res
    assert res.stdout == "", res.stdout
    assert "OPENROUTER_API_KEY" in res.stderr, res.stderr


def test_openrouter_key_is_canonical_only():
    """SECURITY: OpenRouter must require OPENROUTER_API_KEY ONLY. A foreign OPENAI_API_KEY
    must NOT resolve here — otherwise an OpenAI credential would be POSTed to openrouter.ai
    (cross-provider key leak), and the backend would falsely report available."""
    payload = {"choices": [{"message": {"content": "ok"}}], "usage": {}}
    old_open = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen({}, payload)
    with _EnvSandbox():
        os.environ["OPENAI_API_KEY"] = "sk-openai-secret"
        try:
            assert backends.backend_available("openrouter") is False
            raised = False
            try:
                backends._openrouter_key()
            except RuntimeError:
                raised = True
            assert raised, "openrouter must not resolve a foreign OPENAI_API_KEY"
        finally:
            urllib.request.urlopen = old_open


def test_backend_available_reflects_openrouter_key():
    with _EnvSandbox():
        assert backends.backend_available("openrouter") is False
        os.environ["OPENROUTER_API_KEY"] = "sk-or-v1-present"
        assert backends.backend_available("openrouter") is True
        assert (
            backends.backend_available("openrouter:anthropic/claude-3.5-sonnet") is True
        )


def test_backend_available_false_for_forced_openrouter_cli_mode():
    """REVIEW_OPENROUTER_MODE=cli (an unrunnable forced mode) → unavailable even with a key,
    so the moderator/brainstorm filter never selects a backend that can only fail."""
    with _EnvSandbox():
        os.environ["OPENROUTER_API_KEY"] = "sk-or-v1-present"
        assert backends.backend_available("openrouter") is True
        os.environ["REVIEW_OPENROUTER_MODE"] = "cli"
        assert backends.backend_available("openrouter") is False


def test_openrouter_forced_cli_mode_is_a_dead_backend():
    """OpenRouter is api-only: REVIEW_OPENROUTER_MODE=cli must fail loudly, never POST."""

    def _should_not_be_called(
        req, timeout=None
    ):  # pragma: no cover - asserted unreached
        raise AssertionError("api path POSTed despite a forced cli mode")

    old_open = urllib.request.urlopen
    urllib.request.urlopen = _should_not_be_called
    with _EnvSandbox():
        os.environ["OPENROUTER_API_KEY"] = "k"
        os.environ["REVIEW_OPENROUTER_MODE"] = "cli"
        try:
            res = backends.review_openrouter("openrouter", "q", "", REPO_ROOT, 10)
        finally:
            urllib.request.urlopen = old_open
    assert res.returncode == 1, res
    assert "cli" in res.stderr and "openrouter" in res.stderr


def test_openrouter_no_content_2xx_fails_closed():
    """A 2xx with no assistant content must map to a NON-zero result (not let mode_review
    write a 'reviewed' stamp on an empty review). Shares the type-guarded parse with z.ai."""
    for payload in (
        {"choices": []},
        {"choices": [{"message": {"content": ""}}]},
        {"choices": [{"message": {"content": None}}]},
        {"error": {"message": "rate limited"}},
    ):
        old_open = urllib.request.urlopen
        urllib.request.urlopen = _fake_urlopen({}, payload)
        with _EnvSandbox():
            os.environ["OPENROUTER_API_KEY"] = "k"
            try:
                res = backends.review_openrouter("openrouter", "q", "", REPO_ROOT, 10)
            finally:
                urllib.request.urlopen = old_open
        assert res.returncode == 1, (payload, res)
        assert "no assistant content" in res.stderr, (payload, res.stderr)


def test_openrouter_http_error_maps_to_returncode():
    import urllib.error

    def _raise(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url,
            401,
            "Unauthorized",
            hdrs=None,
            fp=__import__("io").BytesIO(b'{"error":"invalid key"}'),
        )

    old_open = urllib.request.urlopen
    urllib.request.urlopen = _raise
    with _EnvSandbox():
        os.environ["OPENROUTER_API_KEY"] = "k"
        try:
            res = backends.review_openrouter("openrouter", "q", "", REPO_ROOT, 10)
        finally:
            urllib.request.urlopen = old_open
    assert res.returncode == 401, res.returncode
    assert "invalid key" in res.stderr
    assert res.stdout == ""


def test_openrouter_key_resolves_from_temp_env_file():
    import tempfile

    with _EnvSandbox():
        with tempfile.TemporaryDirectory() as d:
            env_path = Path(d) / ".env"
            env_path.write_text("OPENROUTER_API_KEY=or-from-file\n", encoding="utf-8")
            os.environ["GEMINI_ENV_FILE"] = str(env_path)
            assert backends._openrouter_key() == "or-from-file"


def test_mode_review_includes_an_openrouter_seat_without_keyerror():
    """A board/panel diff-review must format an openrouter seat result without KeyError —
    proves the seat is dispatchable alongside the other backends (panels reuse this path)."""
    from reviewlib.modes.review import mode_review

    payload = {"choices": [{"message": {"content": "openrouter says ok"}}], "usage": {}}
    old_open = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen({}, payload)
    with _EnvSandbox():
        os.environ["OPENROUTER_API_KEY"] = "k"
        try:
            rc = mode_review(
                ["openrouter:anthropic/claude-3.5-sonnet"],
                "review",
                "+added",
                REPO_ROOT,
                10,
                False,
            )
        finally:
            urllib.request.urlopen = old_open
    assert rc == 0, rc


# === opencode per-provider auth probe (review-cli#94) ===========================


def _write_oc_auth(directory: str, providers: dict) -> str:
    """Write a minimal auth.json to *directory* and return its path."""
    path = os.path.join(directory, "auth.json")
    with open(path, "w") as f:
        json.dump(providers, f)
    return path


def _write_oc_config(directory: str, providers: dict) -> str:
    """Write a minimal opencode.json to *directory* and return its path."""
    path = os.path.join(directory, "opencode.json")
    with open(path, "w") as f:
        json.dump({"provider": providers}, f)
    return path


def test_oc_bare_opencode_skips_provider_check():
    """Bare 'opencode' (no oc: prefix) must pass with just the binary present."""
    with _EnvSandbox():
        # Sanity: if opencode binary is installed this must return True without any
        # credential files set.  We cannot control the binary presence in tests,
        # so we just confirm that _oc_provider_from_model returns None for bare strings.
        assert backends._oc_provider_from_model("opencode") is None
        assert backends._oc_provider_from_model("some/model") is None


def test_oc_provider_from_model_extracts_prefix():
    """_oc_provider_from_model peels oc:/opencode: prefix and returns provider."""
    assert (
        backends._oc_provider_from_model("oc:anthropic/claude-3-5-sonnet")
        == "anthropic"
    )
    assert (
        backends._oc_provider_from_model("opencode:deepseek/deepseek-v3") == "deepseek"
    )
    assert (
        backends._oc_provider_from_model("oc:commandcode/moonshotai/kimi")
        == "commandcode"
    )
    assert backends._oc_provider_from_model("opencode") is None
    assert backends._oc_provider_from_model("codex") is None


def test_backend_available_oc_anthropic_env_var():
    """oc:anthropic/model is False without a key, True with ANTHROPIC_API_KEY.

    Mocks _which_optional so the opencode binary appears installed on hosts
    that don't have it (e.g. CI), keeping the test focused on credential
    logic rather than binary presence.
    """
    saved_which = backends._which_optional
    backends._which_optional = lambda name: (
        "/fake/bin/opencode" if name == "opencode" else saved_which(name)
    )
    try:
        with _EnvSandbox():
            with tempfile.TemporaryDirectory() as tmpdir:
                # Empty auth.json + no key → unavailable.
                os.environ["OC_AUTH_FILE"] = _write_oc_auth(tmpdir, {})
                os.environ["OC_CONFIG_FILE"] = _write_oc_config(tmpdir, {})
                assert (
                    backends.backend_available("oc:anthropic/claude-sonnet-4-5")
                    is False
                )
                # Set env var → available.
                os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
                assert (
                    backends.backend_available("oc:anthropic/claude-sonnet-4-5") is True
                )
    finally:
        backends._which_optional = saved_which


def test_backend_available_oc_provider_via_auth_json():
    """oc:fireworks/model is True when auth.json has a key for fireworks."""
    with _EnvSandbox():
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["OC_AUTH_FILE"] = _write_oc_auth(
                tmpdir, {"fireworks": {"type": "api", "key": "fpk_test123"}}
            )
            os.environ["OC_CONFIG_FILE"] = _write_oc_config(tmpdir, {})
            # fireworks IS in auth.json → available (even though it's in _DEAD_PROVIDERS
            # for the default-route guard — backend_available is a live-auth probe, not that guard).
            assert backends._oc_provider_auth_available("fireworks") is True


def test_backend_available_oc_commandcode_requires_opencode_provider_auth():
    """oc:commandcode uses opencode's provider auth, not review-cli's COMMANDCODE_API_KEY."""
    with _EnvSandbox():
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["COMMANDCODE_API_KEY"] = "review-cli-direct-key"
            os.environ["OC_AUTH_FILE"] = _write_oc_auth(tmpdir, {})
            os.environ["OC_CONFIG_FILE"] = _write_oc_config(tmpdir, {})
            assert backends._oc_provider_auth_available("commandcode") is False
            os.environ["OC_CONFIG_FILE"] = _write_oc_config(
                tmpdir, {"commandcode": {"options": {"apiKey": "user_opencode"}}}
            )
            assert backends._oc_provider_auth_available("commandcode") is True


def test_backend_available_oc_provider_via_opencode_json():
    """oc:someprovider/model is True when opencode.json has inline options.apiKey."""
    with _EnvSandbox():
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["OC_AUTH_FILE"] = _write_oc_auth(tmpdir, {})
            os.environ["OC_CONFIG_FILE"] = _write_oc_config(
                tmpdir,
                {
                    "customprovider": {
                        "options": {
                            "baseURL": "https://api.example.com",
                            "apiKey": "sk-custom",
                        }
                    }
                },
            )
            assert backends._oc_provider_auth_available("customprovider") is True


def test_backend_available_oc_unknown_provider_conservative_true():
    """Unknown provider with no creds → conservative True (opencode may handle it)."""
    with _EnvSandbox():
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["OC_AUTH_FILE"] = _write_oc_auth(tmpdir, {})
            os.environ["OC_CONFIG_FILE"] = _write_oc_config(tmpdir, {})
            # 'newprovider2099' is not in _OC_PROVIDER_ENV_VARS and not in auth/config.
            assert backends._oc_provider_auth_available("newprovider2099") is True


def test_backend_available_oc_known_provider_no_creds_false():
    """Known provider (anthropic) with empty auth.json, no config, no env → False."""
    with _EnvSandbox():
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["OC_AUTH_FILE"] = _write_oc_auth(tmpdir, {})
            os.environ["OC_CONFIG_FILE"] = _write_oc_config(tmpdir, {})
            assert backends._oc_provider_auth_available("anthropic") is False
            assert backends._oc_provider_auth_available("openai") is False
            assert backends._oc_provider_auth_available("commandcode") is False
            assert backends._oc_provider_auth_available("fireworks") is False


def test_backend_available_oc_local_provider_no_key_needed():
    """Local inference providers (ollama, lmstudio) are always available — no key."""
    with _EnvSandbox():
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["OC_AUTH_FILE"] = _write_oc_auth(tmpdir, {})
            os.environ["OC_CONFIG_FILE"] = _write_oc_config(tmpdir, {})
            assert backends._oc_provider_auth_available("ollama") is True
            assert backends._oc_provider_auth_available("lmstudio") is True


def test_oc_auth_has_provider_missing_file():
    """Missing auth.json → False, not an exception."""
    with _EnvSandbox():
        os.environ["OC_AUTH_FILE"] = "/nonexistent/no-such-dir/auth.json"
        assert backends._oc_auth_has_provider("anthropic") is False


def test_oc_config_has_provider_key_missing_file():
    """Missing opencode.json → False, not an exception."""
    with _EnvSandbox():
        os.environ["OC_CONFIG_FILE"] = "/nonexistent/no-such-dir/opencode.json"
        assert backends._oc_config_has_provider_key("anthropic") is False


def test_unavailable_reason_is_none_iff_available():
    """backend_available is the boolean over backend_unavailable_reason — they must never
    disagree. A missing commandcode key => a non-None reason AND backend_available False."""
    with _EnvSandbox():
        # No COMMANDCODE_API_KEY set in the sandbox → down, with a concrete reason.
        reason = backends.backend_unavailable_reason("commandcode")
        assert reason is not None
        assert backends.backend_available("commandcode") is False
        assert (
            backends.backend_unavailable_reason("commandcode") is None
        ) == backends.backend_available("commandcode")


def test_unavailable_reason_for_unpaid_provider_names_the_provider():
    """A provider on the unpaid list gets its dedicated unpaid reason BEFORE any key/CLI
    probe or network preflight — the pre-dispatch drop the config `unpaid_providers` key
    (and REVIEW_UNPAID_PROVIDERS) is for."""
    saved = backends._CONFIG_UNPAID_PROVIDERS
    with _EnvSandbox():
        try:
            backends.configure_unpaid_providers(["commandcode"])
            reason = backends.backend_unavailable_reason(
                "commandcode:moonshotai/Kimi-K2.7-Code"
            )
            assert reason is not None
            assert "commandcode" in reason
            assert "unpaid" in reason.lower()
            assert (
                backends.backend_available("commandcode:moonshotai/Kimi-K2.7-Code")
                is False
            )
        finally:
            backends._CONFIG_UNPAID_PROVIDERS = saved


def test_unavailable_reason_missing_gemini_key_mentions_the_env_var():
    """A down gemini seat surfaces the actual missing-key message, not a generic blank."""
    with _EnvSandbox():
        for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
            os.environ.pop(name, None)
        os.environ["GEMINI_ENV_FILE"] = "/nonexistent/no-such-dir/.env"
        reason = backends.backend_unavailable_reason("gemini:gemini-2.5-flash")
        assert reason is not None
        assert "GEMINI_API_KEY" in reason


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
