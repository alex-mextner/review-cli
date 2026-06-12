#!/usr/bin/env python3
"""Per-project module registry + TOFU trust tests (§6). NO real API calls.

Proves:
  * Discovery: a project's `.review/visual-modules.json` is found from --project/--cwd,
    its entries parsed, each `entry` resolved relative to the project.
  * Load + activation gating: a trusted module loads from its entry file and only
    `activates` when one of its `activates_on` tags is requested (via --check) or
    present in the intent/expectation; a plain run leaves it off.
  * TOFU quarantine: a freshly-dropped (untrusted) module is INERT (absent, not a
    block — it must never break a run); a loud banner is emitted once.
  * trust-module: pins entry_sha256 + activates_on; a subsequent load with the SAME
    bytes activates; a CHANGED entry → back to quarantine (re-trust required).
  * REVIEW_MODULES_TRUST=auto bypasses quarantine.
  * Audit: every load decision is appended to the JSONL audit log.

Everything is isolated to a tmp HOME so the real ~/.config is never touched.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import visual_fixtures as vf  # noqa: E402
from reviewlib.features.visual import registry as reg  # noqa: E402
from reviewlib.features.visual.contract import derive_contract  # noqa: E402
from reviewlib.features.visual.cv_gate import compute_signals  # noqa: E402
from reviewlib.features.visual.module_api import VisualContext  # noqa: E402

# A minimal contributed module source: a class exposing the VisualModule protocol with
# an `activates_on`-driven activate + a CV block when a flag is set.
_MODULE_SRC = '''\
from reviewlib.features.visual.module_api import ModuleVerdict, VisualContext

class _Mod:
    name = "selection-highlight"
    activates_on = ["selection"]
    _vision_field = "selection_present"

    def activates(self, ctx):
        return True  # registry gates on activates_on BEFORE calling this

    def cv_check(self, ctx):
        return ModuleVerdict(module=self.name, decision="pass", confidence=0.5, reason="stub")

    def vision_questions(self, ctx):
        return ["Is a selection outline drawn? Answer in `selection_present`."]

    def judge(self, ctx, vision):
        return ModuleVerdict(module=self.name, decision="abstain", confidence=0.0, reason="stub")

MODULE = _Mod()
'''


def _project_with_module(tmp: Path, *, src: str = _MODULE_SRC, activates_on=None) -> Path:
    review_dir = tmp / ".review"
    (review_dir / "modules").mkdir(parents=True, exist_ok=True)
    entry = review_dir / "modules" / "selection_highlight.py"
    entry.write_text(src, encoding="utf-8")
    manifest = {
        "review_api": "review-visual/v1",
        "modules": [
            {
                "name": "selection-highlight",
                "runtime": "python",
                "entry": ".review/modules/selection_highlight.py",
                "activates_on": activates_on if activates_on is not None else ["selection"],
                "description": "test module",
            }
        ],
    }
    (review_dir / "visual-modules.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp


def _ctx(requested_checks=None, intent=None) -> VisualContext:
    img = Path(tempfile.mkstemp(suffix=".png")[1])
    vf.styled_render(img)
    return VisualContext(
        after_image=img.read_bytes(),
        before_image=None,
        expectation=derive_contract(None, intent, has_before=False),
        cv_signals=compute_signals(img),
        intent=intent,
        requested_checks=requested_checks or [],
    )


def _env(home: Path):
    """An isolated config/cache environment dict for the registry."""
    return reg.RegistryEnv(
        trust_path=home / ".config" / "review-cli" / "modules-trust.json",
        global_registry_path=home / ".config" / "review-cli" / "modules.json",
        audit_path=home / ".cache" / "review-cli" / "visual" / "modules-audit.jsonl",
    )


def test_discovery_finds_and_parses_manifest():
    with tempfile.TemporaryDirectory() as d:
        proj = _project_with_module(Path(d))
        specs = reg.discover_specs(project=proj, env=_env(Path(d) / "home"))
        assert len(specs) == 1
        s = specs[0]
        assert s.name == "selection-highlight"
        assert s.activates_on == ["selection"]
        assert s.entry_path.is_file(), "entry resolved relative to project"
        assert s.runtime == "python"


def test_untrusted_module_is_quarantined_inert():
    """A freshly-dropped module is INERT (returns no usable module) and prints a banner,
    but is NOT a block — load_modules just yields nothing for it."""
    with tempfile.TemporaryDirectory() as d:
        proj = _project_with_module(Path(d))
        home = Path(d) / "home"
        loaded, quarantined = reg.load_modules(project=proj, env=_env(home))
        assert loaded == [], "an untrusted module must be inert (absent), not active"
        assert any(q.name == "selection-highlight" for q in quarantined)


def test_trust_module_pins_sha_and_activates():
    with tempfile.TemporaryDirectory() as d:
        proj = _project_with_module(Path(d))
        home = Path(d) / "home"
        env = _env(home)
        rc = reg.trust_module("selection-highlight", project=proj, env=env)
        assert rc == 0
        # Trust store pins entry_sha256 + activates_on, mode 0600.
        store = json.loads(env.trust_path.read_text())
        assert "selection-highlight" in store
        assert len(store["selection-highlight"]["entry_sha256"]) == 64
        assert store["selection-highlight"]["activates_on"] == ["selection"]
        mode = oct(env.trust_path.stat().st_mode & 0o777)
        assert mode == "0o600", f"trust store must be 0600, got {mode}"
        # Now it loads.
        loaded, quarantined = reg.load_modules(project=proj, env=env)
        assert [m.name for m in loaded] == ["selection-highlight"]
        assert quarantined == []


def test_changed_entry_returns_to_quarantine():
    with tempfile.TemporaryDirectory() as d:
        proj = _project_with_module(Path(d))
        home = Path(d) / "home"
        env = _env(home)
        reg.trust_module("selection-highlight", project=proj, env=env)
        # Tamper with the entry AFTER trust → hash mismatch → quarantine.
        entry = proj / ".review" / "modules" / "selection_highlight.py"
        entry.write_text(_MODULE_SRC + "\n# tampered\n", encoding="utf-8")
        loaded, quarantined = reg.load_modules(project=proj, env=env)
        assert loaded == [], "a changed entry must NOT load on the old trust pin"
        assert any("changed" in q.reason for q in quarantined)


def test_activates_on_change_requires_retrust():
    """A manifest-only edit that widens `activates_on` (without touching the entry file,
    so the hash still matches) must re-quarantine the module — a module trusted for
    'selection' can't silently start firing on other tags (codex P2)."""
    with tempfile.TemporaryDirectory() as d:
        proj = _project_with_module(Path(d), activates_on=["selection"])
        home = Path(d) / "home"
        env = _env(home)
        reg.trust_module("selection-highlight", project=proj, env=env)
        # Widen activates_on in the manifest ONLY (entry file untouched → same hash).
        manifest = proj / ".review" / "visual-modules.json"
        data = json.loads(manifest.read_text())
        data["modules"][0]["activates_on"] = ["selection", "everything"]
        manifest.write_text(json.dumps(data), encoding="utf-8")
        loaded, quarantined = reg.load_modules(project=proj, env=env)
        assert loaded == [], "a widened activates_on must re-quarantine until re-trust"
        assert any("activates_on" in q.reason for q in quarantined)


