"""Per-project visual-module registry (§6) — trust-by-default.

A project ships a manifest at a well-known path inside its tree:

    <project>/.review/visual-modules.json

declaring contributed `VisualModule`s (name + entry file + `activates_on` tags). At
run time `review --visual … --project <dir>` (default `--cwd`) discovers the manifest,
resolves each `entry` relative to the project, loads it, and folds its `cv_check` /
`vision_questions` / `judge` into the same pipeline the built-in modules use.

SECURITY NOTE — a project visual-module is EXECUTABLE CODE that review will run. Only
run `review --visual` on repos you trust (your own repos, or code you have read). The
common case — reviewing your OWN repositories — is trusted by construction, so by
DEFAULT a discovered module loads and runs with **zero ceremony**: no `trust-module`
step, no quarantine.

Trust model (§6.3, trust-by-default):
  * DEFAULT: every discovered contributed module is trusted and loaded. There is no
    quarantine in the common path. (The audit log below still records every load.)
  * `REVIEW_UNTRUSTED_MODULES=1` — an OPT-IN, off-by-default guard for the rare
    untrusted-repo case (reviewing an external PR / a cloned stranger's repo). Under
    the guard the legacy TOFU quarantine + sha-pin re-engages:
      - A newly-discovered (untrusted) module is QUARANTINED: a loud one-line banner is
        printed and the module is treated as ABSENT — never as a block.
      - `review trust-module <name>` pins `{entry_sha256, activates_on}` into
        `~/.config/review-cli/modules-trust.json` (mode 0600). At load time the entry
        is re-hashed; a mismatch → back to quarantine (`module changed, re-trust
        required`).
      - `REVIEW_MODULES_TRUST=auto` is the conscious escape hatch for batch/agent runs.
  * Every load decision is appended to `~/.cache/review-cli/visual/modules-audit.jsonl`
    (cheap, useful — kept in BOTH the default and guarded paths).

Built-in modules (`modules/builtins.py`) are trusted implicitly (they ship in review's
own source). ONLY contributed modules are subject to the optional guard.

This module imports NOTHING heavy at import time; the entry file is loaded with
`importlib` only for a module about to run.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .module_api import ModuleVerdict, VisualContext, VisualModule

MANIFEST_RELPATH = Path(".review") / "visual-modules.json"
REVIEW_API = "review-visual/v1"

# Opt-in, off-by-default guard. When set (truthy), the legacy TOFU quarantine + sha-pin
# re-engages for the rare untrusted-repo case (external PR / cloned stranger's repo).
# Unset (the default) = trust-by-default: every discovered module loads with no ceremony.
UNTRUSTED_GUARD_ENV = "REVIEW_UNTRUSTED_MODULES"


def _untrusted_guard_active() -> bool:
    """True when the opt-in untrusted-repo guard is enabled (quarantine re-engaged)."""
    return os.environ.get(UNTRUSTED_GUARD_ENV, "").strip().lower() in ("1", "true", "yes", "on")


# --- Configurable environment (so tests isolate from the real ~/.config). ----------
def _default_trust_path() -> Path:
    return Path.home() / ".config" / "review-cli" / "modules-trust.json"


def _default_global_registry_path() -> Path:
    return Path.home() / ".config" / "review-cli" / "modules.json"


def _default_audit_path() -> Path:
    return Path.home() / ".cache" / "review-cli" / "visual" / "modules-audit.jsonl"


@dataclass(frozen=True)
class RegistryEnv:
    # Late-bound through the module attribute so tests can monkeypatch the `_default_*`
    # functions and have `RegistryEnv()` (constructed inside run_pipeline → load_modules
    # with no explicit env) pick up the isolated test paths.
    trust_path: Path = field(default_factory=lambda: _default_trust_path())
    global_registry_path: Path = field(default_factory=lambda: _default_global_registry_path())
    audit_path: Path = field(default_factory=lambda: _default_audit_path())


# --- A discovered manifest entry (before trust is evaluated). ----------------------
@dataclass(frozen=True)
class ModuleSpec:
    name: str
    runtime: str
    entry_path: Path  # resolved absolute path to the entry file
    activates_on: list[str]
    description: str
    manifest_path: Path


@dataclass(frozen=True)
class Quarantined:
    name: str
    reason: str


def _entry_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- Discovery (§6.1). -------------------------------------------------------------
def _parse_manifest(manifest_path: Path) -> list[ModuleSpec]:
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    if data.get("review_api") not in (REVIEW_API, "review-visual/v1"):
        # Unknown manifest schema → ignore rather than guess (forward-compatible).
        return []
    base = manifest_path.parent.parent  # <project>/  (manifest lives in <project>/.review/)
    specs: list[ModuleSpec] = []
    for entry in data.get("modules", []) or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        rel = str(entry.get("entry", "")).strip()
        if not name or not rel:
            continue
        entry_path = (base / rel).resolve()
        activates_on = [str(t) for t in (entry.get("activates_on") or []) if str(t).strip()]
        specs.append(
            ModuleSpec(
                name=name,
                runtime=str(entry.get("runtime", "python")),
                entry_path=entry_path,
                activates_on=activates_on,
                description=str(entry.get("description", "")),
                manifest_path=manifest_path,
            )
        )
    return specs


def discover_specs(*, project: Path | None = None, env: RegistryEnv | None = None) -> list[ModuleSpec]:
    """Discover every contributed module spec for this run.

    Sources (deduplicated by name, project-local wins over a global registration):
      1. `<project>/.review/visual-modules.json` (the project-local manifest).
      2. every manifest path recorded in the global registry (`review register-module`).
    """
    env = env or RegistryEnv()
    project = (project or Path.cwd()).expanduser()
    specs: list[ModuleSpec] = []
    seen: set[str] = set()

    local_manifest = (project / MANIFEST_RELPATH)
    if local_manifest.is_file():
        for s in _parse_manifest(local_manifest):
            if s.name not in seen:
                specs.append(s)
                seen.add(s.name)

    for manifest_path in _global_manifest_paths(env):
        if not manifest_path.is_file():
            continue
        for s in _parse_manifest(manifest_path):
            if s.name not in seen:
                specs.append(s)
                seen.add(s.name)
    return specs


def _global_manifest_paths(env: RegistryEnv) -> list[Path]:
    try:
        data = json.loads(env.global_registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    return [Path(p).expanduser() for p in (data.get("manifests") or []) if isinstance(p, str)]


# --- Trust store (§6.3). -----------------------------------------------------------
def _load_trust(env: RegistryEnv) -> dict:
    try:
        data = json.loads(env.trust_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_trust(env: RegistryEnv, store: dict) -> None:
    env.trust_path.parent.mkdir(parents=True, exist_ok=True)
    # Write then tighten to 0600 (the pins gate arbitrary code execution).
    env.trust_path.write_text(json.dumps(store, indent=2) + "\n", encoding="utf-8")
    try:
        env.trust_path.chmod(0o600)
    except OSError:
        pass


def _audit(env: RegistryEnv, *, module: str, entry_sha256: str, trust_state: str, decision: str, duration_ms: float) -> None:
    """Append-only audit row. Best-effort — auditing must never break a verification."""
    row = {
        "ts": time.time(),
        "id": uuid.uuid4().hex,
        "module": module,
        "entry_sha256": entry_sha256,
        "trust_state": trust_state,
        "decision": decision,
        "duration_ms": round(duration_ms, 2),
    }
    try:
        env.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with env.audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass


def _trust_state_for(spec: ModuleSpec, store: dict) -> tuple[str, str]:
    """Return (state, reason) for a spec given the trust store. State is one of
    'trusted' | 'untrusted' | 'changed' | 'auto'.

    DEFAULT (guard off) = trust-by-default: every discovered module is trusted, no
    quarantine. The TOFU quarantine/sha-pin below only runs under the opt-in
    `REVIEW_UNTRUSTED_MODULES=1` guard (the rare untrusted-repo case)."""
    if not _untrusted_guard_active():
        return "trusted", "trust-by-default (no untrusted-repo guard)"
    if os.environ.get("REVIEW_MODULES_TRUST") == "auto":
        return "auto", "REVIEW_MODULES_TRUST=auto"
    pin = store.get(spec.name)
    if not isinstance(pin, dict) or "entry_sha256" not in pin:
        return "untrusted", "never trusted"
    try:
        current = _entry_sha256(spec.entry_path)
    except OSError as exc:
        return "untrusted", f"entry unreadable: {exc}"
    if current != pin["entry_sha256"]:
        return "changed", "module changed, re-trust required"
    # `activates_on` is pinned at trust time and governs WHEN the module runs. A
    # manifest-only edit can widen the tags without touching the entry file (the hash
    # still matches), so a module trusted for `selection` could start firing on
    # unrelated intents/checks. Re-quarantine on a tag change → re-trust required
    # (codex P2).
    pinned_tags = pin.get("activates_on")
    if isinstance(pinned_tags, list) and list(pinned_tags) != list(spec.activates_on):
        return "changed", "module activates_on changed since trust, re-trust required"
    return "trusted", "trusted (hash pinned)"


# --- Loading a trusted entry (§6 step "load"). -------------------------------------
def _load_entry_object(spec: ModuleSpec) -> object | None:
    """Import the entry file and return its VisualModule object.

    A contributed entry exposes its module as a top-level `MODULE` object, or a
    `module()`/`get_module()` factory, or a class named `Module`. Returns None if none
    is found / the file fails to import."""
    mod_name = f"_review_visual_contrib_{spec.name.replace('-', '_')}_{uuid.uuid4().hex[:8]}"
    try:
        impl_spec = importlib.util.spec_from_file_location(mod_name, spec.entry_path)
        if impl_spec is None or impl_spec.loader is None:
            return None
        module = importlib.util.module_from_spec(impl_spec)
        sys.modules[mod_name] = module
        impl_spec.loader.exec_module(module)
    except Exception:  # noqa: BLE001 — a broken contributed entry must not crash review
        sys.modules.pop(mod_name, None)
        return None
    obj = getattr(module, "MODULE", None)
    if obj is None:
        for factory in ("module", "get_module"):
            fn = getattr(module, factory, None)
            if callable(fn):
                try:
                    obj = fn()
                except Exception:  # noqa: BLE001
                    obj = None
                break
    if obj is None:
        cls = getattr(module, "Module", None)
        if isinstance(cls, type):
            try:
                obj = cls()
            except Exception:  # noqa: BLE001
                obj = None
    return obj


@dataclass(frozen=True)
class ContributedModule:
    """A loaded contributed module, wrapped so its `activates` is gated by the
    manifest's `activates_on` tags (the registry owns the WHEN, the module owns the
    WHAT). Conforms to the `VisualModule` Protocol the pipeline consumes."""

    name: str
    activates_on: list[str]
    _impl: object
    # The resolved entry file path, exposed so the known-good cache can fold the entry's
    # content hash into its context key (a changed module → invalidated keeps).
    entry_path: Path | None = None

    def activates(self, ctx: VisualContext) -> bool:
        # A contributed module auto-activates when one of its tags — or its own name —
        # is force-requested (--check) or appears in the (untrusted, read-only) intent.
        requested = {c.lower() for c in ctx.requested_checks}
        if self.name.lower() in requested:
            return True
        tags = [t.lower() for t in self.activates_on]
        if any(t in requested for t in tags):
            return True
        intent = (ctx.intent or "").lower()
        if intent and any(t in intent for t in tags):
            return True
        return False

    def cv_check(self, ctx: VisualContext) -> ModuleVerdict | None:
        fn = getattr(self._impl, "cv_check", None)
        return fn(ctx) if callable(fn) else None

    def vision_questions(self, ctx: VisualContext) -> list[str]:
        fn = getattr(self._impl, "vision_questions", None)
        return list(fn(ctx)) if callable(fn) else []

    def judge(self, ctx: VisualContext, vision) -> ModuleVerdict:
        fn = getattr(self._impl, "judge", None)
        if callable(fn):
            return fn(ctx, vision)
        return ModuleVerdict(module=self.name, decision="abstain", confidence=0.0, reason="no judge")

    @property
    def _vision_field(self) -> str:
        return getattr(self._impl, "_vision_field", "")


_BANNERED: set[str] = set()


def _banner(spec: ModuleSpec, state: str) -> None:
    """Print the quarantine banner ONCE per module name per process (so a busy run
    isn't spammed)."""
    if spec.name in _BANNERED:
        return
    _BANNERED.add(spec.name)
    if state == "changed":
        msg = (
            f"NEW review module (not active): {spec.name} CHANGED since trust — "
            f"re-run 'review trust-module {spec.name}'."
        )
    else:
        msg = (
            f"NEW review module (not active): {spec.name} from {spec.manifest_path.parent.parent}. "
            f"Run 'review trust-module {spec.name}' or set REVIEW_MODULES_TRUST=auto"
        )
    print(msg, file=sys.stderr)


def load_modules(
    *, project: Path | None = None, env: RegistryEnv | None = None
) -> tuple[list[ContributedModule], list[Quarantined]]:
    """Discover → trust-gate → load. Returns (active contributed modules, quarantined).

    A quarantined or unloadable module is ABSENT (never a block). The built-in modules
    are handled separately (they are not contributed) — this returns ONLY contributed
    modules to be merged with the built-ins by the pipeline."""
    env = env or RegistryEnv()
    store = _load_trust(env)
    loaded: list[ContributedModule] = []
    quarantined: list[Quarantined] = []

    for spec in discover_specs(project=project, env=env):
        t0 = time.time()
        try:
            sha = _entry_sha256(spec.entry_path)
        except OSError as exc:
            quarantined.append(Quarantined(spec.name, f"entry unreadable: {exc}"))
            continue
        state, reason = _trust_state_for(spec, store)
        if state in ("untrusted", "changed"):
            _banner(spec, state)
            _audit(env, module=spec.name, entry_sha256=sha, trust_state="quarantined", decision="absent", duration_ms=(time.time() - t0) * 1000)
            quarantined.append(Quarantined(spec.name, reason))
            continue
        # Trusted (or auto): load the entry.
        obj = _load_entry_object(spec)
        if obj is None:
            _audit(env, module=spec.name, entry_sha256=sha, trust_state=state, decision="load-failed", duration_ms=(time.time() - t0) * 1000)
            quarantined.append(Quarantined(spec.name, "entry did not expose a VisualModule (MODULE/module()/Module)"))
            continue
        if not isinstance(obj, VisualModule):
            _audit(env, module=spec.name, entry_sha256=sha, trust_state=state, decision="not-a-module", duration_ms=(time.time() - t0) * 1000)
            quarantined.append(Quarantined(spec.name, "loaded object does not satisfy the VisualModule protocol"))
            continue
        loaded.append(ContributedModule(name=spec.name, activates_on=spec.activates_on, _impl=obj, entry_path=spec.entry_path))
        trust_state = "trusted" if state == "trusted" else "auto"
        _audit(env, module=spec.name, entry_sha256=sha, trust_state=trust_state, decision="loaded", duration_ms=(time.time() - t0) * 1000)
    return loaded, quarantined


# --- Subcommands (`review trust-module` / `review register-module`). ---------------
def trust_module(name: str, *, project: Path | None = None, env: RegistryEnv | None = None) -> int:
    """Pin `{entry_sha256, activates_on}` for a discovered module (the untrusted-repo
    guard case). Re-hash the entry at pin time so a later load can detect tampering.
    Append-only audit.

    In the DEFAULT (trust-by-default) world this verb is a NO-OP: project modules
    already load and run, so there is nothing to pin. It only does real pinning under
    `REVIEW_UNTRUSTED_MODULES=1`. The verb is kept so the guarded flow still has its
    "trust this module" affordance."""
    env = env or RegistryEnv()
    if not _untrusted_guard_active():
        print(
            f"review: '{name}' already loads (trust-by-default). "
            f"trust-module only pins under {UNTRUSTED_GUARD_ENV}=1 (untrusted-repo guard)."
        )
        return 0
    specs = {s.name: s for s in discover_specs(project=project, env=env)}
    spec = specs.get(name)
    if spec is None:
        print(f"review trust-module: no module named {name!r} discovered (looked in {project or Path.cwd()})", file=sys.stderr)
        return 1
    try:
        sha = _entry_sha256(spec.entry_path)
    except OSError as exc:
        print(f"review trust-module: cannot read entry for {name}: {exc}", file=sys.stderr)
        return 1
    store = _load_trust(env)
    store[name] = {
        "entry_sha256": sha,
        "activates_on": spec.activates_on,
        "entry": str(spec.entry_path),
        "trusted_at": time.time(),
    }
    _write_trust(env, store)
    _audit(env, module=name, entry_sha256=sha, trust_state="trusted", decision="trust-pinned", duration_ms=0.0)
    print(f"review: trusted module {name} (sha256 {sha[:12]}…, activates_on={spec.activates_on})")
    return 0


def register_module(manifest_path: str, *, env: RegistryEnv | None = None) -> int:
    """Record a project manifest path in the global registry so its modules are
    available outside the project tree (idempotent — mirrors `review install-skill`)."""
    env = env or RegistryEnv()
    path = Path(manifest_path).expanduser().resolve()
    if not path.is_file():
        print(f"review register-module: no such manifest: {path}", file=sys.stderr)
        return 1
    try:
        data = json.loads(env.global_registry_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, json.JSONDecodeError):
        data = {}
    manifests = data.setdefault("manifests", [])
    if not isinstance(manifests, list):
        manifests = []
        data["manifests"] = manifests
    if str(path) not in manifests:
        manifests.append(str(path))
    env.global_registry_path.parent.mkdir(parents=True, exist_ok=True)
    env.global_registry_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"review: registered module manifest {path}")
    return 0
