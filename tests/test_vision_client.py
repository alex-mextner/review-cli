#!/usr/bin/env python3
"""vision_client tests — the multimodal path via the AGENT CLIs (§3.2). NO real CLI/API.

`review` works by INVOKING the agent CLIs (codex/claude/opencode); the visual path
mirrors that — it shells out to the SAME CLIs with an image attached and parses the
structured verdict from the CLI's text output. Gemini is the ONE exception (its CLI is
broken → REST key). These tests MOCK the CLI invocation (the streaming subprocess runner)
and the Gemini REST call; no real CLI is spawned and no real API is hit.

Proves:
  * per-CLI IMAGE ATTACH: codex `-i <file>`, claude `@<file>` ref + Read tool, opencode
    `-f <file>` — asserted against the captured argv + staged temp image;
  * forced structured output is requested (codex --output-schema; claude/opencode prompt
    instruction) and PARSED from the CLI's text output (verdict marshals; invalid enum →
    None so policy fails closed);
  * fail-closed: no vision backend configured → available=False (→ unverified); a CLI
    that emits no parseable JSON → available=True/verdict=None (→ human_review);
  * capability gating + selection: opencode is now vision-capable; a CLI binary absent →
    not reachable; Gemini selectable iff its key resolves;
  * Gemini stays the REST-key exception and honors $GEMINI_MODEL.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib.features.visual import vision_client as vc  # noqa: E402

_IMG_BYTES = b"\x89PNG\r\n\x1a\nFAKEPNGBYTES"
_IMG = vc.encode_image(_IMG_BYTES)


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


# --- A reusable fake for review's streaming CLI runner (process._run_streamed, which the
# visual CLI dispatch calls through `_run_cli`). Captures the argv/stdin and returns a
# canned CompletedProcess. The codex path also writes its verdict to the `-o` file, so the
# fake honors `--output-schema`/`-o` by writing the canned JSON there. -----------------
def _patch_runner(capture: dict, *, stdout: str = "", returncode: int = 0, write_output_file: str | None = None):
    """Monkeypatch reviewlib.process._run_streamed (the seam `_run_cli` uses). Returns the
    old function so the caller can restore it."""
    import shutil

    import reviewlib.backends as backends
    import reviewlib.process as process

    def fake(
        argv, *, cwd, input_text=None, env=None, timeout=1200, backend="backend",
        round_no=0, announce=False, idle_floor=None, timeout_mode="idle",
    ):
        argv = list(argv)
        capture["argv"] = argv
        capture["input_text"] = input_text
        capture["cwd"] = str(cwd)
        capture["backend"] = backend
        capture["timeout_mode"] = timeout_mode
        capture.setdefault("calls", []).append(argv)
        # The staged image temp dir is deleted when call_ai_vision returns, so snapshot the
        # attached image bytes (per CLI flag) HERE, while the files still exist.
        staged = {}
        for flag in ("-i", "-f"):
            if flag in argv:
                try:
                    staged[flag] = Path(argv[argv.index(flag) + 1]).read_bytes()
                except OSError:
                    pass
        # claude references the image via @<path> in the -p prompt; snapshot it too.
        if "-p" in argv:
            prompt = argv[argv.index("-p") + 1]
            for tok in prompt.split():
                if tok.startswith("@"):
                    try:
                        staged["@"] = Path(tok[1:]).read_bytes()
                    except OSError:
                        pass
        capture["staged"] = staged
        # codex writes its final structured message to the file after `-o`; emulate it so
        # the dispatch reads the verdict from there (its real behavior).
        if write_output_file is not None and "-o" in argv:
            out_path = Path(argv[argv.index("-o") + 1])
            out_path.write_text(write_output_file, encoding="utf-8")
        return subprocess.CompletedProcess(args=argv, returncode=returncode, stdout=stdout, stderr="")

    old = process._run_streamed
    old_which = shutil.which
    old_fireworks_key = os.environ.get("FIREWORKS_API_KEY")
    old_preflight = backends._provider_payment_preflight_unavailable_reason
    process._run_streamed = fake
    shutil.which = lambda name: f"/fake/bin/{name}" if name in ("codex", "claude", "opencode") else old_which(name)
    backends._provider_payment_preflight_unavailable_reason = lambda _model: None
    os.environ["FIREWORKS_API_KEY"] = "test-fireworks-key"
    return old, old_which, old_fireworks_key, old_preflight


def _restore_runner(old):
    import shutil

    import reviewlib.backends as backends
    import reviewlib.process as process

    process._run_streamed = old[0]
    shutil.which = old[1]
    backends._provider_payment_preflight_unavailable_reason = old[3]
    if old[2] is None:
        os.environ.pop("FIREWORKS_API_KEY", None)
    else:
        os.environ["FIREWORKS_API_KEY"] = old[2]


def _patch_preflight_allow():
    import reviewlib.backends as backends

    old = backends._provider_payment_preflight_unavailable_reason
    backends._provider_payment_preflight_unavailable_reason = lambda _model: None
    return old


def _restore_preflight(old) -> None:
    import reviewlib.backends as backends

    backends._provider_payment_preflight_unavailable_reason = old


def _staged_image_bytes(cap: dict, flag: str) -> bytes | None:
    """The image bytes the dispatch staged and passed after `flag` (snapshotted by the
    fake runner while the temp file still existed)."""
    return (cap.get("staged") or {}).get(flag)


# === codex CLI vision ============================================================
def test_codex_cli_attaches_image_and_parses_output_schema():
    """codex vision: `codex exec -i <image> --output-schema <schema> -o <out> -`. The
    image is attached via `-i`, the forced schema via `--output-schema`, and the verdict
    is read from the `-o` file."""
    cap: dict = {}
    old = _patch_runner(
        cap,
        write_output_file='{"verdict":"keep","confidence":0.95,"selection_present":true}',
    )
    try:
        v = vc.call_ai_vision("codex", blocks=_blocks(), output_schema=vc.build_output_schema(["selection_present"]))
    finally:
        _restore_runner(old)
    argv = cap["argv"]
    assert argv[0] == "codex" and argv[1] == "exec", argv
    # Image attached via -i, and the staged file held the real PNG bytes.
    assert "-i" in argv, "codex must attach the image with -i"
    assert _staged_image_bytes(cap, "-i") == _IMG_BYTES, "the -i image was not the staged screenshot"
    # Forced structured output via --output-schema + -o, and the verdict read from -o.
    assert "--output-schema" in argv and "-o" in argv
    assert v.available and v.verdict == "keep" and v.confidence == 0.95
    assert v.module_answers.get("selection_present") is True
    assert v.backend == "codex"


def test_codex_cli_honors_model_suffix():
    cap: dict = {}
    old = _patch_runner(cap, write_output_file='{"verdict":"keep","confidence":0.9}')
    try:
        vc.call_ai_vision("codex:gpt-5-vision", blocks=_blocks())
    finally:
        _restore_runner(old)
    argv = cap["argv"]
    assert "-m" in argv and argv[argv.index("-m") + 1] == "gpt-5-vision"


# === claude CLI vision ===========================================================
def test_claude_cli_references_image_and_enables_read():
    """claude vision: the image is referenced as `@<path>` in the prompt and the Read tool
    is ENABLED (vision needs to load the file). `--output-format json` wraps the answer;
    the verdict is the `result` field."""
    cap: dict = {}
    wrapper = json.dumps({"type": "result", "result": '{"verdict":"rollback","confidence":0.8}'})
    old = _patch_runner(cap, stdout=wrapper)
    try:
        v = vc.call_ai_vision("claude:claude-fable-5", blocks=_blocks())
    finally:
        _restore_runner(old)
    argv = cap["argv"]
    assert argv[0] == "claude" and "-p" in argv
    prompt = argv[argv.index("-p") + 1]
    assert "@" in prompt, "claude prompt must reference the image with @<path>"
    # Read is the only enabled tool; the visual wrapper must not pass stale deny-list
    # names that newer Claude CLIs reject before reading the screenshot.
    assert "--tools" in argv and argv[argv.index("--tools") + 1] == "Read"
    assert "--json-schema" in argv
    assert "--no-session-persistence" in argv
    assert "--permission-mode" in argv and argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert "--strict-mcp-config" in argv
    assert "--disable-slash-commands" in argv
    assert "--safe-mode" in argv
    assert "--disallowedTools" not in argv
    assert "--output-format" in argv and "json" in argv
    assert "--add-dir" in argv
    # The @<path> in the prompt pointed at a real staged image carrying the PNG bytes.
    assert _staged_image_bytes(cap, "@") == _IMG_BYTES
    # model suffix honored.
    assert argv[argv.index("--model") + 1] == "claude-fable-5"
    # parsed from the result field.
    assert v.available and v.verdict == "rollback" and v.confidence == 0.8
    assert v.backend == "claude:claude-fable-5"


def test_claude_cli_parses_bare_json_when_not_wrapped():
    """If claude returns the JSON directly (not wrapped), the parser still extracts it."""
    cap: dict = {}
    old = _patch_runner(cap, stdout='Here is the verdict:\n{"verdict":"keep","confidence":0.91}\nDone.')
    try:
        v = vc.call_ai_vision("claude:opus", blocks=_blocks())
    finally:
        _restore_runner(old)
    assert v.verdict == "keep" and v.confidence == 0.91


# === opencode CLI vision (NOW ENABLED) ===========================================
def test_opencode_cli_attaches_image_and_parses():
    """opencode is vision-capable now: `opencode run "<prompt>" -m <model> -f <image>`
    attaches the file and routes to the named vision model; the verdict is parsed from
    the text output."""
    cap: dict = {}
    old = _patch_runner(cap, stdout='{"verdict":"keep","confidence":0.88}')
    try:
        v = vc.call_ai_vision("oc:fireworks/qwen2-vl", blocks=_blocks())
    finally:
        _restore_runner(old)
    argv = cap["argv"]
    assert argv[0] == "opencode" and argv[1] == "run"
    # Message is the positional BEFORE -m/-f (the -f array flag is greedy).
    assert "-m" in argv and argv[argv.index("-m") + 1] == "fireworks/qwen2-vl"
    assert "-f" in argv, "opencode must attach the image with -f"
    assert _staged_image_bytes(cap, "-f") == _IMG_BYTES
    # The prompt is positional (argv[2]), before the flags.
    assert argv.index("-m") > 2 and argv.index("-f") > 2
    assert v.available and v.verdict == "keep" and v.confidence == 0.88


def test_opencode_is_vision_capable():
    """opencode is no longer vision-incapable: capability_for resolves it and select can
    pick it when reachable."""
    cap = vc.capability_for("oc:fireworks/qwen2-vl")
    assert cap is not None and cap.vision and cap.live_dispatch
    assert cap.wire == "opencode-cli"


# === routing / capability ========================================================
def test_build_request_routes_to_cli_or_gemini():
    schema = vc.build_output_schema()
    route_c, _ = vc.build_request("claude:opus", "s", _blocks(), schema)
    route_g, _ = vc.build_request("gemini", "s", _blocks(), schema)
    route_x, _ = vc.build_request("codex", "s", _blocks(), schema)
    route_o, _ = vc.build_request("oc:fireworks/qwen2-vl", "s", _blocks(), schema)
    assert route_c == "claude-cli"
    assert route_g == "gemini"
    assert route_x == "codex-cli"
    assert route_o == "opencode-cli"


def test_all_four_routes_live_dispatch_wired():
    """All four routes are live: three agent CLIs + Gemini REST. opencode is INCLUDED
    now (the CTO correction — it routes to vision models via the CLI)."""
    for route in ("claude", "codex", "opencode", "gemini"):
        cap = vc._CAPABILITIES[route]
        assert cap.vision and cap.live_dispatch is True, f"{route} live dispatch must be wired"


def test_schema_includes_module_fields():
    schema = vc.build_output_schema(["selection_present", "unstyled"])
    props = schema["properties"]
    assert "selection_present" in props and props["selection_present"]["type"] == "boolean"
    assert "unstyled" in props
    assert "verdict" in schema["required"] and "confidence" in schema["required"]
    # Active module fields must be REQUIRED so the model can't omit them (codex P2).
    assert "selection_present" in schema["required"]
    assert "unstyled" in schema["required"]


def test_schema_instruction_lists_keys_and_verdict_enum():
    """The CLI path delivers the forced schema as a TEXT instruction (the CLIs return
    text). It must name the allowed keys + the verdict enum."""
    schema = vc.build_output_schema(["selection_present"])
    instr = vc._schema_instruction(schema)
    assert "verdict" in instr and "selection_present" in instr
    assert all(v in instr for v in vc.VISION_VERDICTS)


# === structured-output parse (shared by all routes) ==============================
def test_parse_structured_valid():
    v = vc.parse_structured(
        {"verdict": "keep", "confidence": 0.9, "note": "looks fine", "selection_present": True},
        backend="codex",
    )
    assert v.available and v.verdict == "keep"
    assert v.confidence == 0.9
    assert v.module_answers.get("selection_present") is True


def test_parse_structured_invalid_enum_yields_none():
    v = vc.parse_structured({"verdict": "definitely-fine", "confidence": 1.0})
    assert v.available is True
    assert v.verdict is None, "an invalid verdict enum must marshal to None so policy fails closed"


def test_extract_first_json_object_from_prose_and_fences():
    """The CLI output parser must find the JSON verdict whether it is bare, wrapped in
    prose, or inside a ```json fence."""
    bare = vc._extract_first_json_object('{"verdict":"keep","confidence":0.9}')
    assert bare and bare["verdict"] == "keep"
    prose = vc._extract_first_json_object('Sure! Here:\n{"verdict":"rollback","confidence":0.5} — done')
    assert prose and prose["verdict"] == "rollback"
    fenced = vc._extract_first_json_object('```json\n{"verdict":"repair","confidence":0.4}\n```')
    assert fenced and fenced["verdict"] == "repair"
    nested = vc._extract_first_json_object('{"verdict":"keep","confidence":0.9,"defects":[{"x":1}]}')
    assert nested and nested["defects"] == [{"x": 1}]
    assert vc._extract_first_json_object("no json at all here") is None


def test_safe_confidence_fails_closed():
    assert vc._safe_confidence("high") == 0.0  # non-numeric → 0.0, no crash
    assert vc._safe_confidence(2.0) == 0.0  # out of range → 0.0
    assert vc._safe_confidence(-1.0) == 0.0
    assert vc._safe_confidence(float("nan")) == 0.0  # non-finite → 0.0
    assert vc._safe_confidence(True) == 0.0  # bool (int subclass) must NOT become 1.0
    assert vc._safe_confidence(False) == 0.0
    assert vc._safe_confidence(0.85) == 0.85  # valid value preserved
    parsed = vc.parse_structured({"verdict": "keep", "confidence": "totally"})
    assert parsed.verdict == "keep" and parsed.confidence == 0.0


def test_parse_structured_malformed_list_fields_fail_closed():
    v = vc.parse_structured({"verdict": "keep", "confidence": 0.9, "defects": 1, "observed_change_regions": "nope"})
    assert v.verdict == "keep"
    assert v.defects == []
    assert v.observed_change_regions == []


# === fail-closed ==================================================================
def test_call_ai_vision_fail_closed_when_no_backend():
    v = vc.call_ai_vision(None, blocks=_blocks())
    assert v.available is False
    assert v.verdict is None


def test_cli_no_parseable_json_fails_closed_to_human_review():
    """A CLI that returns prose with no JSON verdict → available=True/verdict=None so the
    policy engine fails closed to human_review (never a silent keep)."""
    cap: dict = {}
    old = _patch_runner(cap, stdout="I could not determine a verdict from the image.", returncode=0)
    try:
        v = vc.call_ai_vision("codex", blocks=_blocks())
    finally:
        _restore_runner(old)
    assert v.available is True and v.verdict is None
    assert "no parseable JSON" in (v.error or "")


def test_cli_nonzero_exit_fails_closed_even_with_keep_in_stdout():
    """A CLI that exits NON-ZERO must fail closed BEFORE parsing — a failed/auth-erroring
    CLI can still print a parseable `{"verdict":"keep"}` to stdout, which must never be
    trusted (codex high finding)."""
    cap: dict = {}
    old = _patch_runner(cap, stdout='{"verdict":"keep","confidence":0.99}', returncode=1)
    try:
        v = vc.call_ai_vision("claude:opus", blocks=_blocks())
    finally:
        _restore_runner(old)
    assert v.available is True and v.verdict is None, "non-zero exit must not trust a keep in stdout"
    assert "non-zero" in (v.error or "")


def test_opencode_uses_readonly_reviewer_agent():
    """opencode vision must run the READ-ONLY reviewer agent (untrusted screenshot prompt
    must not gain tool powers) — codex high finding."""
    cap: dict = {}
    old = _patch_runner(cap, stdout='{"verdict":"keep","confidence":0.9}')
    try:
        vc.call_ai_vision("oc:fireworks/qwen2-vl", blocks=_blocks())
    finally:
        _restore_runner(old)
    argv = cap["argv"]
    assert "--agent" in argv and argv[argv.index("--agent") + 1] == "read-only-reviewer"


def test_text_opencode_model_not_selected_for_vision():
    """The DEFAULT config's TEXT opencode model must NOT be picked for --visual: a text
    model can't verify an image (codex high finding). A vision-looking model still is."""
    old = vc.vision_backend_available
    # Restore real reachability logic but pretend the binary exists.
    import shutil

    old_which = shutil.which
    old_key = os.environ.get("FIREWORKS_API_KEY")
    old_preflight = _patch_preflight_allow()
    shutil.which = lambda name: "/usr/bin/opencode" if name == "opencode" else old_which(name)
    os.environ["FIREWORKS_API_KEY"] = "test-fireworks-key"
    vc.vision_backend_available = old  # use the real function under test
    try:
        text_default = "oc:fireworks/accounts/fireworks/routers/kimi-k2p6-turbo"
        assert vc.vision_backend_available(text_default) is False, "text opencode model must be unreachable for vision"
        assert vc.select_vision_backend([text_default]) is None
        # A vision-looking opencode model IS reachable.
        assert vc.vision_backend_available("oc:fireworks/qwen2-vl") is True
    finally:
        shutil.which = old_which
        _restore_preflight(old_preflight)
        if old_key is None:
            os.environ.pop("FIREWORKS_API_KEY", None)
        else:
            os.environ["FIREWORKS_API_KEY"] = old_key


def test_opencode_vision_allowlist_env_override():
    """$REVIEW_OPENCODE_VISION_MODELS lets a user opt a non-obvious model id into vision."""
    import os
    import shutil

    old_which = shutil.which
    shutil.which = lambda name: "/usr/bin/opencode" if name == "opencode" else old_which(name)
    old_env = os.environ.get("REVIEW_OPENCODE_VISION_MODELS")
    old_key = os.environ.get("FIREWORKS_API_KEY")
    old_preflight = _patch_preflight_allow()
    os.environ["REVIEW_OPENCODE_VISION_MODELS"] = "kimi-k2p6-turbo"
    os.environ["FIREWORKS_API_KEY"] = "test-fireworks-key"
    try:
        assert vc.vision_backend_available("oc:fireworks/accounts/fireworks/routers/kimi-k2p6-turbo") is True
    finally:
        shutil.which = old_which
        _restore_preflight(old_preflight)
        if old_env is None:
            os.environ.pop("REVIEW_OPENCODE_VISION_MODELS", None)
        else:
            os.environ["REVIEW_OPENCODE_VISION_MODELS"] = old_env
        if old_key is None:
            os.environ.pop("FIREWORKS_API_KEY", None)
        else:
            os.environ["FIREWORKS_API_KEY"] = old_key


def test_opencode_vision_requires_provider_auth_for_known_providers():
    """The visual opencode path must reuse the text path's provider-auth startup skip."""
    import shutil
    import tempfile

    old_which = shutil.which
    old_key = os.environ.pop("FIREWORKS_API_KEY", None)
    old_auth = os.environ.get("OC_AUTH_FILE")
    old_config = os.environ.get("OC_CONFIG_FILE")
    shutil.which = lambda name: "/usr/bin/opencode" if name == "opencode" else old_which(name)
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["OC_AUTH_FILE"] = str(Path(tmp) / "auth.json")
        os.environ["OC_CONFIG_FILE"] = str(Path(tmp) / "opencode.json")
        try:
            assert vc.vision_backend_available("oc:fireworks/qwen2-vl") is False
        finally:
            shutil.which = old_which
            if old_key is not None:
                os.environ["FIREWORKS_API_KEY"] = old_key
            if old_auth is None:
                os.environ.pop("OC_AUTH_FILE", None)
            else:
                os.environ["OC_AUTH_FILE"] = old_auth
            if old_config is None:
                os.environ.pop("OC_CONFIG_FILE", None)
            else:
                os.environ["OC_CONFIG_FILE"] = old_config


def test_opencode_zai_vision_requires_opencode_auth_not_zai_rest_key():
    """`ZAI_API_KEY` is direct REST auth only; `oc:zai/...` needs opencode provider auth."""
    import shutil
    import tempfile

    old_which = shutil.which
    old_zai_key = os.environ.get("ZAI_API_KEY")
    old_auth = os.environ.get("OC_AUTH_FILE")
    old_config = os.environ.get("OC_CONFIG_FILE")
    shutil.which = lambda name: "/usr/bin/opencode" if name == "opencode" else old_which(name)
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["OC_AUTH_FILE"] = str(Path(tmp) / "auth.json")
        os.environ["OC_CONFIG_FILE"] = str(Path(tmp) / "opencode.json")
        os.environ["ZAI_API_KEY"] = "direct-rest-key-only"
        try:
            assert vc.vision_backend_available("oc:zai/glm-4.5v") is False
        finally:
            shutil.which = old_which
            if old_zai_key is None:
                os.environ.pop("ZAI_API_KEY", None)
            else:
                os.environ["ZAI_API_KEY"] = old_zai_key
            if old_auth is None:
                os.environ.pop("OC_AUTH_FILE", None)
            else:
                os.environ["OC_AUTH_FILE"] = old_auth
            if old_config is None:
                os.environ.pop("OC_CONFIG_FILE", None)
            else:
                os.environ["OC_CONFIG_FILE"] = old_config


def test_unpaid_opencode_provider_not_selected_for_vision():
    """Payment/entitlement skips apply to visual opencode seats before CLI launch too."""
    import os
    import shutil

    old_env = os.environ.get("REVIEW_UNPAID_PROVIDERS")
    old_which = shutil.which
    shutil.which = lambda name: "/usr/bin/opencode" if name == "opencode" else old_which(name)
    os.environ["REVIEW_UNPAID_PROVIDERS"] = "fireworks"
    try:
        assert vc.vision_backend_available("oc:fireworks/qwen2-vl") is False
        assert vc.select_vision_backend(["oc:fireworks/qwen2-vl"]) is None
    finally:
        shutil.which = old_which
        if old_env is None:
            os.environ.pop("REVIEW_UNPAID_PROVIDERS", None)
        else:
            os.environ["REVIEW_UNPAID_PROVIDERS"] = old_env


def test_unpaid_opencode_vision_call_does_not_spawn_runner():
    """A direct visual call for an unpaid provider returns unavailable without subprocesses."""
    import os

    import reviewlib.process as process

    old_env = os.environ.get("REVIEW_UNPAID_PROVIDERS")
    old_runner = process._run_streamed

    def _boom(*args, **kwargs):  # pragma: no cover - asserted by not raising
        raise AssertionError("visual opencode runner spawned despite unpaid provider")

    process._run_streamed = _boom
    os.environ["REVIEW_UNPAID_PROVIDERS"] = "fireworks"
    try:
        v = vc.call_ai_vision("oc:fireworks/qwen2-vl", blocks=_blocks())
    finally:
        process._run_streamed = old_runner
        if old_env is None:
            os.environ.pop("REVIEW_UNPAID_PROVIDERS", None)
        else:
            os.environ["REVIEW_UNPAID_PROVIDERS"] = old_env
    assert v.available is False and v.verdict is None
    assert "unpaid/disabled" in (v.error or "")


def test_opencode_vision_missing_binary_does_not_run_payment_preflight():
    """Local opencode prerequisites must fail before any provider `/models` preflight."""
    import shutil
    import urllib.request

    old_key = os.environ.get("FIREWORKS_API_KEY")
    old_which = shutil.which
    old_open = urllib.request.urlopen

    def _no_opencode(name):
        if name == "opencode":
            return None
        return old_which(name)

    def _network_should_not_run(*_args, **_kwargs):
        raise AssertionError("visual payment preflight ran before opencode binary check")

    shutil.which = _no_opencode
    urllib.request.urlopen = _network_should_not_run
    os.environ["FIREWORKS_API_KEY"] = "fw_present"
    try:
        v = vc.call_ai_vision("oc:fireworks/qwen2-vl", blocks=_blocks())
    finally:
        shutil.which = old_which
        urllib.request.urlopen = old_open
        if old_key is None:
            os.environ.pop("FIREWORKS_API_KEY", None)
        else:
            os.environ["FIREWORKS_API_KEY"] = old_key
    assert v.available is False and "opencode CLI not found" in (v.error or "")


def test_unpaid_visual_cli_call_does_not_spawn_runner():
    """Direct call_ai_vision callers must not bypass unpaid-provider skips for CLI routes."""
    import reviewlib.process as process

    old_env = os.environ.get("REVIEW_UNPAID_PROVIDERS")
    old_runner = process._run_streamed

    def _boom(*args, **kwargs):  # pragma: no cover - asserted by not raising
        raise AssertionError("visual CLI runner spawned despite unpaid provider")

    process._run_streamed = _boom
    os.environ["REVIEW_UNPAID_PROVIDERS"] = "claude"
    try:
        v = vc.call_ai_vision("claude:opus", blocks=_blocks())
    finally:
        process._run_streamed = old_runner
        if old_env is None:
            os.environ.pop("REVIEW_UNPAID_PROVIDERS", None)
        else:
            os.environ["REVIEW_UNPAID_PROVIDERS"] = old_env
    assert v.available is False and v.verdict is None
    assert "unpaid/disabled" in (v.error or "")


def test_unpaid_claude_gateway_visual_call_does_not_spawn_runner():
    """Visual Claude inherits ANTHROPIC_* gateway vars and must honor unpaid gateways."""
    import shutil

    import reviewlib.process as process

    saved_env = {
        key: os.environ.get(key)
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "REVIEW_UNPAID_PROVIDERS")
    }
    old_runner = process._run_streamed
    old_which = shutil.which

    def _boom(*args, **kwargs):  # pragma: no cover - asserted by not raising
        raise AssertionError("visual Claude runner spawned despite unpaid CommandCode gateway")

    process._run_streamed = _boom
    shutil.which = lambda name: "/usr/bin/claude" if name == "claude" else old_which(name)
    os.environ["ANTHROPIC_API_KEY"] = "user_x"
    os.environ["ANTHROPIC_BASE_URL"] = "https://api.commandcode.ai/provider"
    os.environ["REVIEW_UNPAID_PROVIDERS"] = "commandcode"
    try:
        assert vc.vision_backend_available("claude:opus") is False
        v = vc.call_ai_vision("claude:opus", blocks=_blocks())
    finally:
        process._run_streamed = old_runner
        shutil.which = old_which
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    assert v.available is False and v.verdict is None
    assert "provider 'commandcode'" in (v.error or "")


def test_unpaid_visual_rest_call_does_not_post():
    """Direct call_ai_vision callers must not bypass unpaid-provider skips for REST routes."""
    import urllib.request

    old_env = os.environ.get("REVIEW_UNPAID_PROVIDERS")
    old_open = urllib.request.urlopen

    def _boom(*args, **kwargs):  # pragma: no cover - asserted by not raising
        raise AssertionError("Gemini REST call posted despite unpaid provider")

    urllib.request.urlopen = _boom
    os.environ["REVIEW_UNPAID_PROVIDERS"] = "gemini"
    try:
        v = vc.call_ai_vision("gemini", blocks=_blocks())
    finally:
        urllib.request.urlopen = old_open
        if old_env is None:
            os.environ.pop("REVIEW_UNPAID_PROVIDERS", None)
        else:
            os.environ["REVIEW_UNPAID_PROVIDERS"] = old_env
    assert v.available is False and v.verdict is None
    assert "unpaid/disabled" in (v.error or "")


def test_codex_strict_schema_closes_additional_properties():
    """codex --output-schema enforces OpenAI strict: additionalProperties:false + every
    property required + free-form object array items downgraded to strings."""
    strict = vc._strict_output_schema(vc.build_output_schema(["unstyled"]))
    assert strict["additionalProperties"] is False
    assert set(strict["required"]) == set(strict["properties"].keys())
    # free-form object arrays became string arrays (OpenAI strict can't take open objects).
    assert strict["properties"]["defects"]["items"]["type"] == "string"
    assert strict["properties"]["observed_change_regions"]["items"]["type"] == "string"
    # optional field made nullable; required module field stays a plain boolean.
    assert "null" in strict["properties"]["note"]["type"]
    assert strict["properties"]["unstyled"]["type"] == "boolean"


def test_cli_missing_required_module_field_fails_closed():
    """A CLI (claude/opencode return free text) that omits a REQUIRED module field must
    fail closed — NOT silently produce empty module_answers that let a module judge fall
    back to a CV pass (codex P2). codex enforces the field server-side via --output-schema;
    claude/opencode only get the prompt instruction, so we validate OUTSIDE the model."""
    cap: dict = {}
    # claude returns a keep but OMITS the required `unstyled` module field.
    wrapper = json.dumps({"type": "result", "result": '{"verdict":"keep","confidence":0.99}'})
    old = _patch_runner(cap, stdout=wrapper)
    try:
        v = vc.call_ai_vision("claude:opus", blocks=_blocks(), output_schema=vc.build_output_schema(["unstyled"]))
    finally:
        _restore_runner(old)
    assert v.available is True and v.verdict is None, "missing required module field must fail closed"
    assert "required schema" in (v.error or "") and "unstyled" in (v.error or "")


def test_cli_nonboolean_required_module_field_fails_closed():
    """A required boolean module field returned as a non-boolean (e.g. "yes") fails closed:
    a truthy string must NOT be coerced into a module pass."""
    cap: dict = {}
    old = _patch_runner(cap, stdout='{"verdict":"keep","confidence":0.9,"unstyled":"false"}')
    try:
        v = vc.call_ai_vision("oc:fireworks/qwen2-vl", blocks=_blocks(), output_schema=vc.build_output_schema(["unstyled"]))
    finally:
        _restore_runner(old)
    assert v.available is True and v.verdict is None
    assert "must be a boolean" in (v.error or "")


def test_cli_valid_required_module_field_passes_validation():
    """When the required module field IS present and correctly typed, the verdict parses."""
    cap: dict = {}
    old = _patch_runner(cap, stdout='{"verdict":"keep","confidence":0.9,"unstyled":false}')
    try:
        v = vc.call_ai_vision("oc:fireworks/qwen2-vl", blocks=_blocks(), output_schema=vc.build_output_schema(["unstyled"]))
    finally:
        _restore_runner(old)
    assert v.available is True and v.verdict == "keep"
    assert v.module_answers.get("unstyled") is False


def test_cli_timeout_sets_timed_out_flag():
    """A CLI timeout (returncode 124 from the streaming runner) sets timed_out → exit 124."""
    cap: dict = {}
    old = _patch_runner(cap, stdout="partial...", returncode=124)
    try:
        v = vc.call_ai_vision("claude:opus", blocks=_blocks())
    finally:
        _restore_runner(old)
    assert v.available is True and v.verdict is None and v.timed_out is True


def test_visual_cli_runner_uses_wall_timeout_mode():
    cap: dict = {}
    old = _patch_runner(cap, stdout='{"verdict":"keep","confidence":0.9}')
    try:
        vc.call_ai_vision("claude:opus", blocks=_blocks())
    finally:
        _restore_runner(old)
    assert cap["timeout_mode"] == "wall"


def test_cli_backend_unreachable_when_binary_missing():
    """vision_backend_available is the CLI BINARY check (NOT a REST key) for the agent
    CLIs — a missing binary makes the backend unreachable so the selector skips it."""
    import shutil

    old_which = shutil.which
    shutil.which = lambda name: None if name in ("codex", "claude", "opencode") else old_which(name)
    try:
        assert vc.vision_backend_available("codex") is False
        assert vc.vision_backend_available("claude:opus") is False
        assert vc.vision_backend_available("oc:fireworks/qwen2-vl") is False
        assert vc.select_vision_backend(["codex", "claude:opus"]) is None
    finally:
        shutil.which = old_which


def test_selection_prefers_first_reachable_cli():
    """select_vision_backend honors the requested order, returning the first reachable
    vision backend (CLI binary present / Gemini key present)."""
    old = vc.vision_backend_available
    vc.vision_backend_available = lambda m: True  # pretend everything reachable
    try:
        assert vc.select_vision_backend(["codex", "gemini"]) == "codex"
        assert vc.select_vision_backend(["oc:fireworks/qwen2-vl", "codex"]) == "oc:fireworks/qwen2-vl"
    finally:
        vc.vision_backend_available = old
    assert vc.select_vision_backend([]) is None


def test_select_vision_backends_keeps_ordered_fallbacks_and_rejects_text_glm():
    old = vc.vision_backend_available
    vc.vision_backend_available = lambda m: True
    try:
        models = [
            "claude:claude-opus-4-8",
            "commandcode:zai-org/GLM-5.2",
            "oc:zai/glm-4.5v",
            "oc:commandcode/moonshotai/Kimi-K2.7-Code",
        ]
        assert vc.select_vision_backends(models) == [
            "claude:claude-opus-4-8",
            "oc:zai/glm-4.5v",
            "oc:commandcode/moonshotai/Kimi-K2.7-Code",
        ]
    finally:
        vc.vision_backend_available = old


def test_call_ai_vision_with_fallback_skips_unusable_primary():
    calls: list[str | None] = []
    old = vc.call_ai_vision

    def fake(model, **kwargs):
        calls.append(model)
        if model == "claude:claude-opus-4-8":
            return vc.VisionVerdict(
                available=True,
                verdict=None,
                error="model is currently unavailable",
                backend=model,
            )
        return vc.VisionVerdict(available=True, verdict="keep", confidence=0.91, backend=model)

    vc.call_ai_vision = fake
    try:
        verdict = vc.call_ai_vision_with_fallback(
            ["claude:claude-opus-4-8", "oc:zai/glm-4.5v"],
            blocks=_blocks(),
        )
    finally:
        vc.call_ai_vision = old
    assert calls == ["claude:claude-opus-4-8", "oc:zai/glm-4.5v"], calls
    assert verdict.verdict == "keep"
    assert verdict.backend == "oc:zai/glm-4.5v"


# === Gemini: the ONE REST-key exception (CLI broken) =============================
def test_gemini_stays_rest_key_and_honors_env_model():
    """Gemini's vision call goes over the REST API key (its CLI is broken), honoring
    $GEMINI_MODEL exactly like review_gemini. urlopen + key faked — NO real network."""
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
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        return _FakeResp()

    old_open = urllib.request.urlopen
    old_key = backends._gemini_key
    old_env = os.environ.get("GEMINI_MODEL")
    urllib.request.urlopen = fake_urlopen
    backends._gemini_key = lambda: "fake-key"
    os.environ["GEMINI_MODEL"] = "gemini-3.0-pro"
    try:
        body = vc.build_gemini_request("sys", _blocks(), vc.build_output_schema())
        assert body["generationConfig"]["response_schema"]["type"] == "OBJECT"
        v = vc.call_ai_vision("gemini", blocks=_blocks())
        assert v.verdict == "keep"
        assert "gemini-3.0-pro:generateContent" in captured["url"], captured.get("url")
        # REST key path: x-goog-api-key header present (NOT a CLI invocation).
        assert captured["headers"].get("x-goog-api-key") == "fake-key"
    finally:
        urllib.request.urlopen = old_open
        backends._gemini_key = old_key
        if old_env is None:
            os.environ.pop("GEMINI_MODEL", None)
        else:
            os.environ["GEMINI_MODEL"] = old_env


def test_gemini_vision_default_model_is_current():
    """The vision Gemini fallback must POST to a CURRENT, non-retired model when
    $GEMINI_MODEL is unset. Guards vision_client.py's default independently of
    review_gemini's (issue #139): the old `gemini-2.5-flash` fallback 404'd and
    shuts down 2026-10-16; `gemini-3.5-flash` is Google's GA replacement."""
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
    old_env = os.environ.pop("GEMINI_MODEL", None)
    urllib.request.urlopen = fake_urlopen
    backends._gemini_key = lambda: "fake-key"
    try:
        v = vc.call_ai_vision("gemini", blocks=_blocks())
        assert v.verdict == "keep"
        assert "models/gemini-3.5-flash:generateContent" in captured["url"], captured.get("url")
    finally:
        urllib.request.urlopen = old_open
        backends._gemini_key = old_key
        if old_env is not None:
            os.environ["GEMINI_MODEL"] = old_env


def test_gemini_unreachable_without_key():
    """No Gemini key → available=False (fail-closed → unverified), never a crash."""
    import reviewlib.backends as backends

    old = backends._gemini_key

    def _raise():
        raise RuntimeError("GEMINI_API_KEY not found")

    backends._gemini_key = _raise
    try:
        assert vc.vision_backend_available("gemini") is False
        v = vc.call_ai_vision("gemini", blocks=_blocks())
    finally:
        backends._gemini_key = old
    assert v.available is False and v.verdict is None


def test_gemini_request_shape_unchanged():
    """Gemini request shape (inline_data image part + sanitized response_schema) is
    unchanged — it is still the REST exception."""
    schema = vc.build_output_schema()
    body = vc.build_gemini_request("sys", _blocks(), schema)
    parts = body["contents"][0]["parts"]
    img = [p for p in parts if "inline_data" in p]
    assert img and img[0]["inline_data"]["mime_type"] == "image/png"
    assert img[0]["inline_data"]["data"] == _IMG
    rs = body["generationConfig"]["response_schema"]
    assert rs["properties"]["verdict"]["enum"] == list(vc.VISION_VERDICTS)
    assert "additionalProperties" not in rs
    assert "maxLength" not in rs["properties"]["note"]


def test_before_after_labels_emitted_in_cli_prompt():
    """A before/after pair must be captioned in the CLI prompt so the model knows which is
    the baseline (the images are attached as files; the labels go in the text)."""
    schema = vc.build_output_schema()
    prompt = vc._prompt_text(_before_after_blocks(), schema)
    assert "BEFORE image" in prompt and "AFTER image" in prompt


def test_stage_images_writes_real_files_with_right_suffix():
    """Image staging decodes the base64 blocks to temp files with the correct extension
    so the CLI's content sniffing picks the media type."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        jpg_block = vc.VisionBlock(kind="image", label="after", media_type="image/jpeg", data_base64=_IMG)
        paths = vc._stage_images([vc.VisionBlock(kind="text", text="x"), jpg_block], Path(d))
        assert len(paths) == 1
        assert paths[0].suffix == ".jpg"
        assert paths[0].read_bytes() == _IMG_BYTES


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
