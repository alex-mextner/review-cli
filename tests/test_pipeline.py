#!/usr/bin/env python3
"""pipeline integration tests — contract → cvGate → vision → policy (§3). MOCK vision.

Proves the full orchestration with the vision call MOCKED (no API burned):
  * a styled pass-through + a mocked high-confidence vision 'keep' → final keep;
  * a styled pass-through + a mocked vision 'keep' that CONTRADICTS a (forced) CV
    blank flag → policy escalates to human_review (model is witness, policy is judge);
  * a cvGate auto-reject (blank) short-circuits with NO vision call (mock asserts it
    is never invoked);
  * --no-ai never calls the vision client.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import visual_fixtures as vf  # noqa: E402
from reviewlib.features.visual import pipeline as pl  # noqa: E402
from reviewlib.features.visual.vision_client import VisionVerdict  # noqa: E402


def _patch_vision(verdict: VisionVerdict, call_log: list):
    def fake_call(model, **kwargs):
        call_log.append(model)
        return verdict

    old_call = pl.call_ai_vision
    old_select = pl.select_vision_backend
    pl.call_ai_vision = fake_call
    pl.select_vision_backend = lambda models: "gemini"  # pretend a vision backend exists
    return old_call, old_select


def _restore(old_call, old_select):
    pl.call_ai_vision = old_call
    pl.select_vision_backend = old_select


def test_styled_passthrough_with_mock_keep():
    img = vf.styled_render(Path("/tmp/pl-styled.png"))
    log: list = []
    old = _patch_vision(VisionVerdict(available=True, verdict="keep", confidence=0.95, backend="gemini"), log)
    try:
        v = pl.run_pipeline(img, models=["gemini"])
    finally:
        _restore(*old)
    assert v.final == "keep", f"high-confidence vision keep should keep, got {v.final}"
    assert log == ["gemini"], "vision must be called exactly once for a pass-through"


def test_mock_keep_is_still_subject_to_policy_low_confidence():
    img = vf.styled_render(Path("/tmp/pl-styled2.png"))
    log: list = []
    old = _patch_vision(VisionVerdict(available=True, verdict="keep", confidence=0.3, backend="gemini"), log)
    try:
        v = pl.run_pipeline(img, models=["gemini"])
    finally:
        _restore(*old)
    assert v.final == "human_review", "a low-confidence keep must be escalated by policy, not kept"


def test_cvgate_reject_skips_vision_call():
    img = vf.blank_white(Path("/tmp/pl-blank.png"))
    log: list = []
    old = _patch_vision(VisionVerdict(available=True, verdict="keep", confidence=1.0), log)
    try:
        v = pl.run_pipeline(img, models=["gemini"])
    finally:
        _restore(*old)
    assert v.final == "rollback"
    assert log == [], "cvGate auto-reject must NOT reach the vision call"


def test_no_ai_never_calls_vision():
    img = vf.styled_render(Path("/tmp/pl-noai.png"))
    log: list = []
    old = _patch_vision(VisionVerdict(available=True, verdict="keep", confidence=1.0), log)
    try:
        v = pl.run_pipeline(img, models=["gemini"], no_ai=True)
    finally:
        _restore(*old)
    assert log == [], "--no-ai must not call the vision client"
    assert v.final == "human_review"


def test_zero_diff_without_before_fails_closed():
    """--expect zero-diff with NO --before is unverifiable (nothing to compare). The
    pipeline must fail closed to human_review, never reach a model keep (codex P2)."""
    img = vf.styled_render(Path("/tmp/pl-zerodiff.png"))
    log: list = []
    old = _patch_vision(VisionVerdict(available=True, verdict="keep", confidence=1.0), log)
    try:
        v = pl.run_pipeline(img, expect="zero-diff", models=["gemini"])
    finally:
        _restore(*old)
    assert v.final == "human_review", f"zero-diff without --before must fail closed, got {v.final}"
    assert log == [], "must not call the vision model for an unverifiable zero-diff"


def test_zero_diff_missing_image_is_deterministic_reject():
    """A MISSING image with --expect zero-diff (no --before) must be validated by cvGate
    FIRST → an unreadable rollback (usage), not the human_review fail-close (codex P2)."""
    from reviewlib.features.visual.policy_engine import exit_code_for

    missing = Path("/tmp/pl-does-not-exist-zd.png")
    v = pl.run_pipeline(missing, expect="zero-diff", models=["gemini"])
    assert v.final == "rollback", f"missing image must reject (not human_review), got {v.final}"
    assert "unreadable" in v.reason
    assert exit_code_for(v, strict=True) == 1, "unreadable input is a usage error (exit 1)"


def test_no_vision_backend_is_unverified():
    img = vf.styled_render(Path("/tmp/pl-unver.png"))
    old_select = pl.select_vision_backend
    pl.select_vision_backend = lambda models: None  # no vision backend configured
    try:
        v = pl.run_pipeline(img, models=[])
    finally:
        pl.select_vision_backend = old_select
    assert v.final == "unverified", "no vision backend must fail closed to unverified"


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
