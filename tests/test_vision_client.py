#!/usr/bin/env python3
"""vision_client tests — the multimodal path (§3.2). NO real API calls.

Proves:
  * per-provider request SHAPE: Anthropic base64 image block, OpenAI image_url
    data-URI, Gemini inline_data — asserted against the pure request builders;
  * forced structured output is requested (tool_use / json_schema / response_schema);
  * structured-output PARSE marshals a verdict and rejects an invalid enum;
  * fail-closed: no vision backend configured → available=False (→ unverified);
  * capability gating: a non-vision backend is not selectable.

The live call itself is NOT exercised (Stage 2 covers recorded responses); these
tests target the request construction + parsing seams.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib.features.visual import vision_client as vc  # noqa: E402

_IMG = vc.encode_image(b"\x89PNG\r\n\x1a\nFAKEPNGBYTES")


def _blocks():
    return [
        vc.VisionBlock(kind="text", text="verify this"),
        vc.VisionBlock(kind="image", label="after", media_type="image/png", data_base64=_IMG),
    ]


def _before_after_blocks():
    return [
        vc.VisionBlock(kind="text", text="verify this"),
        vc.VisionBlock(kind="image", label="before", media_type="image/png", data_base64=_IMG),
        vc.VisionBlock(kind="image", label="after", media_type="image/png", data_base64=_IMG),
    ]


def test_before_after_labels_emitted_each_provider():
    """Each provider request must caption the before/after images so the model knows
    which is the baseline (codex P2)."""
    schema = vc.build_output_schema()

    a = vc.build_anthropic_request("claude:opus", "s", _before_after_blocks(), schema)
    texts = [c["text"] for c in a["messages"][0]["content"] if c.get("type") == "text"]
    assert any("BEFORE image" in t for t in texts) and any("AFTER image" in t for t in texts)

    o = vc.build_openai_request("codex", "s", _before_after_blocks(), schema)
    otexts = [c["text"] for c in o["messages"][1]["content"] if c.get("type") == "text"]
    assert any("BEFORE image" in t for t in otexts) and any("AFTER image" in t for t in otexts)

    g = vc.build_gemini_request("s", _before_after_blocks(), schema)
    gtexts = [p["text"] for p in g["contents"][0]["parts"] if "text" in p]
    assert any("BEFORE image" in t for t in gtexts) and any("AFTER image" in t for t in gtexts)


def test_anthropic_request_shape():
    schema = vc.build_output_schema()
    body = vc.build_anthropic_request("claude:claude-fable-5", "sys", _blocks(), schema)
    content = body["messages"][0]["content"]
    img = [c for c in content if c.get("type") == "image"]
    assert img, "no anthropic image block built"
    src = img[0]["source"]
    assert src["type"] == "base64"
    assert src["media_type"] == "image/png"
    assert src["data"] == _IMG
    # Forced structured output via tool_use + input_schema.
    assert body["tool_choice"]["type"] == "tool"
    assert body["tools"][0]["input_schema"] == schema


def test_openai_request_shape():
    schema = vc.build_output_schema()
    body = vc.build_openai_request("codex", "sys", _blocks(), schema)
    content = body["messages"][1]["content"]
    img = [c for c in content if c.get("type") == "image_url"]
    assert img, "no openai image_url block built"
    url = img[0]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,"), f"not a data URI: {url[:40]}"
    assert url.endswith(_IMG)
    # Forced structured output via json_schema strict.
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True


def test_gemini_request_shape():
    schema = vc.build_output_schema()
    body = vc.build_gemini_request("sys", _blocks(), schema)
    parts = body["contents"][0]["parts"]
    img = [p for p in parts if "inline_data" in p]
    assert img, "no gemini inline_data part built"
    assert img[0]["inline_data"]["mime_type"] == "image/png"
    assert img[0]["inline_data"]["data"] == _IMG
    # Structured output via response_schema, sanitized to Gemini's OpenAPI subset:
    # the verdict enum survives, but JSON-schema-only keys (additionalProperties) are
    # stripped so Gemini accepts it.
    rs = body["generationConfig"]["response_schema"]
    assert rs["properties"]["verdict"]["enum"] == list(vc.VISION_VERDICTS)
    assert "additionalProperties" not in rs
    assert "maxLength" not in rs["properties"]["note"]


def test_build_request_routes_by_provider():
    schema = vc.build_output_schema()
    wire_a, _ = vc.build_request("claude:opus", "s", _blocks(), schema)
    wire_g, _ = vc.build_request("gemini", "s", _blocks(), schema)
    wire_o, _ = vc.build_request("codex", "s", _blocks(), schema)
    assert wire_a == "anthropic"
    assert wire_g == "gemini"
    assert wire_o == "openai"


def test_schema_includes_module_fields():
    schema = vc.build_output_schema(["selection_present", "unstyled"])
    props = schema["properties"]
    assert "selection_present" in props and props["selection_present"]["type"] == "boolean"
    assert "unstyled" in props
    assert "verdict" in schema["required"] and "confidence" in schema["required"]
    # Active module fields must be REQUIRED so the model can't omit them (codex P2).
    assert "selection_present" in schema["required"]
    assert "unstyled" in schema["required"]


def test_parse_structured_valid():
    v = vc.parse_structured(
        {"verdict": "keep", "confidence": 0.9, "note": "looks fine", "selection_present": True},
        backend="gemini",
    )
    assert v.available and v.verdict == "keep"
    assert v.confidence == 0.9
    assert v.module_answers.get("selection_present") is True


def test_parse_structured_invalid_enum_yields_none():
    v = vc.parse_structured({"verdict": "definitely-fine", "confidence": 1.0})
    assert v.available is True
    assert v.verdict is None, "an invalid verdict enum must marshal to None so policy fails closed"


def test_call_ai_vision_fail_closed_when_no_backend():
    v = vc.call_ai_vision(None, blocks=_blocks())
    assert v.available is False
    assert v.verdict is None


def test_safe_confidence_fails_closed():
    """Untrusted confidence: non-numeric must not crash, out-of-range/NaN must collapse
    to 0.0 (so it can't slip a keep past the low-confidence escalation) — codex P2."""
    assert vc._safe_confidence("high") == 0.0  # non-numeric → 0.0, no crash
    assert vc._safe_confidence(2.0) == 0.0  # out of range → 0.0
    assert vc._safe_confidence(-1.0) == 0.0
    assert vc._safe_confidence(float("nan")) == 0.0  # non-finite → 0.0
    assert vc._safe_confidence(True) == 0.0  # bool (int subclass) must NOT become 1.0
    assert vc._safe_confidence(False) == 0.0
    assert vc._safe_confidence(0.85) == 0.85  # valid value preserved
    # And the full parse path must not crash on a string confidence.
    parsed = vc.parse_structured({"verdict": "keep", "confidence": "totally"})
    assert parsed.verdict == "keep" and parsed.confidence == 0.0


def test_gemini_honors_env_model_and_uppercases_schema():
    """The visual Gemini call must honor $GEMINI_MODEL (like review_gemini) and send an
    uppercased response_schema type (codex P1/P2). We capture the request without a
    real network call by faking urlopen + the key."""
    import os
    import urllib.request

    import reviewlib.backends as backends

    captured = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"candidates":[{"content":{"parts":[{"text":"{\\"verdict\\":\\"keep\\",\\"confidence\\":0.9}"}]}}]}'

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return _FakeResp()

    old_open = urllib.request.urlopen
    old_key = backends._gemini_key
    old_env = os.environ.get("GEMINI_MODEL")
    urllib.request.urlopen = fake_urlopen
    backends._gemini_key = lambda: "fake-key"
    os.environ["GEMINI_MODEL"] = "gemini-3.0-pro"
    try:
        # Uppercased schema type:
        body = vc.build_gemini_request("sys", _blocks(), vc.build_output_schema())
        assert body["generationConfig"]["response_schema"]["type"] == "OBJECT"
        # Env-model honored on a bare `gemini` backend:
        v = vc.call_ai_vision("gemini", blocks=_blocks())
        assert v.verdict == "keep"
        assert "gemini-3.0-pro:generateContent" in captured["url"], captured.get("url")
    finally:
        urllib.request.urlopen = old_open
        backends._gemini_key = old_key
        if old_env is None:
            os.environ.pop("GEMINI_MODEL", None)
        else:
            os.environ["GEMINI_MODEL"] = old_env


