"""HTTP server for ``review spec-web <spec.md>`` (stdlib only, no deps).

A ``ThreadingHTTPServer`` that renders a spec server-side and serves an interactive
reviewer UI: select text -> ask a question / leave a comment, accumulate a pending batch,
submit the review, answer inline. Reads are open; writes are origin-guarded.

Routes
------
GET  /                         -> the reviewer SPA shell (spec rendered server-side into it)
GET  /static/<file>            -> app.css / app.js (allowlisted)
GET  /asset/<name>             -> a figure referenced by the spec (served from disk)
GET  /api/health               -> {ok, spec_path, store_path, allowed_origins, ...}
GET  /api/spec                 -> {html, headings, title}
GET  /api/comments             -> [comment, ...]
GET  /api/drafts               -> {slot: draft, ...} (restore in-progress composer text)
POST /api/comments             -> create a comment (enters the pending batch)
POST /api/comments/<id>/reply  -> thread an inline answer
POST /api/comments/<id>/edit   -> edit a comment's body (and optionally its kind)
POST /api/comments/<id>/status -> {status: pending|submitted|answered|resolved}
POST /api/comments/<id>/delete -> remove a comment
POST /api/drafts/<slot>        -> autosave an in-progress composer draft (debounced client)
POST /api/submit               -> flip the pending batch to submitted; deliver to the agent
POST /api/import               -> seed/import an initial review thread (JSON payload)

On SUBMIT the server signals the launching ``review spec-web`` process (a threading.Event
on the server) so the structured review is handed back to the agent that started it (see
``run_specweb``); the store stays the single source of truth on disk.

Security
--------
Binds 127.0.0.1 by default; ``--host 0.0.0.0`` (or a Tailscale IP) exposes it for remote
review over Tailscale. READS are open (anyone who can reach the port can read the spec and
comments — there are no secrets here). WRITES are origin-guarded with the dashboard
pattern (Host allowlist + Origin/Referer check + Content-Type guard + body-size cap), but
the allowlist INCLUDES the configured Tailscale host so phone/remote review can post.
Extra allowed hosts can be added via ``$REVIEW_SPECWEB_ALLOWED_HOSTS`` (comma-separated).
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import registry
from . import render as srender
from .store import DEFAULT_KIND, NEW_DRAFT_SLOT, SpecStore, _as_int, _as_token, edit_draft_slot, store_dir

from .service import DEFAULT_SPECWEB_HOST, DEFAULT_SPECWEB_PORT

STATIC_DIR = Path(__file__).resolve().parent / "static"
_ALLOWED_STATIC = {"app.js", "app.css"}
_CONTENT_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}

# Max request body for a write. Comments/answers are free text; 256 KB is generous and
# caps a runaway/malicious POST before it is read into memory.
_MAX_WRITE_BODY_BYTES = 256 * 1024

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"}

# Tailscale identity is DISCOVERED at runtime (this is a packaged CLI used on many
# machines — a personal host must never be compiled in). Discovery is best-effort and
# cached: `tailscale status --json` -> this node's DNSName + TailscaleIPs. Falls back to
# nothing if tailscale isn't installed/up. Add hosts explicitly via the env var.
_tailscale_cache: set[str] | None = None


def _discover_tailscale_hosts() -> set[str]:
    """Best-effort: this machine's own Tailscale DNS name + IPs (cached). Empty if absent."""
    global _tailscale_cache
    if _tailscale_cache is not None:
        return _tailscale_cache
    hosts: set[str] = set()
    try:
        import shutil
        import subprocess

        exe = shutil.which("tailscale")
        if exe:
            out = subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=5)
            if out.returncode == 0 and out.stdout.strip():
                data = json.loads(out.stdout)
                me = (data or {}).get("Self") or {}
                dns = (me.get("DNSName") or "").strip(".").lower()
                if dns:
                    hosts.add(dns)
                for ip in me.get("TailscaleIPs") or []:
                    if isinstance(ip, str) and ip.strip():
                        hosts.add(ip.strip().lower())
    except Exception:  # noqa: BLE001 — discovery must never break serving
        hosts = set()
    _tailscale_cache = hosts
    return hosts


