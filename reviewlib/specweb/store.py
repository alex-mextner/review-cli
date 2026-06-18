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
  * ``kind``        — ``question`` (expects an answer from the author) | ``remark``
                       (feedback that does not). Drives the sidebar label/icon/colour.
  * ``body``        — the comment / question text;
  * ``author``      — who left it (single implicit reviewer; not shown in the UI but kept
                       so export/import payloads round-trip);
  * ``created``     — ISO-8601 UTC;
  * ``status``      — ``pending`` | ``submitted`` | ``answered`` | ``resolved``;
  * ``batch``       — the submit-batch timestamp once submitted (None while pending);
  * ``replies``     — a thread of {id, author, body, created} answers.

Re-anchoring is pragmatic (quote-within-section search + highlight). When a quote can't
be re-found the comment is shown as "unanchored" in the sidebar — never a crash. That
search lives client-side; the store just persists the fields.

DRAFTS (in-progress composer text) are persisted too, keyed by a ``slot`` id under the
file's ``drafts`` map, so a half-typed note survives a page reload (the composer
autosaves debounced and restores on load). Slot ``"new"`` is the new-note composer; slot
``"edit:<comment_id>"`` is an edit-in-progress. A draft is NOT a comment — it never shows
in the review, never submits — it is purely the recoverable composer buffer; saving the
note (POST /api/comments or /edit) clears its slot.

The store is intentionally INDEPENDENT of the dashboard store — same author style, no
import, different shape (the dashboard keys by session; this keys by spec path).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl  # POSIX advisory file locking (cross-process)
except ImportError:  # pragma: no cover - Windows has no fcntl
    fcntl = None  # type: ignore[assignment]

# In-process lock: serialises threads within ONE process (the server's request threads).
# Cross-process correctness (the server process AND a separate `review spec-web reply`
# process writing the same per-spec file) is provided by an advisory flock on a sibling
# `.lock` file, held across the whole load-modify-write — see SpecStore._guard.
_LOCK = threading.RLock()

VALID_STATUSES = ("pending", "submitted", "answered", "resolved")
# A note is either a QUESTION (expects an answer from the spec author) or a REMARK
# (feedback that does not). Default REMARK: a note created without an explicit kind, and
# every legacy comment persisted before this field existed, reads as a remark.
VALID_KINDS = ("question", "remark")
DEFAULT_KIND = "remark"

# The author string stamped on a reply left by the AGENT (via `review spec-web reply`),
# vs the human reviewer's "reviewer". The UI styles an agent reply distinctly (it is the
# spec author answering, not the reviewer), so it must be a stable, recognisable marker.
AGENT_AUTHOR = "agent"

# The fixed slot id of the NEW-note composer draft. An edit-in-progress draft uses
# ``edit:<comment_id>`` (see ``edit_draft_slot``). One source of truth so the client and
# the store agree on the key.
NEW_DRAFT_SLOT = "new"


def edit_draft_slot(comment_id: str) -> str:
    """Draft slot id for an in-progress EDIT of an existing comment."""
    return f"edit:{comment_id}"


def _norm_kind(value: object) -> str:
    """Coerce an arbitrary value to a valid kind string, defaulting to ``remark``."""
    return value if isinstance(value, str) and value in VALID_KINDS else DEFAULT_KIND


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


