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
from reviewlib.specweb.store import (  # noqa: E402
    AGENT_AUTHOR,
    NEW_DRAFT_SLOT,
    SpecStore,
    edit_draft_slot,
    spec_key,
)

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


def test_store_kind_defaults_remark_and_persists_question():
    # A note's kind drives the sidebar label/icon; default is remark, an explicit question
    # round-trips, and an invalid kind is coerced to the remark default (never crashes).
    with _TempStoreEnv():
        store = SpecStore(FIXTURE)
        r = store.add_comment(quote="q", body="a remark")
        assert r["kind"] == "remark", "default kind is remark"
        q = store.add_comment(quote="q", body="a question", kind="question")
        assert q["kind"] == "question"
        bad = store.add_comment(quote="q", body="bogus", kind="not-a-kind")
        assert bad["kind"] == "remark", "invalid kind coerces to remark"
        # persists across a fresh store instance
        assert SpecStore(FIXTURE).get_comment(q["id"])["kind"] == "question"


def test_store_edit_comment_changes_body_keeps_status():
    # Editing a note's text must not disturb its status/batch — the reviewer is correcting
    # what they wrote, not reopening the thread.
    with _TempStoreEnv():
        store = SpecStore(FIXTURE)
        c = store.add_comment(quote="q", body="original", kind="question")
        store.submit_pending()
        assert store.get_comment(c["id"])["status"] == "submitted"
        edited = store.edit_comment(c["id"], body="corrected text")
        assert edited is not None and edited["body"] == "corrected text"
        again = store.get_comment(c["id"])
        assert again["status"] == "submitted", "edit must not reset status"
        assert again["kind"] == "question", "kind preserved when not passed"
        # persists across a fresh store instance (not an in-memory-only mutation)
        assert SpecStore(FIXTURE).get_comment(c["id"])["body"] == "corrected text"
        # editing the kind too; a bogus kind coerces to remark
        store.edit_comment(c["id"], body="corrected text", kind="remark")
        assert store.get_comment(c["id"])["kind"] == "remark"
        store.edit_comment(c["id"], body="corrected text", kind="garbage")
        assert store.get_comment(c["id"])["kind"] == "remark", "invalid kind coerces to remark"
        # empty body rejected; unknown id -> None
        try:
            store.edit_comment(c["id"], body="   ")
            raise AssertionError("empty body should raise")
        except ValueError:
            pass
        assert store.edit_comment("nope", body="x") is None


def test_store_import_carries_kind():
    with _TempStoreEnv():
        store = SpecStore(FIXTURE)
        store.import_thread({"comments": [
            {"body": "a q", "kind": "question"},
            {"body": "a r"},  # no kind -> remark
        ]})
        kinds = {c["body"]: c["kind"] for c in store.all_comments()}
        assert kinds["a q"] == "question"
        assert kinds["a r"] == "remark"


def test_store_review_payload_structure_and_counts():
    # The structured review payload is what the launching AGENT receives on submit: every
    # comment carries its id (so the agent can `reply <id>`), kind, status, body, and its
    # reply thread; counts split questions vs remarks.
    with _TempStoreEnv():
        store = SpecStore(FIXTURE)
        q = store.add_comment(quote="the cascade winner", body="which one?", kind="question", section_title="1. Overview")
        store.add_comment(quote="x", body="a remark", kind="remark")
        store.add_reply(q["id"], body="the probe-positive one", author="agent")
        payload = store.review_payload()
        assert payload["spec_path"] == store.spec_path
        assert payload["counts"] == {"questions": 1, "remarks": 1, "total": 2}, payload["counts"]
        by_id = {c["id"]: c for c in payload["comments"]}
        assert q["id"] in by_id, "every comment carries its id for the agent to answer"
        rec = by_id[q["id"]]
        assert rec["kind"] == "question" and rec["body"] == "which one?"
        assert rec["replies"][0]["author"] == "agent"
        assert rec["replies"][0]["body"] == "the probe-positive one"


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


def test_store_submit_returns_review_and_records_last_submit():
    # submit_pending records the batch on the store (last_submit) and returns the structured
    # review the launching process hands to the agent. An empty submit does not touch it.
    with _TempStoreEnv():
        store = SpecStore(FIXTURE)
        assert store.last_submit() is None
        empty = store.submit_pending()  # nothing pending
        assert empty["count"] == 0 and empty["batch"] is None
        assert store.last_submit() is None, "an empty submit must not record a batch"
        store.add_comment(quote="q", body="why this one?", kind="question")
        res = store.submit_pending()
        assert res["count"] == 1 and res["batch"]
        assert res["review"]["counts"]["total"] == 1
        assert res["review"]["batch"] == res["batch"]
        assert store.last_submit() == res["batch"], "submit recorded for the launching process"
        # persists across a fresh store instance
        assert SpecStore(FIXTURE).last_submit() == res["batch"]
        # a SECOND non-empty submit REPLACES last_submit with the newer batch
        store.add_comment(quote="q2", body="another", kind="remark")
        res2 = store.submit_pending()
        assert res2["batch"] != res["batch"]
        assert store.last_submit() == res2["batch"], "last_submit advances to the newest batch"
        # an EMPTY submit after that does NOT reset/clear it
        store.submit_pending()
        assert store.last_submit() == res2["batch"], "an empty submit must not touch last_submit"


