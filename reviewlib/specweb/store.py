"""Comment / reply / review-batch persistence for the spec-web reviewer.

One JSON file PER SPEC, keyed by the sha1 of the spec's ABSOLUTE path, under
``~/.config/review-cli/spec-web/<sha1>.json`` (overridable via ``$REVIEW_SPECWEB_DIR``
for tests). Created 0600 (it holds review notes). Writes are atomic (temp file +
``os.replace``). Survives server restarts.

A comment captures enough to RE-ANCHOR it in the rendered spec on reload:
  * ``quote``       — the selected text (verbatim);
  * ``section_id``  — the id of the containing heading (the section the selection is in);
  * ``section_title`` — that heading's human text (for the sidebar);
  * ``start``/``end`` — char offsets of the selection WITHIN its section's text (a hint
                        for fuzzy re-anchoring; not authoritative);
  * ``body``        — the comment / question text;
  * ``author``      — who left it;
  * ``created``     — ISO-8601 UTC;
  * ``status``      — ``pending`` | ``submitted`` | ``answered`` | ``resolved``;
  * ``batch``       — the submit-batch timestamp once submitted (None while pending);
  * ``replies``     — a thread of {id, author, body, created} answers.

Re-anchoring is pragmatic (quote-within-section search + highlight). When a quote can't
be re-found the comment is shown as "unanchored" in the sidebar — never a crash. That
search lives client-side; the store just persists the fields.

The store is intentionally INDEPENDENT of the dashboard store — same author style, no
import, different shape (the dashboard keys by session; this keys by spec path).
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

_LOCK = threading.RLock()

VALID_STATUSES = ("pending", "submitted", "answered", "resolved")


def store_dir() -> Path:
    override = os.environ.get("REVIEW_SPECWEB_DIR")
    base = Path(override).expanduser() if override else Path.home() / ".config" / "review-cli" / "spec-web"
    base.mkdir(parents=True, exist_ok=True)
    return base


def spec_key(spec_path: Path | str) -> str:
    """Deterministic per-spec key = sha1 of the resolved absolute path."""
    abspath = str(Path(spec_path).expanduser().resolve())
    return hashlib.sha1(abspath.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


def _str(value, default: str = "") -> str:
    """Coerce an imported JSON field to a string. Imported/seed payloads are user-provided,
    so a field may be a non-string (e.g. ``"body": 123``) — coerce instead of crashing with
    an AttributeError on ``.strip()``. None/missing -> the default."""
    if value is None:
        return default
    return value if isinstance(value, str) else str(value)


class SpecStore:
    """Persistence for ONE spec's review thread, keyed by the spec's absolute path."""

    def __init__(self, spec_path: Path | str) -> None:
        self.spec_path = str(Path(spec_path).expanduser().resolve())
        self.key = spec_key(self.spec_path)
        self.path = store_dir() / f"{self.key}.json"

    # ---- raw load / save -------------------------------------------------- #
    def _empty(self) -> dict:
        return {"version": 1, "spec_path": self.spec_path, "comments": []}

    def load(self) -> dict:
        if not self.path.exists():
            return self._empty()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._empty()
        if not isinstance(data, dict) or not isinstance(data.get("comments"), list):
            return self._empty()
        data.setdefault("version", 1)
        data.setdefault("spec_path", self.spec_path)
        return data

    def _write(self, data: dict) -> None:
        p = self.path
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".specweb.", suffix=".json")
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

    # ---- reads ------------------------------------------------------------ #
    def all_comments(self) -> list[dict]:
        with _LOCK:
            return list(self.load()["comments"])

    def get_comment(self, comment_id: str) -> dict | None:
        with _LOCK:
            for c in self.load()["comments"]:
                if c.get("id") == comment_id:
                    return c
        return None

    # ---- comment CRUD ----------------------------------------------------- #
    def add_comment(
        self,
        *,
        quote: str,
        body: str,
        section_id: str = "",
        section_title: str = "",
        start: int | None = None,
        end: int | None = None,
        author: str = "reviewer",
    ) -> dict:
        """Create a new comment. It enters the PENDING batch (GitHub-review style)."""
        rec = {
            "id": _new_id(),
            "quote": (quote or "").strip(),
            "section_id": (section_id or "").strip(),
            "section_title": (section_title or "").strip(),
            "start": start,
            "end": end,
            "body": (body or "").strip(),
            "author": (author or "reviewer").strip() or "reviewer",
            "created": _now(),
            "status": "pending",
            "batch": None,
            "replies": [],
        }
        with _LOCK:
            data = self.load()
            data["comments"].append(rec)
            self._write(data)
        return rec

    def add_reply(self, comment_id: str, *, body: str, author: str = "reviewer") -> dict | None:
        """Thread an inline answer under a comment. Bumps an answered comment's status.

        A reply does NOT auto-resolve; the status flips to ``answered`` only if the
        comment was already submitted (an answer to a pending comment leaves it pending —
        the batch hasn't been submitted yet)."""
        reply = {
            "id": _new_id(),
            "author": (author or "reviewer").strip() or "reviewer",
            "body": (body or "").strip(),
            "created": _now(),
        }
        with _LOCK:
            data = self.load()
            for c in data["comments"]:
                if c.get("id") == comment_id:
                    c.setdefault("replies", []).append(reply)
                    if c.get("status") == "submitted":
                        c["status"] = "answered"
                    self._write(data)
                    return c
        return None

    def set_status(self, comment_id: str, status: str) -> dict | None:
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status {status!r}; expected one of {VALID_STATUSES}")
        with _LOCK:
            data = self.load()
            for c in data["comments"]:
                if c.get("id") == comment_id:
                    c["status"] = status
                    self._write(data)
                    return c
        return None

    def delete_comment(self, comment_id: str) -> bool:
        with _LOCK:
            data = self.load()
            before = len(data["comments"])
            data["comments"] = [c for c in data["comments"] if c.get("id") != comment_id]
            if len(data["comments"]) == before:
                return False
            self._write(data)
            return True

    # ---- review batch (GitHub "Submit review") ---------------------------- #
    def submit_pending(self) -> dict:
        """Flip every PENDING comment to ``submitted`` with one shared batch timestamp.

        Returns {batch, count}. A submit with no pending comments is a no-op (count 0).
        """
        batch = _now()
        with _LOCK:
            data = self.load()
            count = 0
            for c in data["comments"]:
                if c.get("status") == "pending":
                    c["status"] = "submitted"
                    c["batch"] = batch
                    count += 1
            if count:
                self._write(data)
        return {"batch": batch if count else None, "count": count}

    # ---- seeding / import ------------------------------------------------- #
    def import_thread(self, payload: dict, *, replace: bool = False) -> dict:
        """Import an initial review thread from a JSON payload.

        Payload shape (``comments`` is the only required key)::

            {
              "comments": [
                {
                  "quote": "selected text",        # required-ish (may be "" -> unanchored)
                  "body": "the question/comment",  # required
                  "section_id": "94-...",          # optional, for re-anchoring
                  "section_title": "§9.4 ...",     # optional, sidebar label
                  "start": 12, "end": 40,          # optional char offsets
                  "author": "alex",                # optional, default "reviewer"
                  "status": "submitted",           # optional, default "pending"
                  "batch": "2026-06-14T...",        # optional
                  "replies": [                      # optional thread of answers
                    {"author": "claude", "body": "answer text"}
                  ]
                }
              ]
            }

        Unknown keys are ignored. Missing ids/created are generated. With ``replace=True``
        the existing comments are discarded first; otherwise the imported comments are
        appended (deduped by id when an explicit id is supplied).
        """
        if not isinstance(payload, dict) or not isinstance(payload.get("comments"), list):
            raise ValueError("seed payload must be an object with a 'comments' array")
        imported: list[dict] = []
        for raw in payload["comments"]:
            if not isinstance(raw, dict):
                continue
            status = raw.get("status", "pending")
            if status not in VALID_STATUSES:
                status = "pending"
            # A malformed seed may give a truthy non-list `replies` (e.g. 123/true) —
            # iterating it would raise TypeError (500 / CLI traceback). Treat non-lists as
            # empty.
            replies_in = raw.get("replies")
            if not isinstance(replies_in, list):
                replies_in = []
            replies: list[dict] = []
            for r in replies_in:
                if not isinstance(r, dict):
                    continue
                replies.append(
                    {
                        "id": _str(r.get("id")) or _new_id(),
                        "author": _str(r.get("author"), "reviewer") or "reviewer",
                        "body": _str(r.get("body")).strip(),
                        "created": _str(r.get("created")) or _now(),
                    }
                )
            start = raw.get("start")
            end = raw.get("end")
            imported.append(
                {
                    "id": _str(raw.get("id")) or _new_id(),
                    "quote": _str(raw.get("quote")).strip(),
                    "section_id": _str(raw.get("section_id")).strip(),
                    "section_title": _str(raw.get("section_title")).strip(),
                    "start": start if isinstance(start, int) else None,
                    "end": end if isinstance(end, int) else None,
                    "body": _str(raw.get("body")).strip(),
                    "author": _str(raw.get("author"), "reviewer") or "reviewer",
                    "created": _str(raw.get("created")) or _now(),
                    "status": status,
                    "batch": _str(raw.get("batch")) or None,
                    "replies": replies,
                }
            )
        with _LOCK:
            data = self.load()
            if replace:
                data["comments"] = []
            existing_ids = {c.get("id") for c in data["comments"]}
            for rec in imported:
                if rec["id"] in existing_ids:
                    continue
                data["comments"].append(rec)
                existing_ids.add(rec["id"])
            self._write(data)
        return {"imported": len(imported)}

    # ---- export ----------------------------------------------------------- #
    def export_markdown(self) -> str:
        """Dump the whole review as markdown: quotes + questions + threaded answers."""
        comments = self.all_comments()
        lines: list[str] = [f"# Spec review — {Path(self.spec_path).name}", ""]
        lines.append(f"_Spec:_ `{self.spec_path}`  ")
        lines.append(f"_Comments:_ {len(comments)}")
        lines.append("")
        if not comments:
            lines.append("_(no comments yet)_")
            return "\n".join(lines) + "\n"
        # Group by section title for a readable export.
        by_section: dict[str, list[dict]] = {}
        for c in comments:
            key = c.get("section_title") or c.get("section_id") or "(unanchored)"
            by_section.setdefault(key, []).append(c)
        for section, items in by_section.items():
            lines.append(f"## {section}")
            lines.append("")
            for c in items:
                status = c.get("status", "pending")
                author = c.get("author", "reviewer")
                created = c.get("created", "")
                lines.append(f"### [{status}] {author} — {created}")
                quote = c.get("quote", "")
                if quote:
                    for q in quote.splitlines() or [quote]:
                        lines.append(f"> {q}")
                    lines.append("")
                lines.append(c.get("body", ""))
                lines.append("")
                for r in c.get("replies", []):
                    lines.append(f"- **{r.get('author', 'reviewer')}** ({r.get('created', '')}): {r.get('body', '')}")
                if c.get("replies"):
                    lines.append("")
        return "\n".join(lines) + "\n"
