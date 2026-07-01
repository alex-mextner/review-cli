#!/usr/bin/env python3
"""Per-mode multimodal fan-out tests (§2.1 Stage 2). MOCK the vision API — no real call.

Stage 1 left "companion modes don't deliver raw pixels" as a stub: a companion mode
(brainstorm/quorum/just-ask/default) only got a cvGate-described context note. Stage 2
delivers the actual IMAGE to the model via `call_ai_vision` (a single real multimodal
call carrying the active modules' vision_questions) and folds that GROUNDED observation
into each persona/voter/reviewer prompt — so the personas reason over what a vision model
actually SAW, not just cvGate signals.

Proves (mocking call_ai_vision):
  * the image is threaded into a real `call_ai_vision` call (the seam invokes it), with
    the active modules' vision_questions folded into the request;
  * the grounded observation (the model's note + verdict + module answers) is woven into
    each persona's / voter's prompt — asserted by capturing the mode call;
  * fan-out is fail-closed: if a required vision call is unavailable/unusable the
    companion text mode is blocked instead of pretending the screenshot was reviewed;
  * standalone pipeline path is unchanged (still one vision call).
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import visual_fixtures as vf  # noqa: E402
from reviewlib import cli  # noqa: E402
from reviewlib.backends import ReviewResult  # noqa: E402
from reviewlib.features.visual import compose as cmp  # noqa: E402
from reviewlib.features.visual.vision_client import VisionVerdict  # noqa: E402
from reviewlib.modes import brainstorm as _brainstorm_mod  # noqa: E402
from reviewlib.modes import just_ask as _just_ask_mod  # noqa: E402
from reviewlib.modes import quorum as _quorum_mod  # noqa: E402


def _styled(tmp: str = "/tmp/fanout-styled.png") -> str:
    return str(vf.styled_render(Path(tmp)))


def _patch_vision(verdict: VisionVerdict, capture: dict):
    def fake_call(model, **kwargs):
        capture["model"] = model
        capture["blocks"] = kwargs.get("blocks")
        capture["schema"] = kwargs.get("output_schema")
        return verdict

    old_call = cmp.call_ai_vision
    old_select = cmp.select_vision_backend
    cmp.call_ai_vision = fake_call
    cmp.select_vision_backend = lambda models: "gemini"
    return old_call, old_select


def _restore(old_call, old_select):
    cmp.call_ai_vision = old_call
    cmp.select_vision_backend = old_select


def test_image_threaded_into_real_vision_call():
    """The companion seam runs call_ai_vision with the actual image block attached."""
    cap: dict = {}
    old = _patch_vision(
        VisionVerdict(available=True, verdict="keep", confidence=0.9, note="a blue dashboard with cards", backend="gemini"),
        cap,
    )
    try:
        comp = cmp.build_mode_visual_context(Path(_styled()), models=["gemini"])
    finally:
        _restore(*old)
    # An actual image block was delivered to the vision call (not just a text note).
    blocks = cap.get("blocks") or []
    img_blocks = [b for b in blocks if b.kind == "image"]
    assert img_blocks, "no image block delivered to call_ai_vision"
    assert img_blocks[0].data_base64, "image block had no base64 pixels"
    # The grounded observation rode into the context note woven into mode prompts.
    assert comp.observation is not None
    assert "a blue dashboard with cards" in comp.context_note


def test_modules_vision_questions_folded_into_fanout():
    """When a module is active (e.g. selection via --check), its vision_questions are
    folded into the single fan-out vision call's request + schema."""
    cap: dict = {}
    old = _patch_vision(VisionVerdict(available=True, verdict="keep", confidence=0.9, note="ok"), cap)
    try:
        cmp.build_mode_visual_context(
            Path(_styled()), models=["gemini"], requested_checks=["style-presence"],
        )
    finally:
        _restore(*old)
    # The built-in style-presence module's boolean field must be in the schema, and its
    # question text must appear in the text block delivered to the model.
    schema = cap.get("schema") or {}
    assert "unstyled" in schema.get("properties", {}), "module field not folded into schema"
    blocks = cap.get("blocks") or []
    text = " ".join(b.text or "" for b in blocks if b.kind == "text")
    assert "unstyled" in text, "module vision question not folded into the fan-out prompt"


def test_brainstorm_personas_see_grounded_observation():
    """End-to-end through the CLI: each brainstorm persona's prompt carries the grounded
    visual observation (the vision model's note), not merely the cvGate note."""
    cap: dict = {}
    old = _patch_vision(
        VisionVerdict(available=True, verdict="keep", confidence=0.9, note="GROUNDED-SIGHT: teal sidebar, 6 cards"),
        cap,
    )
    captured = {}

    def fake_brainstorm(topic, *a, **k):
        captured["topic"] = topic
        return 0

    old_bs = _brainstorm_mod.mode_brainstorm
    _brainstorm_mod.mode_brainstorm = fake_brainstorm
    try:
        rc = cli.main(["brainstorm", "is the layout good", "--visual", _styled(), "-C", str(REPO_ROOT)])
    finally:
        _brainstorm_mod.mode_brainstorm = old_bs
        _restore(*old)
    assert rc == 0
    assert "GROUNDED-SIGHT: teal sidebar, 6 cards" in captured["topic"], "persona prompt lacks grounded observation"
    assert "is the layout good" in captured["topic"]


def test_quorum_voters_see_grounded_observation():
    cap: dict = {}
    old = _patch_vision(
        VisionVerdict(available=True, verdict="keep", confidence=0.9, note="GROUNDED-SIGHT: button is centered"),
        cap,
    )
    captured = {}

    def fake_quorum(question, *a, **k):
        captured["question"] = question
        return 0

    old_q = _quorum_mod.mode_quorum
    _quorum_mod.mode_quorum = fake_quorum
    try:
        rc = cli.main(["quorum", "is it centered?", "--visual", _styled(), "-C", str(REPO_ROOT)])
    finally:
        _quorum_mod.mode_quorum = old_q
        _restore(*old)
    assert rc == 0
    assert "GROUNDED-SIGHT: button is centered" in captured["question"]