def test_store_import_replace_clears_drafts_and_last_submit():
    # A REPLACE seed discards the previous review entirely — including its in-progress
    # drafts (so a stale composer can't reopen over the fresh seed) and its last_submit
    # marker. A non-replace append leaves them intact.
    with _TempStoreEnv():
        store = SpecStore(FIXTURE)
        store.add_comment(quote="q", body="old")
        store.save_draft(NEW_DRAFT_SLOT, body="half-typed old draft")
        store.submit_pending()
        assert store.last_submit() is not None
        assert store.get_draft(NEW_DRAFT_SLOT) is not None
        # replace wipes drafts + last_submit
        store.import_thread({"comments": [{"body": "fresh seed"}]}, replace=True)
        assert store.get_draft(NEW_DRAFT_SLOT) is None, "replace must clear in-progress drafts"
        assert store.last_submit() is None, "replace must clear last_submit"
        assert [c["body"] for c in store.all_comments()] == ["fresh seed"]
        # a non-replace append leaves a fresh draft intact
        store.save_draft(NEW_DRAFT_SLOT, body="new draft")
        store.import_thread({"comments": [{"body": "another"}]}, replace=False)
        assert store.get_draft(NEW_DRAFT_SLOT) is not None, "append must not touch drafts"


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
            # submit flips it AND returns the structured review for the agent (with ids)
            st, body, _ = s.post("/api/submit", {})
            assert st == 200
            sub = json.loads(body)
            assert sub["count"] == 1
            assert sub["review"]["counts"]["total"] == 1
            assert sub["review"]["comments"][0]["id"] == cid
            assert sub["review"]["comments"][0]["body"] == "which candidate wins?"
            st, body, _ = s.get("/api/comments")
            assert json.loads(body)[0]["status"] == "submitted"
            # the removed markdown-export endpoint must be gone (404), not silently served
            st, _, _ = s.get("/api/export")
            assert st == 404, st
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


def test_server_comment_kind_and_edit():
    # The create payload carries a kind (question|remark); /edit updates the body and the
    # kind in place without changing status.
    with _TempStoreEnv():
        s = _Server()
        try:
            st, body, _ = s.post("/api/comments", {"body": "is this right?", "kind": "question"})
            assert st == 201, (st, body)
            cid = json.loads(body)["comment"]["id"]
            assert json.loads(body)["comment"]["kind"] == "question"
            # a create WITHOUT a kind defaults to remark at the HTTP boundary
            st, body, _ = s.post("/api/comments", {"body": "no kind"})
            assert st == 201 and json.loads(body)["comment"]["kind"] == "remark", body
            # edit the body + flip to remark
            st, body, _ = s.post("/api/comments/%s/edit" % cid, {"body": "rephrased", "kind": "remark"})
            assert st == 200, (st, body)
            rec = json.loads(body)["comment"]
            assert rec["body"] == "rephrased" and rec["kind"] == "remark"
            # editing with an empty body -> 400; editing an unknown id -> 404
            st, _, _ = s.post("/api/comments/%s/edit" % cid, {"body": "   "})
            assert st == 400, st
            st, _, _ = s.post("/api/comments/does-not-exist/edit", {"body": "x"})
            assert st == 404, st
            # /edit WITHOUT kind keeps the existing kind (HTTP path passes kind=None).
            st, body, _ = s.post("/api/comments/%s/edit" % cid, {"body": "no kind given"})
            assert st == 200 and json.loads(body)["comment"]["kind"] == "remark", body
            # an invalid kind on the HTTP path coerces to remark (never errors).
            st, body, _ = s.post("/api/comments/%s/edit" % cid, {"body": "x", "kind": "not-a-kind"})
            assert st == 200 and json.loads(body)["comment"]["kind"] == "remark", body
        finally:
            s.stop()


def test_server_create_honours_explicit_author():
    # The UI omits author (single implicit reviewer), but an explicit author on the create
    # payload is still honoured so import/seed-style writes round-trip. Pin it so a future
    # cleanup that drops the author line is caught.
    with _TempStoreEnv():
        s = _Server()
        try:
            st, body, _ = s.post("/api/comments", {"body": "x", "author": "alex"})
            assert st == 201, (st, body)
            assert json.loads(body)["comment"]["author"] == "alex"
            # omitting author defaults to the implicit reviewer
            st, body, _ = s.post("/api/comments", {"body": "y"})
            assert json.loads(body)["comment"]["author"] == "reviewer"
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


# --------------------------------------------------------------------------- #
# Feature 2: drafts (in-progress composer text, debounced + reload-safe)
# --------------------------------------------------------------------------- #
def test_store_draft_save_restore_and_clear():
    # A new-note draft persists with its selection context and restores across a fresh store
    # instance (the reload case); an empty body clears the slot.
    with _TempStoreEnv():
        store = SpecStore(FIXTURE)
        d = store.save_draft(
            NEW_DRAFT_SLOT, body="half-typed que", kind="question",
            quote="the cascade winner", section_id="1-overview", section_title="1. Overview",
            start=3, end=9,
        )
        assert d["slot"] == NEW_DRAFT_SLOT and d["body"] == "half-typed que"
        assert d["kind"] == "question" and d["quote"] == "the cascade winner"
        # restores across a fresh instance (page reload)
        again = SpecStore(FIXTURE).get_draft(NEW_DRAFT_SLOT)
        assert again is not None and again["body"] == "half-typed que"
        assert again["section_id"] == "1-overview" and again["start"] == 3
        # all_drafts maps slot -> draft
        assert NEW_DRAFT_SLOT in SpecStore(FIXTURE).all_drafts()
        # an empty body clears the slot (returns {})
        assert store.save_draft(NEW_DRAFT_SLOT, body="   ") == {}
        assert SpecStore(FIXTURE).get_draft(NEW_DRAFT_SLOT) is None


# === draft ordering token / tombstone (review-cli#30) ========================
# The store rejects a token-carrying write that is OLDER than the slot's last applied token,
# so a late out-of-order autosave can't clobber a newer write/clear regardless of the order
# the per-slot lock is acquired. A token-less write keeps legacy last-writer-wins.

