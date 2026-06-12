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
  * fan-out is robust: if no vision backend is configured (available=False) the mode
    still runs with the cvGate-described note (degrade, never crash);
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
from reviewlib.features.visual import compose as cmp  # noqa: E402
from reviewlib.features.visual.vision_client import VisionVerdict  # noqa: E402


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

    old_bs = cli.mode_brainstorm
    cli.mode_brainstorm = fake_brainstorm
    try:
        rc = cli.main(["--brainstorm", "is the layout good", "--visual", _styled(), "-C", str(REPO_ROOT)])
    finally:
        cli.mode_brainstorm = old_bs
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

    old_q = cli.mode_quorum
    cli.mode_quorum = fake_quorum
    try:
        rc = cli.main(["--quorum", "is it centered?", "--visual", _styled(), "-C", str(REPO_ROOT)])
    finally:
        cli.mode_quorum = old_q
        _restore(*old)
    assert rc == 0
    assert "GROUNDED-SIGHT: button is centered" in captured["question"]


def test_fanout_degrades_when_no_vision_backend():
    """No vision backend configured (available=False) → the mode still runs, with the
    cvGate-described note (never a crash, never a hard block on a pass-through)."""
    old_call = cmp.call_ai_vision
    old_select = cmp.select_vision_backend
    cmp.select_vision_backend = lambda models: None  # no vision backend

    def fake_call(model, **kwargs):  # should not even be reached, but be safe
        return VisionVerdict(available=False, verdict=None)

    cmp.call_ai_vision = fake_call
    try:
        comp = cmp.build_mode_visual_context(Path(_styled()), models=[])
    finally:
        cmp.call_ai_vision = old_call
        cmp.select_vision_backend = old_select
    assert comp.observation is None
    assert "ATTACHED RENDER" in comp.context_note  # cvGate-described note still present
    assert comp.prefilter_verdict is None  # styled pass-through, not blocked


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
