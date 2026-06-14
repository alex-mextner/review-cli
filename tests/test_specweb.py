#!/usr/bin/env python3
"""Tests for the spec-web reviewer (render + store + server routes + origin guard).

Same harness style as tests/test_cwd.py / tests/test_streaming.py: plain test_* functions
run by the __main__ block; no pytest required (smoke.sh invokes this directly), but pytest
collects them too. All offline — a loopback ThreadingHTTPServer on an ephemeral port, torn
down per test. No API keys, no network beyond loopback.
"""
from __future__ import annotations

import http.client
import json
import os
import stat
import sys
import tempfile
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib.specweb import render as srender  # noqa: E402
from reviewlib.specweb import server as sserver  # noqa: E402
from reviewlib.specweb.store import SpecStore, spec_key  # noqa: E402

FIXTURE = REPO_ROOT / "fixtures" / "specweb" / "sample-spec.md"
SEED = REPO_ROOT / "fixtures" / "specweb" / "seed-thread.json"


# --------------------------------------------------------------------------- #
# Test isolation: point the store at a temp dir so we never touch ~/.config.
# --------------------------------------------------------------------------- #
class _TempStoreEnv:
    def __enter__(self):
        self._tmp = tempfile.mkdtemp(prefix="specweb-test-")
        self._old = os.environ.get("REVIEW_SPECWEB_DIR")
        os.environ["REVIEW_SPECWEB_DIR"] = self._tmp
        return Path(self._tmp)

    def __exit__(self, *exc):
        if self._old is None:
            os.environ.pop("REVIEW_SPECWEB_DIR", None)
        else:
            os.environ["REVIEW_SPECWEB_DIR"] = self._old


# --------------------------------------------------------------------------- #
# render
# --------------------------------------------------------------------------- #
def test_render_produces_html_with_headings_and_slugs():
    res = srender.render_spec(FIXTURE)
    assert "<h1" in res.html and "<h2" in res.html, "expected headings"
    ids = {hid for (_lv, _t, hid) in res.headings}
    # The spec's internal links [§2 Architecture](#2-architecture) must resolve: the slug
    # the renderer emits must equal what the link points at (GitHub slug scheme).
    assert "2-architecture" in ids, ids
    assert "3-open-questions" in ids, ids
    assert "1-overview" in ids, ids


def test_render_figure_is_http_reference_not_inlined():
    res = srender.render_spec(FIXTURE)
    # Figure must be served by reference (the static-file bug was inlining/empty figures).
    assert '/asset/fig-arch.svg' in res.html, "figure should be referenced as /asset/<name>"
    assert "data:image" not in res.html, "figure must NOT be inlined as a data URI"
    assert "fig-arch.svg" in res.assets, "asset must be discovered for serving"
    assert res.assets["fig-arch.svg"].is_file()


def test_render_missing_figure_is_placeholder_not_crash():
    with tempfile.TemporaryDirectory() as d:
        spec = Path(d) / "s.md"
        spec.write_text("# T\n\n![x](./assets/nope.png)\n", encoding="utf-8")
        res = srender.render_spec(spec)
        assert "figure missing" in res.html, "missing figure should render a placeholder"


def test_render_table_and_code():
    res = srender.render_spec(FIXTURE)
    assert "md-table" in res.html, "table should render"
    assert "<pre><code" in res.html, "fenced code should render"


def test_render_link_with_ampersand_not_double_escaped():
    # A query-string URL with & must render as a SINGLE &amp; (not &amp;amp;), else the
    # browser navigates to a corrupted URL (codex P2).
    with tempfile.TemporaryDirectory() as d:
        spec = Path(d) / "s.md"
        spec.write_text("# T\n\n[link](https://x.test/p?a=1&b=2)\n", encoding="utf-8")
        res = srender.render_spec(spec)
        assert "a=1&amp;b=2" in res.html, res.html
        assert "&amp;amp;" not in res.html, "URL must not be double-escaped"


