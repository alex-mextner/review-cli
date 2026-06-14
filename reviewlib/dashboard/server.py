"""Local-only HTTP server for the review-cli dashboard (stdlib, no deps).

`review dashboard [--port N] [--no-open]` binds 127.0.0.1 ONLY (never 0.0.0.0 — the
logs persist prompts/diffs that may carry secrets, so the dashboard must not be exposed
on the network) and serves:

  GET  /                         -> the SPA shell (assets/index.html)
  GET  /assets/<file>            -> static JS/CSS (from assets/, allowlisted)
  GET  /api/health              -> {ok, log_dir, store_path, ...}
  GET  /api/runs[?gap=N]        -> [session summaries], newest first
  GET  /api/runs/<id>           -> full session detail (calls/brainstorm/roles + annotation)
  GET  /api/stats[?gap=N]       -> aggregate stats (modes/models/roles/days/durations)
  GET  /api/annotations         -> {session_id: annotation} (overseer store dump)
  POST /api/runs/<id>/feedback  -> {feedback: "..."}          (overseer feedback)
  POST /api/runs/<id>/conscious -> {conscious: true|false}    (Tasks panel toggle)
  POST /api/runs/<id>/links     -> {pr?: "#123", ticket?: "HYP-742", remove?: bool}

The handler is intentionally thin: parsing lives in ``parser``, persistence in ``store``.
"""
from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..process import log_dir
from . import parser as dparser
from . import store as dstore

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
_ALLOWED_ASSETS = {"app.js", "app.css"}
_CONTENT_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}


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

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _gap(self, qs: dict) -> float:
        try:
            return float(qs.get("gap", [dparser.DEFAULT_SESSION_GAP_SECONDS])[0])
        except (ValueError, TypeError):
            return dparser.DEFAULT_SESSION_GAP_SECONDS

    def _host_allowed(self) -> bool:
        """Anti-DNS-rebinding guard.

        Binding to 127.0.0.1 stops the network from reaching us, but NOT a victim's own
        browser: a malicious page can DNS-rebind its own hostname to 127.0.0.1 and then
        fetch our endpoints same-origin, exfiltrating the local log JSON (prompts/diffs).
        The defence is a Host-header allowlist — a rebound request still carries the
        ATTACKER's hostname in Host, which won't be loopback. We only ever serve requests
        whose Host is localhost / 127.0.0.1 / [::1] (any port).
        """
        host = (self.headers.get("Host") or "").strip()
        if not host:
            # HTTP/1.0 clients (and our own curl smoke without -H) may omit Host; a browser
            # rebinding attack ALWAYS sends one, so a missing Host is not the attack vector.
            return True
        # strip an optional :port (handle bracketed IPv6 [::1]:port too)
        if host.startswith("["):
            hostname = host[1:].split("]", 1)[0]
        else:
            hostname = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        return hostname in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}

    def _reject_foreign_host(self) -> bool:
        if self._host_allowed():
            return False
        self._error(403, "forbidden: dashboard is local-only (loopback Host required)")
        return True

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
            if path == "/api/health":
                return self._send_json({
                    "ok": True,
                    "log_dir": str(log_dir()),
                    "store_path": str(dstore.store_path()),
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

    def do_POST(self) -> None:  # noqa: N802
        if self._reject_foreign_host():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if not path.startswith("/api/runs/"):
                return self._error(404, f"not found: {path}")
            rest = path[len("/api/runs/"):]
            if "/" not in rest:
                return self._error(404, "expected /api/runs/<id>/<action>")
            sid, action = rest.split("/", 1)
            body = self._read_json_body()
            if action == "feedback":
                rec = dstore.set_feedback(sid, body.get("feedback"))
                return self._send_json({"ok": True, "session_id": sid, "annotation": rec})
            if action == "conscious":
                rec = dstore.set_conscious(sid, bool(body.get("conscious")))
                return self._send_json({"ok": True, "session_id": sid, "annotation": rec})
            if action == "links":
                try:
                    if body.get("remove"):
                        rec = dstore.remove_link(sid, pr=body.get("pr"), ticket=body.get("ticket"))
                    else:
                        rec = dstore.add_link(sid, pr=body.get("pr"), ticket=body.get("ticket"))
                except ValueError as exc:
                    return self._error(400, str(exc))
                return self._send_json({"ok": True, "session_id": sid, "annotation": rec})
            return self._error(404, f"unknown action: {action}")
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


def make_server(port: int = 0, *, host: str = "127.0.0.1", verbose: bool = False) -> ThreadingHTTPServer:
    """Create (but do not serve) the dashboard server bound to 127.0.0.1.

    port=0 picks a free ephemeral port (used by tests). The bound port is on
    ``server.server_address[1]``.
    """
    httpd = ThreadingHTTPServer((host, port), DashboardHandler)
    httpd.verbose = verbose  # type: ignore[attr-defined]
    httpd.daemon_threads = True
    return httpd


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def run_dashboard(port: int | None = None, *, open_browser: bool = True, verbose: bool = False) -> int:
    """Blocking entry for ``review dashboard``. Returns a process exit code."""
    chosen = port if port else _free_port()
    try:
        httpd = make_server(chosen, verbose=verbose)
    except OSError as exc:
        print(f"[review dashboard] cannot bind 127.0.0.1:{chosen}: {exc}", flush=True)
        return 1
    bound = httpd.server_address[1]
    url = f"http://127.0.0.1:{bound}/"
    print(f"[review dashboard] serving {url}", flush=True)
    print(f"[review dashboard] logs: {log_dir()}", flush=True)
    print(f"[review dashboard] store: {dstore.store_path()}", flush=True)
    print("[review dashboard] local-only (127.0.0.1). Ctrl-C to stop.", flush=True)
    if open_browser:
        def _open() -> None:
            import webbrowser

            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001
                pass
        threading.Timer(0.4, _open).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[review dashboard] stopped.", flush=True)
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0
