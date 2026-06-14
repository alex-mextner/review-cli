#!/usr/bin/env python3
"""Unit tests for the keyed OpenAI-compatible provider backends (HYP-741).

Covers the two new backends added to reviewlib.backends:
  * z.ai (Zhipu / GLM) — OpenAI-compatible /chat/completions, keyed via
    ZAI_API_KEY / ZHIPU_API_KEY.
  * common-code (commandcode / DeepSeek family) — same OpenAI-compatible wire
    shape, keyed via COMMON_CODE_API_KEY / COMMANDCODE_API_KEY / DEEPSEEK_API_KEY.

Proven here, all offline (urllib.request.urlopen faked — NO real network):
  (a) resolve_backend routes zai/glm/z.ai/zhipu + zai:<model> + glm: → review_zai,
      and common-code/common_code + common-code:<model> → review_common_code;
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
    "COMMON_CODE_API_KEY",
    "COMMANDCODE_API_KEY",
    "DEEPSEEK_API_KEY",
    "ZAI_MODEL",
    "ZAI_BASE_URL",
    "COMMON_CODE_MODEL",
    "COMMON_CODE_BASE_URL",
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


def test_resolve_backend_routes_common_code():
    for name in ("common-code", "common_code", "commoncode", "Common-Code"):
        assert backends.resolve_backend(name) is backends.review_common_code, name
    assert backends.resolve_backend("common-code:deepseek-coder") is backends.review_common_code
    assert backends.resolve_backend("common_code:deepseek-chat") is backends.review_common_code


def test_existing_routes_unchanged():
    # The new prefixes must not steal codex/gemini/claude/opencode routing.
    assert backends.resolve_backend("codex") is backends.review_codex
    assert backends.resolve_backend("gemini") is backends.review_gemini
    assert backends.resolve_backend("claude:claude-fable-5") is backends.review_claude
    assert backends.resolve_backend("oc:fireworks/x") is backends.review_opencode
    # An unknown model still defaults to opencode (unchanged behaviour).
    assert backends.resolve_backend("totally-unknown") is backends.review_opencode


def test_aliases_expand():
    assert _expand_alias("glm46") == "zai:glm-4.6"
    assert _expand_alias("glm45") == "zai:glm-4.5"
    assert _expand_alias("glm") == "zai:glm-4.6"
    assert _expand_alias("commoncode") == "common-code"
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
    # Default endpoint = general OpenAI-compatible base + /chat/completions.
    assert captured["url"] == "https://api.z.ai/api/paas/v4/chat/completions", captured["url"]
    assert captured["method"] == "POST"
    # OpenAI request shape — NOT the gemini contents/parts shape.
    body = captured["body"]
    assert "messages" in body and "contents" not in body, body
    assert body["model"] == "glm-4.6", body  # bare `zai` → ZAI_DEFAULT_MODEL
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
    assert "glm-4.6" in res.command
    assert "glm-4.5" in res2.command


def test_mode_review_keys_by_requested_model_without_keyerror():
    """End-to-end (codex P1): the plain diff-review mode must format z.ai/common-code
    results without KeyError — proves ReviewResult.model matches the request string."""
    from reviewlib.modes.review import mode_review

    payload = {"choices": [{"message": {"content": "looks fine"}}], "usage": {}}
    old_open = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen({}, payload)
    with _EnvSandbox():
        os.environ["ZAI_API_KEY"] = "k"
        os.environ["DEEPSEEK_API_KEY"] = "k"
        try:
            # staged=False so it doesn't write a review stamp; returns 0 on success.
            rc = mode_review(["zai", "common-code"], "review", "+added", REPO_ROOT, 10, False)
        finally:
            urllib.request.urlopen = old_open
    assert rc == 0, rc


# === common-code request shape ==================================================
def test_common_code_request_is_openai_shape():
    captured: dict = {}
    payload = {
        "choices": [{"message": {"content": "cc says hi"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }
    old_open = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen(captured, payload)
    with _EnvSandbox():
        os.environ["COMMON_CODE_API_KEY"] = "cc-secret"
        try:
            res = backends.review_common_code("common-code", "hello", "", REPO_ROOT, 30)
        finally:
            urllib.request.urlopen = old_open
    assert captured["url"] == "https://api.deepseek.com/chat/completions", captured["url"]
    body = captured["body"]
    assert "messages" in body and "contents" not in body
    assert body["model"] == "deepseek-chat", body
    assert body["messages"][0]["content"] == "hello"
    assert captured["headers"].get("authorization") == "Bearer cc-secret"
    assert res.returncode == 0 and "cc says hi" in res.stdout
    assert "prompt_tokens=5 output_tokens=2" in res.stdout


def test_common_code_base_url_and_model_override():
    captured: dict = {}
    payload = {"choices": [{"message": {"content": "x"}}], "usage": {}}
    old_open = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen(captured, payload)
    with _EnvSandbox():
        os.environ["DEEPSEEK_API_KEY"] = "dk"  # third fallback key name resolves
        os.environ["COMMON_CODE_BASE_URL"] = "https://example.test/v1"
        os.environ["COMMON_CODE_MODEL"] = "deepseek-coder"
        try:
            backends.review_common_code("common-code", "q", "", REPO_ROOT, 10)
        finally:
            urllib.request.urlopen = old_open
    assert captured["url"] == "https://example.test/v1/chat/completions", captured["url"]
    assert captured["body"]["model"] == "deepseek-coder"


# === key resolution from a temp .env (the shared config surface) ================
def test_zai_key_resolves_from_temp_env_file():
    import tempfile

    with _EnvSandbox():
        with tempfile.TemporaryDirectory() as d:
            env_path = Path(d) / ".env"
            env_path.write_text('ZAI_API_KEY="from-env-file"\n', encoding="utf-8")
            os.environ["GEMINI_ENV_FILE"] = str(env_path)
            assert backends._zai_key() == "from-env-file"


def test_common_code_key_resolves_from_temp_env_file():
    import tempfile

    with _EnvSandbox():
        with tempfile.TemporaryDirectory() as d:
            env_path = Path(d) / ".env"
            # COMMANDCODE_API_KEY is one of the accepted fallback var names.
            env_path.write_text("COMMANDCODE_API_KEY=cc-from-file\n", encoding="utf-8")
            os.environ["GEMINI_ENV_FILE"] = str(env_path)
            assert backends._common_code_key() == "cc-from-file"


def test_env_var_takes_precedence_over_file():
    import tempfile

    with _EnvSandbox():
        with tempfile.TemporaryDirectory() as d:
            env_path = Path(d) / ".env"
            env_path.write_text("ZAI_API_KEY=file-key\n", encoding="utf-8")
            os.environ["GEMINI_ENV_FILE"] = str(env_path)
            os.environ["ZAI_API_KEY"] = "env-key"
            assert backends._zai_key() == "env-key"


# === backend_available reflects key presence ====================================
def test_backend_available_reflects_zai_key():
    with _EnvSandbox():
        # No key anywhere → unavailable, never a crash.
        assert backends.backend_available("zai") is False
        os.environ["ZAI_API_KEY"] = "present"
        assert backends.backend_available("zai") is True
        assert backends.backend_available("glm") is True
        assert backends.backend_available("zai:glm-4.6") is True


def test_backend_available_reflects_common_code_key():
    with _EnvSandbox():
        assert backends.backend_available("common-code") is False
        os.environ["DEEPSEEK_API_KEY"] = "present"
        assert backends.backend_available("common-code") is True
        assert backends.backend_available("common_code:deepseek-coder") is True


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