def test_render_emphasis_does_not_corrupt_href():
    # A URL with underscores must not have the emphasis pass rewrite inside href (codex P2).
    with tempfile.TemporaryDirectory() as d:
        spec = Path(d) / "s.md"
        spec.write_text("# T\n\n[x](https://e.test/?q=_hi_)\n", encoding="utf-8")
        res = srender.render_spec(spec)
        assert "q=_hi_" in res.html, res.html
        assert "<em>hi</em>" not in res.html, "emphasis leaked into the href"


def test_render_backtick_in_href_does_not_inject_attributes():
    # A backtick inside a link destination must NOT let a restored code span break out of
    # the href attribute and inject an onclick (codex P1 XSS). The label renders as text.
    with tempfile.TemporaryDirectory() as d:
        spec = Path(d) / "s.md"
        spec.write_text('# T\n\n[x](https://e.test/`" onclick="alert(1)" x="`)\n', encoding="utf-8")
        res = srender.render_spec(spec)
        assert "onclick" not in res.html, res.html
        assert "x" in res.html  # label survives as plain text
        # a normal link still works
        spec.write_text("# T\n\n[ok](https://good.test/p)\n", encoding="utf-8")
        res = srender.render_spec(spec)
        assert '<a href="https://good.test/p"' in res.html, res.html


def test_render_unsafe_scheme_link_is_stripped():
    # An untrusted spec's javascript: link must NOT become a clickable same-origin <a>
    # (codex P2) — the label survives as plain text, the scheme does not.
    with tempfile.TemporaryDirectory() as d:
        spec = Path(d) / "s.md"
        spec.write_text("# T\n\n[click me](javascript:fetch('/api/comments'))\n", encoding="utf-8")
        res = srender.render_spec(spec)
        assert "javascript:" not in res.html, res.html
        assert "click me" in res.html, "label text should survive"
        for safe in ["#x", "https://ok.test", "./rel.md", "mailto:a@b.c"]:
            assert srender._is_safe_href(safe), safe
        for bad in ["javascript:x", "data:text/html,x", "vbscript:x"]:
            assert not srender._is_safe_href(bad), bad


def test_render_heading_ids_are_globally_unique():
    # `# Foo`, `# Foo`, `# Foo 1` must yield three DISTINCT ids (a naive per-base counter
    # would collide foo-1 with the suffixed dup) so links/anchoring don't target a dup id.
    with tempfile.TemporaryDirectory() as d:
        spec = Path(d) / "s.md"
        spec.write_text("# Foo\n\n# Foo\n\n# Foo 1\n", encoding="utf-8")
        res = srender.render_spec(spec)
        ids = [hid for (_lv, _t, hid) in res.headings]
        assert len(ids) == len(set(ids)), ("duplicate heading ids", ids)
        assert ids == ["foo", "foo-1", "foo-1-1"] or len(set(ids)) == 3, ids


def test_render_image_alt_with_inline_code_does_not_crash():
    # `![`id`](./assets/x.png)` — alt with inline code must not raise IndexError (500) when
    # render_image re-handles the alt (codex P2).
    with tempfile.TemporaryDirectory() as d:
        spec = Path(d) / "s.md"
        (spec.parent / "assets").mkdir()
        (spec.parent / "assets" / "x.png").write_bytes(b"\x89PNG\r\n")
        spec.write_text("# T\n\n![`id` label](./assets/x.png)\n", encoding="utf-8")
        res = srender.render_spec(spec)  # must not raise
        assert "/asset/x.png" in res.html, res.html
        assert "\x00CODE" not in res.html, "sentinels must be fully restored"


def test_render_accepted_image_types_have_mime():
    # Every extension render_image ACCEPTS must have a non-octet-stream MIME on the server
    # side (codex P3 — avif/bmp/ico were accepted but served as octet-stream).
    from reviewlib.specweb import server as _srv

    for ext in srender.IMAGE_MIME_TYPES:
        mime = _srv._asset_content_type("fig." + ext)
        assert mime.startswith("image/"), (ext, mime)
    assert _srv._asset_content_type("x.txt") == "application/octet-stream"


