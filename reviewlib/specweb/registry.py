"""Spec REGISTRY for the multi-spec spec-web daemon (stdlib only, no deps).

The daemon (``review spec-web start``) serves MANY specs from ONE port, addressed by NAME at
``/spec/<name>``. This module is the durable name -> spec-path map that survives a daemon
restart/reboot, so ``start`` re-serves every previously-registered spec without re-adding it.

Layout
------
One JSON file at ``<store_dir>/registry.json`` (same dir as the per-spec comment stores, so a
single ``$REVIEW_SPECWEB_DIR`` override relocates everything for tests). Shape::

    {
      "version": 1,
      "specs": {
        "<name>": {"path": "/abs/path/spec.md", "added": "2026-07-02T..."},
        ...
      }
    }

The KEY is a URL-safe name derived from the spec's filename stem (GitHub-style slug), deduped
against collisions with a DIFFERENT path. Registration is IDEMPOTENT by resolved absolute path:
re-adding an already-registered spec returns its EXISTING name (never a second entry / a
``-2`` twin), so ``add`` is safe to call repeatedly.

Concurrency
-----------
The daemon reads the registry per request while a separate ``review spec-web add`` process
writes it — same cross-process hazard the comment store solves. Every mutator holds an
exclusive advisory flock on a sibling ``registry.lock`` across its load->modify->write (atomic
``os.replace``), degrading to the in-process lock where ``fcntl`` is absent (Windows).
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from .render import slug
from .store import store_dir

try:
    import fcntl  # POSIX advisory file locking (cross-process)
except ImportError:  # pragma: no cover - Windows has no fcntl
    fcntl = None  # type: ignore[assignment]

_LOCK = threading.RLock()

# A registered name must be a URL path segment and a filesystem-safe slug. The slug pass
# already strips everything but ``[\w-]``; this is the fallback when a stem slugs to empty
# (e.g. a spec literally named ``.md`` or all-punctuation).
_FALLBACK_NAME = "spec"


def registry_path() -> Path:
    return store_dir() / "registry.json"


def _lock_path() -> Path:
    return store_dir() / "registry.lock"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextlib.contextmanager
def _guard():
    """Serialise a load-modify-write across threads AND processes (see module docstring)."""
    with _LOCK:
        if fcntl is None:
            yield
            return
        fd = None
        try:
            fd = os.open(str(_lock_path()), os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError:
            if fd is not None:
                os.close(fd)
                fd = None
        try:
            yield
        finally:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(fd)


def _empty() -> dict:
    return {"version": 1, "specs": {}}


def load_registry() -> dict:
    """Read the registry (never raises; a missing/corrupt file reads as empty)."""
    p = registry_path()
    if not p.exists():
        return _empty()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty()
    if not isinstance(data, dict) or not isinstance(data.get("specs"), dict):
        return _empty()
    data.setdefault("version", 1)
    # Drop any malformed entry rather than crash a later read: each value must be a dict with a
    # string path.
    specs = {}
    for name, rec in data["specs"].items():
        if isinstance(name, str) and isinstance(rec, dict) and isinstance(rec.get("path"), str):
            specs[name] = rec
    data["specs"] = specs
    return data


def _write_registry(data: dict) -> None:
    p = registry_path()
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".registry.", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _abspath(spec_path: Path | str) -> str:
    return str(Path(spec_path).expanduser().resolve())


def _base_name(spec_path: Path | str) -> str:
    """A URL-safe base name from the spec's filename stem (before dedup)."""
    stem = Path(spec_path).stem
    name = slug(stem)
    return name or _FALLBACK_NAME


def _unique_name(base: str, specs: dict) -> str:
    """``base`` if free, else ``base-2``, ``base-3``, … — the first not already a KEY."""
    if base not in specs:
        return base
    n = 2
    while f"{base}-{n}" in specs:
        n += 1
    return f"{base}-{n}"


def name_for_path(spec_path: Path | str, data: dict | None = None) -> str | None:
    """The registered name for a spec path (resolved), or None if not registered."""
    target = _abspath(spec_path)
    reg = data if data is not None else load_registry()
    for name, rec in reg["specs"].items():
        if rec.get("path") == target:
            return name
    return None


def register(spec_path: Path | str, *, agent: str | None = None) -> str:
    """Register a spec and return its name. IDEMPOTENT by resolved absolute path.

    Re-registering an already-known spec returns its existing name (no duplicate / no ``-2``
    twin). A new spec gets a slug of its filename stem, deduped against name collisions with a
    DIFFERENT path.

    ``agent`` records the OWNING agent session for submit delivery (see ``deliver.py``); a
    re-register with a (different) agent UPDATES the owner — the latest ``serve --agent`` is
    the session actually waiting on this spec. ``None`` never clears an existing owner. The
    field is ADDITIVE: registries written before it existed load unchanged.
    """
    target = _abspath(spec_path)
    with _guard():
        data = load_registry()
        existing = name_for_path(target, data)
        if existing is not None:
            if agent and data["specs"][existing].get("agent") != agent:
                data["specs"][existing]["agent"] = agent
                _write_registry(data)
            return existing
        name = _unique_name(_base_name(target), data["specs"])
        rec = {"path": target, "added": _now()}
        if agent:
            rec["agent"] = agent
        data["specs"][name] = rec
        _write_registry(data)
        return name


def unregister(name: str) -> bool:
    """Drop a registered spec by name. Missing name -> False (idempotent)."""
    with _guard():
        data = load_registry()
        if name not in data["specs"]:
            return False
        data["specs"].pop(name, None)
        _write_registry(data)
        return True


def resolve(name: str) -> Path | None:
    """The registered spec PATH for ``name``, or None if the name isn't registered.

    Returns the path even if the file no longer exists on disk (the caller decides how to
    present a missing file); a name that was never registered returns None.
    """
    rec = load_registry()["specs"].get(name)
    if rec is None:
        return None
    return Path(rec["path"])


def list_specs() -> list[dict]:
    """Every registered spec, sorted by name, each ``{name, path, added, exists, mtime}``.

    ``exists``/``mtime`` are probed live so the navigator can flag a spec whose file was moved
    or deleted since it was registered.
    """
    data = load_registry()
    out: list[dict] = []
    for name in sorted(data["specs"]):
        rec = data["specs"][name]
        p = Path(rec["path"])
        try:
            st = p.stat()
            exists, mtime = True, st.st_mtime
        except OSError:
            exists, mtime = False, None
        out.append(
            {
                "name": name,
                "path": rec["path"],
                "added": rec.get("added", ""),
                "agent": rec.get("agent"),
                "exists": exists,
                "mtime": mtime,
            }
        )
    return out