def test_trust_auto_env_bypasses_quarantine(monkeypatch=None):
    with tempfile.TemporaryDirectory() as d:
        proj = _project_with_module(Path(d))
        home = Path(d) / "home"
        env = _env(home)
        old = os.environ.get("REVIEW_MODULES_TRUST")
        os.environ["REVIEW_MODULES_TRUST"] = "auto"
        try:
            loaded, quarantined = reg.load_modules(project=proj, env=env)
        finally:
            if old is None:
                os.environ.pop("REVIEW_MODULES_TRUST", None)
            else:
                os.environ["REVIEW_MODULES_TRUST"] = old
        assert [m.name for m in loaded] == ["selection-highlight"], "auto-trust must load untrusted modules"


def test_activation_gating_on_tag():
    """A loaded contributed module only activates when one of its activates_on tags is
    requested (--check) or present in the intent; a plain run leaves it OFF."""
    with tempfile.TemporaryDirectory() as d:
        proj = _project_with_module(Path(d))
        home = Path(d) / "home"
        env = _env(home)
        reg.trust_module("selection-highlight", project=proj, env=env)
        loaded, _ = reg.load_modules(project=proj, env=env)
        mod = loaded[0]
        # No tag requested → inactive.
        assert mod.activates(_ctx(requested_checks=[])) is False
        # --check selection → active.
        assert mod.activates(_ctx(requested_checks=["selection"])) is True
        # --check by module NAME also force-activates.
        assert mod.activates(_ctx(requested_checks=["selection-highlight"])) is True
        # Intent mentioning the tag → active.
        assert mod.activates(_ctx(intent="verify the selection outline renders")) is True