def test_render_non_image_asset_not_registered():
    # `![x](./assets/notes.txt)` must NOT register a non-image file for serving (codex P2);
    # it renders as a missing-figure placeholder instead.
    with tempfile.TemporaryDirectory() as d:
        spec = Path(d) / "s.md"
        (spec.parent / "assets").mkdir()
        (spec.parent / "assets" / "notes.txt").write_text("private", encoding="utf-8")
        spec.write_text("# T\n\n![x](./assets/notes.txt)\n", encoding="utf-8")
        res = srender.render_spec(spec)
        assert "notes.txt" not in res.assets, "non-image must not be registered"
        assert "figure missing" in res.html or "/asset/notes.txt" not in res.html


def test_render_image_src_url_and_entity_decoded():
    # `./assets/my%20diagram.svg` (URL-encoded) and `./assets/a&b.svg` (entity-escaped
    # after html.escape) must resolve to the real on-disk files, not render as missing
    # (codex P2 — src was not decoded before the disk lookup).
    with tempfile.TemporaryDirectory() as d:
        spec = Path(d) / "s.md"
        (spec.parent / "assets").mkdir()
        (spec.parent / "assets" / "my diagram.svg").write_text("<svg/>", encoding="utf-8")
        (spec.parent / "assets" / "a&b.svg").write_text("<svg/>", encoding="utf-8")
        spec.write_text("# T\n\n![d](./assets/my%20diagram.svg)\n\n![e](./assets/a&b.svg)\n", encoding="utf-8")
        res = srender.render_spec(spec)
        assert "my diagram.svg" in res.assets, res.assets
        assert "a&b.svg" in res.assets, res.assets
        assert "figure missing" not in res.html, res.html


def test_render_image_encoded_slash_does_not_probe_outside():
    # `![x](%2Fetc%2Fpasswd)` must NOT let an encoded slash decode into an absolute path
    # that `assets_dir / fname` then probes outside the assets dir (a file-existence oracle,
    # codex P2). It renders as a missing-figure placeholder regardless.
    with tempfile.TemporaryDirectory() as d:
        spec = Path(d) / "s.md"
        (spec.parent / "assets").mkdir()
        spec.write_text("# T\n\n![x](%2Fetc%2Fpasswd)\n\n![y](..%2F..%2Fsecret.svg)\n", encoding="utf-8")
        res = srender.render_spec(spec)
        # nothing absolute/parent registered for serving
        for k in res.assets:
            assert "/" not in k and ".." != k, k
        assert "/etc/passwd" not in res.html
        assert "figure missing" in res.html or "<img" not in res.html


def test_render_heading_slug_from_visible_text():
    # `## See [API](api.md)` must slug to GitHub's rendered-text "see-api", not include the
    # link destination (codex P2).
    with tempfile.TemporaryDirectory() as d:
        spec = Path(d) / "s.md"
        spec.write_text("# See [API](api.md)\n", encoding="utf-8")
        res = srender.render_spec(spec)
        ids = [hid for (_lv, _t, hid) in res.headings]
        assert ids == ["see-api"], ids


def test_render_heading_preserves_content_hash():
    # `## C#` / `## F#` must keep the trailing # in both text and slug (codex P2) — only an
    # ATX closing hash sequence (space-delimited) is stripped.
    with tempfile.TemporaryDirectory() as d:
        spec = Path(d) / "s.md"
        spec.write_text("# C#\n\n## Done ##\n", encoding="utf-8")
        res = srender.render_spec(spec)
        texts = [t for (_lv, t, _h) in res.headings]
        ids = [hid for (_lv, _t, hid) in res.headings]
        assert texts[0] == "C#", texts
        assert ids[0] == "c", ids  # slug drops non-word chars (GitHub: "C#" -> "c")
        assert texts[1] == "Done", ("ATX closing ## should be stripped", texts)