def test_parse_structured_malformed_list_fields_fail_closed():
    """A schema-invalid scalar where a list is expected (`defects: 1`) must NOT crash
    the parse (codex P2) — it degrades to an empty list."""
    v = vc.parse_structured({"verdict": "keep", "confidence": 0.9, "defects": 1, "observed_change_regions": "nope"})
    assert v.verdict == "keep"
    assert v.defects == []
    assert v.observed_change_regions == []


def test_capability_gating_and_selection():
    # codex resolves to a vision-capable backend in the table.
    cap = vc.capability_for("codex")
    assert cap is not None and cap.vision
    # select_vision_backend returns None when no model resolves to a reachable vision
    # backend (empty list → None, the fail-closed path).
    assert vc.select_vision_backend([]) is None


def test_selection_skips_unwired_live_dispatch():
    """Stage-1: only Gemini's live send is wired. The selector must skip an unwired
    (codex/claude/opencode) provider even if it is 'available', and pick the wired
    Gemini instead (codex P1)."""
    import reviewlib.backends as backends

    old = backends.backend_available
    backends.backend_available = lambda m: True  # pretend everything is reachable
    try:
        # codex first, then gemini: codex's live dispatch is unwired → gemini chosen.
        chosen = vc.select_vision_backend(["codex", "gemini"])
        assert chosen == "gemini", f"must skip unwired codex and pick wired gemini, got {chosen}"
        # All-unwired list → None (fail-closed), even though they are 'available'.
        assert vc.select_vision_backend(["codex", "claude:opus"]) is None
    finally:
        backends.backend_available = old


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
