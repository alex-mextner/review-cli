#!/usr/bin/env python3
"""policy_engine tests — the judge OUTSIDE the model (§3.3).

Proves the model is a witness, not the judge:
  * verdict → exit-code mapping (0 keep / 10 blocking-under-strict / 1 unreadable / 124 timeout);
  * a model 'keep' against a CV blank/overlay suspicion does NOT keep (escalates);
  * a low-confidence keep escalates;
  * injection_suspected escalates;
  * a module 'block' veto turns a model 'keep' into rollback (modules only tighten);
  * no vision backend → unverified (fail-closed, never keep).
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib.features.visual.cv_gate import CvSignals  # noqa: E402
from reviewlib.features.visual.module_api import ModuleVerdict  # noqa: E402
from reviewlib.features.visual.policy_engine import (  # noqa: E402
    EXIT_BLOCK_STRICT,
    EXIT_KEEP,
    EXIT_TIMEOUT,
    EXIT_USAGE,
    Verdict,
    decide_from_cv,
    decide_from_vision,
    exit_code_for,
)
from reviewlib.features.visual.vision_client import VisionVerdict  # noqa: E402


def _signals(**kw) -> CvSignals:
    base = dict(
        width=400, height=300, palette_entropy=0.7, dominant_coverage=0.15,
        quant_colors=120, mean_luma=0.5, palette_chroma=0.3, error_red_fraction=0.0,
    )
    base.update(kw)
    return CvSignals(**base)


def test_exit_code_map():
    keep = Verdict(final="keep", reason="ok")
    rollback = Verdict(final="rollback", reason="broken")
    human = Verdict(final="human_review", reason="unsure")
    unver = Verdict(final="unverified", reason="no backend")

    assert exit_code_for(keep, strict=True) == EXIT_KEEP
    assert exit_code_for(keep, strict=False) == EXIT_KEEP
    # Blocking verdicts only bite under --strict.
    assert exit_code_for(rollback, strict=True) == EXIT_BLOCK_STRICT
    assert exit_code_for(rollback, strict=False) == EXIT_KEEP
    assert exit_code_for(human, strict=True) == EXIT_BLOCK_STRICT
    assert exit_code_for(unver, strict=True) == EXIT_BLOCK_STRICT


def test_exit_code_unreadable_is_usage():
    v = decide_from_cv("reject", "unreadable: no such file", None)
    assert v.final == "rollback"
    assert exit_code_for(v, strict=True) == EXIT_USAGE
    assert exit_code_for(v, strict=False) == EXIT_USAGE


def test_exit_code_timeout():
    timed = Verdict(final="human_review", reason="timed out", timed_out=True)
    assert exit_code_for(timed, strict=False) == EXIT_TIMEOUT
    assert exit_code_for(timed, strict=True) == EXIT_TIMEOUT


def test_model_keep_against_blank_cv_escalates():
    """model says keep, CV flags blank → must NOT keep (the price-100→1.00 catastrophe)."""
    vision = VisionVerdict(available=True, verdict="keep", confidence=0.95)
    v = decide_from_vision(vision, _signals(blank_suspected=True))
    assert v.final == "human_review", f"keep against a blank CV flag must escalate, got {v.final}"


def test_model_keep_low_confidence_escalates():
    vision = VisionVerdict(available=True, verdict="keep", confidence=0.4)
    v = decide_from_vision(vision, _signals())
    assert v.final == "human_review"


def test_model_keep_high_confidence_keeps():
    vision = VisionVerdict(available=True, verdict="keep", confidence=0.92)
    v = decide_from_vision(vision, _signals())
    assert v.final == "keep"


def test_injection_suspected_escalates():
    vision = VisionVerdict(available=True, verdict="keep", confidence=0.95, injection_suspected=True)
    v = decide_from_vision(vision, _signals())
    assert v.final == "human_review"
    assert v.injection_suspected


def test_module_block_veto_overrides_keep():
    """A module 'block' turns a high-confidence model keep into rollback (only tightens)."""
    vision = VisionVerdict(available=True, verdict="keep", confidence=0.95)
    mods = [ModuleVerdict(module="selection-highlight", decision="block", confidence=0.9, reason="no outline")]
    v = decide_from_vision(vision, _signals(), mods)
    assert v.final == "rollback", f"a module block must veto a keep, got {v.final}"


def test_module_block_wins_over_low_confidence_escalation():
    """A module hard veto must apply even when an early model-keep escalation branch
    (low confidence) would otherwise return human_review first (codex P2). The hard
    veto promised by the module API wins → rollback, not human_review."""
    vision = VisionVerdict(available=True, verdict="keep", confidence=0.6)  # would escalate
    mods = [ModuleVerdict(module="selection-highlight", decision="block", confidence=0.9, reason="no outline")]
    v = decide_from_vision(vision, _signals(), mods)
    assert v.final == "rollback", f"a module block must win over the low-confidence escalation, got {v.final}"
    assert "module veto" in v.reason and "selection-highlight" in v.reason


def test_no_backend_is_unverified():
    vision = VisionVerdict(available=False, verdict=None, error="no vision backend")
    v = decide_from_vision(vision, _signals())
    assert v.final == "unverified"
    assert exit_code_for(v, strict=True) == EXIT_BLOCK_STRICT


def test_invalid_model_output_fails_closed():
    vision = VisionVerdict(available=True, verdict=None, error="unparseable")
    v = decide_from_vision(vision, _signals())
    assert v.final == "human_review", "invalid/None model verdict must fail closed, not keep"


def test_real_timeout_maps_to_124_network_failure_does_not():
    """timed_out comes from the VisionVerdict flag, NOT an error-text substring (codex
    P2): a genuine timeout → exit 124; a DNS/connection failure → human_review exit 0
    (non-strict), never a spurious 124."""
    timed = VisionVerdict(available=True, verdict=None, error="vision call timed out: ...", timed_out=True)
    vt = decide_from_vision(timed, _signals())
    assert vt.timed_out
    assert exit_code_for(vt, strict=False) == EXIT_TIMEOUT

    # A network failure whose text happens to be benign must NOT be a timeout.
    net = VisionVerdict(available=True, verdict=None, error="vision call failed: Name or service not known", timed_out=False)
    vn = decide_from_vision(net, _signals())
    assert not vn.timed_out
    assert vn.final == "human_review"
    assert exit_code_for(vn, strict=False) == EXIT_KEEP, "a non-timeout network failure must not exit 124"


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
