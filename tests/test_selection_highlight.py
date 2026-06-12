#!/usr/bin/env python3
"""selection-highlight contributed module tests (§6 worked example). NO real API.

The selection-highlight module is HyperIDE's contributed per-project checker: it
repackages `bin/frames-check` (a deterministic colour+shape detector for the 2px
rgb(59,130,246) selection outline) as a `VisualModule` that `activates_on: ["selection"]`.

Proves:
  * detection: a fixture with a real 2px blue hollow outline is detected (>=1 frame);
    a fixture WITHOUT the outline detects 0 frames.
  * activation: the module activates ONLY when the "selection" tag is requested (or the
    intent mentions selection) — a plain --visual run leaves it off.
  * veto: when "selection" is expected but NO outline is present, the module's judge
    BLOCKS (a hard veto), so a model `keep` cannot pass a missing-selection proof.
  * pass: when the outline IS present, the module does not block.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import visual_fixtures as vf  # noqa: E402
from reviewlib.features.visual.contract import derive_contract  # noqa: E402
from reviewlib.features.visual.cv_gate import compute_signals  # noqa: E402
from reviewlib.features.visual.module_api import VisualContext  # noqa: E402
from reviewlib.features.visual.vision_client import VisionVerdict  # noqa: E402

# Load the contributed reference module directly (the file the registry would import).
import importlib.util  # noqa: E402

_ENTRY = REPO_ROOT / "reviewlib" / "features" / "visual" / "contrib" / "selection_highlight.py"
_spec = importlib.util.spec_from_file_location("selection_highlight_ref", _ENTRY)
selection_highlight = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(selection_highlight)
MODULE = selection_highlight.MODULE

SELECTION_BLUE = (59, 130, 246)


def _with_outline(path: Path, size=(400, 300)) -> Path:
    """A styled-ish render with a real 2px rgb(59,130,246) hollow rectangle outline."""
    img = Image.new("RGB", size, "rgb(245,247,250)")
    d = ImageDraw.Draw(img)
    # A content card (NON-white fill so the selection ring stays HOLLOW after the
    # colour mask — a white fill would merge with the masked-to-white outline and read
    # as a solid block, which the shape gate correctly rejects).
    d.rectangle([60, 60, 240, 200], fill="rgb(228,232,240)", outline="rgb(200,200,200)")
    # The 2px rgb(59,130,246) selection outline around the element.
    d.rectangle([60, 60, 240, 200], outline=SELECTION_BLUE, width=2)
    img.save(path)
    return path


def _without_outline(path: Path) -> Path:
    return vf.styled_render(path)  # rich render, but no blue selection frame


def _ctx(image: Path, *, requested_checks=None, intent=None) -> VisualContext:
    return VisualContext(
        after_image=image.read_bytes(),
        before_image=None,
        expectation=derive_contract(None, intent, has_before=False),
        cv_signals=compute_signals(image),
        intent=intent,
        requested_checks=requested_checks or [],
    )


def test_detects_real_outline():
    img = Path(tempfile.mkstemp(suffix="-sel.png")[1])
    _with_outline(img)
    frames = selection_highlight.detect_selection_frames(img)
    assert len(frames) >= 1, f"expected >=1 selection frame, got {frames}"


def test_no_outline_detects_zero():
    img = Path(tempfile.mkstemp(suffix="-nosel.png")[1])
    _without_outline(img)
    frames = selection_highlight.detect_selection_frames(img)
    assert frames == [], f"styled render has no selection frame, got {frames}"


def test_activates_only_on_selection_tag():
    img = Path(tempfile.mkstemp(suffix="-sel.png")[1])
    _with_outline(img)
    assert MODULE.activates(_ctx(img, requested_checks=[])) is False
    assert MODULE.activates(_ctx(img, requested_checks=["selection"])) is True
    assert MODULE.activates(_ctx(img, intent="confirm the selection outline is drawn")) is True


def test_cv_check_blocks_when_outline_missing():
    """Expecting a selection, no outline → the module's cv_check BLOCKS (a hard veto),
    short-circuiting before any vision call."""
    img = Path(tempfile.mkstemp(suffix="-nosel.png")[1])
    _without_outline(img)
    mv = MODULE.cv_check(_ctx(img, requested_checks=["selection"]))
    assert mv is not None
    assert mv.decision == "block", f"missing selection must veto, got {mv.decision}"


def test_cv_check_passes_when_outline_present():
    img = Path(tempfile.mkstemp(suffix="-sel.png")[1])
    _with_outline(img)
    mv = MODULE.cv_check(_ctx(img, requested_checks=["selection"]))
    assert mv is not None
    assert mv.decision == "pass", f"present selection must pass, got {mv.decision}"


def test_judge_blocks_when_model_says_missing():
    """Even if CV is borderline, a model that answers selection_present=False on a
    selection check must block (the judge folds the model answer)."""
    img = Path(tempfile.mkstemp(suffix="-sel.png")[1])
    _with_outline(img)
    vision = VisionVerdict(available=True, verdict="keep", confidence=0.9, module_answers={"selection_present": False})
    mv = MODULE.judge(_ctx(img, requested_checks=["selection"]), vision)
    assert mv.decision == "block", "model-confirmed missing selection must veto"


def test_contributes_selection_vision_question():
    img = Path(tempfile.mkstemp(suffix="-sel.png")[1])
    _with_outline(img)
    qs = MODULE.vision_questions(_ctx(img, requested_checks=["selection"]))
    assert any("selection_present" in q for q in qs), qs


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