def _extra_allowed_hosts() -> set[str]:
    raw = os.environ.get("REVIEW_SPECWEB_ALLOWED_HOSTS", "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def allowed_hosts() -> set[str]:
    """The full allowed-host set for write-origin checks: loopback + discovered Tailscale
    + configured extras. (A genuine same-origin write — Origin host == the served Host —
    is also accepted regardless of this set; see SpecWebHandler._origin_allowed.)"""
    return _LOOPBACK_HOSTS | _discover_tailscale_hosts() | _extra_allowed_hosts()


@dataclass
class _SpecContext:
    """Everything a request needs to serve ONE spec, resolved per request.

    Single-spec mode (``make_server`` / ``run_specweb``): ``name`` is ``""`` and ``base`` is
    ``""`` — the spec lives at the server root (``/``, ``/api/...``, ``/asset/...``). Daemon
    mode (``make_daemon_server``): ``name`` is the registered name and ``base`` is
    ``/spec/<name>`` — every URL the SPA builds (API fetches, figure ``src``) is prefixed with
    it so the ONE shared origin routes to the right spec.
    """

    name: str
    spec_path: Path
    store: SpecStore
    base: str  # "" (single-spec) or "/spec/<name>" (daemon)

    @property
    def asset_base(self) -> str:
        return self.base + "/asset/"


def _split_spec_path(path: str) -> tuple[str, str] | None:
    """Split a daemon URL ``/spec/<name>[/<sub>]`` into ``(name, sub)``.

    ``sub`` always starts with ``/`` and is ``/`` for the bare spec page (so it maps to the
    SPA index). Returns None for anything not under ``/spec/`` (a daemon-level route). The
    name is percent-decoded (registered names are plain slugs, but decode defensively).
    """
    prefix = "/spec/"
    if not path.startswith(prefix):
        return None
    rest = path[len(prefix):]
    if not rest:
        return None
    if "/" in rest:
        name, sub = rest.split("/", 1)
        return unquote(name), "/" + sub
    return unquote(rest), "/"


def _spec_mtime(spec_path: Path) -> float | None:
    """The spec file's mtime, or None if it's missing (used by the SSE change-watcher)."""
    try:
        return spec_path.stat().st_mtime
    except OSError:
        return None


class SpecWebHandler(BaseHTTPRequestHandler):
    server_version = "review-specweb/1.0"

    # Set at the START of each request (do_GET/do_POST) to the resolved per-spec context, so the
    # per-spec handlers below are mode-agnostic — they read ``self._ctx`` instead of the server's
    # single bound spec.
    _ctx: "_SpecContext | None" = None

    # ---- per-request spec context ---------------------------------------- #
    @property
    def _spec_path(self) -> Path:
        return self._ctx.spec_path  # type: ignore[union-attr]

    @property
    def _store(self) -> SpecStore:
        return self._ctx.store  # type: ignore[union-attr]

    def _single_context(self) -> "_SpecContext":
        """The context for single-spec mode: the one spec bound on the server at ``base=""``."""
        return _SpecContext(
            name="",
            spec_path=self.server.spec_path,  # type: ignore[attr-defined]
            store=self.server.store,  # type: ignore[attr-defined]
            base="",
        )

    def _context_for_name(self, name: str) -> "_SpecContext | None":
        """Resolve a registered spec name to a context, or None if it isn't registered.

        The registry is the durable name->path map; the store is keyed per absolute path (the
        same store the single-spec server and the ``reply`` CLI use), so comments/drafts/replies
        are shared regardless of how the spec is reached."""
        spec_path = registry.resolve(name)
        if spec_path is None:
            return None
        resolved = spec_path.expanduser().resolve()
        return _SpecContext(
            name=name,
            spec_path=resolved,
            store=SpecStore(resolved),
            base=f"/spec/{name}",
        )

    def _asset_cache(self) -> dict:
        """The server's spec-name -> {figure: disk-path} cache (populated on /api/spec)."""
        cache = getattr(self.server, "_asset_cache", None)
        if cache is None:
            cache = {}
            self.server._asset_cache = cache  # type: ignore[attr-defined]
        return cache

    def _notify_submit(self, review: dict) -> None:
        """Wake the launching process: a non-empty Submit happened. Enqueues THE REVIEW
        SNAPSHOT taken at submit time (``submit_pending()['review']``) on the server's submit
        QUEUE so ``run_specweb`` (draining it) hands exactly that batch's payload to the
        agent. A QUEUE carrying the snapshot — not a bare Event, and not a re-read of the
        store — so two rapid submits each deliver THEIR OWN marker-framed payload (the
        documented "each later submit re-emits" contract): an Event would coalesce them, and
        re-reading the store on drain could emit a later batch's state for an earlier submit.
        Best-effort — a missing queue (a test driving make_server with no watcher) is a no-op,
        never a 500."""
        q = getattr(self.server, "submit_queue", None)
        if q is not None:
            q.put(review)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    # ---- response helpers ------------------------------------------------- #
    def _send_json(self, obj, status: int = 200, *, extra_headers: dict | None = None) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, content_type: str, status: int = 200, *, extra_headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, msg: str) -> None:
        self._send_json({"error": msg}, status=status)

    # ---- origin / host guards (writes only) ------------------------------- #
    @staticmethod
    def _origin_hostname(value: str) -> str | None:
        try:
            parsed = urlparse(value.strip())
        except ValueError:
            return None
        if not parsed.scheme or not parsed.netloc:
            return None
        host = parsed.hostname
        return host.lower() if host else None

    def _host_hostname(self) -> str | None:
        """The bare hostname from this request's Host header (port + IPv6 brackets stripped)."""
        host = (self.headers.get("Host") or "").strip()
        if not host:
            return None
        if host.startswith("["):
            return host[1:].split("]", 1)[0].lower()
        return (host.rsplit(":", 1)[0] if host.count(":") == 1 else host).lower()

    def _origin_allowed(self) -> bool:
        """CSRF + anti-DNS-rebinding defence for WRITES.

        TWO conditions must hold:
          1. The request's OWN Host header host must be in the allowlist (loopback +
             discovered Tailscale + $REVIEW_SPECWEB_ALLOWED_HOSTS). This blocks DNS
             REBINDING: on a predictable port a malicious page can serve from its own DNS
             name, rebind it to 127.0.0.1 / the Tailscale IP, then POST same-origin — but
             that rebound attacker hostname is NOT in the allowlist, so the write is
             refused. (Accepting any Host that merely equals the Origin would let the
             rebound origin through, which is the bug this guards.)
          2. The Origin (or, absent it, Referer) host must MATCH that Host host — the
             classic CSRF check: a cross-site page carries its own site in Origin.
        A request with NEITHER Origin nor Referer is a non-browser / same-origin call
        (curl, the import CLI) and passes the CSRF half; it still needs an allowed Host.
        Combined with the Content-Type guard, a foreign simple-request form post can't send
        JSON anyway.
        """
        allowed = allowed_hosts()
        host_self = self._host_hostname()
        # (1) anti-rebinding: the Host we were reached at must be allowlisted. A missing Host
        # (HTTP/1.0 / curl without -H) is a non-browser caller, not a rebinding vector.
        if host_self is not None and host_self not in allowed:
            return False

        def csrf_ok(value: str) -> bool:
            h = self._origin_hostname(value)
            return h is not None and h == host_self

        # (2) CSRF: Origin/Referer must match the (now allowlisted) Host.
        origin = (self.headers.get("Origin") or "").strip()
        if origin:
            return csrf_ok(origin)
        referer = (self.headers.get("Referer") or "").strip()
        if referer:
            return csrf_ok(referer)
        return True

    def _content_type_is_json(self) -> bool:
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        return ctype == "application/json"

    def _read_write_body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (ValueError, TypeError):
            length = 0
        if length > _MAX_WRITE_BODY_BYTES:
            self._error(413, f"request body too large (max {_MAX_WRITE_BODY_BYTES} bytes)")
            return None
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if len(raw) > _MAX_WRITE_BODY_BYTES:
            self._error(413, f"request body too large (max {_MAX_WRITE_BODY_BYTES} bytes)")
            return None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._error(400, "invalid JSON body")
            return None
        if not isinstance(data, dict):
            self._error(400, "JSON body must be an object")
            return None
        return data

    # ---- routing: GET ----------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            # Static assets (app.js / app.css) are SHARED across specs, served at /static in
            # BOTH modes (single-spec and daemon), so the SPA can load them with an absolute
            # path regardless of which /spec/<name> page it was served from.
            if path.startswith("/static/"):
                return self._serve_static(path[len("/static/"):])
            if getattr(self.server, "multi_spec", False):
                return self._route_daemon_get(path)
            self._ctx = self._single_context()
            return self._route_spec_get(path)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001 — never crash the server thread
            self._error(500, f"{type(exc).__name__}: {exc}")

    def _route_daemon_get(self, path: str) -> None:
        """Daemon-level GET routing: navigator at /, health, else resolve /spec/<name>."""
        if path in ("/", "/index.html"):
            return self._serve_navigator()
        if path == "/api/health":
            return self._send_json(self._daemon_health())
        if path == "/favicon.ico":
            return self._error(404, "no favicon")
        spec = _split_spec_path(path)
        if spec is None:
            return self._error(404, f"not found: {path}")
        name, sub = spec
        ctx = self._context_for_name(name)
        if ctx is None:
            return self._error(
                404, f"unknown spec: {name!r} — register it with `review spec-web add <path>`"
            )
        self._ctx = ctx
        return self._route_spec_get(sub)

    def _route_spec_get(self, path: str) -> None:
        """Per-spec GET routing (mode-agnostic; ``self._ctx`` is already resolved)."""
        if path in ("/", "/index.html"):
            return self._serve_index()
        if path.startswith("/asset/"):
            return self._serve_asset(path[len("/asset/"):])
        if path == "/api/events":
            return self._serve_events()
        if path == "/api/health":
            return self._send_json(
                {
                    "ok": True,
                    "spec_path": str(self._spec_path),
                    "store_path": str(self._store.path),
                    "store_dir": str(store_dir()),
                    "allowed_origins": sorted(allowed_hosts()),
                    "version": self.server_version,
                }
            )
        if path == "/api/spec":
            return self._serve_spec_json()
        if path == "/api/comments":
            return self._send_json(self._store.all_comments())
        if path == "/api/drafts":
            # The seed header lets the client start its per-session write counter above
            # the durable high-water-mark, so a reloaded session's autosaves aren't
            # rejected as stale against old tombstones (review-cli#30). The body shape
            # (slot -> draft map) is unchanged.
            return self._send_json(
                self._store.all_drafts(),
                extra_headers={"X-Draft-Token-Seed": str(self._store.max_draft_token())},
            )
        return self._error(404, f"not found: {path}")

    # ---- routing: POST ---------------------------------------------------- #
    def do_POST(self) -> None:  # noqa: N802
        # Write guards: foreign Origin -> 403; non-JSON -> 415. Together with the
        # body-size cap this stops a foreign page mutating the local comment store while
        # still allowing loopback + the configured Tailscale host.
        if not self._origin_allowed():
            return self._error(403, "forbidden: cross-origin write blocked (allowed: loopback + Tailscale host)")
        if not self._content_type_is_json():
            return self._error(415, "unsupported media type: Content-Type must be application/json")
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if getattr(self.server, "multi_spec", False):
                spec = _split_spec_path(path)
                if spec is None:
                    return self._error(404, f"not found: {path}")
                name, sub = spec
                ctx = self._context_for_name(name)
                if ctx is None:
                    return self._error(
                        404,
                        f"unknown spec: {name!r} — register it with `review spec-web add <path>`",
                    )
                self._ctx = ctx
                target = sub
            else:
                self._ctx = self._single_context()
                target = path
            body = self._read_write_body()
            if body is None:
                return  # error already sent
            return self._route_spec_post(target, body)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            self._error(500, f"{type(exc).__name__}: {exc}")

    def _route_spec_post(self, path: str, body: dict) -> None:
        """Per-spec POST routing (mode-agnostic; ``self._ctx`` is already resolved)."""
        try:
            if path == "/api/comments":
                return self._create_comment(body)
            if path == "/api/submit":
                result = self._store.submit_pending()
                # Signal the launching process so it can hand the structured review to the
                # agent. Only a non-empty submit (count>0) is a real handoff; an empty
                # submit (no pending notes) must not wake the agent with nothing. Enqueue the
                # review SNAPSHOT from this submit so the watcher emits exactly this batch.
                delivery = None
                if result.get("count"):
                    self._notify_submit(result.get("review") or {})
                    delivery = self._deliver_submit(result)
                return self._send_json({"ok": True, "delivery": delivery, **result})
            if path == "/api/import":
                try:
                    result = self._store.import_thread(body, replace=bool(body.get("replace")))
                except ValueError as exc:
                    return self._error(400, str(exc))
                return self._send_json({"ok": True, **result})
            if path.startswith("/api/drafts/"):
                slot = unquote(path[len("/api/drafts/"):])
                return self._save_draft(slot, body)
            if path.startswith("/api/comments/"):
                rest = path[len("/api/comments/"):]
                if "/" not in rest:
                    return self._error(404, "expected /api/comments/<id>/<action>")
                cid, action = rest.split("/", 1)
                return self._comment_action(cid, action, body)
            return self._error(404, f"not found: {path}")
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            self._error(500, f"{type(exc).__name__}: {exc}")

    def _owning_agent(self) -> str | None:
        """The agent session that owns THIS spec's reviews: the spec's registry ``agent``
        (daemon mode, set by ``add/serve --agent``) falling back to the server-wide default
        (the required ``--agent`` on ``start``/``run``/the legacy single-spec server)."""
        agent = None
        ctx = self._ctx
        if ctx is not None and ctx.name:
            rec = registry.load_registry()["specs"].get(ctx.name) or {}
            agent = rec.get("agent")
        return agent or getattr(self.server, "default_agent", None)

    def _deliver_submit(self, result: dict) -> dict:
        """Push a non-empty submitted batch into the owning agent's tmux pane (see deliver.py).

        Best-effort: the review is already durably in the store, so a failed delivery is
        LOGGED (daemon log) + reported in the response, never a 500. The tg-ctl-style
        injection is what makes a submit actually REACH someone — before this, batches sat
        in the store until a watcher happened to poll (i.e. usually never)."""
        from . import deliver

        ctx = self._ctx
        agent = self._owning_agent()
        if not agent:
            return {"agent": None, "delivered": False, "detail": "no owning agent configured"}
        ok, detail = deliver.deliver_review(
            agent=agent,
            spec_name=ctx.name or ctx.spec_path.name,  # type: ignore[union-attr]
            spec_path=ctx.spec_path,  # type: ignore[union-attr]
            review=result.get("review") or {},
            batch=result.get("batch"),
        )
        print(
            f"[review spec-web] submit delivery -> agent '{agent}': "
            f"{'ok' if ok else 'FAILED'} ({detail})",
            flush=True,
        )
        return {"agent": agent, "delivered": ok, "detail": detail}

    # ---- POST handlers ---------------------------------------------------- #
    def _create_comment(self, body: dict) -> None:
        quote = body.get("quote", "")
        text = body.get("body", "")
        if not isinstance(text, str) or not text.strip():
            return self._error(400, "comment 'body' is required")
        rec = self._store.add_comment(
            quote=quote if isinstance(quote, str) else "",
            body=text,
            section_id=body.get("section_id", "") if isinstance(body.get("section_id"), str) else "",
            section_title=body.get("section_title", "") if isinstance(body.get("section_title"), str) else "",
            start=_as_int(body.get("start")),
            end=_as_int(body.get("end")),
            kind=body.get("kind") if isinstance(body.get("kind"), str) else DEFAULT_KIND,
            # Single implicit reviewer: the UI client doesn't expose an author field, so a
            # normal create omits it and defaults to "reviewer". An explicit author (e.g. an
            # import/seed payload) is still honoured so those round-trip.
            author=body.get("author", "reviewer") if isinstance(body.get("author"), str) else "reviewer",
        )
        # The note is now persisted as a comment — clear the new-note composer draft so a
        # stale draft never restores over a note the reviewer already saved.
        self._store.delete_draft(NEW_DRAFT_SLOT)
        self._send_json({"ok": True, "comment": rec}, status=201)

    def _save_draft(self, slot: str, body: dict) -> None:
        """Autosave an in-progress composer draft (the debounced client write). An EMPTY
        body clears the slot (returns {ok, draft: null}); a non-empty one stores it. The
        slot id comes from the URL (``new`` or ``edit:<id>``) and is required.

        ORDERING (review-cli#30): an optional ``token`` (the client's monotonic per-session
        send sequence) makes the write order-safe. The store rejects a token <= the slot's
        last applied one; on rejection the response carries ``stale: true`` and the CURRENT
        slot draft, so a late autosave that lost the race can't clobber a newer write and the
        client mirror stays authoritative. A token-less write keeps legacy behaviour."""
        slot = (slot or "").strip()
        if not slot:
            return self._error(404, "expected /api/drafts/<slot>")
        text = body.get("body", "")
        if not isinstance(text, str):
            return self._error(400, "draft 'body' must be a string")
        try:
            result = self._store.save_draft_result(
                slot,
                body=text,
                kind=body.get("kind") if isinstance(body.get("kind"), str) else DEFAULT_KIND,
                quote=body.get("quote", "") if isinstance(body.get("quote"), str) else "",
                section_id=body.get("section_id", "") if isinstance(body.get("section_id"), str) else "",
                section_title=body.get("section_title", "") if isinstance(body.get("section_title"), str) else "",
                start=_as_int(body.get("start")),
                end=_as_int(body.get("end")),
                token=_as_token(body.get("token")),
            )
        except ValueError as exc:
            return self._error(400, str(exc))
        # `applied` is decided inside the store lock, so `stale` needs no second read (no
        # TOCTOU). A rejected stale write returns the authoritative current slot state, not
        # the client's own body — its mirror stays correct. An empty/cleared slot -> {} ->
        # report null so the client knows there is nothing to restore. The seed header rides
        # EVERY write response so the client re-seeds its counter from the server's current
        # high-water-mark — a counter that fell behind (lost boot-seed, a second tab, a token
        # collision) self-heals on the next write instead of silently rejecting forever (#30).
        return self._send_json(
            {"ok": True, "draft": result.draft or None, "stale": not result.applied},
            extra_headers={"X-Draft-Token-Seed": str(self._store.max_draft_token())},
        )

    def _comment_action(self, cid: str, action: str, body: dict) -> None:
        if action == "reply":
            text = body.get("body", "")
            if not isinstance(text, str) or not text.strip():
                return self._error(400, "reply 'body' is required")
            rec = self._store.add_reply(
                cid,
                body=text,
                author=body.get("author", "reviewer") if isinstance(body.get("author"), str) else "reviewer",
            )
            if rec is None:
                return self._error(404, f"unknown comment {cid}")
            return self._send_json({"ok": True, "comment": rec})
        if action == "edit":
            text = body.get("body", "")
            if not isinstance(text, str) or not text.strip():
                return self._error(400, "comment 'body' is required")
            kind = body.get("kind") if isinstance(body.get("kind"), str) else None
            try:
                rec = self._store.edit_comment(cid, body=text, kind=kind)
            except ValueError as exc:
                return self._error(400, str(exc))
            if rec is None:
                return self._error(404, f"unknown comment {cid}")
            # The edit is persisted — clear that comment's edit-in-progress draft.
            self._store.delete_draft(edit_draft_slot(cid))
            return self._send_json({"ok": True, "comment": rec})
        if action == "status":
            status = body.get("status", "")
            try:
                rec = self._store.set_status(cid, status if isinstance(status, str) else "")
            except ValueError as exc:
                return self._error(400, str(exc))
            if rec is None:
                return self._error(404, f"unknown comment {cid}")
            return self._send_json({"ok": True, "comment": rec})
        if action == "delete":
            ok = self._store.delete_comment(cid)
            if not ok:
                return self._error(404, f"unknown comment {cid}")
            return self._send_json({"ok": True, "deleted": cid})
        return self._error(404, f"unknown action: {action}")

    # ---- static / spec / assets ------------------------------------------ #
    def _serve_index(self) -> None:
        try:
            html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        except OSError:
            return self._error(500, "index.html missing")
        title = self._spec_path.name
        # {{BASE}} is the URL prefix the SPA prepends to every API fetch: "" for single-spec,
        # "/spec/<name>" under the daemon (so the ONE origin routes to the right spec). Escaped
        # as a JS string below in index.html.
        html = html.replace("{{SPEC_TITLE}}", _esc_attr(title)).replace(
            "{{BASE}}", _esc_attr(self._ctx.base)  # type: ignore[union-attr]
        )
        self._send_bytes(html.encode("utf-8"), "text/html; charset=utf-8")

    def _serve_static(self, name: str) -> None:
        if name not in _ALLOWED_STATIC:
            return self._error(404, f"static asset not allowed: {name}")
        p = STATIC_DIR / name
        try:
            body = p.read_bytes()
        except OSError:
            return self._error(404, f"static asset missing: {name}")
        ctype = _CONTENT_TYPES.get(p.suffix, "application/octet-stream")
        self._send_bytes(body, ctype)

    def _serve_spec_json(self) -> None:
        ctx = self._ctx
        result = srender.render_spec(ctx.spec_path, asset_base=ctx.asset_base)  # type: ignore[union-attr]
        # Cache the asset map per spec-name so /asset/<name> can validate against it (one
        # cache serves both modes; the single-spec name is "").
        self._asset_cache()[ctx.name] = result.assets  # type: ignore[union-attr]
        self._send_json(
            {
                "title": ctx.spec_path.name,  # type: ignore[union-attr]
                "html": result.html,
                "mtime": _spec_mtime(ctx.spec_path),  # type: ignore[union-attr]
                "headings": [{"level": lv, "text": t, "id": hid} for (lv, t, hid) in result.headings],
            }
        )

    def _serve_asset(self, name: str) -> None:
        """Serve a figure the spec references, by basename, from the spec's assets dir.

        The URL path arrives percent-encoded (render emits e.g. /asset/my%20diagram.svg),
        so decode it BEFORE the basename guard / disk lookup, else a figure with a space or
        other escaped char would 404. No path traversal: only a bare basename is honored
        (after decode), resolved under the assets dir, and the resolved path must stay
        inside it. We render the spec once if the asset map isn't populated yet (a direct
        /asset hit before /api/spec)."""
        decoded = unquote(name)
        fname = Path(decoded).name
        # Reject anything that isn't a bare basename after decode (no slashes / traversal).
        if fname != decoded or not fname or fname in (".", ".."):
            return self._error(404, f"asset not allowed: {name}")
        ctx = self._ctx
        cache = self._asset_cache()
        assets = cache.get(ctx.name)  # type: ignore[union-attr]
        if not assets:
            assets = srender.render_spec(ctx.spec_path, asset_base=ctx.asset_base).assets  # type: ignore[union-attr]
            cache[ctx.name] = assets  # type: ignore[union-attr]
        # Serve ONLY figures the markdown actually references (in the renderer's asset map),
        # never an arbitrary basename from the assets dir — otherwise a reachable reviewer
        # (especially over Tailscale) could download any unrelated file sitting in that dir.
        disk = assets.get(fname)
        if disk is None:
            return self._error(404, f"asset not referenced by spec: {name}")
        # Defence in depth for UNTRUSTED specs against symlink escapes (read_bytes follows
        # links). TWO independent containment checks must BOTH hold, or a single symlink
        # defeats the other:
        #   (1) the followed asset must stay under the followed assets dir — blocks
        #       `assets/leak.svg -> ../.env` or `-> /etc/passwd`;
        #   (2) the followed assets dir must stay under the followed spec dir — blocks the
        #       `assets` directory ITSELF being a symlink to e.g. /etc.
        spec_root = self._spec_path.parent.resolve()
        assets_dir = (self._spec_path.parent / "assets").resolve()
        real = Path(disk).resolve()
        under = lambda child, parent: child == parent or parent in child.parents  # noqa: E731
        if not (under(real, assets_dir) and under(assets_dir, spec_root)):
            return self._error(404, f"asset not allowed: {name}")
        # The referenced NAME has an image extension, but a symlink could point it at a
        # NON-image file inside the assets dir (e.g. `leak.svg -> private-notes.txt`),
        # bypassing the figures-only guard. Require the RESOLVED target to also be an image
        # type, so a referenced image name can only ever serve a real image.
        real_ext = real.suffix.lower().lstrip(".")
        if real_ext not in srender.IMAGE_MIME_TYPES:
            return self._error(404, f"asset not an image: {name}")
        try:
            data = real.read_bytes()
        except OSError:
            return self._error(404, f"asset missing: {name}")
        ctype = _asset_content_type(fname)
        # An SVG from an UNTRUSTED spec's assets dir can carry inline <script>. As an <img>
        # source it is already inert, but a markdown link [open](/asset/evil.svg) navigated
        # to TOP-LEVEL would run that script in the spec-web origin (and could hit the write
        # APIs). Serve SVG with a `sandbox` CSP (kills scripts in the resource) + nosniff +
        # an attachment disposition, so a direct open is inert/downloaded while <img>
        # rendering is unaffected. Other image types can't carry script — no extra headers.
        extra = None
        if ctype == "image/svg+xml":
            extra = {
                "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": "inline",
            }
        self._send_bytes(data, ctype, extra_headers=extra)

    # ---- daemon: navigator / health / SSE -------------------------------- #
    def _spec_counts(self, spec_path: Path) -> dict:
        """Comment tally for a spec (for the navigator + daemon health). Never raises: a spec
        whose store file is unreadable reads as all-zero."""
        counts = {"total": 0, "pending": 0, "submitted": 0, "answered": 0, "resolved": 0,
                  "questions": 0, "remarks": 0, "open": 0}
        try:
            comments = SpecStore(spec_path).all_comments()
        except Exception:  # noqa: BLE001 — a bad store must not break the navigator
            return counts
        for c in comments:
            counts["total"] += 1
            status = c.get("status") if c.get("status") in counts else None
            if status:
                counts[status] += 1
            if c.get("kind") == "question":
                counts["questions"] += 1
            else:
                counts["remarks"] += 1
            # "open" = a note still needing attention (pending or submitted, not answered/resolved).
            if c.get("status") in ("pending", "submitted"):
                counts["open"] += 1
        return counts

    def _spec_summaries(self) -> list[dict]:
        """Every registered spec with its URL + comment counts (navigator + health payload)."""
        out: list[dict] = []
        for rec in registry.list_specs():
            out.append(
                {
                    "name": rec["name"],
                    "path": rec["path"],
                    "exists": rec["exists"],
                    "url": f"/spec/{rec['name']}",
                    "counts": self._spec_counts(Path(rec["path"])),
                }
            )
        return out

    def _daemon_health(self) -> dict:
        return {
            "ok": True,
            "mode": "daemon",
            "version": self.server_version,
            "store_dir": str(store_dir()),
            "allowed_origins": sorted(allowed_hosts()),
            "specs": self._spec_summaries(),
        }

    def _serve_navigator(self) -> None:
        """The daemon root: a self-contained HTML index of every registered spec.

        Server-rendered plain HTML (no SPA) so the navigator loads instantly and needs no
        packaged asset. Each spec is a card linking to ``/spec/<name>`` with its open-comment
        count; a spec whose file has gone missing is flagged rather than hidden."""
        specs = self._spec_summaries()
        cards: list[str] = []
        for s in specs:
            counts = s["counts"]
            badge = ""
            if counts["open"]:
                badge = f'<span class="nav-badge nav-open">{counts["open"]} open</span>'
            elif counts["total"]:
                badge = f'<span class="nav-badge">{counts["total"]} note(s)</span>'
            missing = "" if s["exists"] else '<span class="nav-badge nav-missing">file missing</span>'
            cards.append(
                '<a class="nav-card" href="{url}">'
                '<div class="nav-name">{name}</div>'
                '<div class="nav-path">{path}</div>'
                '<div class="nav-meta">{badge}{missing}</div>'
                "</a>".format(
                    url=_esc_attr(s["url"]),
                    name=_esc_html(s["name"]),
                    path=_esc_html(s["path"]),
                    badge=badge,
                    missing=missing,
                )
            )
        body = (
            "".join(cards)
            if cards
            else '<div class="nav-empty">No specs registered yet. Add one with '
            "<code>review spec-web add &lt;path/to/spec.md&gt;</code>.</div>"
        )
        html = _NAVIGATOR_HTML.replace("{{CARDS}}", body).replace("{{COUNT}}", str(len(specs)))
        self._send_bytes(html.encode("utf-8"), "text/html; charset=utf-8")

    def _serve_events(self) -> None:
        """Server-Sent Events stream of spec-file changes for live reload (one per connection).

        Polls the spec's mtime once a second; on a change it emits ``event: spec-changed`` so
        the SPA re-fetches ``/api/spec`` and swaps the content in place (no full reload, no
        scroll jump — the no-jump + highlight logic lives client-side in app.js). Heartbeat
        comments in between let the client/proxies detect a dropped connection. Runs on its own
        request thread (ThreadingHTTPServer); ``daemon_threads`` lets the process exit even with
        a stream open, and ``server._sse_stop`` ends the loop on a clean shutdown."""
        ctx = self._ctx
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        last = _spec_mtime(ctx.spec_path)  # type: ignore[union-attr]
        if not self._sse_send("hello", {"mtime": last}):
            return
        # Poll interval is a server attribute so tests can shrink it (default 1s is plenty
        # responsive for a human editing a file, and keeps the per-connection wakeups cheap).
        poll = getattr(self.server, "sse_poll_seconds", 1.0)
        while not getattr(self.server, "_sse_stop", False):
            time.sleep(poll)
            cur = _spec_mtime(ctx.spec_path)  # type: ignore[union-attr]
            if cur != last:
                last = cur
                if not self._sse_send("spec-changed", {"mtime": cur}):
                    return
            else:
                try:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, OSError):
                    return

    def _sse_send(self, event: str, data: dict) -> bool:
        """Write one SSE frame; return False if the client has gone (so the loop can stop)."""
        frame = f"event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8")
        try:
            self.wfile.write(frame)
            self.wfile.flush()
            return True
        except (BrokenPipeError, OSError):
            return False