def test_render_asset_with_space_is_url_encoded():
    # A figure name with a space must emit a %20-encoded URL (codex P2) so the browser's
    # encoded request matches; the server must decode before the disk lookup.
    with tempfile.TemporaryDirectory() as d:
        spec = Path(d) / "s.md"
        (spec.parent / "assets").mkdir()
        (spec.parent / "assets" / "my diagram.svg").write_text("<svg/>", encoding="utf-8")
        spec.write_text("# T\n\n![d](./assets/my diagram.svg)\n", encoding="utf-8")
        res = srender.render_spec(spec)
        assert "/asset/my%20diagram.svg" in res.html, res.html
        assert "my diagram.svg" in res.assets


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #
def test_store_roundtrip_and_0600():
    with _TempStoreEnv():
        store = SpecStore(FIXTURE)
        c = store.add_comment(quote="the cascade winner", body="why?", section_id="1-overview", section_title="1. Overview")
        assert c["status"] == "pending"
        assert c["batch"] is None
        # persisted + round-trips
        again = SpecStore(FIXTURE).get_comment(c["id"])
        assert again is not None and again["body"] == "why?"
        # 0600 perms
        mode = stat.S_IMODE(os.stat(store.path).st_mode)
        assert mode == 0o600, oct(mode)
        # keyed by sha1 of abspath
        assert store.path.name == spec_key(FIXTURE) + ".json"


def test_store_reply_and_status_transitions():
    with _TempStoreEnv():
        store = SpecStore(FIXTURE)
        c = store.add_comment(quote="q", body="b")
        # reply to a pending comment leaves it pending
        store.add_reply(c["id"], body="answer", author="claude")
        assert store.get_comment(c["id"])["status"] == "pending"
        assert len(store.get_comment(c["id"])["replies"]) == 1
        # submit flips pending -> submitted with a shared batch
        res = store.submit_pending()
        assert res["count"] == 1 and res["batch"]
        sub = store.get_comment(c["id"])
        assert sub["status"] == "submitted" and sub["batch"] == res["batch"]
        # a reply to a submitted comment marks it answered
        store.add_reply(c["id"], body="more")
        assert store.get_comment(c["id"])["status"] == "answered"
        # explicit resolve
        store.set_status(c["id"], "resolved")
        assert store.get_comment(c["id"])["status"] == "resolved"


def test_store_submit_pending_only_touches_pending():
    with _TempStoreEnv():
        store = SpecStore(FIXTURE)
        a = store.add_comment(quote="a", body="a")
        store.submit_pending()  # a -> submitted
        b = store.add_comment(quote="b", body="b")  # new pending
        res = store.submit_pending()
        assert res["count"] == 1, "only the second comment was pending"
        # a keeps its original batch (not re-stamped)
        assert store.get_comment(a["id"])["batch"] != res["batch"]
        assert store.get_comment(b["id"])["status"] == "submitted"


def test_store_delete():
    with _TempStoreEnv():
        store = SpecStore(FIXTURE)
        c = store.add_comment(quote="q", body="b")
        assert store.delete_comment(c["id"]) is True
        assert store.get_comment(c["id"]) is None
        assert store.delete_comment("nope") is False


def test_store_import_seed_and_unanchored_preserved():
    with _TempStoreEnv():
        payload = json.loads(SEED.read_text(encoding="utf-8"))
        store = SpecStore(FIXTURE)
        res = store.import_thread(payload)
        assert res["imported"] == 2
        comments = store.all_comments()
        assert len(comments) == 2
        # the second seed comment has a quote not in the spec — store still keeps it; the
        # client flags it unanchored. The store must never drop it.
        bodies = {c["body"] for c in comments}
        assert any("unanchored" in b for b in bodies)
        # a submitted seed item keeps its batch + replies
        sub = [c for c in comments if c["status"] == "submitted"][0]
        assert sub["batch"] and sub["replies"]


def test_store_import_coerces_non_string_fields():
    # A seed/import payload with a non-string body (or other field) must not crash on
    # .strip() — it is coerced, not a 500/traceback (codex P2).
    with _TempStoreEnv():
        store = SpecStore(FIXTURE)
        res = store.import_thread({"comments": [{"body": 123, "quote": 456, "author": 7}]})
        assert res["imported"] == 1
        c = store.all_comments()[0]
        assert c["body"] == "123" and c["quote"] == "456" and c["author"] == "7"
        # a reply with a non-string body is coerced too
        store.import_thread({"comments": [{"body": "ok", "replies": [{"body": 999}]}]})
        assert any(r["body"] == "999" for cc in store.all_comments() for r in cc["replies"])


