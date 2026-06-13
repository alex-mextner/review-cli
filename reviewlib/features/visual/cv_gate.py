"""cvGate — the deterministic, image-only pixel pre-filter (§3.1).

cvGate operates purely on pixels via ImageMagick (`magick`, the proven path
`frames-check` already shells out to). It has three outcomes:

  * AUTO-REJECT — the 100%-unambiguously-broken set (unreadable / blank-solid /
    unstyled-FOUC / error-overlay). Skips the vision model entirely.
  * NO-EFFECT BYPASS — the ONLY CV auto-keep, and only with `--before`: a
    byte-identical render when the intent expected a change is "did not apply"
    (here surfaced as a reject); a byte-identical render when no change was
    expected is the narrow audited keep.
  * PASS-THROUGH — anything that is not unambiguously broken goes to the vision
    model. On a *maybe*, cvGate NEVER rejects (no false-positive auto-reject) and
    NEVER auto-keeps (no symmetric SSIM "looks fine" keep — §3.1).

It emits `CvSignals` (palette entropy, dominant-colour coverage, blank/overlay
suspicion, …) that ride along to the vision model and the policy engine.

NO DOM. Every signal is derived from the image. The unstyled heuristic is the
*image* version of the DOM `computeStylePresence` idea (§0): recognise a bare
no-CSS render from pixels, not by inspecting a stylesheet.
"""
from __future__ import annotations

import hashlib
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# ImageMagick 7 ships `magick`. cvGate hard-requires it (same dependency as
# frames-check); a missing binary is a fatal CV error (cannot judge → unreadable).
MAGICK = "magick"

# --- Heuristic thresholds (tuned against the Stage-1 golden suite, §10). ---------
# Blank/solid: a single colour covering essentially the whole frame.
_BLANK_DOMINANT_COVERAGE = 0.999
# Unstyled (no-CSS FOUC): near-mono palette + low entropy + a big background block,
# i.e. black-ish text on a near-white page with no themed chrome. Conservative — a
# styled light-theme page has accent colours that push entropy / distinct-colour count
# up and dominant coverage down, so it passes through instead.
_UNSTYLED_MAX_ENTROPY = 0.40
_UNSTYLED_MIN_BG_COVERAGE = 0.85
_UNSTYLED_MAX_QUANT_COLORS = 24
_UNSTYLED_MAX_PALETTE_CHROMA = 0.06  # near-grayscale palette (low colourfulness)
# The no-CSS FOUC signature is dark text on a near-WHITE page. A dark themed UI (a black
# dashboard/terminal with light labels) is ALSO near-grayscale + low entropy + high bg
# coverage, so without this near-white-background gate the unstyled predicate would
# false-reject a legitimate dark render (codex P2). Require a bright (near-white)
# dominant background.
_UNSTYLED_MIN_MEAN_LUMA = 0.85
# Error overlay: a dark surface with a saturated error-red band (dev-server overlay).
_OVERLAY_MAX_MEAN = 0.30
_OVERLAY_MIN_RED_FRACTION = 0.15


@dataclass(frozen=True)
class CvSignals:
    width: int
    height: int
    palette_entropy: float
    dominant_coverage: float
    quant_colors: int
    mean_luma: float
    palette_chroma: float
    error_red_fraction: float
    blank_suspected: bool = False
    overlay_suspected: bool = False
    unstyled_suspected: bool = False


@dataclass(frozen=True)
class CvGateResult:
    # outcome: 'reject' | 'no_effect_bypass' | 'pass_through'
    outcome: str
    reason: str
    signals: CvSignals | None
    # 'rollback' when outcome == 'reject', 'keep' when no_effect_bypass, else None.
    verdict: str | None = field(default=None)


class CvError(RuntimeError):
    """Unreadable / non-image input, or a missing/failed `magick`."""


