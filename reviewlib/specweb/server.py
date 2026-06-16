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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import render as srender
from .store import DEFAULT_KIND, NEW_DRAFT_SLOT, SpecStore, _as_int, edit_draft_slot, store_dir

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


class SpecWebHandler(BaseHTTPRequestHandler):
    server_version = "review-specweb/1.0"

    # ---- access to per-server config ------------------------------------- #
    @property
    def _spec_path(self) -> Path:
        return self.server.spec_path  # type: ignore[attr-defined]

    @property
    def _store(self) -> SpecStore:
        return self.server.store  # type: ignore[attr-defined]

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
    def _send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
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
            if path in ("/", "/index.html"):
                return self._serve_index()
            if path.startswith("/static/"):
                return self._serve_static(path[len("/static/"):])
            if path.startswith("/asset/"):
                return self._serve_asset(path[len("/asset/"):])
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
                return self._send_json(self._store.all_drafts())
            return self._error(404, f"not found: {path}")
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001 — never crash the server thread
            self._error(500, f"{type(exc).__name__}: {exc}")

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
            body = self._read_write_body()
            if body is None:
                return  # error already sent

            if path == "/api/comments":
                return self._create_comment(body)
            if path == "/api/submit":
                result = self._store.submit_pending()
                # Signal the launching process so it can hand the structured review to the
                # agent. Only a non-empty submit (count>0) is a real handoff; an empty
                # submit (no pending notes) must not wake the agent with nothing. Enqueue the
                # review SNAPSHOT from this submit so the watcher emits exactly this batch.
                if result.get("count"):
                    self._notify_submit(result.get("review") or {})
                return self._send_json({"ok": True, **result})
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
        slot id comes from the URL (``new`` or ``edit:<id>``) and is required."""
        slot = (slot or "").strip()
        if not slot:
            return self._error(404, "expected /api/drafts/<slot>")
        text = body.get("body", "")
        if not isinstance(text, str):
            return self._error(400, "draft 'body' must be a string")
        try:
            draft = self._store.save_draft(
                slot,
                body=text,
                kind=body.get("kind") if isinstance(body.get("kind"), str) else DEFAULT_KIND,
                quote=body.get("quote", "") if isinstance(body.get("quote"), str) else "",
                section_id=body.get("section_id", "") if isinstance(body.get("section_id"), str) else "",
                section_title=body.get("section_title", "") if isinstance(body.get("section_title"), str) else "",
                start=_as_int(body.get("start")),
                end=_as_int(body.get("end")),
            )
        except ValueError as exc:
            return self._error(400, str(exc))
        # An empty body cleared the slot -> draft is {} -> report null so the client knows
        # there is nothing to restore for this slot.
        return self._send_json({"ok": True, "draft": draft or None})

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
        html = html.replace("{{SPEC_TITLE}}", _esc_attr(title))
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
        result = srender.render_spec(self._spec_path)
        # Cache the asset map on the server so /asset/<name> can validate against it.
        self.server.assets = result.assets  # type: ignore[attr-defined]
        self._send_json(
            {
                "title": self._spec_path.name,
                "html": result.html,
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
        assets = getattr(self.server, "assets", None)
        if not assets:
            assets = srender.render_spec(self._spec_path).assets
            self.server.assets = assets  # type: ignore[attr-defined]
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


def _esc_attr(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _asset_content_type(fname: str) -> str:
    # Same canonical map render uses to ACCEPT figures, so every served figure gets a
    # correct image MIME (no octet-stream fallthrough for accepted types).
    ext = fname.lower().rsplit(".", 1)[-1] if "." in fname else ""
    return srender.IMAGE_MIME_TYPES.get(ext, "application/octet-stream")


def make_server(spec_path: Path | str, *, host: str = "127.0.0.1", port: int = 0, verbose: bool = False) -> ThreadingHTTPServer:
    """Create (but do not serve) the spec-web server bound to ``host:port``.

    port=0 picks a free ephemeral port (used by tests). The bound port is on
    ``server.server_address[1]``.
    """
    spec_path = Path(spec_path).expanduser().resolve()
    httpd = ThreadingHTTPServer((host, port), SpecWebHandler)
    httpd.spec_path = spec_path  # type: ignore[attr-defined]
    httpd.store = SpecStore(spec_path)  # type: ignore[attr-defined]
    httpd.assets = {}  # type: ignore[attr-defined]
    httpd.verbose = verbose  # type: ignore[attr-defined]
    # Each non-empty POST /api/submit puts its batch timestamp here so the launching process
    # (draining the queue in run_specweb) hands the finalized review back to the agent. A
    # QUEUE, not an Event, so two rapid submits each get delivered (no coalescing). A test
    # that drives make_server with no watcher just leaves items unread — harmless.
    httpd.submit_queue = queue.Queue()  # type: ignore[attr-defined]
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
        httpd = make_server(spec_path, host=host, port=chosen, verbose=verbose)
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
        httpd.shutdown()
        httpd.server_close()
    return 0
