#!/usr/bin/env python3
"""Stage-2a known-good-cache pre-classifier tests (§3.1a). NO real API calls.

The HONEST v1 of the §3.1a cost-saver: NOT a trained ML model — a per-context cache of the
renders that previously earned a `keep`. It can ONLY short-circuit a PIXEL-IDENTICAL match
to `keep` (saving the paid vision call); it never auto-rejects, never fuzzy-matches (which
could mask a small semantic regression), and never resolves an ambiguous case (that defers
to vision). An 8×8 aHash is used only as a cheap bucket index; the decision is exact pixel
identity.

Proves (vision MOCKED throughout — no API burned):
  * Cache MISS escalates to vision (vision IS called); a fresh `keep` populates the cache.
  * Cache HIT on a pixel-identical render short-circuits to `keep` with NO vision call.
  * A metadata-only re-encode (same pixels) still HITS; a LOSSY re-encode and a
    same-layout-but-different-label render MISS (pixel identity, never fuzzy).
  * A perceptually-DIFFERENT render MISSES → escalates to vision.
  * --no-local-model (local_model=False) disables the tier: cache is neither read nor
    written, flow is cvGate → vision unchanged.
  * The pre-classifier NEVER auto-rejects: a cache miss can only escalate, never block.
  * Context isolation: the key folds in project/intent/expect + the --check set + the
    --before baseline + the active-modules signature (codex P1/P2), so a keep in one
    context never short-circuits another.

Everything is isolated to a tmp cache dir so the real ~/.cache is never touched.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import visual_fixtures as vf  # noqa: E402
from reviewlib.features.visual import pipeline as pl  # noqa: E402
from reviewlib.features.visual import preclassifier as pc  # noqa: E402
from reviewlib.features.visual.vision_client import VisionVerdict  # noqa: E402


def _cache(root: Path) -> pc.KnownGoodCache:
    return pc.KnownGoodCache(root=root)


def _pipeline_ctx_key(*, project=None, intent=None, expect=None, requested_checks=None, before=None, selected_backend="gemini") -> str:
    """The exact context key run_pipeline computes for a STANDALONE run: it folds in a
    signature of the ACTIVE modules and the SELECTED backend. For a plain run with no
    --check, only the three built-ins activate, so the signature is theirs. The pipeline
    tests below patch select_vision_backend → "gemini", so that is the default here."""
    from reviewlib.features.visual.modules.builtins import builtin_modules

    active = list(builtin_modules())  # built-ins self-activate on every --visual run
    return pc.KnownGoodCache.context_key(
        project=project, intent=intent, expect=expect,
        requested_checks=requested_checks, before=before,
        modules_signature=pc.modules_signature(active),
        selected_backend=selected_backend,
    )


def _patch_vision(verdict: VisionVerdict, call_log: list):
    def fake_call(model, **kwargs):
        call_log.append(model)
        return verdict

    old_call = pl.call_ai_vision
    old_select = pl.select_vision_backend
    pl.call_ai_vision = fake_call
    pl.select_vision_backend = lambda models: "gemini"
    return old_call, old_select


def _restore(old_call, old_select):
    pl.call_ai_vision = old_call
    pl.select_vision_backend = old_select


# --- Unit tests on the cache itself. ------------------------------------------------
def test_cache_miss_then_hit_on_identical():
    with tempfile.TemporaryDirectory() as d:
        cache = _cache(Path(d))
        img = vf.styled_render(Path(d) / "shot.png")
        assert cache.lookup(img, context="ctx") is False, "empty cache must miss"
        cache.remember(img, context="ctx")
        assert cache.lookup(img, context="ctx") is True, "an identical render must hit after remember"


def test_cache_hit_on_metadata_only_reencode():
    """A metadata-only re-encode that changes BYTES but not a single PIXEL must still hit
    — the match is pixel-identity (byte-hash fast path, then decoded-pixel compare), not a
    raw byte-hash."""
    import subprocess

    with tempfile.TemporaryDirectory() as d:
        png = vf.styled_render(Path(d) / "shot.png")
        stripped = Path(d) / "shot-stripped.png"
        subprocess.run(["magick", str(png), "-strip", str(stripped)], check=True)
        cache = _cache(Path(d))
        cache.remember(png, context="ctx")
        assert cache.lookup(stripped, context="ctx") is True, "a metadata-only re-encode (same pixels) must hit"


def test_cache_miss_on_lossy_reencode():
    """A LOSSY re-encode (jpeg) changes pixels → it is NOT pixel-identical → MISS. The
    cache must never fuzzy-match (a fuzzy match could mask a small semantic regression —
    that is the AI-vision authority's job, codex P1)."""
    import subprocess

    with tempfile.TemporaryDirectory() as d:
        png = vf.styled_render(Path(d) / "shot.png")
        jpg = Path(d) / "shot.jpg"
        subprocess.run(["magick", str(png), "-quality", "80", str(jpg)], check=True)
        cache = _cache(Path(d))
        cache.remember(png, context="ctx")
        assert cache.lookup(jpg, context="ctx") is False, "a lossy re-encode is not pixel-identical → must miss"


def test_cache_miss_on_small_semantic_change():
    """A render with the SAME layout but a different label/number (a small semantic change)
    must MISS — it is not pixel-identical, so it escalates to vision rather than reusing a
    prior keep. This is the codex P1 case the exact-match design protects against."""
    from PIL import Image, ImageDraw, ImageFont

    with tempfile.TemporaryDirectory() as d:
        png = vf.styled_render(Path(d) / "shot.png")
        changed = Path(d) / "shot-changed.png"
        img = Image.open(png).convert("RGB")
        ImageDraw.Draw(img).text((120, 90), "$42.00", fill="black", font=ImageFont.load_default())
        img.save(changed)
        cache = _cache(Path(d))
        cache.remember(png, context="ctx")
        assert cache.lookup(changed, context="ctx") is False, (
            "a same-layout render with a changed label must miss (not be reused as known-good)"
        )


def test_cache_miss_on_different_render():
    with tempfile.TemporaryDirectory() as d:
        cache = _cache(Path(d))
        styled = vf.styled_render(Path(d) / "styled.png")
        dark = vf.dark_ui_render(Path(d) / "dark.png")
        cache.remember(styled, context="ctx")
        assert cache.lookup(dark, context="ctx") is False, "a perceptually-different render must miss"


def test_cache_context_isolation():
    with tempfile.TemporaryDirectory() as d:
        cache = _cache(Path(d))
        img = vf.styled_render(Path(d) / "shot.png")
        cache.remember(img, context="ctx-A")
        assert cache.lookup(img, context="ctx-B") is False, "a keep in one context must not hit another"
        assert cache.lookup(img, context="ctx-A") is True


def test_cached_screenshots_are_private():
    """Cached renders are full screenshots that may hold private UI / secrets, so the
    cache dir is owner-only (0700) and the stored PNG + index are 0600 (codex P2)."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "kg-root"
        cache = pc.KnownGoodCache(root=root)
        img = vf.styled_render(Path(d) / "shot.png")
        cache.remember(img, context="ctx")
        ctx_dir = cache._dir_for("ctx")
        assert oct(ctx_dir.stat().st_mode & 0o777) == "0o700", "cache dir must be owner-only"
        assert oct(root.stat().st_mode & 0o777) == "0o700", "cache root must be owner-only"
        files = list(ctx_dir.iterdir())
        assert files, "a reference must have been stored"
        for f in files:
            assert oct(f.stat().st_mode & 0o777) == "0o600", f"cached file {f.name} must be 0600"


def test_context_key_folds_checks_and_baseline():
    """The context key must capture EVERY verdict input a cached keep is conditioned on
    (codex P1): the active --check set and the --before baseline. A keep earned under a
    plain run must NOT be reused for a run that adds checks or a different baseline —
    those add a module/baseline veto path the cache can't see."""
    with tempfile.TemporaryDirectory() as d:
        before = vf.dark_ui_render(Path(d) / "before.png")
        other_before = vf.styled_render(Path(d) / "before2.png")
        plain = pc.KnownGoodCache.context_key(project="/p", intent=None, expect=None)
        with_check = pc.KnownGoodCache.context_key(
            project="/p", intent=None, expect=None, requested_checks=["selection"]
        )
        with_before = pc.KnownGoodCache.context_key(
            project="/p", intent=None, expect=None, before=before
        )
        with_other_before = pc.KnownGoodCache.context_key(
            project="/p", intent=None, expect=None, before=other_before
        )
        assert plain != with_check, "a --check run must be a different cache namespace"
        assert plain != with_before, "a baselined run must be a different cache namespace"
        assert with_before != with_other_before, "a different --before must be a different namespace"
        # The baseline marker must be an EXACT content fingerprint, not the coarse aHash:
        # two distinct baselines that aHash-COLLIDE (e.g. solid black vs solid white, both
        # threshold to all-bits-set) MUST still be different namespaces (codex P1).
        black = vf.solid_fill(Path(d) / "black.png", color="rgb(0,0,0)")
        white = vf.solid_fill(Path(d) / "white.png", color="rgb(255,255,255)")
        assert pc.perceptual_ahash(black) == pc.perceptual_ahash(white), "precondition: these aHash-collide"
        k_black = pc.KnownGoodCache.context_key(project="/p", intent=None, expect=None, before=black)
        k_white = pc.KnownGoodCache.context_key(project="/p", intent=None, expect=None, before=white)
        assert k_black != k_white, "aHash-colliding baselines must NOT share a cache namespace"
        # Check-set ordering must not matter (stable key).
        a = pc.KnownGoodCache.context_key(project="/p", intent=None, expect=None, requested_checks=["a", "b"])
        b = pc.KnownGoodCache.context_key(project="/p", intent=None, expect=None, requested_checks=["b", "a"])
        assert a == b, "the --check set is order-independent"


def test_context_key_folds_modules_signature():
    """A changed active-module set/signature must be a different cache namespace (codex
    P2): adding/updating a module that activates for the same project/intent/checks must
    invalidate prior keeps so a new vision-only module veto is never bypassed."""
    base = pc.KnownGoodCache.context_key(project="/p", intent=None, expect=None, modules_signature="m1@aaaa")
    changed = pc.KnownGoodCache.context_key(project="/p", intent=None, expect=None, modules_signature="m1@bbbb")
    added = pc.KnownGoodCache.context_key(project="/p", intent=None, expect=None, modules_signature="m1@aaaa,m2@cccc")
    assert base != changed, "a changed module entry hash must be a different namespace"
    assert base != added, "an added active module must be a different namespace"


def test_context_key_folds_selected_backend():
    """The SELECTED vision backend must be part of the cache namespace (codex P2): a keep
    cached under one backend must not short-circuit a run that resolves to a different
    (e.g. stricter) backend — even if the raw --model request is unchanged but availability
    shifted."""
    a = pc.KnownGoodCache.context_key(project="/p", intent=None, expect=None, selected_backend="gemini")
    b = pc.KnownGoodCache.context_key(project="/p", intent=None, expect=None, selected_backend="codex")
    none = pc.KnownGoodCache.context_key(project="/p", intent=None, expect=None)
    assert a != b, "a different selected backend must be a different namespace"
    assert a != none, "an explicit selected backend differs from none"


def test_pipeline_backend_change_does_not_reuse_keep():
    """End-to-end of codex P2: a keep cached when one backend was selected must NOT
    short-circuit a later run that now resolves to a DIFFERENT backend (e.g. availability
    changed) — the new backend must still be consulted."""
    with tempfile.TemporaryDirectory() as d:
        cache = _cache(Path(d))
        img = vf.styled_render(Path(d) / "shot.png")
        # Run 1: backend resolves to gemini → keep → cached under backend:gemini.
        log1: list = []
        old_call = pl.call_ai_vision
        old_select = pl.select_vision_backend
        pl.call_ai_vision = lambda model, **k: (log1.append(model), VisionVerdict(available=True, verdict="keep", confidence=0.95, backend="gemini"))[1]
        pl.select_vision_backend = lambda models: "gemini"
        try:
            pl.run_pipeline(img, models=["gemini", "codex"], known_good_cache=cache)
        finally:
            pl.call_ai_vision, pl.select_vision_backend = old_call, old_select
        assert log1 == ["gemini"]
        # Run 2: SAME request, but availability shifted → now resolves to codex. Must MISS
        # (different backend namespace) → vision consulted again.
        log2: list = []
        pl.call_ai_vision = lambda model, **k: (log2.append(model), VisionVerdict(available=True, verdict="keep", confidence=0.95, backend="codex"))[1]
        pl.select_vision_backend = lambda models: "codex"
        try:
            pl.run_pipeline(img, models=["gemini", "codex"], known_good_cache=cache)
        finally:
            pl.call_ai_vision, pl.select_vision_backend = old_call, old_select
        assert log2 == ["codex"], "a run that now selects a different backend must not reuse a prior keep"


def test_builtin_signature_changes_with_builtin_source():
    """The module signature must fold in the SOURCE HASH of built-in modules, not just
    their name (codex P2): a review upgrade that changes a built-in's question/judge logic
    (same name) must change the signature so the old cache namespace stops hitting."""
    from reviewlib.features.visual.modules.builtins import builtin_modules

    sig = pc.modules_signature(list(builtin_modules()))
    # Every built-in must carry a source-hash suffix (name@hash), never a bare name.
    for part in sig.split(","):
        assert "@" in part, f"built-in signature part {part!r} must include a source hash"
        assert not part.endswith("@unknown"), f"built-in source must be resolvable: {part!r}"


def test_modules_signature_changes_with_entry_content():
    """modules_signature() must change when a contributed module's entry file changes —
    so a tampered/updated module never reuses a keep cached under the old code."""
    with tempfile.TemporaryDirectory() as d:
        entry = Path(d) / "mod.py"
        entry.write_text("MODULE = object()\n", encoding="utf-8")

        class _Contrib:
            name = "selection-highlight"
            entry_path = entry

        sig1 = pc.modules_signature([_Contrib()])
        entry.write_text("MODULE = object()\n# changed\n", encoding="utf-8")
        sig2 = pc.modules_signature([_Contrib()])
        assert sig1 != sig2, "a changed contributed entry file must change the module signature"
        assert "selection-highlight@" in sig1


def test_modules_signature_hashes_impl_not_only_entry_stub():
    """A contributed entry that is a thin WRAPPER importing the real `Module` from another
    file must also hash that impl file (codex P2): changing the impl (entry unchanged) must
    change the signature, so a stale cache can't bypass an updated module judge."""
    import importlib.util

    with tempfile.TemporaryDirectory() as d:
        impl_file = Path(d) / "real_impl.py"
        impl_file.write_text("class RealModule:\n    name = 'm'\n    VERSION = 1\n", encoding="utf-8")
        # Load the impl file as a module and instantiate its class (mimics the registry's
        # thin-wrapper case: entry_path points at a stub, _impl lives in real_impl.py).
        spec = importlib.util.spec_from_file_location("real_impl_mod", impl_file)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["real_impl_mod"] = mod
        spec.loader.exec_module(mod)
        entry = Path(d) / "entry_stub.py"
        entry.write_text("from real_impl import RealModule as Module\n", encoding="utf-8")

        class _Contrib:
            name = "m"
            entry_path = entry

            def __init__(self, impl):
                self._impl = impl

        contrib = _Contrib(mod.RealModule())
        sig1 = pc.modules_signature([contrib])
        # Change the IMPL file only (entry stub untouched) — must change the signature.
        impl_file.write_text("class RealModule:\n    name = 'm'\n    VERSION = 2\n", encoding="utf-8")
        sig2 = pc.modules_signature([contrib])
        assert sig1 != sig2, "a changed impl file (entry unchanged) must change the signature"


def test_context_key_folds_verdict_code_version():
    """The cache key must include the verdict-code version salt (codex P2): a package
    upgrade that changes policy/contract/prompt/schema logic invalidates prior keeps even
    when inputs/modules/backend are unchanged."""
    base = pc.KnownGoodCache.context_key(project="/p", intent=None, expect=None)
    # The key materially depends on the verdict-code version: a different salt → different
    # key. Patch the module-level constant to simulate an upgrade.
    old = pc._VERDICT_CODE_VERSION
    try:
        pc._VERDICT_CODE_VERSION = "deadbeef0000"
        upgraded = pc.KnownGoodCache.context_key(project="/p", intent=None, expect=None)
    finally:
        pc._VERDICT_CODE_VERSION = old
    assert base != upgraded, "a verdict-code upgrade must change the cache namespace"


def test_pipeline_check_run_does_not_reuse_plain_keep():
    """End-to-end of the codex P1: a keep cached from a plain run must NOT short-circuit a
    later --check run on the SAME image — the --check run must still escalate to vision so
    the module judge phase runs."""
    with tempfile.TemporaryDirectory() as d:
        cache = _cache(Path(d))
        img = vf.styled_render(Path(d) / "shot.png")
        # Plain run → vision keep → cached under the plain context.
        log1: list = []
        old = _patch_vision(VisionVerdict(available=True, verdict="keep", confidence=0.95, backend="gemini"), log1)
        try:
            pl.run_pipeline(img, models=["gemini"], known_good_cache=cache)
        finally:
            _restore(*old)
        assert log1 == ["gemini"]
        # Same image but WITH an explicit non-CV-vetoing --check -> must MISS (different
        # namespace) -> vision. Do not use a locally decidable check such as `selection`
        # here: cvGate can satisfy it before the vision backend, which would test a
        # different optimization path than the cache namespace boundary.
        log2: list = []
        old = _patch_vision(VisionVerdict(available=True, verdict="keep", confidence=0.95, backend="gemini"), log2)
        try:
            pl.run_pipeline(img, models=["gemini"], requested_checks=["error-text"], known_good_cache=cache)
        finally:
            _restore(*old)
        assert log2 == ["gemini"], "a --check run must not reuse a plain-run keep — it must escalate to vision"


# --- Pipeline integration: the §3.1a hook. ------------------------------------------
def test_pipeline_miss_escalates_then_keep_populates_cache():
    with tempfile.TemporaryDirectory() as d:
        cache = _cache(Path(d))
        img = vf.styled_render(Path(d) / "shot.png")
        log: list = []
        old = _patch_vision(VisionVerdict(available=True, verdict="keep", confidence=0.95, backend="gemini"), log)
        try:
            v = pl.run_pipeline(img, models=["gemini"], known_good_cache=cache)
        finally:
            _restore(*old)
        assert v.final == "keep"
        assert log == ["gemini"], "a cache miss must escalate to vision"
        assert cache.lookup(img, context=_pipeline_ctx_key()) is True, (
            "a fresh vision keep must populate the known-good cache"
        )


def test_pipeline_hit_short_circuits_without_vision():
    with tempfile.TemporaryDirectory() as d:
        cache = _cache(Path(d))
        img = vf.styled_render(Path(d) / "shot.png")
        # First run: miss → vision keep → cache populated.
        log1: list = []
        old = _patch_vision(VisionVerdict(available=True, verdict="keep", confidence=0.95, backend="gemini"), log1)
        try:
            pl.run_pipeline(img, models=["gemini"], known_good_cache=cache)
        finally:
            _restore(*old)
        assert log1 == ["gemini"]
        # Second identical run: HIT → keep with NO vision call.
        log2: list = []
        old = _patch_vision(VisionVerdict(available=True, verdict="keep", confidence=1.0), log2)
        try:
            v = pl.run_pipeline(img, models=["gemini"], known_good_cache=cache)
        finally:
            _restore(*old)
        assert v.final == "keep", "a known-good cache hit must short-circuit to keep"
        assert log2 == [], "a cache hit must NOT call the vision model (the cost-saver)"
        assert v.source == "local_model", "a cache short-circuit must be attributed to the local pre-classifier"


def test_no_local_model_disables_cache():
    with tempfile.TemporaryDirectory() as d:
        cache = _cache(Path(d))
        img = vf.styled_render(Path(d) / "shot.png")
        # Pre-populate the cache so a HIT would short-circuit IF the tier ran.
        cache.remember(img, context=_pipeline_ctx_key())
        log: list = []
        old = _patch_vision(VisionVerdict(available=True, verdict="keep", confidence=0.95, backend="gemini"), log)
        try:
            v = pl.run_pipeline(img, models=["gemini"], known_good_cache=cache, local_model=False)
        finally:
            _restore(*old)
        assert log == ["gemini"], "--no-local-model must skip the cache and call vision as usual"
        assert v.final == "keep"
        assert v.source == "vision"


def test_preclassifier_never_auto_rejects():
    """A cache MISS on a render that vision will reject must NOT be turned into a keep by
    the cache, and the cache must never itself emit a reject — it can only short-circuit
    a confident keep-match or defer up to vision."""
    with tempfile.TemporaryDirectory() as d:
        cache = _cache(Path(d))
        img = vf.styled_render(Path(d) / "shot.png")
        log: list = []
        old = _patch_vision(VisionVerdict(available=True, verdict="rollback", confidence=0.9, backend="gemini"), log)
        try:
            v = pl.run_pipeline(img, models=["gemini"], known_good_cache=cache)
        finally:
            _restore(*old)
        assert log == ["gemini"], "a miss must escalate to vision, never decide on its own"
        assert v.final == "rollback", "vision's reject stands"
        # A rejected render must NOT be added to the known-good cache.
        assert cache.lookup(img, context=_pipeline_ctx_key()) is False, (
            "a non-keep render must never be cached as known-good"
        )


def test_low_confidence_keep_not_cached():
    """A keep that policy DOWNGRADES (low-confidence → human_review) must not be cached as
    known-good — only a final `keep` populates the cache."""
    with tempfile.TemporaryDirectory() as d:
        cache = _cache(Path(d))
        img = vf.styled_render(Path(d) / "shot.png")
        log: list = []
        old = _patch_vision(VisionVerdict(available=True, verdict="keep", confidence=0.3, backend="gemini"), log)
        try:
            v = pl.run_pipeline(img, models=["gemini"], known_good_cache=cache)
        finally:
            _restore(*old)
        assert v.final == "human_review", "a low-confidence keep is escalated by policy"
        assert cache.lookup(img, context=_pipeline_ctx_key()) is False, (
            "only a FINAL keep may be cached, not a policy-downgraded one"
        )


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
