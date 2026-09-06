#!/usr/bin/env python3
"""Tests for the MULTI-SPEC spec-web daemon (registry + /spec/<name> routing + SSE + CLI wiring).

What the daemon adds over the single-spec server (tests/test_specweb.py):
  * ``registry``      — durable name -> spec-path map; register is idempotent by path, names
                        are slug-deduped, the file survives corruption.
  * ``/spec/<name>``  — ONE port serves every registered spec by NAME (navigator at ``/``),
                        with per-spec API/asset routing and the SAME per-path comment stores
                        the single-spec server uses (full compatibility).
  * ``/api/events``   — SSE live-reload stream (``spec-changed`` on file mtime change).
  * ``watch_submits`` — the cross-process submit -> agent stdout handoff (marker-framed JSON).
  * CLI wiring        — backstop classification, idempotent ``start``, lib-absent fallbacks
                        (following tests/test_dashboard_service.py: the shared service lib is
                        OPTIONAL in CI, so anything needing it is mocked or skipped loudly).

Same harness style as tests/test_specweb.py: plain test_* functions run by the __main__
block; pytest collects them too. All offline — loopback ThreadingHTTPServer on an ephemeral
port, torn down per test. No API keys, no network beyond loopback.
"""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import cli  # noqa: E402
from reviewlib.dashboard import service as dservice  # noqa: E402
from reviewlib.specweb import deliver as sdeliver  # noqa: E402
from reviewlib.specweb import registry as sregistry  # noqa: E402
from reviewlib.specweb import server as sserver  # noqa: E402
from reviewlib.specweb import service as sservice  # noqa: E402
from reviewlib.specweb.store import SpecStore  # noqa: E402

FIXTURE = REPO_ROOT / "fixtures" / "specweb" / "sample-spec.md"

# `_spec_web_ensure_running`'s actually-starting path imports `agenttools_daemon`
# (AlreadyRunningError). CI installs the core deps WITHOUT the shared service/daemon libs on
# purpose (see tests/test_dashboard_service.py) — the one test that walks that path SKIPS
# loudly there instead of erroring; every other test here never touches the libs.
try:
    import agenttools_daemon as _agenttools_daemon  # noqa: F401

    _HAS_DAEMON_LIB = True
except ImportError:
    _HAS_DAEMON_LIB = False


# --------------------------------------------------------------------------- #
# Test isolation: point the store (and thus the registry, which lives in the same
# dir) at a temp dir so we never touch ~/.config. SYNC: tests/test_specweb.py has
# the same helper — kept as small local copies because each test file is a
# standalone script (CI runs them directly, no shared conftest).
# --------------------------------------------------------------------------- #
class _TempStoreEnv:
    def __enter__(self):
        self._tmp = tempfile.mkdtemp(prefix="specweb-daemon-test-")
        self._old = os.environ.get("REVIEW_SPECWEB_DIR")
        os.environ["REVIEW_SPECWEB_DIR"] = self._tmp
        return Path(self._tmp)

    def __exit__(self, *exc):
        if self._old is None:
            os.environ.pop("REVIEW_SPECWEB_DIR", None)
        else:
            os.environ["REVIEW_SPECWEB_DIR"] = self._old


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
def test_registry_register_is_idempotent_by_path():
    with _TempStoreEnv():
        n1 = sregistry.register(FIXTURE)
        n2 = sregistry.register(FIXTURE)
        assert n1 == n2 == "sample-spec", (n1, n2)
        assert len(sregistry.list_specs()) == 1
        assert sregistry.resolve(n1) == FIXTURE.resolve()


def test_registry_dedupes_name_collisions_between_different_paths():
    with _TempStoreEnv():
        with tempfile.TemporaryDirectory() as d:
            twin = Path(d) / "sample-spec.md"
            twin.write_text("# Twin\n", encoding="utf-8")
            n1 = sregistry.register(FIXTURE)
            n2 = sregistry.register(twin)
            assert n1 == "sample-spec", n1
            assert n2 == "sample-spec-2", n2
            # each name still resolves to ITS OWN path
            assert sregistry.resolve(n1) == FIXTURE.resolve()
            assert sregistry.resolve(n2) == twin.resolve()


def test_registry_unregister_and_unknown_name():
    with _TempStoreEnv():
        name = sregistry.register(FIXTURE)
        assert sregistry.unregister(name) is True
        assert sregistry.unregister(name) is False  # idempotent
        assert sregistry.resolve(name) is None
        assert sregistry.list_specs() == []