def test_store_import_non_list_replies_does_not_crash():
    # A malformed seed with a truthy non-list `replies` (e.g. 123) must not raise TypeError
    # (codex P2) — non-list replies are treated as empty.
    with _TempStoreEnv():
        store = SpecStore(FIXTURE)
        res = store.import_thread({"comments": [{"body": "x", "replies": 123}]})
        assert res["imported"] == 1
        assert store.all_comments()[0]["replies"] == []


def test_store_export_markdown():
    with _TempStoreEnv():
        store = SpecStore(FIXTURE)
        c = store.add_comment(quote="the cascade winner", body="why this one?", section_title="1. Overview")
        store.add_reply(c["id"], body="because probe says so", author="claude")
        md = store.export_markdown()
        assert "the cascade winner" in md
        assert "why this one?" in md
        assert "because probe says so" in md
        assert md.startswith("# Spec review")


def test_store_import_rejects_bad_payload():
    with _TempStoreEnv():
        store = SpecStore(FIXTURE)
        for bad in [{}, {"comments": "x"}, []]:
            try:
                store.import_thread(bad)  # type: ignore[arg-type]
                raise AssertionError(f"should have rejected {bad!r}")
            except ValueError:
                pass


# --------------------------------------------------------------------------- #
# server (loopback, ephemeral port)
# --------------------------------------------------------------------------- #
class _Server:
    def __init__(self):
        self.httpd = sserver.make_server(FIXTURE, host="127.0.0.1", port=0)
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

    def post(self, path, obj, headers=None):
        c = self.conn()
        h = {"Content-Type": "application/json"}
        if headers:
            h.update(headers)
        c.request("POST", path, body=json.dumps(obj).encode("utf-8"), headers=h)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body, dict(r.getheaders())

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_server_serves_index_spec_and_asset():
    with _TempStoreEnv():
        s = _Server()
        try:
            st, body, _ = s.get("/")
            assert st == 200 and b"Spec review" in body
            st, body, _ = s.get("/api/spec")
            assert st == 200
            data = json.loads(body)
            assert "<h1" in data["html"] and data["headings"]
            assert "/asset/fig-arch.svg" in data["html"]
            # the figure is served as a real HTTP resource (the whole point)
            st, body, hdrs = s.get("/asset/fig-arch.svg")
            assert st == 200, st
            assert b"<svg" in body
            assert "image/svg+xml" in hdrs.get("Content-Type", "")
            # SVG from a (possibly untrusted) spec must be served inertly: a sandbox CSP
            # kills inline <script> on a direct top-level open + nosniff.
            assert "sandbox" in hdrs.get("Content-Security-Policy", ""), hdrs
            assert hdrs.get("X-Content-Type-Options") == "nosniff", hdrs
        finally:
            s.stop()


def test_server_asset_traversal_blocked():
    with _TempStoreEnv():
        s = _Server()
        try:
            st, _, _ = s.get("/asset/..%2f..%2fetc%2fpasswd")
            assert st == 404, st
            st, _, _ = s.get("/asset/../store.py")
            assert st in (404, 400), st
        finally:
            s.stop()


def test_server_unreferenced_asset_not_served():
    # Only figures the markdown REFERENCES are served; an unrelated file sitting in the
    # assets dir must 404 (else a reachable reviewer over Tailscale could download it).
    with _TempStoreEnv(), tempfile.TemporaryDirectory() as d:
        root = Path(d)
        spec = root / "s.md"
        assets = root / "assets"
        assets.mkdir()
        (assets / "fig.svg").write_text("<svg id='ref'/>", encoding="utf-8")
        (assets / "private-notes.txt").write_text("CONFIDENTIAL-PAYLOAD", encoding="utf-8")
        spec.write_text("# T\n\n![x](./assets/fig.svg)\n", encoding="utf-8")
        httpd = sserver.make_server(spec, host="127.0.0.1", port=0)
        port = httpd.server_address[1]
        th = threading.Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request("GET", "/api/spec")
            c.getresponse().read()
            c.close()
            # referenced figure -> 200
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request("GET", "/asset/fig.svg")
            r = c.getresponse(); r.read(); c.close()
            assert r.status == 200, r.status
            # unreferenced file in the same dir -> 404
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request("GET", "/asset/private-notes.txt")
            r = c.getresponse(); body = r.read(); c.close()
            assert r.status == 404, (r.status, body)
            assert b"CONFIDENTIAL-PAYLOAD" not in body
        finally:
            httpd.shutdown()
            httpd.server_close()