def test_store_draft_rejects_out_of_order_token_write():
    # An older token (a late autosave that lost the race) is rejected; the newer write stands.
    with _TempStoreEnv():
        store = SpecStore(FIXTURE)
        store.save_draft(NEW_DRAFT_SLOT, body="newer text", token=5)
        # A stale autosave with a LOWER token arrives after -> rejected, slot unchanged.
        out = store.save_draft(NEW_DRAFT_SLOT, body="stale older text", token=3)
        assert out.get("body") == "newer text", "stale write must not overwrite the newer draft"
        assert SpecStore(FIXTURE).get_draft(NEW_DRAFT_SLOT)["body"] == "newer text"
        # The equal token is also stale (a re-delivery of the already-applied write).
        store.save_draft(NEW_DRAFT_SLOT, body="dup of token 5", token=5)
        assert SpecStore(FIXTURE).get_draft(NEW_DRAFT_SLOT)["body"] == "newer text"
        # A strictly HIGHER token is applied.
        store.save_draft(NEW_DRAFT_SLOT, body="newest text", token=6)
        assert SpecStore(FIXTURE).get_draft(NEW_DRAFT_SLOT)["body"] == "newest text"


def test_store_draft_tombstone_survives_clear_and_blocks_late_write():
    # The tombstone OUTLIVES the draft: after a clear at a high token, a late autosave with a
    # lower token (sent before the clear, arriving after) is rejected — no resurrection.
    with _TempStoreEnv():
        store = SpecStore(FIXTURE)
        store.save_draft(NEW_DRAFT_SLOT, body="half-typed", token=10)
        # The trailing clear wins with a higher token and leaves a tombstone.
        assert store.save_draft(NEW_DRAFT_SLOT, body="", token=11) == {}
        assert store.get_draft_token(NEW_DRAFT_SLOT) == 11
        # A stale autosave (token 10, sent before the clear) lands late -> rejected.
        out = store.save_draft(NEW_DRAFT_SLOT, body="resurrected stale text", token=10)
        assert out == {}, "tombstone must reject a late write after the clear"
        assert SpecStore(FIXTURE).get_draft(NEW_DRAFT_SLOT) is None, "no resurrection"
        # A genuinely NEW draft (a fresh higher token) is still accepted on the same slot.
        store.save_draft(NEW_DRAFT_SLOT, body="a brand-new note", token=12)
        assert SpecStore(FIXTURE).get_draft(NEW_DRAFT_SLOT)["body"] == "a brand-new note"


def test_store_draft_tokenless_write_keeps_legacy_last_writer_wins():
    # A token-less write (legacy client) opts out of the ordering check entirely: it always
    # applies, and it does not record a tombstone that could block a later token-less write.
    with _TempStoreEnv():
        store = SpecStore(FIXTURE)
        store.save_draft(NEW_DRAFT_SLOT, body="first", token=9)
        # No token -> applied regardless of the existing high token (legacy behaviour).
        store.save_draft(NEW_DRAFT_SLOT, body="legacy overwrite")
        assert SpecStore(FIXTURE).get_draft(NEW_DRAFT_SLOT)["body"] == "legacy overwrite"


def test_store_draft_malformed_token_is_ignored():
    # A malformed token (negative, bool, str, float, or an absurdly huge value) is treated as
    # token-less, not as a real ordering value — so it can neither forge a low/high water-mark,
    # park the tombstone at ~maxint, nor crash the store. Each applies as a legacy write
    # (last-writer-wins) and leaves the real tombstone untouched.
    from reviewlib.specweb.store import _MAX_DRAFT_TOKEN
    with _TempStoreEnv():
        store = SpecStore(FIXTURE)
        store.save_draft(NEW_DRAFT_SLOT, body="real", token=4)
        for bad in (-1, True, "5", 5.5, _MAX_DRAFT_TOKEN + 1):
            store.save_draft(NEW_DRAFT_SLOT, body=f"bad-{bad!r}", token=bad)
            assert SpecStore(FIXTURE).get_draft(NEW_DRAFT_SLOT)["body"] == f"bad-{bad!r}"
            assert store.get_draft_token(NEW_DRAFT_SLOT) == 4, f"malformed {bad!r} must not move the tombstone"


def test_store_draft_legacy_token_mix_semantics_on_one_slot():
    # MIXING a token-less (legacy) write with token-carrying ones on the SAME slot: the
    # token-less write applies (legacy last-writer-wins) but deliberately does NOT move the
    # tombstone, so the ordering high-water-mark is unaffected. This is intended: a token-less
    # client opted out of ordering, and a later LOWER-token write is still correctly rejected
    # against the prior tombstone. (Real clients are all-token or all-legacy; this pins the
    # documented semantics for the contrived mix.)
    with _TempStoreEnv():
        store = SpecStore(FIXTURE)
        store.save_draft(NEW_DRAFT_SLOT, body="tokened", token=5)      # tombstone -> 5
        store.save_draft(NEW_DRAFT_SLOT, body="legacy overwrite")      # applies, tombstone stays 5
        assert store.get_draft_token(NEW_DRAFT_SLOT) == 5
        assert SpecStore(FIXTURE).get_draft(NEW_DRAFT_SLOT)["body"] == "legacy overwrite"
        # a later write with token <= 5 is still rejected against the standing tombstone
        out = store.save_draft(NEW_DRAFT_SLOT, body="stale token 4", token=4)
        assert out["body"] == "legacy overwrite", "tombstone still rejects a stale tokened write"


def test_store_max_draft_token_seeds_above_all_tombstones():
    # max_draft_token() is the highest token across ALL slots — the client seeds its
    # per-session counter above it so a reloaded session outranks every durable tombstone.
    with _TempStoreEnv():
        store = SpecStore(FIXTURE)
        assert store.max_draft_token() == 0, "no tokens yet -> 0"
        store.save_draft(NEW_DRAFT_SLOT, body="a", token=3)
        store.save_draft("edit:c1", body="b", token=7)
        store.save_draft(NEW_DRAFT_SLOT, body="", token=8)  # clear leaves an 8 tombstone
        assert store.max_draft_token() == 8