def _as_int(value) -> int | None:
    """A char-offset coerced to an int, or None. ``isinstance(True, int)`` is True in Python,
    so a JSON ``true`` would otherwise be stored as the offset 1 and corrupt re-anchoring;
    reject bools explicitly. Defends the store API directly (not just the HTTP boundary)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# Upper bound for a draft ordering token (review-cli#30). The client counter is a per-session
# send sequence — realistically a few thousand writes — so any value beyond this is a
# malformed/forged token. Clamping it out (treated as token-less) stops a single absurd value
# from parking the slot's tombstone at ~maxint and rejecting every legitimate write after it
# (a trusted-localhost tool, so this is cheap belt-and-braces, not a real threat model).
_MAX_DRAFT_TOKEN = 2**31 - 1


def _as_token(value) -> int | None:
    """A draft ORDERING token (review-cli#30) coerced to an int in ``[0, _MAX_DRAFT_TOKEN]``,
    or None when the write carries no usable token. A token is the client's monotonic
    per-session send sequence; the store rejects a write whose token is <= the slot's last
    applied one.

    Reuses ``_as_int`` (so a bool is rejected — a JSON ``true`` must not read as token 1; a
    str/float is rejected too) and additionally rejects out-of-range values: a token is a
    counter, never below zero, and an absurdly large one is forged/malformed (see
    ``_MAX_DRAFT_TOKEN``). A missing/invalid token -> None, which makes ``save_draft`` skip
    the ordering check entirely (legacy clients keep working — they just don't get the
    out-of-order protection)."""
    n = _as_int(value)
    return n if n is not None and 0 <= n <= _MAX_DRAFT_TOKEN else None


@dataclass(frozen=True)
class DraftWriteResult:
    """Outcome of a draft write (review-cli#30). ``draft`` is the authoritative slot state
    after the call (the stored draft, or ``{}`` for a cleared/rejected slot). ``applied`` is
    False ONLY when a token-carrying write was rejected as stale (out-of-order) — decided
    inside the store lock, so the HTTP layer reports staleness without a second read."""

    draft: dict
    applied: bool


class SpecStore:
    """Persistence for ONE spec's review thread, keyed by the spec's absolute path."""

    def __init__(self, spec_path: Path | str) -> None:
        self.spec_path = str(Path(spec_path).expanduser().resolve())
        self.key = spec_key(self.spec_path)
        self.path = store_dir() / f"{self.key}.json"
        # Sibling lock file for the cross-process advisory flock (see _guard). Separate from
        # the data file so locking never races with the atomic os.replace of the data file.
        self.lock_path = store_dir() / f"{self.key}.lock"

    # ---- locking ---------------------------------------------------------- #
    @contextlib.contextmanager
    def _guard(self):
        """Serialise a load-modify-write across BOTH threads (the in-process RLock) AND
        processes (an exclusive advisory flock on the sibling .lock file). The agent's
        ``review spec-web reply`` runs in a SEPARATE process from the server, so the RLock
        alone can't stop its read-modify-write from clobbering a concurrent reviewer
        edit/submit/draft-save (last-writer-wins on os.replace). The flock closes that
        window: every mutator holds it across its whole load→modify→_write. Best-effort on
        platforms without fcntl (Windows): falls back to the in-process lock only."""
        with _LOCK:
            if fcntl is None:
                yield
                return
            # Open (creating) the lock file and hold an EXCLUSIVE lock for the critical
            # section. A failure to lock (e.g. an odd filesystem) must not deadlock the
            # store — degrade to the in-process lock rather than raise.
            fd = None
            try:
                fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT, 0o600)
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

    # ---- raw load / save -------------------------------------------------- #
    def _empty(self) -> dict:
        return {
            "version": 1,
            "spec_path": self.spec_path,
            "comments": [],
            "drafts": {},
            # Per-slot ordering tombstone (review-cli#30): slot -> the highest write token
            # that has been APPLIED to that slot. A write whose token is <= the recorded one
            # is a stale, out-of-order arrival and is rejected. It OUTLIVES the draft (a
            # cleared slot keeps its high-water-mark token), so a late autosave that lands
            # after the trailing clear cannot resurrect the draft. Token-less writes (legacy
            # clients) skip the check and never touch this map.
            "draft_tokens": {},
        }

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
        # `drafts` is a slot->draft map; a legacy file (or a tampered one with a non-dict)
        # reads as no drafts rather than crashing the comment reads that share this load().
        if not isinstance(data.get("drafts"), dict):
            data["drafts"] = {}
        # `draft_tokens` is the slot->token ordering tombstone (review-cli#30). A legacy file
        # has none; a tampered non-dict reads as empty — either way the ordering check just
        # treats every slot as fresh, which is correct (no recorded high-water-mark yet).
        if not isinstance(data.get("draft_tokens"), dict):
            data["draft_tokens"] = {}
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
        kind: str = DEFAULT_KIND,
        author: str = "reviewer",
    ) -> dict:
        """Create a new comment. It enters the PENDING batch (GitHub-review style).

        ``author`` is an import/seed escape hatch: the single-reviewer UI never sends it
        (it defaults to ``reviewer`` and is not displayed), but it is honoured so import/
        export payloads with explicit attribution round-trip.
        """
        rec = {
            "id": _new_id(),
            "quote": (quote or "").strip(),
            "section_id": (section_id or "").strip(),
            "section_title": (section_title or "").strip(),
            "start": start,
            "end": end,
            "kind": _norm_kind(kind),
            "body": (body or "").strip(),
            "author": (author or "reviewer").strip() or "reviewer",
            "created": _now(),
            "status": "pending",
            "batch": None,
            "replies": [],
        }
        with self._guard():
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
        with self._guard():
            data = self.load()
            for c in data["comments"]:
                if c.get("id") == comment_id:
                    c.setdefault("replies", []).append(reply)
                    if c.get("status") == "submitted":
                        c["status"] = "answered"
                    self._write(data)
                    return c
        return None

    def edit_comment(self, comment_id: str, *, body: str, kind: str | None = None) -> dict | None:
        """Edit a comment's body and (optionally) its kind. Status/batch are NOT changed —
        a submitted note stays submitted; the reviewer is just correcting what they wrote.

        Two failure modes (mirrors set_status/add_reply): an EMPTY body raises ValueError
        (the server maps it to 400); an UNKNOWN id returns None (mapped to 404). On success
        returns a COPY of the updated record."""
        new_body = (body or "").strip()
        if not new_body:
            raise ValueError("comment 'body' is required")
        with self._guard():
            data = self.load()
            for c in data["comments"]:
                if c.get("id") == comment_id:
                    c["body"] = new_body
                    if kind is not None:
                        c["kind"] = _norm_kind(kind)
                    self._write(data)
                    return dict(c)
        return None

    def set_status(self, comment_id: str, status: str) -> dict | None:
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status {status!r}; expected one of {VALID_STATUSES}")
        with self._guard():
            data = self.load()
            for c in data["comments"]:
                if c.get("id") == comment_id:
                    c["status"] = status
                    self._write(data)
                    return c
        return None

    def delete_comment(self, comment_id: str) -> bool:
        with self._guard():
            data = self.load()
            before = len(data["comments"])
            data["comments"] = [c for c in data["comments"] if c.get("id") != comment_id]
            if len(data["comments"]) == before:
                return False
            # A deleted comment can't have an outstanding edit-draft; drop it so a stale
            # draft for a gone comment never gets restored into the composer. Drop its
            # ordering tombstone too — the slot id (``edit:<gone-id>``) is never reused, so a
            # retained tombstone would only accumulate unbounded in the store file (#30).
            slot = edit_draft_slot(comment_id)
            data["drafts"].pop(slot, None)
            data["draft_tokens"].pop(slot, None)
            self._write(data)
            return True

    # ---- drafts (in-progress composer text, reload-safe) ------------------ #
    def all_drafts(self) -> dict[str, dict]:
        """The full slot -> draft map (read on page load to restore the composer)."""
        with _LOCK:
            return dict(self.load()["drafts"])

    def get_draft(self, slot: str) -> dict | None:
        with _LOCK:
            return self.load()["drafts"].get(slot)

    def get_draft_token(self, slot: str) -> int | None:
        """The slot's last applied ordering token (review-cli#30), or None if none recorded.
        Exposed for the HTTP layer (to report whether a write was accepted) and tests."""
        with _LOCK:
            return _as_token(self.load()["draft_tokens"].get(slot))

    def max_draft_token(self) -> int:
        """The highest ordering token recorded across ALL slots, or 0 if none (review-cli#30).

        The client seeds its per-session write counter ABOVE this on page load, so every new
        autosave outranks the durable cross-session tombstones. Without the seed, a reloaded
        page would restart its counter at 0 and have every autosave silently rejected as stale
        until the counter clawed back past the old high-water-mark — i.e. autosave would
        appear broken after a reload. The seed makes the per-session counter monotonic across
        sessions for every slot at once."""
        with _LOCK:
            tokens = [t for t in (_as_token(v) for v in self.load()["draft_tokens"].values()) if t is not None]
            return max(tokens, default=0)

    def _is_stale_token(self, data: dict, slot: str, token: int | None) -> bool:
        """True when a token-carrying write is OLDER than the slot's last applied write and
        must be rejected (review-cli#30). A ``None`` token (legacy/token-less write) is never
        stale — the ordering check only applies when the client opts in by sending a token.

        Compares against ``draft_tokens[slot]`` (the high-water-mark that OUTLIVES the draft
        via the tombstone), rejecting ``token <= last``: ``<`` is plainly older, and ``==``
        is a duplicate of the already-applied write (the client increments per send, so equal
        tokens are a re-delivery, not new content) — neither should overwrite a newer state."""
        if token is None:
            return False
        last = _as_token(data.get("draft_tokens", {}).get(slot))
        return last is not None and token <= last

    def save_draft(
        self,
        slot: str,
        *,
        body: str,
        kind: str = DEFAULT_KIND,
        quote: str = "",
        section_id: str = "",
        section_title: str = "",
        start: int | None = None,
        end: int | None = None,
        token: int | None = None,
    ) -> dict:
        """Autosave an in-progress composer buffer; returns the resulting draft (or ``{}`` on
        a clear / a rejected stale write). Back-compat thin wrapper over ``save_draft_result``;
        callers that need to know whether the write was APPLIED use that method instead."""
        return self.save_draft_result(
            slot, body=body, kind=kind, quote=quote, section_id=section_id,
            section_title=section_title, start=start, end=end, token=token,
        ).draft

    def save_draft_result(
        self,
        slot: str,
        *,
        body: str,
        kind: str = DEFAULT_KIND,
        quote: str = "",
        section_id: str = "",
        section_title: str = "",
        start: int | None = None,
        end: int | None = None,
        token: int | None = None,
    ) -> DraftWriteResult:
        """Autosave an in-progress composer buffer to disk under ``slot``, returning
        ``(draft, applied)``.

        A draft is the recoverable composer text + (for a new note) the selection context
        needed to re-open it; it is NOT a comment and never enters the review. An EMPTY
        body deletes the slot (clearing a draft is the same call with an empty body — the
        composer emptied to nothing has nothing to recover). ``draft`` is the stored draft,
        or ``{}`` when the body was empty (a clear) OR the write was rejected as stale.

        ORDERING (review-cli#30): an optional monotonic ``token`` (the client's send sequence)
        makes the write order-safe. A write whose token is <= the slot's last applied token is
        a stale, out-of-order arrival: it is REJECTED (``applied=False``), the slot is left
        untouched, and the CURRENT draft (or ``{}``) is returned so the client mirrors the
        authoritative state. An accepted token is recorded as a tombstone that outlives the
        draft. ``applied`` is decided INSIDE the lock, so the caller never needs a second read
        to learn the outcome (no TOCTOU). A ``None`` token keeps the legacy last-writer-wins
        behaviour (always applied, no tombstone), so older clients are unaffected."""
        slot = (slot or "").strip()
        if not slot:
            raise ValueError("draft 'slot' is required")
        text = body or ""
        token = _as_token(token)
        with self._guard():
            data = self.load()
            if self._is_stale_token(data, slot, token):
                # Out-of-order: a newer write already won this slot. Leave it untouched and
                # report what is actually there, so the client mirrors the authoritative state.
                return DraftWriteResult(draft=dict(data["drafts"].get(slot) or {}), applied=False)
            draft = self._apply_draft_write(
                data, slot, text, kind, quote, section_id, section_title, start, end,
            )
            if token is not None:
                data["draft_tokens"][slot] = token
            self._write(data)
        return DraftWriteResult(draft=draft, applied=True)

    def _apply_draft_write(
        self, data: dict, slot: str, text: str, kind: str, quote: str,
        section_id: str, section_title: str, start, end,
    ) -> dict:
        """Mutate ``data`` for an ACCEPTED draft write: an empty body pops the slot (clear,
        returns ``{}``); a non-empty body stores the draft record (returns it). Does NOT
        write to disk or touch the tombstone — ``save_draft`` owns the token bookkeeping and
        the ``_write``. Split out only to keep ``save_draft`` readable."""
        if not text.strip():
            data["drafts"].pop(slot, None)
            return {}
        draft = {
            "slot": slot,
            "body": text,
            "kind": _norm_kind(kind),
            "quote": (quote or ""),
            "section_id": (section_id or ""),
            "section_title": (section_title or ""),
            "start": _as_int(start),
            "end": _as_int(end),
            "updated": _now(),
        }
        data["drafts"][slot] = draft
        return draft

    def delete_draft(self, slot: str) -> bool:
        """Drop a draft slot (called when the note is SAVED, so a stale draft never restores
        over a now-persisted note). Missing slot -> False (idempotent, never raises)."""
        with self._guard():
            data = self.load()
            if slot not in data["drafts"]:
                return False
            data["drafts"].pop(slot, None)
            self._write(data)
            return True

    # ---- review batch (GitHub "Submit review") ---------------------------- #
    def submit_pending(self) -> dict:
        """Flip every PENDING comment to ``submitted`` with one shared batch timestamp.

        Records the submit on the store (``last_submit`` = the batch) so the launching
        ``review spec-web`` process can detect it and hand the structured review back to
        the agent. Returns {batch, count, review}: ``review`` is the structured payload
        (see ``review_payload``) the agent acts on. A submit with NO pending comments is a
        no-op (count 0, batch None) — it does not re-stamp ``last_submit``.
        """
        batch = _now()
        with self._guard():
            data = self.load()
            count = 0
            for c in data["comments"]:
                if c.get("status") == "pending":
                    c["status"] = "submitted"
                    c["batch"] = batch
                    count += 1
            if count:
                data["last_submit"] = batch
                self._write(data)
            review = self._review_payload(data, batch=batch if count else None)
        return {"batch": batch if count else None, "count": count, "review": review}

    def last_submit(self) -> str | None:
        """The batch timestamp of the most recent non-empty submit, or None if never
        submitted. Used by the launching process to detect a fresh submit."""
        with _LOCK:
            val = self.load().get("last_submit")
        return val if isinstance(val, str) else None

    def review_payload(self) -> dict:
        """The structured review the AGENT acts on (the full current state, all batches)."""
        with _LOCK:
            data = self.load()
            return self._review_payload(data, batch=data.get("last_submit") if isinstance(data.get("last_submit"), str) else None)

    @staticmethod
    def _review_payload(data: dict, *, batch: str | None) -> dict:
        """Build the structured-review payload from a loaded ``data`` dict.

        Shape (stable contract — the launching agent parses this)::

            {
              "spec_path": "/abs/path/spec.md",
              "batch": "2026-06-16T..." | null,   # the submit batch this payload is for
              "counts": {"questions": N, "remarks": M, "total": T},
              "comments": [ {id, kind, status, quote, section_id, section_title,
                             body, created, batch, replies:[{id,author,body,created}]} ]
            }

        Every comment carries its ``id`` so the agent can answer a specific one with
        ``review spec-web reply <id> <answer>``. QUESTIONS are what the agent must answer;
        remarks are feedback. Includes ALL comments (not just this batch) so the agent sees
        the whole thread — ``status`` distinguishes submitted/answered/resolved/pending.
        """
        comments = data.get("comments") or []
        out: list[dict] = []
        questions = remarks = 0
        for c in comments:
            kind = _norm_kind(c.get("kind"))
            if kind == "question":
                questions += 1
            else:
                remarks += 1
            replies = []
            for r in c.get("replies") or []:
                if not isinstance(r, dict):
                    continue
                replies.append(
                    {
                        "id": _str(r.get("id")),
                        "author": _str(r.get("author"), "reviewer") or "reviewer",
                        "body": _str(r.get("body")),
                        "created": _str(r.get("created")),
                    }
                )
            out.append(
                {
                    "id": _str(c.get("id")),
                    "kind": kind,
                    "status": _str(c.get("status"), "pending") or "pending",
                    "quote": _str(c.get("quote")),
                    "section_id": _str(c.get("section_id")),
                    "section_title": _str(c.get("section_title")),
                    "body": _str(c.get("body")),
                    "created": _str(c.get("created")),
                    "batch": _str(c.get("batch")) or None,
                    "replies": replies,
                }
            )
        return {
            "spec_path": data.get("spec_path", ""),
            "batch": batch,
            "counts": {"questions": questions, "remarks": remarks, "total": len(out)},
            "comments": out,
        }

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
                  "kind": "question",              # optional, "question"|"remark" (default remark)
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
                    "kind": _norm_kind(raw.get("kind")),
                    "body": _str(raw.get("body")).strip(),
                    "author": _str(raw.get("author"), "reviewer") or "reviewer",
                    "created": _str(raw.get("created")) or _now(),
                    "status": status,
                    "batch": _str(raw.get("batch")) or None,
                    "replies": replies,
                }
            )
        with self._guard():
            data = self.load()
            if replace:
                # A REPLACE discards the previous review entirely — so also drop the prior
                # drafts (a stale composer draft must not reopen over a fresh seed) and the
                # last_submit marker (it belonged to the discarded thread). Otherwise the SPA
                # would restore an old draft and the launcher could see a stale submit.
                data["comments"] = []
                data["drafts"] = {}
                data.pop("last_submit", None)
            existing_ids = {c.get("id") for c in data["comments"]}
            for rec in imported:
                if rec["id"] in existing_ids:
                    continue
                data["comments"].append(rec)
                existing_ids.add(rec["id"])
            self._write(data)
        return {"imported": len(imported)}
