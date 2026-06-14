#!/usr/bin/env python3
"""Tests for the review-cli local web dashboard (HYP-742).

Covers the three pieces the dashboard adds:
  * the LOG PARSER (`reviewlib.dashboard.parser`) against fixture logs written in the
    EXACT on-disk format `reviewlib.process` / `reviewlib.modes.brainstorm` produce;
  * the OVERSEER STORE (`reviewlib.dashboard.store`) — feedback / conscious / link
    round-trips with atomic JSON persistence;
  * the JSON ENDPOINTS (`reviewlib.dashboard.server`) end-to-end over a real bound
    127.0.0.1 socket: GET runs/stats/detail and POST feedback/conscious/link.

Both the log dir and the store path are redirected to temp dirs (REVIEW_LOG_DIR /
REVIEW_DASHBOARD_STORE) so the tests never touch the user's real logs or annotations.
Run: ``python3 tests/test_dashboard.py`` (standalone, like the other reviewlib tests).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# --- fixtures: write logs in the real on-disk format ------------------------
def _write_call_log(log_dir: Path, stamp: str, backend: str, round_no: int, body: str, *, argv0: str = "/usr/bin/fake") -> Path:
    name = f"{stamp}Z-{backend}-r{round_no}.log"
    p = log_dir / name
    header = f"[review-cli] {backend}: {argv0} (args redacted)\n"
    p.write_text(header + body, encoding="utf-8")
    return p


def _write_brainstorm(log_dir: Path, stamp: str, topic: str, panel: str, moderator: str) -> Path:
    name = f"{stamp}Z-brainstorm.md"
    p = log_dir / name
    content = (
        f"# Brainstorm: {topic}\n\n"
        f"panel={panel} moderator={moderator} rounds>=5 max=5\n\n"
        "# Round 1\n"
        "#### Pragmatic staff engineer (codex)\n"
        "Keep it simple.\n\n"
        "#### (a) A heading inside model output (NOT a persona) (`SOME_MAP`)\n"
        "this line must stay in the persona body, not become a persona\n\n"
        "#### Security reviewer (gemini)\n"
        "Think adversarially.\n\n"
        "# Round 2\n"
        "#### Pragmatic staff engineer (codex)\n"
        "Iterate.\n"
    )
    p.write_text(content, encoding="utf-8")
    return p


def _seed_logs(log_dir: Path) -> None:
    # Session A: a 2-call panel (review/quorum fan-out) at t0.
    _write_call_log(log_dir, "20260601T100000_000000", "codex", 0, "looks good\n")
    _write_call_log(log_dir, "20260601T100005_000000", "gemini", 0, "one nit\n")
    # Session B (>90s later): a single review with an error in the body.
    _write_call_log(log_dir, "20260601T103000_000000", "claude", 0,
                    "There's an issue with the selected model. error: not available\n")
    # Session C (>90s later): a timed-out call.
    _write_call_log(log_dir, "20260601T110000_000000", "codex", 0,
                    "partial output\n[review-cli] TIMEOUT after 240s — partial output above]\n")
    # Session D: brainstorm — per-call round logs + the discussion md, same time window.
    _write_call_log(log_dir, "20260601T120000_000000", "codex", 1, "round one output\n")
    _write_call_log(log_dir, "20260601T120010_000000", "gemini", 1, "round one output\n")
    _write_brainstorm(log_dir, "20260601T120020_000000", "How to decompose a CLI", "codex,gemini", "codex")


# --- parser tests -----------------------------------------------------------
def test_parse_call_log_basic():
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        path = _write_call_log(ld, "20260601T100000_000000", "codex", 0, "hello\nworld\n")
        c = p.parse_call_log(path)
        assert c is not None
        assert c.backend == "codex"
        assert c.round == 0
        assert "hello" in c.body and "world" in c.body
        assert c.argv0 == "/usr/bin/fake"
        assert c.timed_out is False
        assert c.has_error is False


def test_parse_call_log_timeout_and_stderr():
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        path = _write_call_log(ld, "20260601T100000_000000", "codex", 0,
                               "partial\n[stderr] boom went the backend\n[review-cli] TIMEOUT after 12s — partial output above]\n")
        c = p.parse_call_log(path)
        assert c.timed_out is True
        assert c.timeout_secs == 12
        assert c.has_error is True
        assert c.stderr_lines and "boom" in c.stderr_lines[0]
        assert "TIMEOUT" in (c.error_summary or "")


def test_parse_call_log_rejects_bad_name():
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        bad = Path(d) / "not-a-review-log.txt"
        bad.write_text("x")
        assert p.parse_call_log(bad) is None


def test_parse_brainstorm_personas_not_polluted_by_model_headings():
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        path = _write_brainstorm(ld, "20260601T120020_000000", "Topic X", "codex,gemini", "codex")
        bs = p.parse_brainstorm_log(path)
        assert bs is not None
        assert bs.topic == "Topic X"
        assert bs.panel == ["codex", "gemini"]
        assert bs.moderator == "codex"
        # Round 1 has exactly TWO real personas; the `#### (a) ...` line is body text.
        r1 = next(r for r in bs.rounds if r["round"] == 1)
        names = [pp["name"] for pp in r1["personas"]]
        assert names == ["Pragmatic staff engineer", "Security reviewer"], names
        prag = r1["personas"][0]
        assert "must stay in the persona body" in prag["text"]


def test_cluster_sessions_and_modes():
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        _seed_logs(ld)
        sessions = p.load_sessions(ld, gap_seconds=90)
        # 4 clusters: panel(2 calls), review(err), review(timeout), brainstorm.
        assert len(sessions) == 4, [(s.mode, len(s.calls)) for s in sessions]
        by_mode = {}
        for s in sessions:
            by_mode[s.mode] = by_mode.get(s.mode, 0) + 1
        assert by_mode.get("panel") == 1
        assert by_mode.get("brainstorm") == 1
        assert by_mode.get("review") == 2
        bs = next(s for s in sessions if s.mode == "brainstorm")
        assert bs.brainstorm is not None
        assert bs.brainstorm.topic == "How to decompose a CLI"
        assert len(bs.roles()) >= 2


def test_compute_stats_against_fixtures():
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        _seed_logs(ld)
        sessions = p.load_sessions(ld)
        stats = p.compute_stats(sessions)
        assert stats["session_count"] == 4
        assert stats["call_count"] == 6
        assert stats["timeout_calls"] == 1
        assert stats["error_calls"] >= 2  # the explicit error + the timeout
        assert stats["tokens_recorded"] is False
        assert stats["cost_recorded"] is False
        assert "codex" in stats["by_model"]
        assert stats["by_mode"]["brainstorm"] == 1


def test_empty_log_dir_is_graceful():
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        sessions = p.load_sessions(Path(d) / "does-not-exist")
        assert sessions == []
        stats = p.compute_stats(sessions)
        assert stats["session_count"] == 0
        assert stats["success_rate"] is None


def test_cluster_uses_call_end_time_not_start():
    """A call that runs longer than the gap must NOT split one invocation into sessions.

    (codex P2) The moderator/next round starts right after a slow call ENDS but >gap
    after it STARTED; clustering on end-time keeps them in one session.
    """
    from datetime import timedelta

    from reviewlib.dashboard import parser as p

    t0 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    # call A starts at t0 and runs 120s (mtime = t0+120s); call B starts at t0+121s —
    # that's 121s after A's START (> 90s gap) but only 1s after A's END.
    a = p.CallLog("", "a.log", t0, "codex", 0, "", "", mtime=t0 + timedelta(seconds=120))
    b = p.CallLog("", "b.log", t0 + timedelta(seconds=121), "gemini", 0, "", "", mtime=t0 + timedelta(seconds=125))
    sessions = p.cluster_sessions([a, b], [], gap_seconds=90)
    assert len(sessions) == 1, [(s.started, len(s.calls)) for s in sessions]
    assert len(sessions[0].calls) == 2
    # And the inverse: a real >gap idle gap DOES split.
    c = p.CallLog("", "c.log", t0 + timedelta(seconds=400), "codex", 0, "", "", mtime=t0 + timedelta(seconds=402))
    sessions2 = p.cluster_sessions([a, b, c], [], gap_seconds=90)
    assert len(sessions2) == 2


def test_brainstorm_session_id_is_stable_after_logs_age_out():
    """(codex P2) Annotations stay pinned: the brainstorm session id is the brainstorm
    stamp whether or not the per-call logs still exist."""
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        # brainstorm md is written FIRST by mode_brainstorm, then the round logs.
        _write_brainstorm(ld, "20260601T120000_000000", "Topic", "codex,gemini", "codex")
        _write_call_log(ld, "20260601T120005_000000", "codex", 1, "r1\n")
        _write_call_log(ld, "20260601T120010_000000", "gemini", 1, "r1\n")
        with_logs = p.load_sessions(ld)
        bs_sess = next(s for s in with_logs if s.brainstorm)
        id_with_logs = bs_sess.session_id
        # Now the per-call logs age out, leaving only the md.
        for f in ld.glob("*-r*.log"):
            f.unlink()
        only_md = p.load_sessions(ld)
        bs_sess2 = next(s for s in only_md if s.brainstorm)
        assert bs_sess2.session_id == id_with_logs, (id_with_logs, bs_sess2.session_id)
        # and it equals the brainstorm-stamp-derived id
        assert id_with_logs == "sess-20260601T120000_000000"


def test_detect_links_from_branch():
    """detect_links_for_cwd pulls a HYP-style ticket out of the git branch name."""
    import subprocess

    from reviewlib.dashboard import server

    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "checkout", "-q", "-b", "HYP-742-dashboard"], cwd=repo, check=True)
        got = server.detect_links_for_cwd(repo)
        assert got["branch"] == "HYP-742-dashboard"
        assert got["tickets"] == ["HYP-742"]
        assert got["prs"] == []
    # A non-repo dir degrades to empty, never raises.
    with tempfile.TemporaryDirectory() as d:
        got = server.detect_links_for_cwd(Path(d))
        assert got["tickets"] == []


# --- store tests ------------------------------------------------------------
def _with_store(fn):
    with tempfile.TemporaryDirectory() as d:
        os.environ["REVIEW_DASHBOARD_STORE"] = str(Path(d) / "dashboard.json")
        try:
            # Reimport fresh module each time to pick up the env (store reads env per-call).
            from reviewlib.dashboard import store

            fn(store)
        finally:
            os.environ.pop("REVIEW_DASHBOARD_STORE", None)


def test_store_feedback_roundtrip():
    def body(store):
        rec = store.set_feedback("sess-1", "needs a second look")
        assert rec["feedback"] == "needs a second look"
        assert store.get_annotation("sess-1")["feedback"] == "needs a second look"
        # clearing
        store.set_feedback("sess-1", "   ")
        assert store.get_annotation("sess-1")["feedback"] is None

    _with_store(body)


def test_store_conscious_roundtrip():
    def body(store):
        store.set_conscious("sess-2", True)
        assert store.get_annotation("sess-2")["conscious"] is True
        store.set_conscious("sess-2", False)
        assert store.get_annotation("sess-2")["conscious"] is False

    _with_store(body)


def test_store_links_roundtrip_and_validation():
    def body(store):
        store.add_link("sess-3", pr="123", ticket="hyp-742")
        ann = store.get_annotation("sess-3")
        assert ann["links"]["prs"] == ["#123"]
        assert ann["links"]["tickets"] == ["HYP-742"]
        # dedup
        store.add_link("sess-3", pr="#123")
        assert store.get_annotation("sess-3")["links"]["prs"] == ["#123"]
        # remove
        store.remove_link("sess-3", ticket="HYP-742")
        assert store.get_annotation("sess-3")["links"]["tickets"] == []
        # bad input rejected
        try:
            store.add_link("sess-3", pr="not-a-pr")
            raise AssertionError("expected ValueError for bad PR")
        except ValueError:
            pass

    _with_store(body)


def test_store_persists_to_disk_and_is_private():
    def body(store):
        store.set_feedback("sess-4", "x")
        p = store.store_path()
        assert p.exists()
        data = json.loads(p.read_text())
        assert "sess-4" in data["sessions"]
        mode = oct(os.stat(p).st_mode & 0o777)
        assert mode == "0o600", mode

    _with_store(body)


# --- endpoint tests (real bound socket) -------------------------------------
def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def _post(base, path, obj):
    req = urllib.request.Request(
        base + path, data=json.dumps(obj).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def test_host_header_guard_blocks_dns_rebinding():
    """(codex P1) A request with a foreign Host (DNS-rebind attack) is rejected 403;
    loopback Host values are served."""
    with tempfile.TemporaryDirectory() as logd, tempfile.TemporaryDirectory() as stored:
        os.environ["REVIEW_LOG_DIR"] = logd
        os.environ["REVIEW_DASHBOARD_STORE"] = str(Path(stored) / "dashboard.json")
        try:
            from reviewlib.dashboard import server

            httpd = server.make_server(0)
            port = httpd.server_address[1]
            base = f"http://127.0.0.1:{port}"
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            try:
                # Foreign Host -> 403 (the rebinding attacker's hostname).
                req = urllib.request.Request(base + "/api/runs", headers={"Host": "evil.example.com"})
                try:
                    urllib.request.urlopen(req, timeout=10)
                    raise AssertionError("expected 403 for foreign Host")
                except urllib.error.HTTPError as e:
                    assert e.code == 403, e.code
                # Loopback Host (with the real port) -> served.
                req2 = urllib.request.Request(base + "/api/runs", headers={"Host": f"127.0.0.1:{port}"})
                with urllib.request.urlopen(req2, timeout=10) as r:
                    assert r.status == 200
                # localhost is allowed too.
                req3 = urllib.request.Request(base + "/api/health", headers={"Host": f"localhost:{port}"})
                with urllib.request.urlopen(req3, timeout=10) as r:
                    assert r.status == 200
            finally:
                httpd.shutdown()
                httpd.server_close()
        finally:
            os.environ.pop("REVIEW_LOG_DIR", None)
            os.environ.pop("REVIEW_DASHBOARD_STORE", None)


def test_endpoints_end_to_end():
    with tempfile.TemporaryDirectory() as logd, tempfile.TemporaryDirectory() as stored:
        os.environ["REVIEW_LOG_DIR"] = logd
        os.environ["REVIEW_DASHBOARD_STORE"] = str(Path(stored) / "dashboard.json")
        try:
            _seed_logs(Path(logd))
            from reviewlib.dashboard import server

            httpd = server.make_server(0)
            base = f"http://127.0.0.1:{httpd.server_address[1]}"
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            try:
                # health
                st, h = _get(base, "/api/health")
                assert st == 200 and h["ok"] is True
                assert h["log_dir"] == logd

                # runs
                st, runs = _get(base, "/api/runs")
                assert st == 200
                assert len(runs) == 4, [r["mode"] for r in runs]
                # newest first
                assert runs[0]["started"] >= runs[-1]["started"]
                sid = runs[0]["session_id"]

                # stats
                st, stats = _get(base, "/api/stats")
                assert st == 200
                assert stats["session_count"] == 4
                assert stats["conscious_count"] == 0

                # detail
                st, detail = _get(base, f"/api/runs/{sid}")
                assert st == 200
                assert detail["session_id"] == sid
                assert "calls" in detail

                # unknown session -> 404
                try:
                    _get(base, "/api/runs/sess-nope")
                    raise AssertionError("expected 404")
                except urllib.error.HTTPError as e:
                    assert e.code == 404

                # POST feedback round-trip
                st, r = _post(base, f"/api/runs/{sid}/feedback", {"feedback": "reviewed live"})
                assert st == 200 and r["annotation"]["feedback"] == "reviewed live"
                _, detail2 = _get(base, f"/api/runs/{sid}")
                assert detail2["feedback"] == "reviewed live"

                # POST conscious round-trip
                st, r = _post(base, f"/api/runs/{sid}/conscious", {"conscious": True})
                assert st == 200 and r["annotation"]["conscious"] is True
                _, stats2 = _get(base, "/api/stats")
                assert stats2["conscious_count"] == 1

                # POST link round-trip
                st, r = _post(base, f"/api/runs/{sid}/links", {"pr": "456", "ticket": "HYP-742"})
                assert st == 200
                assert r["annotation"]["links"]["prs"] == ["#456"]
                assert r["annotation"]["links"]["tickets"] == ["HYP-742"]

                # bad link -> 400
                try:
                    _post(base, f"/api/runs/{sid}/links", {"pr": "garbage"})
                    raise AssertionError("expected 400")
                except urllib.error.HTTPError as e:
                    assert e.code == 400

                # index + assets served
                with urllib.request.urlopen(base + "/", timeout=10) as resp:
                    assert resp.status == 200
                    assert b"review-cli" in resp.read()
                with urllib.request.urlopen(base + "/assets/app.js", timeout=10) as resp:
                    assert resp.status == 200
                # asset traversal blocked
                try:
                    urllib.request.urlopen(base + "/assets/../server.py", timeout=10)
                    raise AssertionError("expected traversal block")
                except urllib.error.HTTPError as e:
                    assert e.code == 404
            finally:
                httpd.shutdown()
                httpd.server_close()
        finally:
            os.environ.pop("REVIEW_LOG_DIR", None)
            os.environ.pop("REVIEW_DASHBOARD_STORE", None)


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
                import traceback

                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
                traceback.print_exc()
    sys.exit(1 if failures else 0)