def test_store_reloaded_session_seeded_above_tombstone_is_not_rejected():
    # Regression for the ephemeral-counter-vs-durable-tombstone bug (review-cli#30 review):
    # a prior session left a high tombstone; a reloaded session that SEEDS its counter from
    # max_draft_token() and writes ABOVE it is accepted (autosave keeps working after reload).
    with _TempStoreEnv():
        store = SpecStore(FIXTURE)
        # session 1: type, then the trailing clear bumps the tombstone to 11
        store.save_draft(NEW_DRAFT_SLOT, body="old", token=10)
        store.save_draft(NEW_DRAFT_SLOT, body="", token=11)
        # session 2 (page reload): seed the counter above the durable high-water-mark
        seed = store.max_draft_token()
        assert seed == 11
        store.save_draft(NEW_DRAFT_SLOT, body="new session text", token=seed + 1)
        assert SpecStore(FIXTURE).get_draft(NEW_DRAFT_SLOT)["body"] == "new session text", \
            "a seeded reloaded session must NOT be rejected as stale"


def test_store_delete_comment_drops_edit_draft_tombstone():
    # Deleting a comment sweeps its edit draft AND its ordering tombstone, so tombstones for
    # gone (never-reused) edit slots don't accumulate unbounded in the store file (#30).
    with _TempStoreEnv():
        store = SpecStore(FIXTURE)
        c = store.add_comment(quote="q", body="original")
        slot = edit_draft_slot(c["id"])
        store.save_draft(slot, body="mid-edit", token=5)
        assert store.get_draft_token(slot) == 5
        store.delete_comment(c["id"])
        assert store.get_draft_token(slot) is None, "deleting a comment must drop its tombstone"


def test_store_edit_draft_slot_and_delete():
    # An edit-in-progress draft is keyed per comment; deleting the comment drops its draft.
    with _TempStoreEnv():
        store = SpecStore(FIXTURE)
        c = store.add_comment(quote="q", body="original")
        slot = edit_draft_slot(c["id"])
        store.save_draft(slot, body="edit in progress")
        assert store.get_draft(slot)["body"] == "edit in progress"
        # delete_draft is idempotent
        assert store.delete_draft(slot) is True
        assert store.delete_draft(slot) is False
        # re-save then delete the comment -> its edit draft is swept too
        store.save_draft(slot, body="again mid-edit")
        store.delete_comment(c["id"])
        assert store.get_draft(slot) is None, "deleting a comment must drop its edit draft"


def test_store_draft_requires_slot_and_drafts_persist_alongside_comments():
    with _TempStoreEnv():
        store = SpecStore(FIXTURE)
        try:
            store.save_draft("   ", body="x")
            raise AssertionError("empty slot should raise")
        except ValueError:
            pass
        # a draft and a comment coexist in the same file without clobbering each other
        store.add_comment(quote="q", body="a real note")
        store.save_draft(NEW_DRAFT_SLOT, body="a draft")
        fresh = SpecStore(FIXTURE)
        assert len(fresh.all_comments()) == 1
        assert fresh.get_draft(NEW_DRAFT_SLOT)["body"] == "a draft"


def test_server_draft_save_restore_and_clear_on_create():
    # The server autosaves a draft (POST /api/drafts/<slot>), exposes it (GET /api/drafts),
    # and CLEARS the 'new' slot when the note is actually created.
    with _TempStoreEnv():
        s = _Server()
        try:
            st, body, _ = s.post("/api/drafts/new", {
                "body": "mid-typing", "kind": "question",
                "quote": "the cascade winner", "section_id": "1-overview",
            })
            assert st == 200, (st, body)
            assert json.loads(body)["draft"]["body"] == "mid-typing"
            # GET /api/drafts returns the slot map
            st, body, _ = s.get("/api/drafts")
            assert st == 200 and "new" in json.loads(body)
            # creating the note clears the 'new' draft slot
            st, body, _ = s.post("/api/comments", {"body": "the real note", "kind": "question"})
            assert st == 201, (st, body)
            st, body, _ = s.get("/api/drafts")
            assert "new" not in json.loads(body), "create must clear the new-note draft"
        finally:
            s.stop()


def test_server_trailing_clear_wins_over_stale_draft_after_create():
    # The create/edit success path issues a TRAILING draft-clear so the LAST write to the
    # slot is a delete — defeating a stale autosave that landed after the comment was saved.
    # Simulate the race ORDER at the HTTP layer: create -> (stale autosave slips in) ->
    # trailing clear. The slot must end empty so restore can't reopen a saved note.
    with _TempStoreEnv():
        s = _Server()
        try:
            # create the note (server clears the 'new' slot)
            st, _, _ = s.post("/api/comments", {"body": "the real note"})
            assert st == 201, st
            # a STALE autosave that fired just before submit lands AFTER the create
            s.post("/api/drafts/new", {"body": "stale half-typed text"})
            assert "new" in json.loads(s.get("/api/drafts")[1]), "stale draft is present (the race)"
            # the trailing explicit clear (empty body) removes it -> last write wins
            st, body, _ = s.post("/api/drafts/new", {"body": ""})
            assert st == 200 and json.loads(body)["draft"] is None
            assert "new" not in json.loads(s.get("/api/drafts")[1]), "trailing clear must win the race"
        finally:
            s.stop()


def test_server_draft_token_rejects_late_write_after_clear():
    # The #30 server-authoritative fix: a stale autosave that arrives AFTER a higher-token
    # clear is REJECTED, so it can't resurrect the draft — unlike the client's best-effort
    # ordering, this holds regardless of lock-acquisition order. Drives the genuine
    # out-of-order ARRIVAL (clear lands first, the older-token autosave lands after).
    with _TempStoreEnv():
        s = _Server()
        try:
            # autosave (token 1) then the trailing clear (token 2) — the clear wins + tombstones.
            s.post("/api/drafts/new", {"body": "half-typed", "token": 1})
            st, body, _ = s.post("/api/drafts/new", {"body": "", "token": 2})
            assert st == 200 and json.loads(body)["draft"] is None and json.loads(body)["stale"] is False
            # A late STALE autosave (token 1) reaches the server AFTER the clear -> rejected.
            st, body, _ = s.post("/api/drafts/new", {"body": "stale resurrect", "token": 1})
            assert st == 200, st
            payload = json.loads(body)
            assert payload["stale"] is True, "late lower-token write must be reported stale"
            assert payload["draft"] is None, "rejected write returns the current (empty) slot"
            assert "new" not in json.loads(s.get("/api/drafts")[1]), "tombstone must block resurrection"
        finally:
            s.stop()