def _serve_and_get(spec, path):
    httpd = sserver.make_server(spec, host="127.0.0.1", port=0)
    port = httpd.server_address[1]
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("GET", "/api/spec")
        c.getresponse().read()
        c.close()
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("GET", path)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_server_asset_symlink_file_escape_blocked():
    # A referenced asset that is a symlink to a file OUTSIDE the spec tree must NOT be
    # served, even though the renderer cached its path (codex P1 — local file disclosure
    # for untrusted specs). The followed path must stay under the resolved spec directory.
    with _TempStoreEnv(), tempfile.TemporaryDirectory() as d:
        root = Path(d)
        secret = root / "secret.txt"  # OUTSIDE the spec dir
        secret.write_text("TOP SECRET", encoding="utf-8")
        spec_dir = root / "spec"
        (spec_dir / "assets").mkdir(parents=True)
        link = spec_dir / "assets" / "leak.svg"
        try:
            link.symlink_to(secret)
        except (OSError, NotImplementedError):
            return  # platform without symlink support
        spec = spec_dir / "s.md"
        spec.write_text("# T\n\n![x](./assets/leak.svg)\n", encoding="utf-8")
        st, body = _serve_and_get(spec, "/asset/leak.svg")
        assert st == 404, (st, body)
        assert b"TOP SECRET" not in body


def test_server_asset_symlink_within_spec_but_outside_assets_blocked():
    # `assets/leak.svg -> ../.env` escapes the assets dir but still resolves UNDER the spec
    # dir; it must still 404 — the asset has to stay inside the assets dir specifically
    # (codex P1 follow-up), not merely inside the spec tree.
    with _TempStoreEnv(), tempfile.TemporaryDirectory() as d:
        spec_dir = Path(d) / "spec"
        (spec_dir / "assets").mkdir(parents=True)
        env = spec_dir / ".env"  # sibling of assets/, inside the spec dir
        env.write_text("SECRET_TOKEN=hunter2", encoding="utf-8")
        link = spec_dir / "assets" / "leak.svg"
        try:
            link.symlink_to(Path("..") / ".env")
        except (OSError, NotImplementedError):
            return
        spec = spec_dir / "s.md"
        spec.write_text("# T\n\n![x](./assets/leak.svg)\n", encoding="utf-8")
        st, body = _serve_and_get(spec, "/asset/leak.svg")
        assert st == 404, (st, body)
        assert b"hunter2" not in body


def test_server_asset_image_symlink_to_nonimage_inside_assets_blocked():
    # `assets/leak.svg -> assets/private-notes.txt`: the symlink stays INSIDE the assets
    # dir (containment passes) but resolves to a NON-image file. It must 404, not serve the
    # text file under an image name (codex P2 — figures-only bypass via symlink).
    with _TempStoreEnv(), tempfile.TemporaryDirectory() as d:
        spec_dir = Path(d) / "spec"
        assets = spec_dir / "assets"
        assets.mkdir(parents=True)
        (assets / "private-notes.txt").write_text("INSIDE-ASSETS-SECRET", encoding="utf-8")
        try:
            (assets / "leak.svg").symlink_to(assets / "private-notes.txt")
        except (OSError, NotImplementedError):
            return
        spec = spec_dir / "s.md"
        spec.write_text("# T\n\n![x](./assets/leak.svg)\n", encoding="utf-8")
        st, body = _serve_and_get(spec, "/asset/leak.svg")
        assert st == 404, (st, body)
        assert b"INSIDE-ASSETS-SECRET" not in body


