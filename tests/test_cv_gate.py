#!/usr/bin/env python3
"""cvGate tests — the deterministic image-only pre-filter (§3.1).

Proves the contract that protects the whole pipeline:
  * the four auto-reject classes (blank/solid, unstyled-FOUC, error-overlay,
    unreadable) are auto-rejected WITHOUT a vision call;
  * a normal styled screenshot PASSES THROUGH (never a false-positive auto-reject);
  * the --before no-effect path: byte-identical when a change was expected → reject;
    byte-identical under a zero-diff expectation → the audited no_effect_bypass keep.

No network, no vision model. Fixtures are generated deterministically (Pillow) so
there are no committed binary blobs. Self-running (no pytest needed), matching the
repo's existing tests/test_streaming.py convention.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import visual_fixtures as vf  # noqa: E402
from reviewlib.features.visual.cv_gate import CvError, compute_signals, cv_gate  # noqa: E402


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="cvgate-test-"))


def test_blank_white_is_rejected():
    img = vf.blank_white(_tmp() / "blank.png")
    res = cv_gate(img)
    assert res.outcome == "reject", f"blank canvas must auto-reject, got {res.outcome}"
    assert res.verdict == "rollback"
    assert res.signals is not None and res.signals.blank_suspected


def test_solid_fill_is_rejected():
    img = vf.solid_fill(_tmp() / "solid.png")
    res = cv_gate(img)
    assert res.outcome == "reject", f"solid fill must auto-reject, got {res.outcome}"
    assert res.signals is not None and res.signals.blank_suspected


def test_unstyled_render_is_rejected():
    img = vf.unstyled_render(_tmp() / "unstyled.png")
    res = cv_gate(img)
    assert res.outcome == "reject", f"unstyled no-CSS render must auto-reject, got {res.outcome}"
    assert res.signals is not None and res.signals.unstyled_suspected


def test_error_overlay_is_rejected():
    img = vf.error_overlay(_tmp() / "error.png")
    res = cv_gate(img)
    assert res.outcome == "reject", f"error overlay must auto-reject, got {res.outcome}"
    assert res.signals is not None and res.signals.overlay_suspected


def test_styled_render_passes_through():
    """The critical false-positive guard: a real styled screenshot must NEVER be
    auto-rejected by cvGate — it goes to the vision model."""
    img = vf.styled_render(_tmp() / "styled.png")
    res = cv_gate(img)
    assert res.outcome == "pass_through", f"styled render must pass through, got {res.outcome} ({res.reason})"
    assert res.verdict is None
    sig = res.signals
    assert sig is not None
    assert not sig.blank_suspected and not sig.overlay_suspected and not sig.unstyled_suspected


def test_dark_monochrome_ui_passes_through():
    """A valid DARK monochrome UI (black terminal/dashboard with light text) is near-
    grayscale + low entropy + high bg coverage like an unstyled page, but its dark
    background means it is NOT the near-white no-CSS signature — it must pass through,
    not auto-reject (codex P2)."""
    img = vf.dark_ui_render(_tmp() / "darkui.png")
    res = cv_gate(img)
    assert res.outcome == "pass_through", f"a dark themed UI must pass through, got {res.outcome} ({res.reason})"
    assert res.signals is not None and not res.signals.unstyled_suspected


def test_blank_passes_when_global_repaint_expected():
    """A blank/solid frame is only fatal when a global repaint was NOT expected. The
    'global' exemption comes from the TRUSTED contract (--expect → diff_policy)."""
    img = vf.solid_fill(_tmp() / "solid.png")
    res = cv_gate(img, diff_policy="global")
    assert res.outcome == "pass_through", "global diff policy must not auto-reject a solid frame"


def test_intent_cannot_bypass_blank_reject():
    """Untrusted --intent must NOT weaken the blank-frame reject (codex P2). cv_gate no
    longer reads intent at all — the only blank exemption is the trusted diff_policy. So
    a blank frame under a non-global policy still rejects regardless of any intent that
    flowed in upstream."""
    img = vf.solid_fill(_tmp() / "solid.png")
    # diff_policy 'local' (the default for --expect style/text and the fallback) → reject.
    res = cv_gate(img, diff_policy="local")
    assert res.outcome == "reject", "a blank frame must reject under a non-global policy"


def test_unreadable_input_is_rejected():
    missing = _tmp() / "nope.png"
    res = cv_gate(missing)
    assert res.outcome == "reject"
    assert "unreadable" in res.reason

    # A 0-byte file is also unreadable.
    empty = _tmp() / "empty.png"
    empty.write_bytes(b"")
    res2 = cv_gate(empty)
    assert res2.outcome == "reject"

    # compute_signals raises CvError directly on a missing file.
    raised = False
    try:
        compute_signals(missing)
    except CvError:
        raised = True
    assert raised, "compute_signals must raise CvError on a missing file"


def test_no_effect_bypass_rejects_when_change_expected():
    """--before byte-identical to --after, with a change expected → 'did not apply'."""
    tmp = _tmp()
    before = vf.styled_render(tmp / "before.png")
    after = tmp / "after.png"
    after.write_bytes(before.read_bytes())  # byte-identical copy
    res = cv_gate(after, before=before, diff_policy="local")
    assert res.outcome == "reject", f"byte-identical render with change expected must reject, got {res.outcome}"
    assert "no effect" in res.reason


def test_no_effect_bypass_keeps_under_zero_diff():
    """--before byte-identical under a zero-diff expectation → the audited keep."""
    tmp = _tmp()
    before = vf.styled_render(tmp / "before.png")
    after = tmp / "after.png"
    after.write_bytes(before.read_bytes())
    res = cv_gate(after, before=before, diff_policy="zero")
    assert res.outcome == "no_effect_bypass"
    assert res.verdict == "keep"


def test_no_effect_bypass_does_not_keep_broken_render():
    """A byte-identical pair of BLANK renders under zero-diff must STILL reject (the
    fatal blank check runs before the no-effect keep) — codex P2. A 'no change' keep
    must never launder a broken render."""
    tmp = _tmp()
    before = vf.blank_white(tmp / "before.png")
    after = tmp / "after.png"
    after.write_bytes(before.read_bytes())  # byte-identical blank pair
    res = cv_gate(after, before=before, diff_policy="zero")
    assert res.outcome == "reject", f"identical blank renders must reject, not keep, got {res.outcome}"
    assert res.signals is not None and res.signals.blank_suspected

    # Same for an error-overlay pair.
    eb = vf.error_overlay(tmp / "ebefore.png")
    ea = tmp / "eafter.png"
    ea.write_bytes(eb.read_bytes())
    res2 = cv_gate(ea, before=eb, diff_policy="zero")
    assert res2.outcome == "reject", "identical error-overlay renders must reject, not keep"


def test_changed_before_after_passes_through():
    """A real before/after pair (different pixels) under a non-zero policy must NOT trip
    the no-effect path — it passes through to the vision model."""
    tmp = _tmp()
    before = vf.styled_render(tmp / "before.png")
    after = vf.styled_render(tmp / "after.png", size=(420, 320))  # different render
    res = cv_gate(after, before=before, diff_policy="local")
    assert res.outcome == "pass_through"


def test_pixel_identical_different_bytes_is_no_effect():
    """A re-encoded baseline with identical PIXELS but different bytes must count as
    no-effect (pixel compare, not just byte hash) — codex P2."""
    import subprocess

    tmp = _tmp()
    before = vf.styled_render(tmp / "before.png")
    after = tmp / "after.png"
    # Re-encode the same pixels at a different PNG compression level → different bytes.
    subprocess.run(["magick", str(before), "-define", "png:compression-level=1", "-strip", str(after)], capture_output=True)
    assert after.read_bytes() != before.read_bytes(), "fixture must differ in bytes"
    res = cv_gate(after, before=before, diff_policy="zero")
    assert res.outcome == "no_effect_bypass", f"pixel-identical re-encode must be no-effect, got {res.outcome} ({res.reason})"
    assert res.verdict == "keep"


def test_zero_diff_drift_is_deterministic_reject():
    """Under --expect zero-diff (diff_policy 'zero') with a --before, ANY non-identical
    render is a deterministic rollback — it must NOT fall through to the vision model
    where a model `keep` could pass real drift (codex P1)."""
    tmp = _tmp()
    before = vf.styled_render(tmp / "before.png")
    after = vf.styled_render(tmp / "after.png", size=(420, 320))  # different pixels
    res = cv_gate(after, before=before, diff_policy="zero")
    assert res.outcome == "reject", f"zero-diff drift must deterministically reject, got {res.outcome}"
    assert "drift" in res.reason


def test_no_effect_bypass_rejects_unreadable_inputs():
    """Two identical ZERO-BYTE files with --before must reject as unreadable, NEVER
    sneak through the byte-identity no-effect keep (codex P2)."""
    tmp = _tmp()
    before = tmp / "before.png"
    after = tmp / "after.png"
    before.write_bytes(b"")
    after.write_bytes(b"")
    res = cv_gate(after, before=before, diff_policy="zero")
    assert res.outcome == "reject", "identical 0-byte inputs must NOT be a no-effect keep"
    assert "unreadable" in res.reason

    # A valid after but an unreadable --before is also a reject, not a keep.
    valid_after = vf.styled_render(tmp / "valid.png")
    bad_before = tmp / "bad.png"
    bad_before.write_bytes(b"not an image at all")
    res2 = cv_gate(valid_after, before=bad_before, diff_policy="zero")
    assert res2.outcome == "reject"


def test_missing_before_is_rejected_not_ignored():
    """A provided-but-MISSING --before must reject (unreadable), never silently degrade
    to a single-image run (codex P2)."""
    tmp = _tmp()
    after = vf.styled_render(tmp / "after.png")
    missing_before = tmp / "ghost.png"  # does not exist
    res = cv_gate(after, before=missing_before, diff_policy="zero")
    assert res.outcome == "reject", f"missing --before must reject, got {res.outcome}"
    assert "--before" in res.reason


def test_missing_magick_binary_raises_cverror():
    """A missing ImageMagick binary degrades to CvError (→ unreadable), not a crash."""
    import importlib

    cg = importlib.import_module("reviewlib.features.visual.cv_gate")
    old = cg.MAGICK
    cg.MAGICK = "definitely-not-a-real-binary-xyz"
    try:
        raised = False
        try:
            cg.compute_signals(vf.styled_render(_tmp() / "x.png"))
        except CvError:
            raised = True
        assert raised, "a missing magick binary must raise CvError, not crash"
    finally:
        cg.MAGICK = old


def test_prepare_image_for_vision_downscales_over_cap():
    """A large image over the provider's long-side cap is downscaled before encoding
    (codex P2); an image already within limits is returned untouched."""
    from reviewlib.features.visual.cv_gate import prepare_image_for_vision

    tmp = _tmp()
    big = vf.styled_render(tmp / "big.png", size=(2000, 1500))
    data, mt = prepare_image_for_vision(big, max_long_side=512, max_bytes=5 * 1024 * 1024)
    # The returned image's long side must be <= the cap.
    import subprocess

    out = tmp / "out.png"
    out.write_bytes(data)
    dims = subprocess.run(["magick", str(out), "-format", "%w %h", "info:"], capture_output=True, text=True).stdout
    w, h = (int(x) for x in dims.split())
    assert max(w, h) <= 512, f"downscaled long side must be <= 512, got {max(w, h)}"

    # An already-small image is returned as-is (same bytes).
    small = vf.styled_render(tmp / "small.png", size=(300, 200))
    data2, _ = prepare_image_for_vision(small, max_long_side=1568, max_bytes=5 * 1024 * 1024)
    assert data2 == small.read_bytes(), "within-limits image must pass through unchanged"


def test_detect_media_type():
    from reviewlib.features.visual.cv_gate import detect_media_type

    tmp = _tmp()
    png = vf.styled_render(tmp / "shot.png")
    assert detect_media_type(png) == "image/png"
    # Re-encode the same fixture as JPEG and confirm the MIME tracks the real format.
    import subprocess

    jpg = tmp / "shot.jpg"
    subprocess.run(["magick", str(png), str(jpg)], capture_output=True)
    if jpg.exists():
        assert detect_media_type(jpg) == "image/jpeg"


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