def _esc_html(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _esc_attr(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _asset_content_type(fname: str) -> str:
    # Same canonical map render uses to ACCEPT figures, so every served figure gets a
    # correct image MIME (no octet-stream fallthrough for accepted types).
    ext = fname.lower().rsplit(".", 1)[-1] if "." in fname else ""
    return srender.IMAGE_MIME_TYPES.get(ext, "application/octet-stream")


# The daemon navigator (root ``/``): a self-contained HTML index of registered specs. Inline
# CSS so it needs no packaged asset and renders instantly on a phone. ``{{CARDS}}`` and
# ``{{COUNT}}`` are filled server-side (both already HTML-escaped).
_NAVIGATOR_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Spec review — Navigator</title>
<style>
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       background: #0f1115; color: #e6e8eb; }
.wrap { max-width: 820px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 20px; margin: 8px 0 4px; }
.sub { color: #8a92a6; margin: 0 0 20px; font-size: 13px; }
.nav-card { display: block; text-decoration: none; color: inherit; background: #1a1d24;
            border: 1px solid #262b36; border-radius: 12px; padding: 14px 16px; margin: 0 0 12px;
            transition: border-color .12s, transform .12s; }
.nav-card:hover { border-color: #3d76f5; transform: translateY(-1px); }
.nav-name { font-weight: 600; font-size: 16px; color: #dfe6ff; }
.nav-path { color: #6f7789; font-size: 12px; margin-top: 2px; word-break: break-all; }
.nav-meta { margin-top: 8px; display: flex; gap: 8px; flex-wrap: wrap; }
.nav-badge { font-size: 12px; padding: 2px 8px; border-radius: 999px; background: #262b36; color: #aab3c5; }
.nav-open { background: #24344f; color: #79a6ff; }
.nav-missing { background: #4a2530; color: #ff8a9c; }
.nav-empty { color: #8a92a6; background: #1a1d24; border: 1px dashed #363c4a; border-radius: 12px;
             padding: 24px; text-align: center; }
code { background: #262b36; padding: 1px 6px; border-radius: 6px; font-size: 12px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Spec review</h1>
  <p class="sub">{{COUNT}} spec(s) registered · one daemon, name-based URLs</p>
  {{CARDS}}
</div>
</body>
</html>
"""


def make_server(
    spec_path: Path | str, *, host: str = "127.0.0.1", port: int = 0, verbose: bool = False,
    agent: str | None = None,
) -> ThreadingHTTPServer:
    """Create (but do not serve) the spec-web server bound to ``host:port``.

    port=0 picks a free ephemeral port (used by tests). The bound port is on
    ``server.server_address[1]``. ``agent`` is the owning agent session submitted batches
    are DELIVERED to (tmux injection, see deliver.py); None keeps the stdout-watcher-only
    legacy handoff (tests / direct make_server callers).
    """
    spec_path = Path(spec_path).expanduser().resolve()
    httpd = ThreadingHTTPServer((host, port), SpecWebHandler)
    httpd.multi_spec = False  # type: ignore[attr-defined]
    httpd.default_agent = agent  # type: ignore[attr-defined]
    httpd.spec_path = spec_path  # type: ignore[attr-defined]
    httpd.store = SpecStore(spec_path)  # type: ignore[attr-defined]
    httpd._asset_cache = {}  # type: ignore[attr-defined]  # spec-name -> figure map (name "")
    httpd._sse_stop = False  # type: ignore[attr-defined]  # ends /api/events loops on shutdown
    httpd.verbose = verbose  # type: ignore[attr-defined]
    # Each non-empty POST /api/submit puts its batch timestamp here so the launching process
    # (draining the queue in run_specweb) hands the finalized review back to the agent. A
    # QUEUE, not an Event, so two rapid submits each get delivered (no coalescing). A test
    # that drives make_server with no watcher just leaves items unread — harmless.
    httpd.submit_queue = queue.Queue()  # type: ignore[attr-defined]
    httpd.daemon_threads = True
    return httpd


def make_daemon_server(
    *, host: str = DEFAULT_SPECWEB_HOST, port: int = DEFAULT_SPECWEB_PORT, verbose: bool = False,
    agent: str | None = None,
) -> ThreadingHTTPServer:
    """Create (but do not serve) the MULTI-SPEC daemon bound to ``host:port``.

    One server serves EVERY registered spec by name at ``/spec/<name>`` (navigator at ``/``).
    Unlike ``make_server`` it binds no single spec — each request resolves its spec from the
    URL against the registry. ``port=0`` picks a free ephemeral port (used by tests).
    ``agent`` is the daemon-wide DEFAULT owner submitted batches are delivered to when a
    spec's own registry record has no ``agent`` (see deliver.py).
    """
    httpd = ThreadingHTTPServer((host, port), SpecWebHandler)
    httpd.multi_spec = True  # type: ignore[attr-defined]
    httpd.default_agent = agent  # type: ignore[attr-defined]
    httpd._asset_cache = {}  # type: ignore[attr-defined]  # spec-name -> figure map
    httpd._sse_stop = False  # type: ignore[attr-defined]  # ends /api/events loops on shutdown
    httpd.verbose = verbose  # type: ignore[attr-defined]
    # No submit_queue: the daemon is a separate process from any stdout watcher, so the
    # submit->agent handoff is cross-process via the store's last_submit (watch_submits polls
    # it), not an in-process queue. _notify_submit is a no-op when no queue is present.
    httpd.daemon_threads = True
    return httpd


def _reachable_urls(host: str, port: int) -> list[str]:
    """Best-effort list of URLs the server is reachable at (for the startup banner)."""
    urls: list[str] = []
    if host in ("0.0.0.0", "::"):
        urls.append(f"http://127.0.0.1:{port}/")
        # Surface THIS machine's discovered Tailscale name/IPs (if any) so the printed URL
        # is tappable from the phone — discovered at runtime, never hardcoded.
        for h in sorted(_discover_tailscale_hosts()):
            urls.append(f"http://{h}:{port}/")
    else:
        urls.append(f"http://{host}:{port}/")
    return urls


# Stable markers framing the structured-review JSON emitted on Submit, so the launching
# agent can extract it from the process's stdout regardless of the surrounding banner
# lines. The payload sits on the SINGLE line BETWEEN the markers (compact JSON, no internal
# newlines), so the agent reads exactly the one line after the begin marker — a reviewer's
# free-text body that happens to contain the end-marker substring is JSON-escaped on that
# one line and can never be mistaken for the marker line itself.
SUBMIT_MARKER_BEGIN = "<<<REVIEW-SPEC-WEB-SUBMITTED"
SUBMIT_MARKER_END = "REVIEW-SPEC-WEB-SUBMITTED>>>"


def _emit_submitted_review(review: dict) -> None:
    """Print the structured review between stable markers (machine-readable handoff to the
    launching agent) plus a one-line human summary. The JSON is compact (one line) so the
    framing is unambiguous even when a comment body contains marker-like text."""
    counts = review.get("counts") or {}
    print(
        f"[review spec-web] REVIEW SUBMITTED — {counts.get('questions', 0)} question(s), "
        f"{counts.get('remarks', 0)} remark(s), {counts.get('total', 0)} total.",
        flush=True,
    )
    print(SUBMIT_MARKER_BEGIN, flush=True)
    print(json.dumps(review, sort_keys=True, separators=(",", ":")), flush=True)
    print(SUBMIT_MARKER_END, flush=True)


def run_specweb(
    spec_path: Path | str,
    *,
    host: str = "127.0.0.1",
    port: int | None = None,
    open_browser: bool = False,
    seed: Path | str | None = None,
    verbose: bool = False,
    exit_on_submit: bool = False,
    agent: str | None = None,
) -> int:
    """Blocking entry for ``review spec-web``. Returns a process exit code.

    Submit handoff: a separate watcher thread waits on the server's ``submit_event``; on a
    non-empty Submit it prints the structured review (between SUBMIT_MARKER_* markers) to
    stdout so the launching agent receives it. With ``exit_on_submit`` the server stops
    after the FIRST submit (the blocking call returns); otherwise it keeps serving so the
    reviewer can continue and the agent can ``reply`` — each later submit re-emits.
    """
    spec_path = Path(spec_path).expanduser().resolve()
    if not spec_path.is_file():
        print(f"[review spec-web] spec not found: {spec_path}", flush=True)
        return 1

    if seed is not None:
        seed_path = Path(seed).expanduser()
        try:
            payload = json.loads(seed_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"[review spec-web] cannot read seed {seed_path}: {exc}", flush=True)
            return 1
        # `replace` lives at the payload top level, but `payload` may parse to a non-dict
        # (e.g. `[]`); guard before .get so import_thread can raise the handled ValueError
        # instead of an AttributeError traceback.
        replace = bool(payload.get("replace")) if isinstance(payload, dict) else False
        try:
            result = SpecStore(spec_path).import_thread(payload, replace=replace)
        except ValueError as exc:
            print(f"[review spec-web] bad seed: {exc}", flush=True)
            return 1
        print(f"[review spec-web] seeded {result['imported']} comment(s) from {seed_path}", flush=True)

    # port 0 = let the OS pick a free ephemeral port AT BIND TIME (no probe-then-bind TOCTOU
    # race where another process grabs the probed port in between).
    chosen = port or 0
    try:
        httpd = make_server(spec_path, host=host, port=chosen, verbose=verbose, agent=agent)
    except OSError as exc:
        print(f"[review spec-web] cannot bind {host}:{chosen}: {exc}", flush=True)
        return 1
    bound = httpd.server_address[1]
    store = httpd.store  # type: ignore[attr-defined]
    print(f"[review spec-web] spec:  {spec_path}", flush=True)
    print(f"[review spec-web] store: {store.path}", flush=True)
    for url in _reachable_urls(host, bound):
        print(f"[review spec-web] serving {url}", flush=True)
    if host in ("0.0.0.0", "::"):
        print("[review spec-web] bound to all interfaces — reachable over Tailscale. Writes allowed from loopback + the Tailscale host.", flush=True)
    else:
        print("[review spec-web] loopback-only. Pass --host 0.0.0.0 to expose over Tailscale.", flush=True)
    print("[review spec-web] Ctrl-C to stop.", flush=True)

    if open_browser:
        def _open() -> None:
            import webbrowser

            try:
                webbrowser.open(_reachable_urls(host, bound)[0])
            except Exception:  # noqa: BLE001
                pass

        threading.Timer(0.4, _open).start()

    # Submit watcher: each non-empty Submit enqueues ITS OWN review snapshot; we DRAIN the
    # queue and hand each one to the launching agent (stdout). Two rapid submits each emit a
    # marker-framed payload for their batch — no coalescing, no re-read race. A daemon thread
    # so Ctrl-C still exits cleanly. A sentinel (pushed by the finally block) breaks the
    # blocking get on shutdown.
    submit_queue = httpd.submit_queue  # type: ignore[attr-defined]
    _STOP = object()

    def _watch_submits() -> None:
        while True:
            item = submit_queue.get()
            if item is _STOP:
                return
            _emit_submitted_review(item)
            if exit_on_submit:
                print("[review spec-web] --exit-on-submit: stopping after submit.", flush=True)
                httpd.shutdown()
                return

    watcher = threading.Thread(target=_watch_submits, daemon=True)
    watcher.start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[review spec-web] stopped.", flush=True)
    finally:
        submit_queue.put(_STOP)  # break the watcher's blocking get so it exits
        httpd._sse_stop = True  # type: ignore[attr-defined]  # end any live SSE loops (as run_daemon does)
        httpd.shutdown()
        httpd.server_close()
    return 0


def daemon_spec_urls(name: str, host: str, port: int) -> list[str]:
    """The reachable ``/spec/<name>`` URLs for a registered spec (banner + `add` output)."""
    return [u.rstrip("/") + f"/spec/{name}" for u in _reachable_urls(host, port)]


def run_daemon(
    *,
    host: str = DEFAULT_SPECWEB_HOST,
    port: int | None = None,
    verbose: bool = False,
    agent: str | None = None,
) -> int:
    """Blocking entry for the MULTI-SPEC daemon (``review spec-web __serve`` / ``run``).

    Serves EVERY registered spec by name from ONE port (navigator at ``/``). The registry is
    read per request, so a spec ``add``ed while the daemon runs is served immediately — no
    restart. This is the foreground process the managed service (agenttools_service) detaches
    on ``start``; the pidfile/liveness is the service manager's job, not ours. ``agent`` is
    the daemon-wide default delivery target for submitted batches (the CLI REQUIRES it on
    ``start``/``run``).
    """
    chosen = DEFAULT_SPECWEB_PORT if port is None else port
    try:
        httpd = make_daemon_server(host=host, port=chosen, verbose=verbose, agent=agent)
    except OSError as exc:
        print(f"[review spec-web] cannot bind {host}:{chosen}: {exc}", flush=True)
        return 1
    bound = httpd.server_address[1]
    print(f"[review spec-web] daemon store dir: {store_dir()}", flush=True)
    if agent:
        print(f"[review spec-web] default submit-delivery agent: {agent}", flush=True)
    for url in _reachable_urls(host, bound):
        print(f"[review spec-web] navigator {url}", flush=True)
    specs = registry.list_specs()
    print(f"[review spec-web] serving {len(specs)} registered spec(s) at /spec/<name>.", flush=True)
    for rec in specs:
        print(f"[review spec-web]   - {rec['name']}: {rec['path']}", flush=True)
    if host in ("0.0.0.0", "::"):
        print("[review spec-web] bound to all interfaces — reachable over Tailscale. Writes allowed from loopback + the Tailscale host.", flush=True)
    else:
        print("[review spec-web] loopback-only. Pass --host 0.0.0.0 to expose over Tailscale.", flush=True)
    print("[review spec-web] Ctrl-C to stop.", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[review spec-web] daemon stopped.", flush=True)
    finally:
        httpd._sse_stop = True  # type: ignore[attr-defined]  # end any live SSE loops
        httpd.shutdown()
        httpd.server_close()
    return 0


# Sentinel distinguishing "no baseline passed" from a legitimate baseline of None (a spec
# that has never been submitted).
_BASELINE_UNSET = object()


def watch_submits(
    spec_path: Path | str,
    *,
    exit_on_submit: bool = False,
    poll_seconds: float = 0.5,
    baseline: object = _BASELINE_UNSET,
) -> int:
    """Block, watching ONE spec's store for a fresh Submit, emitting the marker-framed review.

    This is the daemon-era submit->agent handoff: the daemon (a separate process) records each
    non-empty Submit via ``SpecStore.submit_pending`` (``last_submit`` timestamp); this poller
    detects a CHANGE in ``last_submit`` and prints the structured review to stdout between the
    stable SUBMIT_MARKER_* markers — the same contract ``run_specweb``'s in-process watcher
    emits, so an agent that ran ``review spec-web serve <spec>`` reads its review identically.
    Ctrl-C returns cleanly (the daemon keeps running). ``exit_on_submit`` returns after the
    first fresh submit.

    ``baseline`` lets the caller pin the "stale as of" point EARLIER than the watch start
    (e.g. ``review spec-web serve`` captures it before starting/registering into the daemon,
    so a reviewer submitting in that window is still delivered). Unset ⇒ baseline now.
    """
    spec_path = Path(spec_path).expanduser().resolve()
    store = SpecStore(spec_path)
    # Baseline the CURRENT last_submit so we only report submits that happen AFTER the watch
    # starts (a stale prior submit from an earlier session must not fire immediately) — unless
    # the caller pinned an earlier baseline (see docstring).
    last_seen = store.last_submit() if baseline is _BASELINE_UNSET else baseline
    try:
        while True:
            time.sleep(poll_seconds)
            cur = store.last_submit()
            if cur != last_seen and cur is not None:
                last_seen = cur
                _emit_submitted_review(store.review_payload())
                if exit_on_submit:
                    return 0
    except KeyboardInterrupt:
        print("\n[review spec-web] stopped watching (daemon still running).", flush=True)
    return 0