def test_server_draft_token_higher_write_applies_and_reports_not_stale():
    # The happy path: a strictly higher token is applied and reported not-stale; a same-or-
    # lower token afterwards is rejected as stale (the slot keeps the higher-token body).
    with _TempStoreEnv():
        s = _Server()
        try:
            st, body, _ = s.post("/api/drafts/new", {"body": "v1", "token": 1})
            assert json.loads(body)["stale"] is False
            st, body, _ = s.post("/api/drafts/new", {"body": "v2", "token": 2})
            assert json.loads(body)["stale"] is False and json.loads(body)["draft"]["body"] == "v2"
            # an out-of-order lower token is rejected; the body stays v2
            st, body, _ = s.post("/api/drafts/new", {"body": "late v1b", "token": 1})
            assert json.loads(body)["stale"] is True
            assert json.loads(body)["draft"]["body"] == "v2", "stale write must not overwrite v2"
            assert json.loads(s.get("/api/drafts")[1])["new"]["body"] == "v2"
        finally:
            s.stop()


def test_server_draft_tokenless_write_is_never_stale():
    # A legacy client (no token field) keeps last-writer-wins and is never flagged stale.
    with _TempStoreEnv():
        s = _Server()
        try:
            st, body, _ = s.post("/api/drafts/new", {"body": "legacy"})
            assert json.loads(body)["stale"] is False, "token-less write opts out of ordering"
            assert json.loads(body)["draft"]["body"] == "legacy"
        finally:
            s.stop()


def test_server_draft_malformed_token_in_body_is_treated_as_tokenless():
    # The HTTP path coerces the body's `token` via _as_token: a str/float/bool/negative is
    # treated as token-less (legacy last-writer-wins, never flagged stale), so a regression in
    # the request parsing can't silently turn a malformed token into a real ordering value.
    with _TempStoreEnv():
        s = _Server()
        try:
            for bad in ("abc", 5.0, True, -3):
                st, body, _ = s.post("/api/drafts/new", {"body": "x", "token": bad})
                assert st == 200, (bad, st)
                assert json.loads(body)["stale"] is False, f"malformed token {bad!r} must be token-less"
        finally:
            s.stop()


def test_server_drafts_get_sends_token_seed_header():
    # GET /api/drafts carries X-Draft-Token-Seed = the highest durable token, so a reloaded
    # client seeds its counter above every tombstone and its autosaves aren't rejected (#30).
    # The body shape (slot -> draft map) is unchanged.
    with _TempStoreEnv():
        s = _Server()
        try:
            st, body, hdrs = s.get("/api/drafts")
            assert st == 200 and hdrs.get("X-Draft-Token-Seed") == "0", hdrs
            s.post("/api/drafts/new", {"body": "x", "token": 5})
            s.post("/api/drafts/new", {"body": "", "token": 9})  # clear -> tombstone 9
            st, body, hdrs = s.get("/api/drafts")
            assert hdrs.get("X-Draft-Token-Seed") == "9", hdrs
            assert json.loads(body) == {}, "body is still the bare slot->draft map"
        finally:
            s.stop()


def test_server_draft_post_response_carries_seed_header_for_selfheal():
    # The seed header rides EVERY write response (not just the GET), so a client whose counter
    # fell behind re-seeds on the next write and self-heals instead of rejecting forever (#30).
    with _TempStoreEnv():
        s = _Server()
        try:
            _, _, hdrs = s.post("/api/drafts/new", {"body": "x", "token": 7})
            assert hdrs.get("X-Draft-Token-Seed") == "7", hdrs
            # even a REJECTED stale write reports the current high-water-mark, so the client
            # can re-seed above it and its next write wins.
            _, body, hdrs = s.post("/api/drafts/new", {"body": "stale", "token": 3})
            assert json.loads(body)["stale"] is True
            assert hdrs.get("X-Draft-Token-Seed") == "7", "stale response still carries the seed"
        finally:
            s.stop()


def test_server_reload_seeded_autosave_survives_old_tombstone():
    # End-to-end of the #30 reload fix: a prior session leaves a high tombstone; a reloaded
    # client reads the seed header and writes ABOVE it -> accepted (autosave keeps working).
    with _TempStoreEnv():
        s = _Server()
        try:
            s.post("/api/drafts/new", {"body": "old", "token": 10})
            s.post("/api/drafts/new", {"body": "", "token": 11})  # trailing clear, tombstone 11
            _, _, hdrs = s.get("/api/drafts")
            seed = int(hdrs["X-Draft-Token-Seed"])
            assert seed == 11
            # the reloaded session's first autosave uses seed+1 and is accepted (not stale)
            st, body, _ = s.post("/api/drafts/new", {"body": "after reload", "token": seed + 1})
            assert json.loads(body)["stale"] is False
            assert json.loads(s.get("/api/drafts")[1])["new"]["body"] == "after reload"
        finally:
            s.stop()


def test_server_draft_empty_body_clears_slot():
    with _TempStoreEnv():
        s = _Server()
        try:
            s.post("/api/drafts/new", {"body": "something"})
            st, body, _ = s.post("/api/drafts/new", {"body": "   "})
            assert st == 200 and json.loads(body)["draft"] is None, body
            st, body, _ = s.get("/api/drafts")
            assert json.loads(body) == {}
        finally:
            s.stop()


