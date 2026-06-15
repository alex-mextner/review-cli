"""HTTP server for the review-cli dashboard (stdlib, no deps).

`review dashboard [--host H] [--port N] [--no-open]` binds 127.0.0.1 by default (the logs
persist prompts/diffs that may carry secrets, so the dashboard is loopback-only unless you
opt in); ``--host 0.0.0.0`` exposes it over Tailscale (mirrors ``review spec-web``). It
serves:

  GET  /                         -> the SPA shell (assets/index.html)
  GET  /assets/<file>            -> static JS/CSS (from assets/, allowlisted)
  GET  /events                  -> Server-Sent Events: live review activity stream
  GET  /api/health              -> {ok, log_dir, store_path, allowed_origins, ...}
  GET  /api/runs[?gap=N]        -> [session summaries], newest first
  GET  /api/runs/<id>           -> full session detail (calls/brainstorm/roles + annotation)
  GET  /api/stats[?gap=N]       -> aggregate stats (modes/models/roles/days/durations)
  GET  /api/annotations         -> {session_id: annotation} (overseer store dump)
  POST /api/runs/<id>/feedback  -> {feedback: "..."}          (overseer feedback)
  POST /api/runs/<id>/conscious -> {conscious: true|false}    (Tasks panel toggle)
  POST /api/runs/<id>/links     -> {pr?: "#123", ticket?: "HYP-742", remove?: bool}

The handler is intentionally thin: parsing lives in ``parser``, persistence in ``store``.

Security: READS are open to anyone who can reach the port (the dashboard's own logs are
the only data — there are no per-user secrets in the API), but a Host-header allowlist
(loopback + the discovered Tailscale host + ``$REVIEW_DASHBOARD_ALLOWED_HOSTS``) defends
against DNS rebinding, and WRITES additionally require the Origin/Referer to be an allowed
host (CSRF). This is the spec-web pattern, ported so the dashboard can be exposed over
Tailscale without dropping the rebinding protection.
"""
from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..process import log_dir
from . import parser as dparser
from . import store as dstore

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
_ALLOWED_ASSETS = {"app.js", "app.css"}
_CONTENT_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}

# Max request body for a write (feedback/conscious/links). Feedback is free text but a
# few KB is generous; this caps a malicious/runaway POST before we read it into memory.
_MAX_WRITE_BODY_BYTES = 64 * 1024
# Loopback host names an Origin/Referer may carry for a same-machine browser request.
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"}

# SSE tuning. The watcher polls the log dir for appended/changed artifacts and pushes a
# delta to every connected browser; a heartbeat comment keeps proxies/Tailscale from
# closing an idle stream.
_SSE_POLL_SECONDS = 1.0
_SSE_HEARTBEAT_SECONDS = 15.0
# How many of the newest sessions to (re-)push as `run` events on each activity tick. The
# changed files are almost always the newest session, so a small window keeps the payload
# tiny while still covering a multi-call invocation that spans the cluster gap.
_SSE_RECENT_RUNS = 8