def test_server_symlinked_assets_dir_escape_blocked():
    # If the `assets` DIRECTORY itself is a symlink pointing outside the spec tree, a
    # referenced basename must still be refused (codex P1 follow-up) — containment is on the
    # resolved spec dir, so a symlinked assets root can't serve its target's files.
    with _TempStoreEnv(), tempfile.TemporaryDirectory() as d:
        root = Path(d)
        outside = root / "outside"
        outside.mkdir()
        (outside / "known.svg").write_text("<svg id='leak'>SECRET-DIR-PAYLOAD</svg>", encoding="utf-8")
        spec_dir = root / "spec"
        spec_dir.mkdir()
        try:
            (spec_dir / "assets").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            return
        spec = spec_dir / "s.md"
        spec.write_text("# T\n\n![x](./assets/known.svg)\n", encoding="utf-8")
        st, body = _serve_and_get(spec, "/asset/known.svg")
        assert st == 404, (st, body)
        assert b"SECRET-DIR-PAYLOAD" not in body


def test_server_comment_crud_and_submit():
    with _TempStoreEnv():
        s = _Server()
        try:
            st, body, _ = s.post("/api/comments", {
                "quote": "the cascade winner", "body": "which candidate wins?",
                "section_id": "1-overview", "section_title": "1. Overview",
            })
            assert st == 201, (st, body)
            cid = json.loads(body)["comment"]["id"]
            # reply threads under it
            st, body, _ = s.post("/api/comments/%s/reply" % cid, {"body": "the probe-positive one"})
            assert st == 200, (st, body)
            assert json.loads(body)["comment"]["replies"][0]["body"] == "the probe-positive one"
            # pending tray: one pending
            st, body, _ = s.get("/api/comments")
            assert len([c for c in json.loads(body) if c["status"] == "pending"]) == 1
            # submit flips it
            st, body, _ = s.post("/api/submit", {})
            assert st == 200 and json.loads(body)["count"] == 1
            st, body, _ = s.get("/api/comments")
            assert json.loads(body)[0]["status"] == "submitted"
            # export endpoint returns markdown
            st, body, hdrs = s.get("/api/export")
            assert st == 200 and "text/markdown" in hdrs.get("Content-Type", "")
            assert b"which candidate wins?" in body
        finally:
            s.stop()


def test_server_comment_requires_body():
    with _TempStoreEnv():
        s = _Server()
        try:
            st, _, _ = s.post("/api/comments", {"quote": "x"})  # no body
            assert st == 400, st
        finally:
            s.stop()


def test_server_origin_guard_loopback_foreign_contenttype():
    with _TempStoreEnv():
        s = _Server()
        try:
            # loopback Origin allowed
            st, _, _ = s.post("/api/comments", {"body": "ok"}, headers={"Origin": "http://127.0.0.1:%d" % s.port})
            assert st == 201, st
            # a foreign origin REJECTED (Origin host != served Host, not in allowed set)
            st, _, _ = s.post("/api/comments", {"body": "evil"}, headers={"Origin": "http://evil.example.com"})
            assert st == 403, st
            # wrong content-type REJECTED (415) even from loopback
            c = s.conn()
            c.request("POST", "/api/comments", body=b"body=x", headers={"Content-Type": "text/plain"})
            r = c.getresponse(); r.read(); c.close()
            assert r.status == 415, r.status
        finally:
            s.stop()


def test_server_write_host_must_be_allowlisted_anti_rebinding():
    # Anti-DNS-rebinding (codex P2): a write whose Host is NOT in the allowlist is refused
    # EVEN with a matching same-origin Origin — a rebound attacker hostname (its own DNS
    # name pointed at loopback) would otherwise pass a pure same-origin check.
    with _TempStoreEnv():
        s = _Server()
        try:
            # Host == Origin == a non-allowlisted hostname (the rebinding scenario) -> 403
            st, _, _ = s.post(
                "/api/comments", {"body": "rebound"},
                headers={"Host": "attacker.test:%d" % s.port, "Origin": "http://attacker.test:%d" % s.port},
            )
            assert st == 403, st
            # loopback Host (allowlisted) with matching Origin -> allowed
            st, _, _ = s.post(
                "/api/comments", {"body": "ok"},
                headers={"Host": "127.0.0.1:%d" % s.port, "Origin": "http://127.0.0.1:%d" % s.port},
            )
            assert st == 201, st
        finally:
            s.stop()