def test_server_edit_draft_cleared_on_edit():
    with _TempStoreEnv():
        s = _Server()
        try:
            st, body, _ = s.post("/api/comments", {"body": "original"})
            cid = json.loads(body)["comment"]["id"]
            slot = "edit:" + cid
            s.post("/api/drafts/" + slot, {"body": "mid-edit"})
            st, body, _ = s.get("/api/drafts")
            assert slot in json.loads(body)
            # the edit clears that comment's edit draft
            st, _, _ = s.post("/api/comments/%s/edit" % cid, {"body": "saved edit"})
            assert st == 200, st
            st, body, _ = s.get("/api/drafts")
            assert slot not in json.loads(body), "edit must clear the edit-in-progress draft"
        finally:
            s.stop()


# --------------------------------------------------------------------------- #
# Feature 3: agent reply (store threading + the `reply` CLI command)
# --------------------------------------------------------------------------- #
def test_store_agent_reply_threads_and_is_distinct_author():
    with _TempStoreEnv():
        store = SpecStore(FIXTURE)
        q = store.add_comment(quote="q", body="a question", kind="question")
        store.submit_pending()
        rec = store.add_reply(q["id"], body="here is the answer", author=AGENT_AUTHOR)
        assert rec is not None
        # the reply threads under the comment and carries the agent author (UI styles it)
        again = store.get_comment(q["id"])
        assert again["replies"][-1]["author"] == AGENT_AUTHOR
        assert again["replies"][-1]["body"] == "here is the answer"
        # a reply to a submitted comment flips it to answered
        assert again["status"] == "answered"


def test_cli_spec_web_reply_threads_and_calls_tg():
    # `review spec-web reply <id> <answer> --spec <spec>` threads the agent's reply into the
    # store and shells out to `tg` — the tg call is STUBBED so no Telegram is sent.
    import reviewlib.cli as cli

    with _TempStoreEnv():
        store = SpecStore(FIXTURE)
        c = store.add_comment(quote="q", body="why?", kind="question")
        calls = []

        def fake_run(args, **kwargs):
            calls.append((args, kwargs))

            class _R:
                returncode = 0
                stdout = ""
                stderr = ""

            return _R()

        # Stub the tg shell-out at its module boundary: shutil.which('tg') -> a fake path;
        # subprocess.run captured (cli imports both lazily inside the reply path).
        import shutil as _shutil
        import subprocess as _subprocess

        old_which = _shutil.which
        old_run = _subprocess.run
        _shutil.which = lambda name: "/fake/tg" if name == "tg" else old_which(name)
        _subprocess.run = fake_run
        try:
            rc = cli._spec_web(["reply", c["id"], "because the probe is positive", "--spec", str(FIXTURE)])
        finally:
            _shutil.which = old_which
            _subprocess.run = old_run
        assert rc == 0, rc
        # the reply is threaded with the agent author
        again = SpecStore(FIXTURE).get_comment(c["id"])
        assert again["replies"][-1]["author"] == AGENT_AUTHOR
        assert again["replies"][-1]["body"] == "because the probe is positive"
        # tg was invoked once, with the answer text in the message argument
        assert len(calls) == 1, calls
        args = calls[0][0]
        assert args[0] == "/fake/tg"
        assert any("because the probe is positive" in a for a in args), args


def test_cli_spec_web_reply_spec_path_not_stolen_by_output_prescan():
    # `--spec` is value-taking, so the global `-o`/`--output` pre-scan must NOT consume a
    # spec path that looks like an option (e.g. `--spec --output` or `--spec -odd.md`).
    from reviewlib.cli import _extract_output_path

    for victim in ("--output", "-odd-name.md", "-o"):
        out, rest = _extract_output_path(["spec-web", "reply", "id", "ans", "--spec", victim])
        assert out is None, f"the --spec value {victim!r} must not be taken as -o (got {out!r})"
        assert rest[-1] == victim, rest


def test_cli_spec_web_reply_unknown_id_returns_error():
    import reviewlib.cli as cli

    with _TempStoreEnv():
        SpecStore(FIXTURE)  # ensure the store file area exists
        rc = cli._spec_web(["reply", "no-such-id", "an answer", "--spec", str(FIXTURE), "--no-tg"])
        assert rc == 1, rc


def test_cli_spec_web_reply_tg_failure_is_best_effort():
    # tg being absent/failing must NOT fail the reply (it is already saved to the store/UI).
    import reviewlib.cli as cli

    with _TempStoreEnv():
        store = SpecStore(FIXTURE)
        c = store.add_comment(quote="q", body="why?")
        import shutil as _shutil

        old_which = _shutil.which
        _shutil.which = lambda name: None if name == "tg" else old_which(name)  # tg not on PATH
        try:
            rc = cli._spec_web(["reply", c["id"], "answer text", "--spec", str(FIXTURE)])
        finally:
            _shutil.which = old_which
        assert rc == 0, "missing tg must not fail the reply"
        assert SpecStore(FIXTURE).get_comment(c["id"])["replies"][-1]["body"] == "answer text"


def test_cli_spec_web_reply_tg_found_but_fails_is_best_effort():
    # tg FOUND but failing (non-zero exit) or raising must ALSO not fail the reply — the
    # reply is already in the store/UI; tg is best-effort only.
    import reviewlib.cli as cli
    import shutil as _shutil
    import subprocess as _subprocess

    for failure in ("nonzero", "raise"):
        with _TempStoreEnv():
            store = SpecStore(FIXTURE)
            c = store.add_comment(quote="q", body="why?")

            def fake_run(args, **kwargs):
                if failure == "raise":
                    raise OSError("tg blew up")

                class _R:
                    returncode = 1
                    stdout = ""
                    stderr = "tg: send failed"

                return _R()

            old_which = _shutil.which
            old_run = _subprocess.run
            _shutil.which = lambda name: "/fake/tg" if name == "tg" else old_which(name)
            _subprocess.run = fake_run
            try:
                rc = cli._spec_web(["reply", c["id"], "answer text", "--spec", str(FIXTURE)])
            finally:
                _shutil.which = old_which
                _subprocess.run = old_run
            assert rc == 0, f"a failing tg ({failure}) must not fail the reply"
            assert SpecStore(FIXTURE).get_comment(c["id"])["replies"][-1]["body"] == "answer text"