def test_registry_corrupt_file_reads_as_empty_not_crash():
    with _TempStoreEnv() as tmp:
        (tmp / "registry.json").write_text("{not json", encoding="utf-8")
        assert sregistry.load_registry()["specs"] == {}
        # and a register over the corrupt file recovers it
        name = sregistry.register(FIXTURE)
        assert sregistry.resolve(name) == FIXTURE.resolve()


def test_registry_list_flags_missing_files():
    with _TempStoreEnv():
        with tempfile.TemporaryDirectory() as d:
            spec = Path(d) / "gone.md"
            spec.write_text("# G\n", encoding="utf-8")
            name = sregistry.register(spec)
            spec.unlink()
            recs = sregistry.list_specs()
            assert len(recs) == 1 and recs[0]["name"] == name
            assert recs[0]["exists"] is False and recs[0]["mtime"] is None


# --------------------------------------------------------------------------- #
# URL splitting
# --------------------------------------------------------------------------- #
def test_split_spec_path():
    f = sserver._split_spec_path
    assert f("/spec/foo") == ("foo", "/")
    assert f("/spec/foo/") == ("foo", "/")
    assert f("/spec/foo/api/spec") == ("foo", "/api/spec")
    assert f("/spec/foo/asset/a.svg") == ("foo", "/asset/a.svg")
    assert f("/spec/my%20name/api/spec") == ("my name", "/api/spec")
    assert f("/spec/") is None
    assert f("/other") is None
    assert f("/") is None