def test_server_write_allowed_for_env_allowlisted_host():
    # A Host explicitly allowlisted via $REVIEW_SPECWEB_ALLOWED_HOSTS (e.g. a Tailscale
    # name) with a matching Origin is allowed; a foreign Origin against it is rejected.
    old = os.environ.get("REVIEW_SPECWEB_ALLOWED_HOSTS")
    os.environ["REVIEW_SPECWEB_ALLOWED_HOSTS"] = "phone.example.ts.net"
    try:
        with _TempStoreEnv():
            s = _Server()
            try:
                st, _, _ = s.post(
                    "/api/comments", {"body": "from phone"},
                    headers={"Host": "phone.example.ts.net:%d" % s.port, "Origin": "http://phone.example.ts.net:%d" % s.port},
                )
                assert st == 201, st
                # foreign Origin against the allowlisted Host -> CSRF reject
                st, _, _ = s.post(
                    "/api/comments", {"body": "evil"},
                    headers={"Host": "phone.example.ts.net:%d" % s.port, "Origin": "http://attacker.test"},
                )
                assert st == 403, st
            finally:
                s.stop()
    finally:
        if old is None:
            os.environ.pop("REVIEW_SPECWEB_ALLOWED_HOSTS", None)
        else:
            os.environ["REVIEW_SPECWEB_ALLOWED_HOSTS"] = old


def test_server_body_size_cap():
    with _TempStoreEnv():
        s = _Server()
        try:
            big = {"body": "x" * (300 * 1024)}
            # The cap is enforced from the declared Content-Length BEFORE the body is read,
            # so the server may answer 413 and close the socket before the client finishes
            # streaming the oversized payload — a connection reset mid-send is an equally
            # valid "rejected, body never ingested" outcome. Accept either; the failure mode
            # we guard against is a 201 (the big body slurped + stored).
            try:
                st, _, _ = s.post("/api/comments", big)
            except (ConnectionResetError, BrokenPipeError):
                st = 413
            assert st == 413, st
            # the oversized comment must NOT have been stored
            assert SpecStore(FIXTURE).all_comments() == [], "oversized body must not persist"
        finally:
            s.stop()


def test_server_serves_url_encoded_asset_name():
    # End-to-end: a figure with a space is requested as %20-encoded and must be served
    # (the server unquotes before the disk lookup) — codex P2.
    with _TempStoreEnv(), tempfile.TemporaryDirectory() as d:
        spec = Path(d) / "s.md"
        (spec.parent / "assets").mkdir()
        (spec.parent / "assets" / "my diagram.svg").write_text("<svg id='x'/>", encoding="utf-8")
        spec.write_text("# T\n\n![d](./assets/my diagram.svg)\n", encoding="utf-8")
        httpd = sserver.make_server(spec, host="127.0.0.1", port=0)
        port = httpd.server_address[1]
        th = threading.Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request("GET", "/api/spec")
            c.getresponse().read()
            c.close()
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request("GET", "/asset/my%20diagram.svg")
            r = c.getresponse()
            body = r.read()
            c.close()
            assert r.status == 200, r.status
            assert b"<svg" in body
        finally:
            httpd.shutdown()
            httpd.server_close()


def test_server_import_seed_route():
    with _TempStoreEnv():
        s = _Server()
        try:
            payload = json.loads(SEED.read_text(encoding="utf-8"))
            st, body, _ = s.post("/api/import", payload)
            assert st == 200 and json.loads(body)["imported"] == 2
            st, body, _ = s.get("/api/comments")
            assert len(json.loads(body)) == 2
        finally:
            s.stop()


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