# Tailscale identity is DISCOVERED at runtime (this is a packaged CLI used on many
# machines — a personal host must never be compiled in). Mirrors specweb: best-effort,
# cached `tailscale status --json` -> this node's DNSName + TailscaleIPs. Falls back to
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
    raw = os.environ.get("REVIEW_DASHBOARD_ALLOWED_HOSTS", "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def allowed_hosts() -> set[str]:
    """The full allowed-host set for the Host allowlist (anti-rebinding) and write-origin
    checks: loopback + discovered Tailscale + configured extras. Binding 127.0.0.1 keeps
    this loopback-only; binding 0.0.0.0 lets the discovered Tailscale host through too."""
    return _LOOPBACK_HOSTS | _discover_tailscale_hosts() | _extra_allowed_hosts()


def _session_index(gap: float) -> dict[str, dparser.Session]:
    sessions = dparser.load_sessions(log_dir(), gap_seconds=gap)
    return {s.session_id: s for s in sessions}


def _merge_annotation(summary: dict, session_id: str) -> dict:
    ann = dstore.get_annotation(session_id)
    summary = dict(summary)
    summary["feedback"] = ann.get("feedback")
    summary["conscious"] = bool(ann.get("conscious"))
    summary["links"] = ann.get("links", {"prs": [], "tickets": []})
    summary["annotation_updated"] = ann.get("updated")
    return summary


def detect_links_for_cwd(cwd: Path) -> dict:
    """Best-effort auto-detect PR#/ticket from a repo's current branch name.

    review-cli logs do NOT record the cwd of a run, so there is no per-session cwd to
    mine. This helper is exposed for the UI's "detect from current repo" affordance and
    for tests: it reads the branch of ``cwd`` and pulls a HYP-style ticket out of the
    branch name (e.g. ``HYP-742-dashboard`` -> ``HYP-742``). PRs aren't knowable offline
    without a network call, so only tickets are auto-seeded.
    """
    import re
    import subprocess

    tickets: list[str] = []
    try:
        # `symbolic-ref --short HEAD` resolves the branch name even on an UNBORN branch
        # (a fresh repo with no commits), where `rev-parse --abbrev-ref HEAD` errors out.
        out = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=str(cwd), capture_output=True, text=True, timeout=10,
        )
        branch = out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        branch = ""
    for m in re.finditer(r"([A-Za-z]+-\d+)", branch):
        t = m.group(1).upper()
        if t not in tickets:
            tickets.append(t)
    return {"branch": branch, "tickets": tickets, "prs": []}


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "review-dashboard/1.0"

    # Quieter logging — one line per request to stderr is fine, but drop the noise.
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    # ---- helpers -------------------------------------------------------------
    def _send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, msg: str) -> None:
        self._send_json({"error": msg}, status=status)

    def _gap(self, qs: dict) -> float:
        try:
            return float(qs.get("gap", [dparser.DEFAULT_SESSION_GAP_SECONDS])[0])
        except (ValueError, TypeError):
            return dparser.DEFAULT_SESSION_GAP_SECONDS

    def _host_hostname(self) -> str | None:
        """The bare hostname from this request's Host header (port + IPv6 brackets stripped)."""
        host = (self.headers.get("Host") or "").strip()
        if not host:
            return None
        # strip an optional :port (handle bracketed IPv6 [::1]:port too)
        if host.startswith("["):
            return host[1:].split("]", 1)[0].lower()
        return (host.rsplit(":", 1)[0] if host.count(":") == 1 else host).lower()

    def _host_allowed(self) -> bool:
        """Anti-DNS-rebinding guard.

        Binding to 127.0.0.1 stops the network from reaching us, but NOT a victim's own
        browser: a malicious page can DNS-rebind its own hostname to 127.0.0.1 / the
        Tailscale IP and then fetch our endpoints same-origin, exfiltrating the local log
        JSON (prompts/diffs). The defence is a Host-header allowlist — a rebound request
        still carries the ATTACKER's hostname in Host, which won't be in the allowlist. We
        only ever serve requests whose Host is loopback, the DISCOVERED Tailscale host, or
        an explicitly configured extra (``$REVIEW_DASHBOARD_ALLOWED_HOSTS``). Binding
        0.0.0.0 widens the allowlist to include the Tailscale host (so the phone/remote can
        reach it) WITHOUT disabling the guard — an arbitrary rebound hostname is still
        rejected.
        """
        hostname = self._host_hostname()
        if hostname is None:
            # HTTP/1.0 clients (and our own curl smoke without -H) may omit Host; a browser
            # rebinding attack ALWAYS sends one, so a missing Host is not the attack vector.
            return True
        return hostname in allowed_hosts()

    def _reject_foreign_host(self) -> bool:
        if self._host_allowed():
            return False
        self._error(403, "forbidden: dashboard is local-only (loopback Host required)")
        return True

    @staticmethod
    def _origin_hostname(value: str) -> str | None:
        """Extract the hostname from an Origin/Referer URL (scheme://host[:port]/...).

        Returns the bare hostname (port stripped, IPv6 brackets kept as-is in the
        loopback set). None if the value is not a parseable absolute URL."""
        try:
            parsed = urlparse(value.strip())
        except ValueError:
            return None
        if not parsed.scheme or not parsed.netloc:
            return None
        host = parsed.hostname  # urlparse drops the port and unwraps IPv6 brackets
        return host.lower() if host else None

    def _origin_is_allowed(self) -> bool:
        """CSRF defence for WRITES: a cross-site page that fetch()es our port carries ITS
        OWN site in the Origin header. We require the Origin (or, if absent, the Referer) to
        be an ALLOWED host (loopback + the discovered Tailscale host + configured extras) —
        so a remote reviewer on the Tailscale host can still leave feedback/links while a
        foreign site (or a "null" sandboxed origin) cannot. A request with NEITHER header is
        treated as same-origin/non-browser (the dashboard's own fetch is same-origin and
        browsers DO send Origin on POST; curl/tests legitimately omit both) and allowed.

        Combined with the Host allowlist + the Content-Type guard, a foreign web page
        cannot mutate the annotation store: a simple-request form post can't set
        Content-Type: application/json, and an XHR/fetch that does will carry a foreign
        Origin that fails here. (The allowed set is loopback + Tailscale, matching the Host
        allowlist — same as spec-web's ``_origin_allowed``.)"""
        allowed = allowed_hosts()
        origin = (self.headers.get("Origin") or "").strip()
        if origin:
            # "null" (sandboxed/file: origins) parses to no host -> rejected.
            host = self._origin_hostname(origin)
            return host is not None and host in allowed
        referer = (self.headers.get("Referer") or "").strip()
        if referer:
            host = self._origin_hostname(referer)
            return host is not None and host in allowed
        # No Origin and no Referer: not a cross-site browser write. Allow.
        return True

    def _content_type_is_json(self) -> bool:
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        return ctype == "application/json"

    # ---- routing -------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        if self._reject_foreign_host():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        try:
            if path == "/" or path == "/index.html":
                return self._serve_index()
            if path.startswith("/assets/"):
                return self._serve_asset(path[len("/assets/"):])
            if path == "/events":
                return self._serve_events(self._gap(qs))
            if path == "/api/health":
                return self._send_json({
                    "ok": True,
                    "log_dir": str(log_dir()),
                    "store_path": str(dstore.store_path()),
                    "allowed_origins": sorted(allowed_hosts()),
                    "version": self.server_version,
                })
            if path == "/api/runs":
                gap = self._gap(qs)
                sessions = dparser.load_sessions(log_dir(), gap_seconds=gap)
                return self._send_json([_merge_annotation(s.to_summary(), s.session_id) for s in sessions])
            if path.startswith("/api/runs/"):
                sid = path[len("/api/runs/"):]
                idx = _session_index(self._gap(qs))
                sess = idx.get(sid)
                if sess is None:
                    return self._error(404, f"unknown session {sid}")
                detail = sess.to_detail()
                detail = _merge_annotation(detail, sid)
                return self._send_json(detail)
            if path == "/api/stats":
                sessions = dparser.load_sessions(log_dir(), gap_seconds=self._gap(qs))
                stats = dparser.compute_stats(sessions)
                # Tasks/overseer rollups come from the store, layered on top of stats.
                anns = dstore.all_annotations()
                stats["conscious_count"] = sum(1 for a in anns.values() if a.get("conscious"))
                stats["feedback_count"] = sum(1 for a in anns.values() if a.get("feedback"))
                return self._send_json(stats)
            if path == "/api/annotations":
                return self._send_json(dstore.all_annotations())
            if path == "/api/detect-links":
                # Best-effort ticket auto-detection from the branch of the directory the
                # dashboard was launched in (logs carry no per-session cwd; the launch dir
                # is the closest signal — usually the repo the overseer is reviewing).
                return self._send_json(detect_links_for_cwd(Path.cwd()))
            return self._error(404, f"not found: {path}")
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001 - never crash the server thread
            self._error(500, f"{type(exc).__name__}: {exc}")

    def _read_write_body(self) -> dict | None:
        """Read + parse the JSON body of a WRITE, enforcing the size cap.

        Returns the dict on success, or None after having already sent an error response
        (413 too large / 400 bad JSON) — the caller must just return when it gets None.
        Reading the declared length and capping it BEFORE we pull bytes keeps a malicious
        oversized POST from being slurped into memory (HYP-742 finding 1)."""
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
        if len(raw) > _MAX_WRITE_BODY_BYTES:  # defence in depth vs a lying Content-Length
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

    def _session_exists(self, sid: str) -> bool:
        """True if ``sid`` is a session present in the parsed runs.

        Writes are only allowed against a session that actually exists (HYP-742
        finding 1): annotating an arbitrary attacker-chosen id would let a CSRF write
        seed junk records into the store. The default gap is used (POSTs carry no
        ?gap=), matching how the UI clusters."""
        return sid in _session_index(dparser.DEFAULT_SESSION_GAP_SECONDS)

    def do_POST(self) -> None:  # noqa: N802
        if self._reject_foreign_host():
            return
        # CSRF guards for state-changing writes (HYP-742 finding 1):
        #   1. a cross-site browser fetch carries a foreign Origin -> 403;
        #   2. a foreign form/simple-request can't set application/json -> 415;
        # together with the Host allowlist this stops a malicious page from mutating
        # the local annotation store via the loopback port.
        if not self._origin_is_allowed():
            return self._error(403, "forbidden: cross-origin write blocked (loopback Origin required)")
        if not self._content_type_is_json():
            return self._error(415, "unsupported media type: Content-Type must be application/json")
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if not path.startswith("/api/runs/"):
                return self._error(404, f"not found: {path}")
            rest = path[len("/api/runs/"):]
            if "/" not in rest:
                return self._error(404, "expected /api/runs/<id>/<action>")
            sid, action = rest.split("/", 1)
            if action not in ("feedback", "conscious", "links"):
                return self._error(404, f"unknown action: {action}")
            # Only annotate a session that exists in the parsed runs — reject writes to an
            # UNKNOWN id (404) so a forged/arbitrary id can't plant a store record.
            if not self._session_exists(sid):
                return self._error(404, f"unknown session {sid}")
            body = self._read_write_body()
            if body is None:
                return  # _read_write_body already sent a 413/400
            if action == "feedback":
                rec = dstore.set_feedback(sid, body.get("feedback"))
                return self._send_json({"ok": True, "session_id": sid, "annotation": rec})
            if action == "conscious":
                rec = dstore.set_conscious(sid, bool(body.get("conscious")))
                return self._send_json({"ok": True, "session_id": sid, "annotation": rec})
            # action == "links"
            try:
                if body.get("remove"):
                    rec = dstore.remove_link(sid, pr=body.get("pr"), ticket=body.get("ticket"))
                else:
                    rec = dstore.add_link(sid, pr=body.get("pr"), ticket=body.get("ticket"))
            except ValueError as exc:
                return self._error(400, str(exc))
            return self._send_json({"ok": True, "session_id": sid, "annotation": rec})
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            self._error(500, f"{type(exc).__name__}: {exc}")

    # ---- static --------------------------------------------------------------
    def _serve_index(self) -> None:
        try:
            html = (ASSETS_DIR / "index.html").read_bytes()
        except OSError:
            return self._error(500, "index.html missing")
        self._send_bytes(html, "text/html; charset=utf-8")

    def _serve_asset(self, name: str) -> None:
        # Allowlist: no traversal, only known assets.
        if name not in _ALLOWED_ASSETS:
            return self._error(404, f"asset not allowed: {name}")
        p = ASSETS_DIR / name
        try:
            body = p.read_bytes()
        except OSError:
            return self._error(404, f"asset missing: {name}")
        ctype = _CONTENT_TYPES.get(p.suffix, "application/octet-stream")
        self._send_bytes(body, ctype)

    # ---- SSE live stream -----------------------------------------------------
    def _sse_write(self, payload: bytes) -> bool:
        """Write one already-framed SSE chunk; return False if the client went away."""
        try:
            self.wfile.write(payload)
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def _serve_events(self, gap: float) -> None:
        """Server-Sent Events stream of LIVE review activity (CTO #3627).

        review-cli has no event bus — the only durable trace of a run is the per-call
        ``.log`` / brainstorm ``.md`` files it appends to ``log_dir()`` as a review streams.
        So this watches that directory: every poll it snapshots each artifact's (mtime,
        size), diffs against the previous snapshot, and for any NEW or GROWN file re-parses
        the affected sessions and pushes them to the browser as an SSE ``run`` event (the
        same summary shape ``/api/runs`` returns, so the front-end can update a row in
        place). A trailing per-file ``log`` event carries the changed filename + backend +
        whether it finished, so an in-progress call is visible the moment its log appears.

        The connection is long-lived; ThreadingHTTPServer gives each one its own thread so
        it never blocks the JSON endpoints. A heartbeat comment keeps idle proxies /
        Tailscale from dropping the stream. The loop ends when the client disconnects.
        """
        ld = log_dir()
        # Establish the baseline snapshot SYNCHRONOUSLY, before flushing the first byte.
        # A client (or test) treats "first byte received" as "the stream is watching",
        # then performs an action that writes a new log. If the baseline were taken lazily
        # on the loop's first tick (up to _SSE_POLL_SECONDS later, on a separate thread that
        # may not have been scheduled yet), a file written in that window would be folded
        # into the baseline and NEVER reported as a live event. Snapshotting here guarantees
        # any artifact that appears after the ": connected" flush is seen as a true delta.
        last = self._snapshot_logs(ld)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        # Defeat proxy buffering (nginx/Tailscale Funnel) so events arrive promptly.
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        # Tell EventSource to back off a bit before reconnecting after a drop.
        if not self._sse_write(b"retry: 3000\n\n"):
            return
        if not self._sse_write(b": connected\n\n"):
            return

        last_beat = time.monotonic()
        # The handler runs on its own daemon thread; loop until the client disconnects
        # (write fails) or the server is torn down.
        while not getattr(self.server, "_sse_stop", False):
            snapshot = self._snapshot_logs(ld)
            changed = [name for name, sig in snapshot.items() if last.get(name) != sig]
            removed = [name for name in last if name not in snapshot]
            if changed or removed:
                # Baseline was taken before the first byte was flushed, so any delta here is
                # genuinely new activity since connect — emit it (no silent first-tick).
                if not self._emit_activity(snapshot, last, gap):
                    return
                last = snapshot
            now = time.monotonic()
            if now - last_beat >= _SSE_HEARTBEAT_SECONDS:
                if not self._sse_write(b": heartbeat\n\n"):
                    return
                last_beat = now
            time.sleep(_SSE_POLL_SECONDS)

    @staticmethod
    def _snapshot_logs(ld: Path) -> dict[str, tuple[float, int]]:
        """{filename: (mtime, size)} for the run artifacts in the log dir (cheap stat-only)."""
        snap: dict[str, tuple[float, int]] = {}
        try:
            entries = list(ld.iterdir())
        except OSError:
            return snap
        for entry in entries:
            if entry.suffix not in (".log", ".md"):
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            snap[entry.name] = (st.st_mtime, st.st_size)
        return snap

    def _emit_activity(
        self,
        snapshot: dict[str, tuple[float, int]],
        last: dict[str, tuple[float, int]],
        gap: float,
    ) -> bool:
        """Push SSE events for the artifacts that changed since ``last``. False on disconnect."""
        ld = log_dir()
        changed_files = sorted(name for name, sig in snapshot.items() if last.get(name) != sig)
        # Per-file `log` events: cheapest, most immediate signal that a call is streaming.
        for name in changed_files:
            grew = name in last
            payload = {"filename": name, "grew": grew}
            c = dparser.parse_call_log(ld / name)
            if c is not None:
                payload.update({
                    "kind": "call",
                    "backend": c.backend,
                    "round": c.round,
                    "completed": c.completed,
                    "has_error": c.has_error,
                })
            elif name.endswith(".md"):
                payload["kind"] = "brainstorm"
            if not self._send_sse_event("log", payload):
                return False
        # Session-level `run` events: the affected sessions, summary shape == /api/runs so
        # the front-end can refresh a row (or the whole list) in place.
        sessions = dparser.load_sessions(ld, gap_seconds=gap)
        # Only the sessions whose window overlaps a changed file are "live"; cheap heuristic
        # = re-emit the newest few summaries (the changed files are almost always the newest).
        for s in sessions[:_SSE_RECENT_RUNS]:
            summ = _merge_annotation(s.to_summary(), s.session_id)
            if not self._send_sse_event("run", summ):
                return False
        return True

    def _send_sse_event(self, event: str, data: dict) -> bool:
        """Frame + write one named SSE event. False if the client disconnected."""
        body = json.dumps(data, separators=(",", ":"))
        chunk = f"event: {event}\ndata: {body}\n\n".encode("utf-8")
        return self._sse_write(chunk)