def test_audit_log_appended():
    with tempfile.TemporaryDirectory() as d:
        proj = _project_with_module(Path(d))
        home = Path(d) / "home"
        env = _env(home)
        # An untrusted load writes a quarantine audit row.
        reg.load_modules(project=proj, env=env)
        assert env.audit_path.exists()
        rows = [json.loads(line) for line in env.audit_path.read_text().splitlines() if line.strip()]
        assert any(r["module"] == "selection-highlight" and r["trust_state"] == "quarantined" for r in rows)
        # Trust + a successful load appends a trusted row (append-only — old rows kept).
        reg.trust_module("selection-highlight", project=proj, env=env)
        reg.load_modules(project=proj, env=env)
        rows2 = [json.loads(line) for line in env.audit_path.read_text().splitlines() if line.strip()]
        assert len(rows2) > len(rows), "audit is append-only"
        assert any(r["trust_state"] == "trusted" for r in rows2)


def test_pipeline_folds_contributed_selection_module():
    """End-to-end: a project that contributes the real selection-highlight module (the
    reference contrib impl), once trusted, has its veto folded into run_pipeline. On a
    styled render with NO selection outline + --check selection, the module's cv_check
    BLOCKS → the pipeline rolls back with NO vision call (the registry → pipeline path)."""
    from reviewlib.features.visual import pipeline as pl
    from reviewlib.features.visual.registry import RegistryEnv, trust_module
    from reviewlib.features.visual.vision_client import VisionVerdict

    with tempfile.TemporaryDirectory() as d:
        home = Path(d) / "home"
        env = _env(home)
        # The project manifest points at the SHIPPED contrib reference module.
        contrib = REPO_ROOT / "reviewlib" / "features" / "visual" / "contrib" / "selection_highlight.py"
        proj = Path(d) / "proj"
        review_dir = proj / ".review"
        review_dir.mkdir(parents=True)
        manifest = {
            "review_api": "review-visual/v1",
            "modules": [
                {
                    "name": "selection-highlight",
                    "runtime": "python",
                    "entry": str(contrib),  # absolute entry → resolved as-is
                    "activates_on": ["selection"],
                    "description": "reference selection-highlight",
                }
            ],
        }
        (review_dir / "visual-modules.json").write_text(json.dumps(manifest), encoding="utf-8")

        # Point the registry's default env at our isolated HOME so load_modules (called
        # inside run_pipeline with no env) uses it.
        import reviewlib.features.visual.registry as regmod

        old_trust = regmod._default_trust_path
        old_glob = regmod._default_global_registry_path
        old_audit = regmod._default_audit_path
        regmod._default_trust_path = lambda: env.trust_path
        regmod._default_global_registry_path = lambda: env.global_registry_path
        regmod._default_audit_path = lambda: env.audit_path
        # Also reset the once-per-process banner set so the quarantine path is exercised.
        regmod._BANNERED.clear()
        try:
            trust_module("selection-highlight", project=proj, env=env)
            img = Path(tempfile.mkstemp(suffix="-nosel.png")[1])
            vf.styled_render(img)  # styled, but NO selection outline

            # A mock vision keep would otherwise pass — the module veto must win, and it
            # must short-circuit BEFORE any vision call.
            call_log: list = []

            def fake_call(model, **kwargs):
                call_log.append(model)
                return VisionVerdict(available=True, verdict="keep", confidence=1.0)

            old_call = pl.call_ai_vision
            old_select = pl.select_vision_backend
            pl.call_ai_vision = fake_call
            pl.select_vision_backend = lambda models: "gemini"
            try:
                v = pl.run_pipeline(img, models=["gemini"], requested_checks=["selection"], project=proj)
            finally:
                pl.call_ai_vision = old_call
                pl.select_vision_backend = old_select
        finally:
            regmod._default_trust_path = old_trust
            regmod._default_global_registry_path = old_glob
            regmod._default_audit_path = old_audit

        assert v.final == "rollback", f"missing selection must veto via the contributed module, got {v.final}"
        assert "selection-highlight" in v.reason
        assert call_log == [], "module veto must short-circuit before the vision call"


def test_global_registered_manifest_discovered():
    """A globally-registered manifest path (review register-module) is discovered even
    outside the project tree."""
    with tempfile.TemporaryDirectory() as d:
        proj = _project_with_module(Path(d) / "elsewhere")
        home = Path(d) / "home"
        env = _env(home)
        env.global_registry_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path = proj / ".review" / "visual-modules.json"
        env.global_registry_path.write_text(json.dumps({"manifests": [str(manifest_path)]}), encoding="utf-8")
        # Discover with a DIFFERENT (empty) project dir — the global registry still finds it.
        empty = Path(d) / "empty-cwd"
        empty.mkdir()
        specs = reg.discover_specs(project=empty, env=env)
        assert any(s.name == "selection-highlight" for s in specs), "global registry manifest not discovered"


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