def _run_magick(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a magick command, mapping a MISSING binary to a CvError (so `review
    --visual` on a host without ImageMagick degrades to the unreadable/usage path
    instead of crashing with a traceback — codex P2)."""
    try:
        return subprocess.run(argv, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise CvError(f"ImageMagick '{MAGICK}' not found on PATH — install it to use --visual ({exc})") from exc


def _magick_format(image: Path, fmt: str, *pre: str) -> str:
    proc = _run_magick([MAGICK, str(image), *pre, "-format", fmt, "info:"])
    if proc.returncode != 0:
        raise CvError(f"magick failed on {image}: {proc.stderr.strip()[:200]}")
    return proc.stdout.strip()


def _dominant_coverage_and_count(image: Path) -> tuple[float, int]:
    """Fraction of (downscaled, quantized) pixels in the single most common colour
    bin, and the number of distinct quantized colour bins. The downscale+quantize
    makes the signal robust to anti-aliasing fringe and JPEG noise."""
    proc = _run_magick(
        [MAGICK, str(image), "-resize", "64x64", "-depth", "5", "-format", "%c", "histogram:info:-"]
    )
    if proc.returncode != 0:
        raise CvError(f"magick histogram failed on {image}: {proc.stderr.strip()[:200]}")
    counts: list[int] = []
    for line in (proc.stdout + proc.stderr).splitlines():
        line = line.strip()
        if ":" not in line or "(" not in line:
            continue
        head = line.split(":", 1)[0].strip()
        if head.isdigit():
            counts.append(int(head))
    if not counts:
        return 0.0, 0
    total = sum(counts)
    return (max(counts) / total if total else 0.0), len(counts)


def _palette_chroma(image: Path) -> float:
    """A cheap colourfulness proxy: (mean of per-pixel max channel) minus (mean of
    per-pixel min channel). Near 0 ⇒ grayscale (the unstyled-text signature); higher
    ⇒ a colourful palette."""
    out = _magick_format(image, "%[fx:mean]", "-channel", "RGB", "-separate", "-evaluate-sequence", "max")
    try:
        hi = float(out)
    except ValueError:
        return 1.0
    lo_out = _magick_format(image, "%[fx:mean]", "-channel", "RGB", "-separate", "-evaluate-sequence", "min")
    try:
        lo = float(lo_out)
    except ValueError:
        return 1.0
    return max(0.0, hi - lo)


def _error_red_fraction(image: Path) -> float:
    """Fraction of pixels in the saturated dev-overlay error-red band."""
    out = _magick_format(
        image,
        "%[fx:mean]",
        "-resize",
        "100x100!",
        "-fx",
        "(r>0.5 && g<0.30 && b<0.30 && (r-g)>0.35) ? 1 : 0",
    )
    try:
        return float(out)
    except ValueError:
        return 0.0


def compute_signals(image: Path) -> CvSignals:
    """Derive the full CvSignals bundle from one image. Raises CvError on unreadable
    input so the caller maps it to the 'unreadable' reject (exit 1)."""
    if not image.exists():
        raise CvError(f"no such file: {image}")
    if image.stat().st_size == 0:
        raise CvError(f"zero-byte file: {image}")
    dims = _magick_format(image, "%w %h")
    try:
        w_str, h_str = dims.split()
        width, height = int(w_str), int(h_str)
    except ValueError as exc:
        raise CvError(f"could not read dimensions of {image}: {dims!r}") from exc
    if width == 0 or height == 0:
        raise CvError(f"zero-dimension image: {image}")

    entropy = float(_magick_format(image, "%[entropy]", "-resize", "64x64") or 0.0)
    mean_luma = float(_magick_format(image, "%[fx:mean]") or 0.0)
    dominant, quant_colors = _dominant_coverage_and_count(image)
    chroma = _palette_chroma(image)
    red_frac = _error_red_fraction(image)

    blank = dominant >= _BLANK_DOMINANT_COVERAGE
    overlay = (mean_luma <= _OVERLAY_MAX_MEAN) and (red_frac >= _OVERLAY_MIN_RED_FRACTION)
    unstyled = (
        not blank
        and mean_luma >= _UNSTYLED_MIN_MEAN_LUMA  # near-WHITE bg — not a dark themed UI
        and entropy <= _UNSTYLED_MAX_ENTROPY
        and dominant >= _UNSTYLED_MIN_BG_COVERAGE
        and quant_colors <= _UNSTYLED_MAX_QUANT_COLORS
        and chroma <= _UNSTYLED_MAX_PALETTE_CHROMA
    )
    return CvSignals(
        width=width,
        height=height,
        palette_entropy=entropy,
        dominant_coverage=dominant,
        quant_colors=quant_colors,
        mean_luma=mean_luma,
        palette_chroma=chroma,
        error_red_fraction=red_frac,
        blank_suspected=blank,
        overlay_suspected=overlay,
        unstyled_suspected=unstyled,
    )


# Map an ImageMagick format token to its MIME type so the vision client labels the
# inline image correctly (codex P2 — a JPEG must not ride in a PNG data URI).
_MIME_BY_FORMAT = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "JPG": "image/jpeg",
    "GIF": "image/gif",
    "WEBP": "image/webp",
    "BMP": "image/bmp",
    "TIFF": "image/tiff",
}


def detect_media_type(image: Path) -> str:
    """Return the image's real MIME type (default image/png if undetectable)."""
    try:
        fmt = _magick_format(image, "%m").strip().upper()
    except CvError:
        return "image/png"
    return _MIME_BY_FORMAT.get(fmt, "image/png")


def prepare_image_for_vision(image: Path, *, max_long_side: int, max_bytes: int) -> tuple[bytes, str]:
    """Return (bytes, media_type) for the vision call, downscaled to fit the provider's
    limits (codex P2): a full-page/retina PNG over the cap would otherwise be rejected
    by the API and a strict gate would block as unverified. The raw bytes are used as-is
    when already within limits; otherwise the image is resized to `max_long_side` and,
    if still too large, re-encoded as JPEG at decreasing quality. Falls back to the raw
    bytes on any magick failure (the call may then fail at the provider, but we never
    crash the verification)."""
    raw = image.read_bytes()
    mt = detect_media_type(image)
    try:
        w_h = _magick_format(image, "%w %h")
        w, h = (int(x) for x in w_h.split())
    except (CvError, ValueError):
        return raw, mt
    long_side = max(w, h)
    if len(raw) <= max_bytes and long_side <= max_long_side:
        return raw, mt  # already within limits — send as-is.

    # Resize to the preferred long side (only DOWN, never up).
    geom = f"{max_long_side}x{max_long_side}>"
    tmp_png = Path(tempfile.mkstemp(suffix="-vision.png")[1])
    try:
        proc = _run_magick([MAGICK, str(image), "-resize", geom, str(tmp_png)])
        if proc.returncode == 0 and tmp_png.exists():
            data = tmp_png.read_bytes()
            if len(data) <= max_bytes:
                return data, "image/png"
            # Still too big → JPEG at descending quality.
            for quality in (85, 70, 55, 40):
                tmp_jpg = Path(tempfile.mkstemp(suffix="-vision.jpg")[1])
                jp = _run_magick([MAGICK, str(tmp_png), "-quality", str(quality), str(tmp_jpg)])
                try:
                    if jp.returncode == 0 and tmp_jpg.exists():
                        jdata = tmp_jpg.read_bytes()
                        if len(jdata) <= max_bytes:
                            return jdata, "image/jpeg"
                finally:
                    tmp_jpg.unlink(missing_ok=True)
            # Couldn't get under the cap; send the smallest we produced (resized PNG).
            return data, "image/png"
    finally:
        tmp_png.unlink(missing_ok=True)
    return raw, mt


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def pixels_identical(a: Path, b: Path) -> bool:
    """True if two images have identical pixels (a byte-hash match is the fast path;
    otherwise compare decoded pixels via ImageMagick, so a re-encode that changed PNG
    metadata/compression but not a single pixel still counts as no-effect — codex P2).
    A dimension mismatch or any non-zero pixel delta → False. A magick failure →
    conservatively False (treat as 'differs')."""
    if _file_sha256(a) == _file_sha256(b):
        return True
    try:
        if _magick_format(a, "%wx%h") != _magick_format(b, "%wx%h"):
            return False  # different dimensions can't be pixel-identical
    except CvError:
        return False
    proc = _run_magick([MAGICK, str(a), str(b), "-metric", "AE", "-compare", "-format", "%[distortion]", "info:"])
    out = (proc.stdout + proc.stderr).strip().split()
    # `-metric AE` prints the absolute count of differing pixels; 0 ⇒ identical.
    for token in out:
        try:
            return float(token) == 0.0
        except ValueError:
            continue
    return False


def cv_gate(
    after: Path,
    *,
    before: Path | None = None,
    diff_policy: str = "local",
) -> CvGateResult:
    """Run the deterministic pre-filter on `after` (with optional `before`).

    Decisions are driven ONLY by the trusted, machine-derived `diff_policy` (from
    --expect) and the pixels — never by untrusted --intent prose, which is folded into
    the contract upstream (`derive_contract`) and may only tighten.

    Returns a CvGateResult whose `outcome` is one of 'reject' | 'no_effect_bypass'
    | 'pass_through'. The verdict pipeline (§3) maps 'reject' → rollback (skip the
    vision call), 'no_effect_bypass' → keep, 'pass_through' → call the vision model.
    """
    # --- Validate BOTH inputs are real images FIRST (codex P2). An unreadable /
    # zero-byte / non-image input is fatal and must NOT be eligible for the no-effect
    # keep — so we decode `after` (and, when supplied, `before`) before any byte
    # comparison. compute_signals raises CvError on unreadable input. ------------
    try:
        signals = compute_signals(after)
    except CvError as exc:
        return CvGateResult(
            outcome="reject",
            reason=f"unreadable: {exc}",
            signals=None,
            verdict="rollback",
        )

    # --- Validate --before, if given (a provided-but-MISSING/unreadable baseline is a
    # usage error, not something to silently ignore — codex P2). --------------------
    if before is not None:
        if not before.exists():
            return CvGateResult(
                outcome="reject",
                reason=f"unreadable --before image: no such file: {before}",
                signals=signals,
                verdict="rollback",
            )
        try:
            compute_signals(before)  # validates --before is a real image too.
        except CvError as exc:
            return CvGateResult(
                outcome="reject",
                reason=f"unreadable --before image: {exc}",
                signals=signals,
                verdict="rollback",
            )

    # --- Fatal-broken checks run BEFORE the no-effect keep (codex P2): a byte-identical
    # pair of BLANK/overlay/unstyled renders is still broken and must NOT be granted the
    # zero-diff no_effect_bypass keep. So reject the unambiguously-broken `after` first.
    # Blank/solid is only fatal when a global repaint was NOT expected; the exemption
    # comes from the TRUSTED contract (diff_policy=='global' via --expect), never the
    # untrusted --intent prose (which may only tighten).
    if signals.blank_suspected and diff_policy != "global":
        return CvGateResult(
            outcome="reject",
            reason="blank/solid canvas (failed mount / FOUC)",
            signals=signals,
            verdict="rollback",
        )
    if signals.overlay_suspected:
        return CvGateResult(
            outcome="reject",
            reason="dev-server / runtime error-overlay signature",
            signals=signals,
            verdict="rollback",
        )
    if signals.unstyled_suspected:
        return CvGateResult(
            outcome="reject",
            reason="unstyled render: bare default-serif text, no CSS",
            signals=signals,
            verdict="rollback",
        )

    # --- --before comparison, on an `after` already proven NOT-unambiguously-broken
    # above. Compare by PIXELS (byte-hash fast path, then decoded-pixel fallback) so a
    # re-encode that changed only PNG metadata/compression still counts as no-effect
    # rather than spurious drift (codex P2). ----------------------------------------
    if before is not None:
        identical = pixels_identical(before, after)
        if identical:
            # No-effect: identical pixels. A change-expecting edit that produced no
            # change "did not apply" → reject; a zero-diff expectation with no change is
            # the narrow, audited keep.
            if diff_policy == "zero":
                return CvGateResult(
                    outcome="no_effect_bypass",
                    reason="pixel-identical render, zero-diff expected (no_effect_bypass)",
                    signals=signals,
                    verdict="keep",
                )
            return CvGateResult(
                outcome="reject",
                reason="edit had no effect: render is pixel-identical to --before",
                signals=signals,
                verdict="rollback",
            )
        # Pixels DIFFER. Under a zero-drift contract (--expect zero-diff / wrap →
        # diff_policy 'zero') ANY change is a violation: a DETERMINISTIC rollback that
        # must NOT fall through to the vision model (a model `keep` would otherwise pass
        # real drift past a strict zero-diff gate) — codex P1.
        if diff_policy == "zero":
            return CvGateResult(
                outcome="reject",
                reason="zero-diff expected but render differs from --before (visual drift)",
                signals=signals,
                verdict="rollback",
            )

    return CvGateResult(
        outcome="pass_through",
        reason="not unambiguously broken — escalate to the vision model",
        signals=signals,
        verdict=None,
    )