def make_server(port: int = 0, *, host: str = "127.0.0.1", verbose: bool = False) -> ThreadingHTTPServer:
    """Create (but do not serve) the dashboard server bound to ``host:port``.

    Default host is loopback (127.0.0.1); pass ``host="0.0.0.0"`` to expose over Tailscale.
    port=0 picks a free ephemeral port (used by tests). The bound port is on
    ``server.server_address[1]``.
    """
    httpd = ThreadingHTTPServer((host, port), DashboardHandler)
    httpd.verbose = verbose  # type: ignore[attr-defined]
    # SSE handlers loop on their own threads; this flag lets shutdown end them promptly so
    # the process can exit instead of hanging on a daemon thread mid-poll.
    httpd._sse_stop = False  # type: ignore[attr-defined]
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


def run_dashboard(
    port: int | None = None,
    *,
    host: str = "127.0.0.1",
    open_browser: bool = True,
    verbose: bool = False,
) -> int:
    """Blocking entry for ``review dashboard``. Returns a process exit code."""
    # Pass 0 straight to the server so the OS picks AND binds a free ephemeral port in one
    # step, reporting it via server_address — instead of probe-bind-close-then-rebind,
    # which leaves a TOCTOU window for another local process to claim the port (codex P3).
    chosen = port or 0
    try:
        httpd = make_server(chosen, host=host, verbose=verbose)
    except OSError as exc:
        target = f"{host}:{chosen}" if chosen else f"{host} (ephemeral port)"
        print(f"[review dashboard] cannot bind {target}: {exc}", flush=True)
        return 1
    bound = httpd.server_address[1]
    urls = _reachable_urls(host, bound)
    for url in urls:
        print(f"[review dashboard] serving {url}", flush=True)
    print(f"[review dashboard] logs: {log_dir()}", flush=True)
    print(f"[review dashboard] store: {dstore.store_path()}", flush=True)
    if host in ("0.0.0.0", "::"):
        print("[review dashboard] bound to all interfaces — reachable over Tailscale. "
              "Reads open; writes allowed from loopback + the Tailscale host. Ctrl-C to stop.", flush=True)
    else:
        print("[review dashboard] loopback-only. Pass --host 0.0.0.0 to expose over Tailscale. Ctrl-C to stop.", flush=True)
    if open_browser:
        def _open() -> None:
            import webbrowser

            try:
                webbrowser.open(urls[0])
            except Exception:  # noqa: BLE001
                pass
        threading.Timer(0.4, _open).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[review dashboard] stopped.", flush=True)
    finally:
        httpd._sse_stop = True  # type: ignore[attr-defined] — end any live SSE loops
        httpd.shutdown()
        httpd.server_close()
    return 0
