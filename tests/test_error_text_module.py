#!/usr/bin/env python3
"""error-text built-in module tests (§4). NO real API calls.

The error-text module is a default-on built-in visual checker with no cvGate pixel
heuristic. It contributes one semantic vision question and only blocks when the
vision answer explicitly confirms visible error/exception/failure text.

Proves:
  * activation: the built-in is always active, regardless of intent or requested checks;
  * CV phase: `cv_check` always abstains with None because there is no pixel signal;
  * judge: `error_text_visible=True` is a hard veto, while False or absent abstains;
  * pipeline: the module veto turns a high-confidence model keep into rollback.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import visual_fixtures as vf  # noqa: E402
from reviewlib.features.visual.contract import derive_contract  # noqa: E402
from reviewlib.features.visual.cv_gate import CvSignals  # noqa: E402
from reviewlib.features.visual.module_api import VisualContext  # noqa: E402
from reviewlib.features.visual.modules.builtins import (  # noqa: E402
    ErrorTextModule,
    builtin_modules,
)
from reviewlib.features.visual.vision_client import (  # noqa: E402
    VisionVerdict,
    build_output_schema,
    parse_structured,
)


MODULE = ErrorTextModule()


def _signals(**kw) -> CvSignals:
    base = dict(
        width=400,
        height=300,
        palette_entropy=0.7,
        dominant_coverage=0.15,
        quant_colors=120,
        mean_luma=0.5,
        palette_chroma=0.3,
        error_red_fraction=0.0,
    )
    base.update(kw)
    return CvSignals(**base)


def _ctx(*, requested_checks=None, intent=None, cv_signals=None) -> VisualContext:
    return VisualContext(
        after_image=b"",
        before_image=None,
        expectation=derive_contract(None, intent, has_before=False),
        cv_signals=cv_signals if cv_signals is not None else _signals(),
        intent=intent,
        requested_checks=requested_checks or [],
    )


def test_activates_always():
    assert MODULE.activates(_ctx()) is True
    assert MODULE.activates(_ctx(requested_checks=["selection"])) is True
    assert MODULE.activates(_ctx(intent="verify a styled dashboard")) is True


def test_cv_check_always_abstains_with_no_pixel_opinion():
    assert MODULE.cv_check(_ctx()) is None
    assert MODULE.cv_check(_ctx(cv_signals=_signals(blank_suspected=True))) is None
    assert MODULE.cv_check(_ctx(cv_signals=_signals(overlay_suspected=True))) is None


def test_judge_blocks_when_error_text_visible_true():
    vision = VisionVerdict(
        available=True,
        verdict="keep",
        confidence=0.93,
        module_answers={"error_text_visible": True},
    )
    mv = MODULE.judge(_ctx(), vision)
    assert mv.decision == "block"
    assert mv.module == "error-text"
    assert mv.confidence == 0.93


def test_judge_abstains_when_error_text_visible_false():
    vision = VisionVerdict(
        available=True,
        verdict="keep",
        confidence=0.93,
        module_answers={"error_text_visible": False},
    )
    mv = MODULE.judge(_ctx(), vision)
    assert mv.decision == "abstain"


def test_judge_abstains_when_error_text_visible_absent():
    vision = VisionVerdict(
        available=True,
        verdict="keep",
        confidence=0.93,
        module_answers={},
    )
    mv = MODULE.judge(_ctx(), vision)
    assert mv.decision == "abstain"


def test_contributes_error_text_vision_question():
    qs = MODULE.vision_questions(_ctx())
    assert any("error_text_visible" in q for q in qs), qs


def test_builtin_modules_includes_error_text():
    assert any(m.name == "error-text" for m in builtin_modules())


def test_real_collector_asks_the_question_and_requires_the_schema_field():
    """Drive the ACTUAL pipeline collector functions (`_active_modules` ->
    `vision_questions` -> `_vision_field_names` -> `build_output_schema`), not a
    hand-rolled re-implementation of the filter: `ErrorTextModule` deliberately does NOT
    subclass `_SignalModule` (it has no CvSignals flag), so a collector that secretly
    keyed off `_SignalModule` (isinstance check, subclass registration, etc.) would
    silently drop it — the question would never be asked and the field would never be
    required — while every other test here (which hand-builds VisionVerdicts/contexts)
    would stay green. This proves the real, unmodified pipeline code asks the question
    and requires the field for a plain --visual run with no --check."""
    from reviewlib.features.visual import pipeline as pl

    ctx = _ctx()
    modules = pl._active_modules(ctx, project=None)
    assert any(m.name == "error-text" for m in modules), (
        "error-text must be selected by the pipeline's own module-activation collector"
    )
    questions = [q for m in modules for q in m.vision_questions(ctx)]
    assert any("error_text_visible" in q for q in questions), questions

    schema = build_output_schema(pl._vision_field_names(modules))
    assert "error_text_visible" in schema["properties"]
    assert schema["properties"]["error_text_visible"] == {"type": "boolean"}
    assert "error_text_visible" in schema["required"]


def test_real_model_json_parses_error_text_visible_into_module_answers():
    """Drive the ACTUAL vision_client parsing path (not a hand-built VisionVerdict): a
    raw structured model response containing `error_text_visible` must land in
    `VisionVerdict.module_answers`, exactly like the existing `unstyled`/`blank`/
    `error_overlay` fields do — `parse_structured` has no per-field whitelist (it is a
    DENYLIST of the fixed core keys), so a new module field needs no registration here,
    but this proves it empirically rather than by reading the denylist."""
    raw = {
        "verdict": "keep",
        "confidence": 0.91,
        "observed_change_regions": [],
        "defects": [],
        "injection_suspected": False,
        "note": "looks fine",
        "error_text_visible": True,
    }
    verdict = parse_structured(raw, backend="gemini")
    assert verdict.module_answers.get("error_text_visible") is True
    mv = MODULE.judge(_ctx(), verdict)
    assert mv.decision == "block", (
        "the real parse path must feed judge() a True that blocks"
    )


def _run_error_text_pipeline(tmp_dir: Path, *, visible: bool):
    from reviewlib.features.visual import pipeline as pl

    img = tmp_dir / "styled.png"
    vf.styled_render(img)

    call_log: list[str] = []

    def fake_call(model, **kwargs):
        call_log.append(model)
        return VisionVerdict(
            available=True,
            verdict="keep",
            confidence=0.98,
            note="mock keep",
            module_answers={"error_text_visible": visible},
            backend=model,
        )

    old_call = pl.call_ai_vision
    old_select = pl.select_vision_backend
    pl.call_ai_vision = fake_call
    pl.select_vision_backend = lambda models: "gemini"
    try:
        verdict = pl.run_pipeline(
            img,
            models=["gemini"],
            local_model=False,
            project=tmp_dir,
        )
    finally:
        pl.call_ai_vision = old_call
        pl.select_vision_backend = old_select
    return verdict, call_log


def test_pipeline_rolls_back_when_vision_confirms_error_text():
    with tempfile.TemporaryDirectory() as d:
        verdict, call_log = _run_error_text_pipeline(Path(d), visible=True)

        assert call_log == ["gemini"]
        assert verdict.final == "rollback"
        assert "error-text" in verdict.reason


def test_pipeline_keeps_when_vision_denies_error_text():
    with tempfile.TemporaryDirectory() as d:
        verdict, call_log = _run_error_text_pipeline(Path(d), visible=False)

        assert call_log == ["gemini"]
        assert verdict.final == "keep"


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
