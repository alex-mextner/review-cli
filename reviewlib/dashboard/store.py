"""The ONLY new persistence layer for the dashboard.

review-cli's log artifacts are read-only history; they do not capture the things the
overseer (the human lead) wants to attach AFTER a run:

  * ``feedback``   — free-text overseer feedback on a session;
  * ``conscious``  — a per-session boolean the overseer toggles to flag a session as
                     "conscious" (deliberately reviewed / acted on — the Tasks panel);
  * ``links``      — PR/ticket associations ({"prs": [...], "tickets": [...]}), which
                     the overseer attaches in the UI and which auto-detection seeds from
                     a run's cwd/branch where possible.

Stored as a single JSON file under the config dir (``~/.config/review-cli/dashboard.json``,
overridable via ``$REVIEW_DASHBOARD_STORE`` for tests), keyed by the deterministic
session id from ``parser._session_id_for`` so annotations stay pinned to a session even
as logs age out and re-cluster. Writes are atomic (temp file + os.replace) and the file
is created 0600 (it may hold review notes).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

_LOCK = threading.RLock()

# A HYP-style ticket or a bare PR number. Loose on purpose — the overseer is trusted,
# this only guards against absurd input, not adversaries (local-only server).
_TICKET_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d+$")
_PR_RE = re.compile(r"^#?\d+$")


def store_path() -> Path:
    override = os.environ.get("REVIEW_DASHBOARD_STORE")
    if override:
        p = Path(override).expanduser()
    else:
        p = Path.home() / ".config" / "review-cli" / "dashboard.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _empty() -> dict:
    return {"version": 1, "sessions": {}}


def load_store() -> dict:
    p = store_path()
    if not p.exists():
        return _empty()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty()
    if not isinstance(data, dict) or "sessions" not in data or not isinstance(data["sessions"], dict):
        return _empty()
    data.setdefault("version", 1)
    return data


def _write_store(data: dict) -> None:
    p = store_path()
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".dashboard.", suffix=".json")
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session_record(data: dict, session_id: str) -> dict:
    return data["sessions"].setdefault(
        session_id,
        {"feedback": None, "conscious": False, "links": {"prs": [], "tickets": []}, "updated": None},
    )


def get_annotation(session_id: str) -> dict:
    """Return the overseer annotation for a session (defaults if none stored)."""
    with _LOCK:
        data = load_store()
        rec = data["sessions"].get(session_id)
    if rec is None:
        return {"feedback": None, "conscious": False, "links": {"prs": [], "tickets": []}, "updated": None}
    # Normalize shape defensively (older/hand-edited files).
    rec.setdefault("feedback", None)
    rec.setdefault("conscious", False)
    links = rec.setdefault("links", {"prs": [], "tickets": []})
    links.setdefault("prs", [])
    links.setdefault("tickets", [])
    rec.setdefault("updated", None)
    return rec


def all_annotations() -> dict:
    with _LOCK:
        return dict(load_store()["sessions"])


def set_feedback(session_id: str, text: str | None) -> dict:
    with _LOCK:
        data = load_store()
        rec = _session_record(data, session_id)
        rec["feedback"] = (text or "").strip() or None
        rec["updated"] = _now()
        _write_store(data)
        return rec


def set_conscious(session_id: str, conscious: bool) -> dict:
    with _LOCK:
        data = load_store()
        rec = _session_record(data, session_id)
        rec["conscious"] = bool(conscious)
        rec["updated"] = _now()
        _write_store(data)
        return rec


def _norm_pr(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    if _PR_RE.match(value):
        return value if value.startswith("#") else "#" + value
    return None


def _norm_ticket(value: str) -> str | None:
    value = value.strip().upper()
    if _TICKET_RE.match(value):
        return value
    return None


def add_link(session_id: str, *, pr: str | None = None, ticket: str | None = None) -> dict:
    """Attach a PR (#123) and/or a ticket (HYP-742) to a session. Dedups."""
    with _LOCK:
        data = load_store()
        rec = _session_record(data, session_id)
        links = rec["links"]
        if pr is not None:
            npr = _norm_pr(pr)
            if npr is None:
                raise ValueError(f"invalid PR reference: {pr!r} (expected '#123' or '123')")
            if npr not in links["prs"]:
                links["prs"].append(npr)
        if ticket is not None:
            nt = _norm_ticket(ticket)
            if nt is None:
                raise ValueError(f"invalid ticket reference: {ticket!r} (expected e.g. 'HYP-742')")
            if nt not in links["tickets"]:
                links["tickets"].append(nt)
        rec["updated"] = _now()
        _write_store(data)
        return rec


def remove_link(session_id: str, *, pr: str | None = None, ticket: str | None = None) -> dict:
    with _LOCK:
        data = load_store()
        rec = _session_record(data, session_id)
        links = rec["links"]
        if pr is not None:
            npr = _norm_pr(pr) or pr
            links["prs"] = [x for x in links["prs"] if x != npr]
        if ticket is not None:
            nt = _norm_ticket(ticket) or ticket.strip().upper()
            links["tickets"] = [x for x in links["tickets"] if x != nt]
        rec["updated"] = _now()
        _write_store(data)
        return rec