def test_server_submit_enqueues_review_snapshot_for_launcher():
    # A non-empty Submit enqueues ITS OWN review snapshot on the server's submit_queue so the
    # launching process hands exactly that batch's payload to the agent; an empty submit does
    # NOT enqueue. Two submits enqueue two snapshots (each re-emits — no coalescing, and the
    # snapshot is per-batch so it can't carry a later batch's state).
    with _TempStoreEnv():
        s = _Server()
        try:
            assert s.httpd.submit_queue.empty()
            st, _, _ = s.post("/api/submit", {})  # nothing pending
            assert st == 200 and s.httpd.submit_queue.empty(), "empty submit must not enqueue"
            s.post("/api/comments", {"body": "first note"})
            st, body, _ = s.post("/api/submit", {})
            assert st == 200 and json.loads(body)["count"] == 1
            snap1 = s.httpd.submit_queue.get_nowait()
            assert snap1["batch"] == json.loads(body)["batch"]
            assert snap1["counts"]["total"] == 1, "first snapshot is THIS batch's state (1 comment)"
            # a SECOND non-empty submit enqueues a fresh snapshot (no coalescing)
            s.post("/api/comments", {"body": "second note"})
            st, body, _ = s.post("/api/submit", {})
            assert st == 200 and json.loads(body)["count"] == 1
            snap2 = s.httpd.submit_queue.get_nowait()
            assert snap2["batch"] == json.loads(body)["batch"]
            assert snap2["batch"] != snap1["batch"], "each submit carries its own batch"
            assert snap2["counts"]["total"] == 2
        finally:
            s.stop()


def test_store_guarded_writes_create_lockfile_and_lose_nothing():
    # The guarded load-modify-write creates the sibling .lock file (the cross-process flock
    # target) and serialises concurrent writers so no update is lost. Exercised here from
    # many THREADS (the flock path runs on every guarded write); the .lock file is the same
    # one a separate `review spec-web reply` PROCESS contends on.
    import concurrent.futures

    with _TempStoreEnv():
        store = SpecStore(FIXTURE)
        # the lock file is created on first guarded write
        store.add_comment(quote="q", body="seed")
        assert store.lock_path.exists(), "a guarded write must create the .lock file"

        def _add(i):
            SpecStore(FIXTURE).add_comment(quote="q", body=f"c{i}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(_add, range(40)))
        # every concurrent write landed (no lost update from a load-modify-write race)
        assert len(SpecStore(FIXTURE).all_comments()) == 41, "all concurrent writes must persist"


def test_store_concurrent_draft_writes_highest_token_always_wins():
    # The CORE #30 race: threads write the same slot CONCURRENTLY with shuffled tokens, so the
    # per-slot lock is acquired in an order unrelated to token order — exactly "lock-acquisition
    # order != send order". Whatever the interleaving, the slot must end on the HIGHEST-token
    # body (every lower-token write that lands after it is rejected by the tombstone). This is
    # what the client's best-effort abort cannot guarantee and the server token does.
    import concurrent.futures
    import random

    with _TempStoreEnv():
        store = SpecStore(FIXTURE)
        tokens = list(range(1, 51))
        random.shuffle(tokens)  # fire them in a random order across threads

        def _write(tok):
            SpecStore(FIXTURE).save_draft(NEW_DRAFT_SLOT, body=f"body-{tok}", token=tok)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(_write, tokens))
        # The highest token (50) must have won regardless of arrival/lock order.
        final = SpecStore(FIXTURE).get_draft(NEW_DRAFT_SLOT)
        assert final is not None and final["body"] == "body-50", final
        assert SpecStore(FIXTURE).get_draft_token(NEW_DRAFT_SLOT) == 50


def test_store_guard_serialises_across_processes():
    # The flock is genuinely cross-PROCESS: spawn real subprocesses that each append a comment
    # to the SAME store file concurrently and assert none is lost (an unguarded
    # load-modify-write would drop some under contention).
    import subprocess
    import sys as _sys

    with _TempStoreEnv() as tmp:
        SpecStore(FIXTURE)  # create the store area
        prog = (
            "import sys;"
            "sys.path.insert(0, %r);" % str(REPO_ROOT)
            + "from reviewlib.specweb.store import SpecStore;"
            "SpecStore(%r).add_comment(quote='q', body='p'+sys.argv[1])" % str(FIXTURE)
        )
        env = dict(os.environ, REVIEW_SPECWEB_DIR=str(tmp))
        procs = [subprocess.Popen([_sys.executable, "-c", prog, str(i)], env=env) for i in range(12)]
        for p in procs:
            assert p.wait(timeout=30) == 0
        assert len(SpecStore(FIXTURE).all_comments()) == 12, "every cross-process write must persist"


def _run_specweb_until_submit(spec, posts):
    """Drive run_specweb(exit_on_submit=True) in a thread: capture its stdout, wait for the
    bound port from the banner, fire ``posts`` (a list of (path, obj)), then join (the server
    stops itself after the submit). Returns (stdout_text, return_code). No leaked server —
    exit_on_submit makes the blocking call return, so the thread joins cleanly."""
    import contextlib
    import http.client
    import io
    import re
    import time

    buf = io.StringIO()
    holder = {}

    def _serve():
        with contextlib.redirect_stdout(buf):
            holder["rc"] = sserver.run_specweb(spec, host="127.0.0.1", port=0, exit_on_submit=True)

    th = threading.Thread(target=_serve, daemon=True)
    th.start()
    port = None
    for _ in range(250):
        m = re.search(r"serving http://127\.0\.0\.1:(\d+)/", buf.getvalue())
        if m:
            port = int(m.group(1))
            break
        time.sleep(0.02)
    assert port, ("server did not start", buf.getvalue())
    for path, obj in posts:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("POST", path, body=json.dumps(obj).encode("utf-8"),
                  headers={"Content-Type": "application/json", "Origin": "http://127.0.0.1:%d" % port})
        c.getresponse().read(); c.close()
    th.join(timeout=10)
    assert not th.is_alive(), ("run_specweb did not return after submit", buf.getvalue())
    return buf.getvalue(), holder.get("rc")