def test_fanout_can_be_explicitly_cv_only_when_no_vision_backend():
    """No vision backend is allowed only when the caller explicitly disables required
    vision, matching --no-ai's cvGate-only/offline path."""
    old_call = cmp.call_ai_vision
    old_select = cmp.select_vision_backend
    cmp.select_vision_backend = lambda models: None  # no vision backend

    def fake_call(model, **kwargs):  # should not even be reached, but be safe
        return VisionVerdict(available=False, verdict=None)

    cmp.call_ai_vision = fake_call
    try:
        comp = cmp.build_mode_visual_context(Path(_styled()), models=[], require_vision=False)
    finally:
        cmp.call_ai_vision = old_call
        cmp.select_vision_backend = old_select
    assert comp.observation is None
    assert comp.vision_error == ""
    assert "ATTACHED RENDER" in comp.context_note  # cvGate-described note still present
    assert comp.prefilter_verdict is None  # styled pass-through, not blocked


def test_invalid_fanout_does_not_claim_grounded_observation():
    """A configured vision backend that returns no usable verdict is not a grounded
    observation. It must be surfaced as an unverified visual run, never as "vision saw it"."""
    cap: dict = {}
    old = _patch_vision(
        VisionVerdict(
            available=True,
            verdict=None,
            confidence=0.0,
            error="CLI returned no parseable JSON verdict",
            backend="gemini",
        ),
        cap,
    )
    try:
        comp = cmp.build_mode_visual_context(Path(_styled()), models=["gemini"])
    finally:
        _restore(*old)
    assert comp.observation is None
    assert "no parseable JSON" in comp.vision_error
    assert "vision model SAW this screenshot" not in comp.context_note
    assert "ATTACHED RENDER UNVERIFIED" in comp.context_note


def test_fanout_skips_unusable_opus_to_glm_vision_fallback():
    calls: list[str] = []
    old_call = cmp.call_ai_vision
    old_select = cmp.select_vision_backend
    old_select_all = cmp.select_vision_backends

    def fake_call(model, **kwargs):
        calls.append(model)
        if model == "claude:claude-opus-4-8":
            return VisionVerdict(
                available=True,
                verdict=None,
                error="model is currently unavailable",
                backend=model,
            )
        return VisionVerdict(
            available=True,
            verdict="keep",
            confidence=0.92,
            note="GLM saw the rendered dashboard",
            backend=model,
        )

    cmp.call_ai_vision = fake_call
    cmp.select_vision_backend = lambda models: "claude:claude-opus-4-8"
    cmp.select_vision_backends = lambda models: ["claude:claude-opus-4-8", "oc:zai/glm-4.5v"]
    try:
        comp = cmp.build_mode_visual_context(
            Path(_styled()),
            models=["claude:claude-opus-4-8", "oc:zai/glm-4.5v"],
        )
    finally:
        cmp.call_ai_vision = old_call
        cmp.select_vision_backend = old_select
        cmp.select_vision_backends = old_select_all
    assert calls == ["claude:claude-opus-4-8", "oc:zai/glm-4.5v"], calls
    assert comp.observation is not None
    assert comp.observation.backend == "oc:zai/glm-4.5v"
    assert "GLM saw the rendered dashboard" in comp.context_note


def test_companion_invalid_vision_blocks_the_mode():
    """End-to-end through the CLI: if the companion fan-out vision call is unusable,
    the text mode must not run and launder the failure into a normal answer."""
    cap: dict = {}
    old = _patch_vision(
        VisionVerdict(
            available=True,
            verdict=None,
            confidence=0.0,
            error="CLI returned no parseable JSON verdict",
            backend="gemini",
        ),
        cap,
    )
    called = {"n": 0}

    def fake_just_ask(question, *a, **k):
        called["n"] += 1
        return 0

    old_ja = _just_ask_mod.mode_just_ask
    _just_ask_mod.mode_just_ask = fake_just_ask
    try:
        rc_strict = cli.main(["just-ask", "describe", "--visual", _styled(), "--strict", "-C", str(REPO_ROOT)])
        rc_advisory = cli.main(["just-ask", "describe", "--visual", _styled(), "-C", str(REPO_ROOT)])
    finally:
        _just_ask_mod.mode_just_ask = old_ja
        _restore(*old)
    assert called["n"] == 0, "the mode must not run when the visual fan-out is unusable"
    assert rc_strict == 10
    assert rc_advisory == 1


def test_companion_panel_job_receives_raw_visual_image():
    """The companion text seat receives the actual image path in PanelJob.images so
    image-capable backends can inspect pixels directly, not only the vision summary."""
    cap: dict = {}
    old = _patch_vision(
        VisionVerdict(available=True, verdict="keep", confidence=0.9, note="looks styled", backend="gemini"),
        cap,
    )
    captured = {}

    def fake_run_panel(jobs, cwd, timeout):
        captured["images"] = jobs[0].images
        return [ReviewResult(model=jobs[0].model, command="fake", returncode=0, stdout="ok", stderr="")]

    old_panel = _just_ask_mod.run_panel
    _just_ask_mod.run_panel = fake_run_panel
    image = Path(_styled())
    try:
        rc = cli.main(["just-ask", "describe", "--visual", str(image), "-C", str(REPO_ROOT)])
    finally:
        _just_ask_mod.run_panel = old_panel
        _restore(*old)
    assert rc == 0
    assert captured["images"] == (image,)


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