# --------------------------------------------------------------------------- #
# daemon server (loopback, ephemeral port)
# --------------------------------------------------------------------------- #
class _Daemon:
    """A live multi-spec daemon on an ephemeral loopback port (torn down via stop())."""

    def __init__(self, agent: str | None = None):
        self.httpd = sserver.make_daemon_server(host="127.0.0.1", port=0, agent=agent)
        self.httpd.sse_poll_seconds = 0.05  # keep the SSE test fast
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)

    def get(self, path):
        c = self.conn()
        c.request("GET", path)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body, dict(r.getheaders())

    def post(self, path, obj):
        c = self.conn()
        c.request(
            "POST",
            path,
            body=json.dumps(obj).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body, dict(r.getheaders())

    def stop(self):
        self.httpd._sse_stop = True
        self.httpd.shutdown()
        self.httpd.server_close()


def test_daemon_navigator_lists_registered_specs():
    with _TempStoreEnv():
        name = sregistry.register(FIXTURE)
        d = _Daemon()
        try:
            st, body, _ = d.get("/")
            assert st == 200, st
            page = body.decode("utf-8")
            assert name in page and f"/spec/{name}" in page, page
            # health carries the machine-readable equivalent
            st, body, _ = d.get("/api/health")
            data = json.loads(body)
            assert data["mode"] == "daemon" and data["ok"] is True
            assert [s["name"] for s in data["specs"]] == [name], data
        finally:
            d.stop()


def test_daemon_navigator_empty_registry_shows_hint_not_error():
    with _TempStoreEnv():
        d = _Daemon()
        try:
            st, body, _ = d.get("/")
            assert st == 200 and b"No specs registered" in body
        finally:
            d.stop()


def test_daemon_exposes_app_shell_routes_at_origin_root():
    with _TempStoreEnv():
        d = _Daemon()
        try:
            st, _, headers = d.get("/")
            assert st == 200, st
            assert headers.get("X-Review-Specweb") == "1", headers
            for path in (
                "/manifest.webmanifest",
                "/sw.js",
                "/offline.html",
                "/app-icon.svg",
            ):
                st, _, headers = d.get(path)
                assert st == 200, (path, st)
                assert headers.get("X-Review-Specweb") == "1", (path, headers)
        finally:
            d.stop()


def test_daemon_serves_spec_by_name_with_prefixed_base_and_assets():
    with _TempStoreEnv():
        name = sregistry.register(FIXTURE)
        d = _Daemon()
        try:
            # the SPA shell carries the per-spec URL prefix for every client fetch
            st, body, headers = d.get(f"/spec/{name}")
            assert st == 200, st
            assert headers.get("X-Review-Specweb") == "1", headers
            assert f'window.__SPECWEB_BASE__ = "/spec/{name}"'.encode() in body, body[
                :600
            ]
            # the rendered spec's figure URLs are prefixed too, and the asset route serves them
            st, body, headers = d.get(f"/spec/{name}/api/spec")
            data = json.loads(body)
            assert st == 200 and "<h1" in data["html"]
            assert headers.get("X-Review-Specweb") == "1", headers
            assert f"/spec/{name}/asset/fig-arch.svg" in data["html"], (
                "asset base not prefixed"
            )
            assert isinstance(data.get("mtime"), float), data.get("mtime")
            st, body, hdrs = d.get(f"/spec/{name}/asset/fig-arch.svg")
            assert st == 200 and b"<svg" in body
            assert hdrs.get("X-Review-Specweb") == "1", hdrs
            assert "image/svg+xml" in hdrs.get("Content-Type", "")
        finally:
            d.stop()


def test_daemon_unknown_spec_is_404_with_guidance():
    with _TempStoreEnv():
        d = _Daemon()
        try:
            st, body, _ = d.get("/spec/nope")
            assert st == 404, st
            assert b"review spec-web add" in body, body
            st, _, _ = d.get("/definitely-not-a-route")
            assert st == 404, st
        finally:
            d.stop()


def test_daemon_comments_share_the_per_path_store_with_single_spec_mode():
    """A comment posted via /spec/<name>/api/comments lands in the SAME per-path store the
    single-spec server / `review spec-web reply` use — daemon adoption must not orphan any
    existing comment stores (they are keyed by spec-path hash, not by name)."""
    with _TempStoreEnv():
        name = sregistry.register(FIXTURE)
        d = _Daemon()
        try:
            st, body, _ = d.post(
                f"/spec/{name}/api/comments",
                {"quote": "the quick", "body": "daemon note", "kind": "question"},
            )
            assert st == 201, (st, body)
            cid = json.loads(body)["comment"]["id"]
            # visible through the daemon API...
            st, body, _ = d.get(f"/spec/{name}/api/comments")
            assert st == 200 and any(c["id"] == cid for c in json.loads(body))
            # ...and in the path-keyed store the rest of the toolchain reads
            assert any(c["id"] == cid for c in SpecStore(FIXTURE).all_comments())
        finally:
            d.stop()


def test_daemon_serves_a_spec_registered_while_running():
    """The registry is read per request: an `add` while the daemon runs is served
    immediately, no restart (the whole point of the single persistent daemon)."""
    with _TempStoreEnv():
        d = _Daemon()
        try:
            st, _, _ = d.get("/spec/late")
            assert st == 404
            with tempfile.TemporaryDirectory() as tdir:
                late = Path(tdir) / "late.md"
                late.write_text("# Late\n\nadded while running\n", encoding="utf-8")
                assert sregistry.register(late) == "late"
                st, body, _ = d.get("/spec/late/api/spec")
                assert st == 200 and "added while running" in json.loads(body)["html"]
        finally:
            d.stop()


# --------------------------------------------------------------------------- #
# SSE live reload
# --------------------------------------------------------------------------- #
def _read_sse_until(resp, wanted_event: str, deadline_s: float) -> dict | None:
    """Read SSE lines until ``event: <wanted_event>`` arrives; return its JSON data."""
    deadline = time.monotonic() + deadline_s
    event = None
    while time.monotonic() < deadline:
        line = resp.fp.readline().decode("utf-8").rstrip("\n")
        if line.startswith("event: "):
            event = line[len("event: ") :]
        elif line.startswith("data: ") and event == wanted_event:
            return json.loads(line[len("data: ") :])
    return None


def test_sse_emits_spec_changed_on_file_change():
    with _TempStoreEnv():
        with tempfile.TemporaryDirectory() as tdir:
            spec = Path(tdir) / "live.md"
            spec.write_text("# Live\n\noriginal text\n", encoding="utf-8")
            name = sregistry.register(spec)
            d = _Daemon()
            c = d.conn()
            try:
                c.request("GET", f"/spec/{name}/api/events")
                resp = c.getresponse()
                assert resp.status == 200
                assert "text/event-stream" in resp.getheader("Content-Type", "")
                hello = _read_sse_until(resp, "hello", deadline_s=5)
                assert hello is not None and hello.get("mtime"), hello
                # change the file -> the stream must announce it (mtime moved)
                spec.write_text("# Live\n\nEDITED text\n", encoding="utf-8")
                os.utime(spec)  # ensure the mtime moves even on coarse filesystems
                changed = _read_sse_until(resp, "spec-changed", deadline_s=5)
                assert changed is not None, "no spec-changed event within 5s"
                assert changed.get("mtime") != hello.get("mtime"), (hello, changed)
            finally:
                c.close()
                d.stop()


# --------------------------------------------------------------------------- #
# watch_submits (the cross-process submit -> agent stdout handoff)
# --------------------------------------------------------------------------- #
def test_watch_submits_emits_marker_framed_review_on_fresh_submit():
    with _TempStoreEnv():
        with tempfile.TemporaryDirectory() as tdir:
            spec = Path(tdir) / "watched.md"
            spec.write_text("# Watched\n\nbody\n", encoding="utf-8")
            store = SpecStore(spec)

            def _submit_later():
                time.sleep(0.3)
                store.add_comment(quote="body", body="please clarify", kind="question")
                store.submit_pending()

            t = threading.Thread(target=_submit_later, daemon=True)
            t.start()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = sserver.watch_submits(spec, exit_on_submit=True, poll_seconds=0.05)
            t.join()
            assert rc == 0, rc
            out = buf.getvalue()
            assert (
                sserver.SUBMIT_MARKER_BEGIN in out and sserver.SUBMIT_MARKER_END in out
            ), out
            payload = (
                out.split(sserver.SUBMIT_MARKER_BEGIN)[1]
                .split(sserver.SUBMIT_MARKER_END)[0]
                .strip()
            )
            review = json.loads(payload)
            assert review["counts"]["questions"] == 1, review
            assert review["comments"][0]["body"] == "please clarify", review


def test_watch_submits_honors_an_earlier_pinned_baseline():
    """`serve` captures the baseline BEFORE starting/registering into the daemon; a submit
    landing in that window must still be delivered (not folded into a watch-start baseline)."""
    with _TempStoreEnv():
        with tempfile.TemporaryDirectory() as tdir:
            spec = Path(tdir) / "raced.md"
            spec.write_text("# Raced\n", encoding="utf-8")
            store = SpecStore(spec)
            baseline = store.last_submit()  # None: never submitted
            # the "race": a submit happens AFTER the baseline but BEFORE the watch starts
            store.add_comment(quote="", body="raced note", kind="question")
            store.submit_pending()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = sserver.watch_submits(
                    spec, exit_on_submit=True, poll_seconds=0.05, baseline=baseline
                )
            assert rc == 0, rc
            assert buf.getvalue().count(sserver.SUBMIT_MARKER_BEGIN) == 1, (
                buf.getvalue()
            )


def test_watch_submits_ignores_a_stale_pre_watch_submit():
    """Only a submit AFTER the watch starts fires — a leftover last_submit from an earlier
    session must not be re-delivered to the agent as if it were fresh."""
    with _TempStoreEnv():
        with tempfile.TemporaryDirectory() as tdir:
            spec = Path(tdir) / "stale.md"
            spec.write_text("# Stale\n", encoding="utf-8")
            store = SpecStore(spec)
            store.add_comment(quote="", body="old note", kind="remark")
            store.submit_pending()  # the STALE submit, before the watch

            def _fresh_submit_later():
                time.sleep(0.3)
                store.add_comment(quote="", body="fresh note", kind="question")
                store.submit_pending()

            t = threading.Thread(target=_fresh_submit_later, daemon=True)
            t.start()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = sserver.watch_submits(spec, exit_on_submit=True, poll_seconds=0.05)
            t.join()
            assert rc == 0, rc
            out = buf.getvalue()
            # exactly ONE marker-framed emission — for the fresh batch, not the stale one
            assert out.count(sserver.SUBMIT_MARKER_BEGIN) == 1, out


def test_watch_submits_emit_current_re_emits_an_already_submitted_batch():
    """The failed-live-delivery recovery path: a batch is ALREADY submitted (its live tmux
    delivery failed), so a bare watch — which baselines at the current last_submit and only
    fires on a LATER change — would wait forever. `emit_current` re-surfaces the stored batch
    immediately and (with exit_on_submit) returns, so the UI's recovery hint actually works."""
    with _TempStoreEnv():
        with tempfile.TemporaryDirectory() as tdir:
            spec = Path(tdir) / "undelivered.md"
            spec.write_text("# Undelivered\n", encoding="utf-8")
            store = SpecStore(spec)
            store.add_comment(quote="", body="reached nobody live", kind="question")
            store.submit_pending()  # already submitted; nothing new will happen

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = sserver.watch_submits(
                    spec, exit_on_submit=True, poll_seconds=0.05, emit_current=True
                )
            assert rc == 0, rc
            out = buf.getvalue()
            # exactly ONE emission — the already-submitted batch, with no fresh submit at all
            assert out.count(sserver.SUBMIT_MARKER_BEGIN) == 1, out
            payload = (
                out.split(sserver.SUBMIT_MARKER_BEGIN)[1]
                .split(sserver.SUBMIT_MARKER_END)[0]
                .strip()
            )
            review = json.loads(payload)
            assert review["comments"][0]["body"] == "reached nobody live", review


def test_watch_submits_emit_current_is_a_noop_when_nothing_submitted():
    """`emit_current` on a never-submitted spec emits nothing up front, then behaves like a
    normal watch — a fresh submit still fires (and only that one)."""
    with _TempStoreEnv():
        with tempfile.TemporaryDirectory() as tdir:
            spec = Path(tdir) / "empty.md"
            spec.write_text("# Empty\n", encoding="utf-8")
            store = SpecStore(spec)

            def _submit_later():
                time.sleep(0.3)
                store.add_comment(quote="", body="the only note", kind="question")
                store.submit_pending()

            t = threading.Thread(target=_submit_later, daemon=True)
            t.start()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = sserver.watch_submits(
                    spec, exit_on_submit=True, poll_seconds=0.05, emit_current=True
                )
            t.join()
            assert rc == 0, rc
            # exactly ONE emission — the fresh submit, not a spurious empty emit_current one
            assert buf.getvalue().count(sserver.SUBMIT_MARKER_BEGIN) == 1, (
                buf.getvalue()
            )


# --------------------------------------------------------------------------- #
# submit delivery (--agent ownership + tg-ctl-style tmux injection)
# --------------------------------------------------------------------------- #
def test_registry_records_and_updates_the_owning_agent():
    with _TempStoreEnv():
        name = sregistry.register(FIXTURE, agent="ext")
        assert sregistry.list_specs()[0]["agent"] == "ext"
        # re-register with a different agent MOVES ownership (the latest serve wins) …
        assert sregistry.register(FIXTURE, agent="other") == name
        assert sregistry.list_specs()[0]["agent"] == "other"
        # … but a None agent never clears an existing owner
        assert sregistry.register(FIXTURE) == name
        assert sregistry.list_specs()[0]["agent"] == "other"


def test_match_agent_pane_prefers_window_then_session():
    rows = [
        {
            "pane_id": "%1",
            "session": "work",
            "window": "zsh",
            "window_active": True,
            "pane_active": True,
        },
        {
            "pane_id": "%2",
            "session": "ext",
            "window": "node",
            "window_active": True,
            "pane_active": True,
        },
        {
            "pane_id": "%3",
            "session": "other",
            "window": "ext",
            "window_active": False,
            "pane_active": False,
        },
        {
            "pane_id": "%4",
            "session": "other",
            "window": "ext",
            "window_active": False,
            "pane_active": True,
        },
    ]
    # window NAME match wins over session match, active pane preferred within the window
    assert sdeliver.match_agent_pane(rows, "ext") == "%4"
    # session match: the session's active window's pane
    assert sdeliver.match_agent_pane(rows, "work") == "%1"
    # case-insensitive fallback
    assert sdeliver.match_agent_pane(rows, "EXT") == "%4"
    assert sdeliver.match_agent_pane(rows, "nope") is None
    assert sdeliver.match_agent_pane([], "ext") is None


def test_format_review_message_is_batch_scoped_and_one_line_per_comment():
    review = {
        "comments": [
            {
                "id": "aaa",
                "kind": "question",
                "batch": "B2",
                "section_title": "4 Goals",
                "quote": "the quick\nbrown fox",
                "body": "why  is\nthis?",
            },
            {
                "id": "old",
                "kind": "remark",
                "batch": "B1",
                "section_title": "1 Intro",
                "quote": "",
                "body": "older, already delivered",
            },
        ]
    }
    msg = sdeliver.format_review_message("my-spec", "/x/my-spec.md", review, "B2")
    lines = msg.splitlines()
    assert "1 comment(s) submitted" in lines[0], lines[0]
    assert "old" not in msg, "a previous batch's comment must not be re-delivered"
    # the CTO-specified per-comment shape, newlines collapsed so one comment = one line
    assert (
        '[SPEC-WEB comment on my-spec §4 Goals] "the quick brown fox" — why is this? (question, id aaa)'
        in lines
    ), lines
    assert 'review spec-web reply <id> "<answer>" --spec /x/my-spec.md' in lines[-1], (
        lines[-1]
    )


def test_daemon_submit_delivers_to_the_specs_registered_agent():
    """POST /api/submit on a spec registered with --agent must hand the batch to deliver
    (the tg-ctl-style tmux injection) and report the outcome in the response."""
    with _TempStoreEnv():
        name = sregistry.register(FIXTURE, agent="ext")
        d = _Daemon(agent="daemon-default")
        calls = {}

        def _fake_deliver(*, agent, spec_name, spec_path, review, batch):
            calls.update(
                agent=agent,
                spec_name=spec_name,
                batch=batch,
                n=len(review.get("comments") or []),
            )
            return True, "injected into pane %7 (fake)"

        try:
            with mock.patch.object(sdeliver, "deliver_review", _fake_deliver):
                st, _, _ = d.post(
                    f"/spec/{name}/api/comments", {"quote": "q", "body": "deliver me"}
                )
                assert st == 201, st
                st, body, _ = d.post(f"/spec/{name}/api/submit", {})
                assert st == 200, (st, body)
                resp = json.loads(body)
        finally:
            d.stop()
        assert calls["agent"] == "ext", (
            calls
        )  # the SPEC's agent, not the daemon default
        assert calls["spec_name"] == name and calls["batch"] == resp["batch"], (
            calls,
            resp,
        )
        assert resp["delivery"] == {
            "agent": "ext",
            "delivered": True,
            "detail": "injected into pane %7 (fake)",
        }, resp


def test_daemon_submit_falls_back_to_daemon_default_agent():
    with _TempStoreEnv():
        name = sregistry.register(FIXTURE)  # no per-spec agent
        d = _Daemon(agent="daemon-default")
        calls = {}

        def _fake_deliver(**kw):
            calls.update(kw)
            return True, "ok"

        try:
            with mock.patch.object(sdeliver, "deliver_review", _fake_deliver):
                d.post(f"/spec/{name}/api/comments", {"quote": "", "body": "note"})
                st, body, _ = d.post(f"/spec/{name}/api/submit", {})
        finally:
            d.stop()
        assert st == 200 and calls["agent"] == "daemon-default", (st, calls)


def test_daemon_submit_without_any_agent_still_succeeds():
    """No owner anywhere (old registry, agentless test server): the submit itself must keep
    working — the review stays in the store; the response reports the undelivered state."""
    with _TempStoreEnv():
        name = sregistry.register(FIXTURE)
        d = _Daemon()  # no daemon default either
        try:
            d.post(f"/spec/{name}/api/comments", {"quote": "", "body": "unowned"})
            st, body, _ = d.post(f"/spec/{name}/api/submit", {})
        finally:
            d.stop()
        assert st == 200, st
        resp = json.loads(body)
        assert resp["ok"] is True and resp["count"] == 1, resp
        assert resp["delivery"]["delivered"] is False, resp
        assert resp["delivery"]["agent"] is None, resp


# --------------------------------------------------------------------------- #
# CLI wiring (mocked service manager — the shared lib is optional in CI, see
# tests/test_dashboard_service.py for the precedent)
# --------------------------------------------------------------------------- #
class _FakeStatus:
    def __init__(self, running: bool, pid: int | None = None):
        self.running = running
        self.pid = pid


class _FakeManager:
    def __init__(self, running: bool, pid: int | None = None):
        self._st = _FakeStatus(running, pid)
        self.start_calls = 0

    def status(self):
        return self._st

    def start(self):
        self.start_calls += 1
        self._st = _FakeStatus(True, 4242)
        return self._st


def test_spec_web_start_is_idempotent_when_already_running():
    with _TempStoreEnv():
        mgr = _FakeManager(running=True, pid=4242)
        with mock.patch.object(
            cli, "_spec_web_manager", lambda host, port, agent=None: mgr
        ):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli._spec_web(["start", "--agent", "ext"])
        out = buf.getvalue()
        assert rc == 0, (rc, out)  # idempotent: already-up start is SUCCESS, not exit 3
        assert "already running" in out and "4242" in out, out
        assert mgr.start_calls == 0, "must not double-start"
        assert "navigator" in out, (
            out
        )  # says what it serves (status), not just 'running'


def test_spec_web_daemon_launching_actions_require_agent():
    """start/run/enable (and serve/the positional) REFUSE without --agent — an agentless
    daemon strands submitted reviews in the store with nobody to deliver them to."""
    for argv in (
        ["start"],
        ["run"],
        ["enable"],
        ["__serve"],
        ["serve", str(FIXTURE)],
        [str(FIXTURE)],
    ):
        with _TempStoreEnv():
            mgr = _FakeManager(running=False)
            with mock.patch.object(
                cli, "_spec_web_manager", lambda host, port, agent=None: mgr
            ):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    rc = cli._spec_web(list(argv))
            assert rc == 2, (argv, rc)
            assert "--agent" in err.getvalue(), (argv, err.getvalue())
            assert mgr.start_calls == 0, argv


def test_spec_web_status_and_stop_do_not_require_agent():
    """status/stop/disable manage the EXISTING instance — no ownership needed to ask/stop
    (the requirement applies only to the daemon-LAUNCHING actions)."""
    for action in ("status", "stop", "disable"):
        assert action not in cli._SPECWEB_AGENT_REQUIRED, action
    for action in ("start", "run", "enable", "__serve"):
        assert action in cli._SPECWEB_AGENT_REQUIRED, action


def test_spec_web_lifecycle_without_lib_fails_loudly_with_fix():
    with mock.patch.object(
        cli, "_spec_web_manager", lambda host, port, agent=None: None
    ):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = cli._spec_web(["status"])
    err = buf.getvalue()
    assert rc == 4, rc
    assert "agenttools_service" in err and "pip install" in err, err


def test_spec_web_add_registers_and_prints_name_url_without_blocking():
    with _TempStoreEnv():
        mgr = _FakeManager(running=True)
        with mock.patch.object(
            cli, "_spec_web_manager", lambda host, port, agent=None: mgr
        ):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli._spec_web(["add", str(FIXTURE)])
        out = buf.getvalue()
        assert rc == 0, (rc, out)
        assert "registered 'sample-spec'" in out, out
        assert "/spec/sample-spec" in out, out
        assert sregistry.resolve("sample-spec") == FIXTURE.resolve()


def test_spec_web_add_starts_the_daemon_when_down_with_agent():
    if not _HAS_DAEMON_LIB:
        print(
            "SKIP test_spec_web_add_starts_the_daemon_when_down_with_agent: agenttools_daemon not installed"
        )
        return
    with _TempStoreEnv():
        mgr = _FakeManager(running=False)
        with mock.patch.object(
            cli, "_spec_web_manager", lambda host, port, agent=None: mgr
        ):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli._spec_web(["add", str(FIXTURE), "--agent", "ext"])
        assert rc == 0, rc
        assert mgr.start_calls == 1, "add must start the daemon when it is down"
        assert "daemon started" in buf.getvalue(), buf.getvalue()


def test_spec_web_add_refuses_to_autostart_without_agent():
    """A daemon may NEVER be launched agentless: `add` with the daemon down and no --agent
    refuses (exit 2) instead of silently starting a daemon whose submits reach nobody."""
    with _TempStoreEnv():
        mgr = _FakeManager(running=False)
        with mock.patch.object(
            cli, "_spec_web_manager", lambda host, port, agent=None: mgr
        ):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = cli._spec_web(["add", str(FIXTURE)])
        assert rc == 2, rc
        assert mgr.start_calls == 0, "must not start an agentless daemon"
        assert "--agent" in err.getvalue(), err.getvalue()


def test_spec_web_legacy_positional_falls_back_without_lib():
    """On a host WITHOUT the shared service lib, `review spec-web <spec.md> --agent A` must
    still work — it falls back to the classic single-spec foreground server (agent passed
    through so submits still tmux-deliver)."""
    calls = {}

    def _fake_foreground(spec, ns):
        calls["spec"] = spec
        calls["agent"] = ns.agent
        return 0

    with _TempStoreEnv():
        with (
            mock.patch.object(
                cli, "_spec_web_manager", lambda host, port, agent=None: None
            ),
            mock.patch.object(cli, "_spec_web_legacy_foreground", _fake_foreground),
        ):
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                rc = cli._spec_web([str(FIXTURE), "--agent", "ext"])
        assert rc == 0, rc
        assert calls["spec"] == FIXTURE.resolve(), calls
        assert calls["agent"] == "ext", calls
        assert "falling back" in buf.getvalue(), buf.getvalue()


def test_spec_web_no_watch_without_lib_refuses_instead_of_blocking():
    """`--no-watch` (and bare `add`) mean "register into the daemon + return"; on a lib-less
    host there is no daemon, and the only fallback is a BLOCKING server — the opposite of what
    was asked. Refuse loudly (exit 4), matching the non-persistent backstop classification."""
    with _TempStoreEnv():
        with mock.patch.object(
            cli, "_spec_web_manager", lambda host, port, agent=None: None
        ):
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                rc = cli._spec_web([str(FIXTURE), "--agent", "ext", "--no-watch"])
    assert rc == 4, rc
    assert "need the daemon" in buf.getvalue(), buf.getvalue()


def test_spec_web_help_lists_all_actions_and_launches_nothing():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli._spec_web([])
    out = buf.getvalue()
    assert rc == 0, rc
    for action in (
        "start",
        "status",
        "stop",
        "run",
        "add",
        "serve",
        "list",
        "remove",
        "watch",
        "reply",
    ):
        assert action in out, (action, out)


def test_spec_web_backstop_classification():
    """Only the BLOCKING invocations bypass the `-o` tee + run backstop."""
    persistent = cli._is_persistent_server_invocation
    # fast management actions -> normal path
    for argv in (
        ["spec-web"],
        ["spec-web", "start"],
        ["spec-web", "status"],
        ["spec-web", "stop"],
        ["spec-web", "add", "x.md"],
        ["spec-web", "list"],
        ["spec-web", "remove", "n"],
        ["spec-web", "reply", "id", "answer"],
    ):
        assert not persistent(argv), argv
    # blocking daemon / watch loops -> bypass
    for argv in (
        ["spec-web", "run"],
        ["spec-web", "__serve"],
        ["spec-web", "watch", "n"],
        ["spec-web", "serve", "x.md"],
        ["spec-web", "x.md"],
    ):
        assert persistent(argv), argv
    # a --no-watch serve / legacy positional returns fast
    assert not persistent(["spec-web", "serve", "x.md", "--no-watch"])
    assert not persistent(["spec-web", "x.md", "--no-watch"])


# --- __serve argv must not inherit an active reentrancy guard (review-cli#180) -----------
def test_specweb_serve_argv_targets_hidden_serve_entry():
    argv = sservice._serve_argv(port=7920, host="0.0.0.0")
    assert "spec-web" in argv and "__serve" in argv
    assert argv[argv.index("spec-web") + 1] == "__serve"
    assert "--port" in argv and "7920" in argv
    assert "--host" in argv and "0.0.0.0" in argv


def test_specweb_serve_argv_carries_env_clear_prefix():
    """review-cli#180 review finding (GLM/k3/GLM-cc, PR #279): the dashboard's fix got a
    behavioral test, the identical specweb wiring got none -- a future edit that drops or
    misorders the prefix here would silently regress `review spec-web start`/`run` to the
    exact #180 false-positive (the __serve child inherits $REVIEW_CLI_ACTIVE from its
    already-active parent `review spec-web run`/`start` invocation and refuses to launch),
    with the full suite staying green. Assert the shape AND prove it works when executed,
    mirroring test_dashboard_service.py's coverage of the dashboard half."""
    prefix = dservice._env_clear_prefix()
    argv = sservice._serve_argv(port=7920, host="0.0.0.0")
    assert argv[: len(prefix)] == prefix, argv

    env = dict(os.environ)
    env[cli.REVIEW_CLI_ACTIVE_ENV] = "1"
    result = subprocess.run(
        [
            *prefix,
            sys.executable,
            "-c",
            f"import os; print({cli.REVIEW_CLI_ACTIVE_ENV!r} in os.environ)",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout.strip() == "False", (result.stdout, result.stderr)


def test_specweb_serve_argv_bakes_in_agent_after_env_clear_prefix():
    argv = sservice._serve_argv(port=7920, host="0.0.0.0", agent="my-session")
    assert "--agent" in argv and "my-session" in argv
    assert argv[argv.index("--agent") + 1] == "my-session"


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