def test_run_specweb_emits_structured_review_on_submit():
    # The headline handoff: run_specweb prints the structured review between the stable
    # markers on Submit, so the launching agent parses it from the SINGLE line between them.
    with _TempStoreEnv():
        out, rc = _run_specweb_until_submit(FIXTURE, [
            ("/api/comments", {"body": "why this design?", "kind": "question"}),
            ("/api/submit", {}),
        ])
        assert rc == 0
        assert sserver.SUBMIT_MARKER_BEGIN in out and sserver.SUBMIT_MARKER_END in out, out
        seg = out.split(sserver.SUBMIT_MARKER_BEGIN, 1)[1].split(sserver.SUBMIT_MARKER_END, 1)[0].strip()
        # the payload is exactly one line (compact JSON) between the markers
        assert "\n" not in seg, ("payload must be a single JSON line", seg)
        payload = json.loads(seg)
        assert payload["counts"]["questions"] == 1
        assert payload["comments"][0]["body"] == "why this design?"
        assert "id" in payload["comments"][0]


def test_run_specweb_marker_safe_against_marker_text_in_body():
    # A reviewer body that literally contains the end-marker substring must NOT break the
    # single-line framing — it is JSON-escaped on the one payload line, so the line AFTER the
    # begin marker is still valid JSON and the parser recovers the body intact.
    with _TempStoreEnv():
        evil = "see " + sserver.SUBMIT_MARKER_END + " here"
        out, rc = _run_specweb_until_submit(FIXTURE, [
            ("/api/comments", {"body": evil}),
            ("/api/submit", {}),
        ])
        assert rc == 0
        seg = out.split(sserver.SUBMIT_MARKER_BEGIN, 1)[1].split("\n", 1)[1].split("\n", 1)[0]
        payload = json.loads(seg)
        assert payload["comments"][0]["body"] == evil


def test_run_specweb_exit_on_submit_returns():
    # --exit-on-submit: run_specweb returns (the blocking call ends) after the first submit.
    with _TempStoreEnv():
        out, rc = _run_specweb_until_submit(FIXTURE, [
            ("/api/comments", {"body": "q"}),
            ("/api/submit", {}),
        ])
        assert rc == 0, out
        assert "--exit-on-submit" in out, out


def test_review_payload_empty_state():
    # The structured contract must be well-formed with zero comments (an empty review).
    with _TempStoreEnv():
        payload = SpecStore(FIXTURE).review_payload()
        assert payload["counts"] == {"questions": 0, "remarks": 0, "total": 0}
        assert payload["comments"] == []
        assert payload["batch"] is None


def test_server_delete_comment_clears_edit_draft():
    # Server-level: deleting a comment sweeps its edit-in-progress draft (store-level is
    # tested separately; this pins the HTTP path).
    with _TempStoreEnv():
        s = _Server()
        try:
            st, body, _ = s.post("/api/comments", {"body": "original"})
            cid = json.loads(body)["comment"]["id"]
            slot = "edit:" + cid
            s.post("/api/drafts/" + slot, {"body": "mid-edit"})
            assert slot in json.loads(s.get("/api/drafts")[1])
            st, _, _ = s.post("/api/comments/%s/delete" % cid, {})
            assert st == 200, st
            assert slot not in json.loads(s.get("/api/drafts")[1]), "delete must sweep the edit draft"
        finally:
            s.stop()


def test_server_draft_rejects_non_int_start_end():
    # A JSON `true` for start/end must NOT be stored as the int 1 (isinstance(True,int) trap).
    with _TempStoreEnv():
        s = _Server()
        try:
            st, body, _ = s.post("/api/drafts/new", {"body": "x", "start": True, "end": True})
            assert st == 200, (st, body)
            d = json.loads(body)["draft"]
            assert d["start"] is None and d["end"] is None, d
        finally:
            s.stop()


def test_cli_spec_web_reply_no_tg_skips_subprocess():
    # --no-tg must not shell out at all (no tg call), while still threading the reply.
    import reviewlib.cli as cli

    with _TempStoreEnv():
        store = SpecStore(FIXTURE)
        c = store.add_comment(quote="q", body="why?")
        import subprocess as _subprocess

        old_run = _subprocess.run

        def _boom(*a, **k):
            raise AssertionError("subprocess.run must not be called under --no-tg")

        _subprocess.run = _boom
        try:
            rc = cli._spec_web(["reply", c["id"], "an answer", "--spec", str(FIXTURE), "--no-tg"])
        finally:
            _subprocess.run = old_run
        assert rc == 0
        assert SpecStore(FIXTURE).get_comment(c["id"])["replies"][-1]["body"] == "an answer"


def test_js_python_draft_constants_in_sync():
    # The draft-slot ids + agent author are defined independently in store.py and app.js;
    # if one drifts, drafts silently break or agent-reply styling is lost. Pin them.
    from reviewlib.specweb.store import AGENT_AUTHOR as PY_AGENT
    from reviewlib.specweb.store import NEW_DRAFT_SLOT as PY_NEW

    js = (REPO_ROOT / "reviewlib" / "specweb" / "static" / "app.js").read_text(encoding="utf-8")
    assert ("var AGENT_AUTHOR = '%s'" % PY_AGENT) in js, "app.js AGENT_AUTHOR out of sync with store.py"
    assert ("var NEW_DRAFT_SLOT = '%s'" % PY_NEW) in js, "app.js NEW_DRAFT_SLOT out of sync with store.py"
    assert "return 'edit:' + id;" in js, "app.js edit-slot format out of sync with store.edit_draft_slot"
    assert "edit:" in edit_draft_slot("x"), "store edit_draft_slot must use the 'edit:' prefix"


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
