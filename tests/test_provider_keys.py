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
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Make the in-repo package importable without an install (mirrors the bin/review shim).
sys.path.insert(0, str(REPO_ROOT))

import reviewlib.backends as backends  # noqa: E402
from reviewlib.config import _expand_alias  # noqa: E402

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
    "GEMINI_ENV_FILE",
)


class _EnvSandbox:
    """Snapshot + restore the provider-related env vars so tests don't leak into
    one another (or pick up a real key from the dev machine)."""

    def __enter__(self):
        self._saved = {name: os.environ.get(name) for name in _KEY_ENV_NAMES}
        for name in _KEY_ENV_NAMES:
            os.environ.pop(name, None)
        # Point the .env fallback at a path that does not exist so, by default,
        # no key resolves unless a test sets one explicitly.
        os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
        return self

    def __exit__(self, *exc):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
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
    assert backends.resolve_backend("commandcode:deepseek/deepseek-coder") is backends.review_commandcode
    # Legacy common-code spellings still resolve (back-compat alias on resolve_backend).
    for legacy in ("common-code", "common_code", "commoncode", "Common-Code"):
        assert backends.resolve_backend(legacy) is backends.review_commandcode, legacy
    assert backends.resolve_backend("common-code:deepseek-coder") is backends.review_commandcode


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
    assert captured["url"] == "https://api.z.ai/api/coding/paas/v4/chat/completions", captured["url"]
    assert captured["method"] == "POST"
    # OpenAI request shape — NOT the gemini contents/parts shape.
    body = captured["body"]
    assert "messages" in body and "contents" not in body, body
    assert body["model"] == "glm-5.2", body  # bare `zai` → ZAI_DEFAULT_MODEL (newest GLM)
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
    assert captured["url"] == "https://api.z.ai/api/coding/paas/v4/chat/completions", captured["url"]


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
            rc = mode_review(["zai", "commandcode"], "review", "+added", REPO_ROOT, 10, False)
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
    assert captured["url"] == "https://api.commandcode.ai/provider/v1/chat/completions", captured["url"]
    body = captured["body"]
    assert "messages" in body and "contents" not in body
    # Bare `commandcode` → COMMANDCODE_DEFAULT_MODEL (an OpenAI-shape, provider-prefixed id).
    assert body["model"] == "deepseek/deepseek-v4-flash", body
    assert body["messages"][0]["content"] == "hello"
    assert captured["headers"].get("authorization") == "Bearer cc-secret"
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
    assert captured["url"] == "https://example.test/v1/chat/completions", captured["url"]
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
            backends.review_commandcode("commandcode:zai-org/GLM-5.2", "q", "", REPO_ROOT, 10)
        finally:
            urllib.request.urlopen = old_open
    assert captured["body"]["model"] == "zai-org/GLM-5.2", captured["body"]
    # And it goes to the default Command Code gateway, not z.ai's host.
    assert captured["url"] == "https://api.commandcode.ai/provider/v1/chat/completions", captured["url"]


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
            backends.review_commandcode("commandcode:zai-org/GLM-5.2", "q", "", REPO_ROOT, 10)
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

    def _should_not_be_called(req, timeout=None):  # pragma: no cover - asserted unreached
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
    def _should_not_be_called(req, timeout=None):  # pragma: no cover - asserted unreached
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
    assert backends.ZAI_DEFAULT_BASE_URL == "https://api.z.ai/api/coding/paas/v4", \
        backends.ZAI_DEFAULT_BASE_URL
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
    assert captured["url"] == "https://api.z.ai/api/coding/paas/v4/chat/completions", captured["url"]


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
    assert captured["url"] == "https://api.z.ai/api/paas/v4/chat/completions", captured["url"]
    assert captured["body"]["model"] == "glm-5.1", captured["body"]


def test_zai_reasoning_content_fallback_when_content_empty():
    """REASONING MODEL (glm-5.2): a 2xx whose message.content is empty/missing but
    carries message.reasoning_content must NOT fail-closed as "no assistant content".
    Surface the reasoning text (rc=0) so a low-output-budget reasoning reply is usable."""
    cases = (
        {"choices": [{"message": {"content": "", "reasoning_content": "I think the diff is fine."}}], "usage": {}},
        {"choices": [{"message": {"reasoning_content": "Only reasoning here, no content key."}}], "usage": {}},
        {"choices": [{"message": {"content": None, "reasoning_content": "null content, reasoning present."}}], "usage": {}},
        {"choices": [{"message": {"content": "   ", "reasoning_content": "whitespace content, reasoning present."}}], "usage": {}},
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
    payload = {"choices": [{"message": {
        "content": "FINAL: the change looks correct.",
        "reasoning_content": "step 1 ... step 2 ...",
    }}], "usage": {}}
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
    assert "step 1" not in res.stdout  # reasoning not surfaced when a final answer exists


def test_zai_empty_with_no_reasoning_still_fails_closed():
    """The fallback must not weaken the empty-output guard: NO content AND NO usable
    reasoning_content must still map to a non-zero dead-backend result."""
    cases = (
        {"choices": [{"message": {"content": ""}}], "usage": {}},
        {"choices": [{"message": {"content": "", "reasoning_content": ""}}], "usage": {}},
        {"choices": [{"message": {"content": "", "reasoning_content": "   "}}], "usage": {}},
        {"choices": [{"message": {"content": "", "reasoning_content": 42}}], "usage": {}},
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
            os.environ.pop("GEMINI_ENV_FILE", None)  # use GEMINI_ENV_FALLBACKS, not override
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
    with _EnvSandbox():
        assert backends.backend_available("commandcode") is False
        os.environ["COMMANDCODE_API_KEY"] = "user_present"
        assert backends.backend_available("commandcode") is True
        assert backends.backend_available("commandcode:deepseek/deepseek-coder") is True
        # The legacy model-name spelling routes to the same backend (key present).
        assert backends.backend_available("common-code") is True


def test_backend_available_false_for_forced_api_only_cli_mode():
    """Codex P2: with REVIEW_COMMANDCODE_MODE=cli (an unrunnable forced mode), the
    availability probe must report False so the moderator/brainstorm filter never
    selects a backend that can only return a dead-backend result — even with a key."""
    with _EnvSandbox():
        os.environ["COMMANDCODE_API_KEY"] = "user_present"
        assert backends.backend_available("commandcode") is True
        os.environ["REVIEW_COMMANDCODE_MODE"] = "cli"
        assert backends.backend_available("commandcode") is False
        # z.ai is api-only too: a forced cli mode makes it unavailable.
        os.environ["ZAI_API_KEY"] = "k"
        assert backends.backend_available("zai") is True
        os.environ["REVIEW_ZAI_MODE"] = "cli"
        assert backends.backend_available("zai") is False


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
            req.full_url, 401, "Unauthorized", hdrs=None,
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
