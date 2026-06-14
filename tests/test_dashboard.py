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
def _write_call_log(
    log_dir: Path,
    stamp: str,
    backend: str,
    round_no: int,
    body: str,
    *,
    argv0: str = "/usr/bin/fake",
    exit_code: int | None = None,
) -> Path:
    name = f"{stamp}Z-{backend}-r{round_no}.log"
    p = log_dir / name
    header = f"[review-cli] {backend}: {argv0} (args redacted)\n"
    footer = f"[review-cli] EXIT {exit_code}\n" if exit_code is not None else ""
    p.write_text(header + body + footer, encoding="utf-8")
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


def test_explicit_exit0_with_error_text_is_not_an_error():
    """(HYP-742 finding 4) A SUCCESSFUL call (EXIT 0) whose body mentions 'error:' /
    'permission denied' must NOT be counted as a failure — review output legitimately
    describes errors in the code it reviews. The explicit return code wins over the body."""
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        body = (
            "Finding: this function can raise error: ValueError on bad input.\n"
            "Also the script prints 'permission denied' and 'command not found' in its help.\n"
        )
        path = _write_call_log(ld, "20260601T100000_000000", "codex", 0, body, exit_code=0)
        c = p.parse_call_log(path)
        assert c is not None
        assert c.exit_code == 0
        assert c.has_error is False, "EXIT 0 must be a success even with error words in the body"
        assert c.error_summary is None
        # The EXIT footer must be stripped from the displayed body.
        assert "EXIT" not in c.body
        # And the error text itself stays in the body (it's real review content).
        assert "error:" in c.body


def test_quoted_exit_line_in_body_is_not_treated_as_status():
    """(codex P2) Only the TRAILING footer is the status. A review output that quotes an
    exact `[review-cli] EXIT 1` line mid-body must NOT be consumed as the status, and must
    stay visible in the body. The real trailing footer still decides success/failure."""
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        body = (
            "Reviewing the dashboard logs, I see a line like:\n"
            "[review-cli] EXIT 1\n"  # quoted in the review prose, NOT the real footer
            "which the parser handles. Overall the change looks correct.\n"
        )
        # The real footer (EXIT 0 = success) is appended last by the writer.
        path = _write_call_log(ld, "20260601T100000_000000", "codex", 0, body, exit_code=0)
        c = p.parse_call_log(path)
        assert c.exit_code == 0, "the TRAILING footer (EXIT 0) is authoritative"
        assert c.has_error is False, "a quoted EXIT 1 mid-body must not mark the call failed"
        # The quoted line stays in the displayed body.
        assert "[review-cli] EXIT 1" in c.body
        # But the real trailing footer is stripped (it appears exactly once — the quote).
        assert c.body.count("[review-cli] EXIT") == 1


def test_quoted_timeout_marker_in_body_is_not_treated_as_a_timeout():
    """(codex P2) A TIMEOUT marker is genuine only when the call actually timed out
    (exit code 124 — the writers always pair them). A successful `EXIT 0` review that
    QUOTES `[review-cli] TIMEOUT after Ns` must NOT be flagged as a timeout, and the
    quote must stay visible — even when the quote is the very last body line (the exact
    residual case codex flagged) — else valid successful calls corrupt the timeout metric."""
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        # (a) quoted mid-body
        body_mid = (
            "While reviewing the logs I noticed a marker:\n"
            "[review-cli] TIMEOUT after 12s — partial output above]\n"
            "The handling looks fine. Overall the change is correct.\n"
        )
        c_mid = p.parse_call_log(_write_call_log(ld, "20260601T100000_000000", "codex", 0, body_mid, exit_code=0))
        assert c_mid.timed_out is False and c_mid.has_error is False
        assert "TIMEOUT after 12s" in c_mid.body
        # (b) quoted as the LAST body line, with EXIT 0 (codex's residual case): position
        # alone would mis-detect it; the exit-code gate (124) keeps it a non-timeout.
        body_last = (
            "Reviewing the timeout handling; the log ends with:\n"
            "[review-cli] TIMEOUT after 12s — partial output above]\n"
        )
        c_last = p.parse_call_log(_write_call_log(ld, "20260601T100010_000000", "codex", 0, body_last, exit_code=0))
        assert c_last.exit_code == 0
        assert c_last.timed_out is False, "EXIT 0 means no timeout even if the last line quotes the marker"
        assert c_last.has_error is False
        assert "TIMEOUT after 12s" in c_last.body, "the quoted marker stays in the body"


def test_real_timeout_marker_before_footer_is_still_detected():
    """The genuine timeout marker (written by the runner immediately before the EXIT
    footer, with rc 124) is still recognised — the anchoring fix must not lose real ones."""
    from reviewlib.dashboard import parser as p
    from reviewlib.process import write_sidecar_log

    with tempfile.TemporaryDirectory() as d:
        os.environ["REVIEW_LOG_DIR"] = d
        try:
            path = write_sidecar_log(
                "gemini", round_no=0, argv0="Gemini API", returncode=124,
                stdout="partial output before the timeout\n", stderr="",
                timed_out=True, timeout_secs=240,
            )
        finally:
            os.environ.pop("REVIEW_LOG_DIR", None)
        c = p.parse_call_log(path)
        assert c.timed_out is True, "the authoritative pre-footer TIMEOUT marker must be detected"
        assert c.timeout_secs == 240
        assert c.exit_code == 124
        assert "TIMEOUT" not in c.body, "the real marker is stripped from the body"


def test_orphan_brainstorm_keeps_model_attribution_from_markdown():
    """(codex P2) When a brainstorm's per-call logs have aged out but the `*-brainstorm.md`
    survives, the session has no calls but the markdown still records the panel models.
    Session.models must fall back to brainstorm.panel so by_model stats keep attribution."""
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        # Only the brainstorm md — no per-call -r{n}.log files (they aged out).
        _write_brainstorm(ld, "20260601T120000_000000", "How to shard the cache",
                          panel="codex,gemini", moderator="codex")
        sessions = p.load_sessions(ld)
        assert len(sessions) == 1, sessions
        s = sessions[0]
        assert s.calls == [], "the orphan session has no per-call logs"
        assert s.brainstorm is not None
        assert s.models == ["codex", "gemini"], s.models
        # And the by_model stats reflect the panel even with no calls.
        stats = p.compute_stats(sessions)
        assert "codex" in stats["by_model"] and "gemini" in stats["by_model"], stats["by_model"]


def test_claude_api_mode_emits_a_sidecar_log():
    """(codex P2) A claude API-mode run is a REST call with no subprocess sidecar, so it
    must emit its own `*-claude-r{n}.log` (like gemini/z.ai/commandcode) — else successful
    or failed claude API calls are invisible to the dashboard and missing from stats."""
    import urllib.request

    from reviewlib import backends
    from reviewlib.dashboard import parser as p

    class _FakeResp:
        def __init__(self, payload: bytes):
            self._payload = payload

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    fake_payload = json.dumps({
        "content": [{"type": "text", "text": "A finding: guard the None branch."}],
        "usage": {"input_tokens": 12, "output_tokens": 6},
    }).encode("utf-8")

    with tempfile.TemporaryDirectory() as logd:
        os.environ["REVIEW_LOG_DIR"] = logd
        os.environ["ANTHROPIC_API_KEY"] = "fake-key"
        old_urlopen = urllib.request.urlopen
        try:
            urllib.request.urlopen = lambda req, timeout=None: _FakeResp(fake_payload)
            result = backends.review_claude_api("claude:claude-opus-4-8", "review", "", Path(logd), 30, round_no=2)
        finally:
            urllib.request.urlopen = old_urlopen
            os.environ.pop("REVIEW_LOG_DIR", None)
            os.environ.pop("ANTHROPIC_API_KEY", None)

        assert result.returncode == 0, result.stderr
        logs = list(Path(logd).glob("*-claude-r2.log"))
        assert len(logs) == 1, [x.name for x in Path(logd).glob("*")]
        c = p.parse_call_log(logs[0])
        assert c is not None and c.backend == "claude"
        assert c.round == 2 and c.exit_code == 0
        assert "finding" in c.body.lower()
        stats = p.compute_stats(p.load_sessions(Path(logd)))
        assert "claude" in stats["by_model"], stats["by_model"]


def test_claude_api_missing_key_still_emits_a_sidecar():
    """A claude API-mode call with no key configured must also emit a per-backend failure
    sidecar (rc 1) — a failed run stays visible in the dashboard."""
    from reviewlib import backends
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as logd:
        os.environ["REVIEW_LOG_DIR"] = logd
        for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            os.environ.pop(k, None)
        old_cfg = backends._anthropic_api_config
        try:
            backends._anthropic_api_config = lambda: None
            result = backends.review_claude_api("claude:claude-opus-4-8", "review", "", Path(logd), 30, round_no=0)
        finally:
            backends._anthropic_api_config = old_cfg
            os.environ.pop("REVIEW_LOG_DIR", None)
        assert result.returncode == 1
        logs = list(Path(logd).glob("*-claude-r0.log"))
        assert len(logs) == 1, [x.name for x in Path(logd).glob("*")]
        c = p.parse_call_log(logs[0])
        assert c.exit_code == 1 and c.has_error is True


def test_explicit_nonzero_exit_is_an_error_even_with_clean_body():
    """(finding 4) The inverse: a non-zero EXIT is a failure even when the body looks
    clean and has no error markers — the return code is authoritative."""
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        path = _write_call_log(ld, "20260601T100000_000000", "gemini", 0,
                               "all good, nothing to report\n", exit_code=1)
        c = p.parse_call_log(path)
        assert c.exit_code == 1
        assert c.has_error is True
        assert "exit code 1" in (c.error_summary or "")


def test_legacy_log_without_exit_footer_falls_back_to_heuristic():
    """(finding 4) Logs that predate the EXIT footer (exit_code None) keep the old
    body-grep behaviour, so historical runs still surface their errors."""
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        ok = _write_call_log(ld, "20260601T100000_000000", "codex", 0, "looks fine\n")
        bad = _write_call_log(ld, "20260601T100100_000000", "codex", 0, "error: not available\n")
        assert p.parse_call_log(ok).exit_code is None
        assert p.parse_call_log(ok).has_error is False
        assert p.parse_call_log(bad).has_error is True  # legacy grep still flags it


def test_success_rate_not_corrupted_by_error_text_in_successful_runs():
    """(finding 4 end-to-end) A panel of successful calls whose output mentions errors
    must report a 100% success rate — the bug inflated errors and tanked success%."""
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        # Three successful reviews, each with error words in the prose.
        _write_call_log(ld, "20260601T100000_000000", "codex", 0,
                        "error: the diff has a bug on line 5\n", exit_code=0)
        _write_call_log(ld, "20260601T100003_000000", "gemini", 0,
                        "permission denied is logged but handled fine\n", exit_code=0)
        _write_call_log(ld, "20260601T100006_000000", "claude", 0,
                        "traceback (most recent call last) appears in a test fixture\n", exit_code=0)
        sessions = p.load_sessions(ld, gap_seconds=90)
        stats = p.compute_stats(sessions)
        assert stats["call_count"] == 3
        assert stats["error_calls"] == 0, "successful runs with error text wrongly counted as errors"
        assert stats["ok_calls"] == 3
        assert stats["success_rate"] == 1.0


def test_gemini_rest_run_emits_parseable_sidecar_log():
    """(HYP-742 finding 2) The Gemini REST backend writes no subprocess log, so it must
    emit a sidecar `.log` the parser can read — else Gemini-only runs are invisible and
    models are undercounted. The sidecar carries the explicit EXIT status (finding 4)."""
    import urllib.request

    from reviewlib import backends
    from reviewlib.dashboard import parser as p

    class _FakeResp:
        def __init__(self, payload: bytes):
            self._payload = payload

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    fake_payload = json.dumps({
        "candidates": [{"content": {"parts": [{"text": "One nit: error: handle the None case."}]}}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
    }).encode("utf-8")

    with tempfile.TemporaryDirectory() as logd:
        os.environ["REVIEW_LOG_DIR"] = logd
        old_urlopen = urllib.request.urlopen
        old_key = backends._gemini_key
        try:
            backends._gemini_key = lambda: "fake-key"
            urllib.request.urlopen = lambda req, timeout=None: _FakeResp(fake_payload)
            result = backends.review_gemini("gemini", "review this", "", Path(logd), 30, round_no=2)
        finally:
            urllib.request.urlopen = old_urlopen
            backends._gemini_key = old_key
            os.environ.pop("REVIEW_LOG_DIR", None)

        assert result.returncode == 0
        # The sidecar log exists, is named with the round we passed, and parses.
        logs = list(Path(logd).glob("*-gemini-r2.log"))
        assert len(logs) == 1, [x.name for x in Path(logd).glob('*')]
        c = p.parse_call_log(logs[0])
        assert c is not None
        assert c.backend == "gemini"
        assert c.round == 2
        assert c.exit_code == 0
        assert c.has_error is False  # EXIT 0 wins over the 'error:' in the body text
        assert "error:" in c.body  # the review content is preserved
        # And the parser counts it as a real (gemini) run, not invisible.
        sessions = p.load_sessions(Path(logd))
        stats = p.compute_stats(sessions)
        assert "gemini" in stats["by_model"]
        assert stats["call_count"] == 1


def test_openai_compatible_rest_runs_emit_per_backend_sidecar_logs():
    """(HYP-742 logging gap) Each OpenAI-compatible REST backend (z.ai, commandcode)
    must emit its dashboard sidecar UNDER ITS OWN backend name, not lumped under a
    hardcoded "gemini". Before the fix _emit_rest_log hardcoded "gemini" and
    _openai_compatible_request emitted nothing, so z.ai/commandcode runs were invisible.
    Assert each backend writes a correctly-named `*-{backend}-r{n}.log` the parser reads
    and the dashboard attributes the run to that backend."""
    from reviewlib import backends
    from reviewlib.dashboard import parser as p

    class _FakeResp:
        def __init__(self, payload: bytes):
            self._payload = payload

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    fake_payload = json.dumps({
        "choices": [{"message": {"content": "One finding: handle the empty list."}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }).encode("utf-8")

    # (backend entry function, model string, expected sidecar backend name, env key)
    cases = [
        (backends.review_zai, "zai", "z.ai", "ZAI_API_KEY"),
        (backends.review_commandcode, "commandcode", "commandcode", "COMMANDCODE_API_KEY"),
    ]
    for entry, model, expected_backend, env_key in cases:
        with tempfile.TemporaryDirectory() as logd:
            os.environ["REVIEW_LOG_DIR"] = logd
            os.environ[env_key] = "fake-key"
            old_urlopen = urllib.request.urlopen
            try:
                urllib.request.urlopen = lambda req, timeout=None: _FakeResp(fake_payload)
                result = entry(model, "review this", "", Path(logd), 30, round_no=4)
            finally:
                urllib.request.urlopen = old_urlopen
                os.environ.pop("REVIEW_LOG_DIR", None)
                os.environ.pop(env_key, None)

            assert result.returncode == 0, (expected_backend, result.stderr)
            # The sidecar is named for THIS backend (not "gemini"), with the round we passed.
            logs = list(Path(logd).glob(f"*-{expected_backend}-r4.log"))
            assert len(logs) == 1, (expected_backend, [x.name for x in Path(logd).glob("*")])
            # And NOT misfiled under gemini.
            assert not list(Path(logd).glob("*-gemini-*.log")), expected_backend
            c = p.parse_call_log(logs[0])
            assert c is not None and c.backend == expected_backend, (expected_backend, c)
            assert c.round == 4
            assert c.exit_code == 0
            assert "finding" in c.body.lower()
            # The dashboard counts it under the per-backend name, so the run is visible.
            stats = p.compute_stats(p.load_sessions(Path(logd)))
            assert expected_backend in stats["by_model"], (expected_backend, stats["by_model"])
            assert stats["call_count"] == 1


def test_openai_compatible_failure_emits_per_backend_sidecar():
    """A FAILED z.ai/commandcode call (HTTP error / missing key) must also emit a sidecar
    under its own backend name — a failed run stays visible, never an invisible 127."""
    from reviewlib import backends
    from reviewlib.dashboard import parser as p

    # Missing key: the backend must log a failure sidecar (not raise out of run_panel).
    with tempfile.TemporaryDirectory() as logd:
        os.environ["REVIEW_LOG_DIR"] = logd
        # Ensure no key is present.
        for k in ("ZAI_API_KEY", "ZHIPU_API_KEY", "GEMINI_ENV_FILE"):
            os.environ.pop(k, None)
        os.environ["GEMINI_ENV_FILE"] = "/nonexistent/review-cli/.env"
        try:
            result = backends.review_zai("zai", "q", "", Path(logd), 10, round_no=0)
        finally:
            os.environ.pop("REVIEW_LOG_DIR", None)
            os.environ.pop("GEMINI_ENV_FILE", None)
        assert result.returncode != 0
        logs = list(Path(logd).glob("*-z.ai-r0.log"))
        assert len(logs) == 1, [x.name for x in Path(logd).glob("*")]
        c = p.parse_call_log(logs[0])
        assert c is not None and c.backend == "z.ai"
        assert c.exit_code == 1
        assert c.has_error is True


def test_gemini_network_failure_still_emits_a_sidecar_log():
    """(codex P2) A non-HTTP Gemini failure (network/DNS/socket timeout, malformed JSON)
    must still produce a `.log` and a non-zero ReviewResult — not raise out of run_panel
    leaving the failed call invisible to the dashboard."""
    import urllib.request

    from reviewlib import backends
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as logd:
        os.environ["REVIEW_LOG_DIR"] = logd
        old_urlopen = urllib.request.urlopen
        old_key = backends._gemini_key
        try:
            backends._gemini_key = lambda: "fake-key"

            def boom(req, timeout=None):
                raise urllib.error.URLError("name resolution failed")

            urllib.request.urlopen = boom
            result = backends.review_gemini("gemini", "review this", "", Path(logd), 5, round_no=0)
        finally:
            urllib.request.urlopen = old_urlopen
            backends._gemini_key = old_key
            os.environ.pop("REVIEW_LOG_DIR", None)

        # Returned a normal non-zero result, did NOT raise.
        assert result.returncode != 0
        assert "URLError" in result.stderr
        logs = list(Path(logd).glob("*-gemini-r0.log"))
        assert len(logs) == 1, "network failure must still write a sidecar"
        c = p.parse_call_log(logs[0])
        assert c.exit_code == 1
        assert c.has_error is True
        assert c.stderr_lines and "URLError" in c.stderr_lines[0]


def test_streamed_exit_footer_anchored_when_stdout_has_no_trailing_newline():
    """(codex P2) A subprocess that flushes stdout WITHOUT a trailing newline must still
    get a parseable `EXIT {code}` footer on its own line — else the footer fuses onto the
    last output line and exit_code stays None (the call is misclassified)."""
    import sys as _sys

    from reviewlib import process
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as logd:
        os.environ["REVIEW_LOG_DIR"] = logd
        try:
            # writes 'no-newline-here' with NO trailing \n, then exits 1
            argv = [_sys.executable, "-c", "import sys; sys.stdout.write('no-newline-here'); sys.exit(1)"]
            r = process._run_streamed(argv, cwd=Path(logd), timeout=30, backend="anchortest", round_no=0)
        finally:
            os.environ.pop("REVIEW_LOG_DIR", None)
        assert r.returncode == 1
        logs = list(Path(logd).glob("*-anchortest-*.log"))
        assert len(logs) == 1
        text = logs[0].read_text()
        # The footer is on its own line, NOT fused onto the output.
        assert "no-newline-hereEXIT" not in text
        assert "\n[review-cli] EXIT 1" in text or "\nEXIT 1" in text or text.endswith("[review-cli] EXIT 1\n")
        c = p.parse_call_log(logs[0])
        assert c.exit_code == 1, "footer must be parseable even with no trailing newline in stdout"
        assert c.has_error is True
        assert "no-newline-here" in c.body


def test_gemini_sidecar_stamps_call_start_not_write_time():
    """(codex P2) The sidecar filename stamp must be the call START time, so a slow REST
    call shows an honest duration and clusters with its panel peers — not a near-zero
    duration anchored at write time."""
    import time as _time

    from reviewlib import process
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as logd:
        os.environ["REVIEW_LOG_DIR"] = logd
        try:
            started = datetime.now(timezone.utc)
            _time.sleep(0.05)  # simulate the call taking time before we write the log
            path = process.write_sidecar_log(
                "gemini", round_no=0, argv0="Gemini API gemini-2.5-flash",
                returncode=0, stdout="ok\n", stderr="", started=started,
            )
        finally:
            os.environ.pop("REVIEW_LOG_DIR", None)
        c = p.parse_call_log(path)
        # The parsed start equals the START stamp we passed (to the second), not "now".
        assert c.started.strftime("%Y%m%dT%H%M%S") == started.strftime("%Y%m%dT%H%M%S")
        # Duration is start->mtime (>= the sleep), i.e. NOT near-zero anchored at write.
        assert c.duration_seconds is not None and c.duration_seconds >= 0


def test_gemini_missing_api_key_still_emits_a_sidecar_log():
    """(codex P2) A missing GEMINI_API_KEY (the COMMON failure — gemini is a default
    model) must still produce a `.log` and a non-zero result, not a `_gemini_key()`
    raise that run_panel turns into an internal 127 with no log (invisible run)."""
    from reviewlib import backends
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as logd:
        os.environ["REVIEW_LOG_DIR"] = logd
        old_key = backends._gemini_key

        def no_key():
            raise RuntimeError("GEMINI_API_KEY not found in env, GEMINI_ENV_FILE, or ~/.config/review-cli/.env")

        try:
            backends._gemini_key = no_key
            result = backends.review_gemini("gemini", "review this", "", Path(logd), 5, round_no=0)
        finally:
            backends._gemini_key = old_key
            os.environ.pop("REVIEW_LOG_DIR", None)

        assert result.returncode != 0
        assert "GEMINI_API_KEY" in result.stderr
        logs = list(Path(logd).glob("*-gemini-r0.log"))
        assert len(logs) == 1, "missing-key auth failure must still write a sidecar"
        c = p.parse_call_log(logs[0])
        assert c.exit_code == 1
        assert c.has_error is True
        assert c.stderr_lines and "GEMINI_API_KEY" in c.stderr_lines[0]


def test_gemini_rest_timeout_is_recorded_as_a_timeout():
    """(codex P2) A Gemini REST timeout must be counted as a TIMEOUT (timeout_calls),
    not a generic error — keeping the dashboard timeout metric consistent with the
    subprocess backends."""
    import socket as _socket
    import urllib.request

    from reviewlib import backends
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as logd:
        os.environ["REVIEW_LOG_DIR"] = logd
        old_urlopen = urllib.request.urlopen
        old_key = backends._gemini_key
        try:
            backends._gemini_key = lambda: "fake-key"

            def slow(req, timeout=None):
                raise _socket.timeout("timed out")

            urllib.request.urlopen = slow
            result = backends.review_gemini("gemini", "review this", "", Path(logd), 7, round_no=0)
        finally:
            urllib.request.urlopen = old_urlopen
            backends._gemini_key = old_key
            os.environ.pop("REVIEW_LOG_DIR", None)

        assert result.returncode == 124, "a REST timeout uses the 124 timeout code"
        logs = list(Path(logd).glob("*-gemini-r0.log"))
        assert len(logs) == 1
        c = p.parse_call_log(logs[0])
        assert c.timed_out is True, "the TIMEOUT marker must be written for a REST timeout"
        assert c.timeout_secs == 7
        assert c.has_error is True
        # And it shows up in the dashboard's timeout_calls, not just error_calls.
        sessions = p.load_sessions(Path(logd))
        stats = p.compute_stats(sessions)
        assert stats["timeout_calls"] == 1


def test_gemini_sidecar_is_private_0600():
    """The Gemini sidecar may carry reviewed prompts/diffs -> owner-only perms."""
    import stat

    from reviewlib import process

    with tempfile.TemporaryDirectory() as logd:
        os.environ["REVIEW_LOG_DIR"] = logd
        try:
            path = process.write_sidecar_log(
                "gemini", round_no=0, argv0="Gemini API gemini-2.5-flash",
                returncode=0, stdout="ok\n", stderr="",
            )
        finally:
            os.environ.pop("REVIEW_LOG_DIR", None)
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode & 0o077 == 0, f"sidecar is group/other-readable (mode {oct(mode)})"


def test_panel_threads_round_no_into_backend_logs():
    """(HYP-742 finding 3) The real round number must reach the backend so its log is
    `-r{N}` (N>=1), not always `-r0`. A brainstorm round-1 PanelJob must produce an
    `-r1` log, which makes the parser infer 'brainstorm' mode correctly."""
    from reviewlib import panel
    from reviewlib.dashboard import parser as p

    captured: dict[str, int] = {}

    def fake_backend(model, prompt, diff, cwd, timeout, round_no=0):
        captured["round_no"] = round_no
        return panel.ReviewResult(model=model, command="fake", returncode=0, stdout="ok", stderr="")

    old_resolve = panel.resolve_backend
    try:
        panel.resolve_backend = lambda model: fake_backend
        with tempfile.TemporaryDirectory() as d:
            results = panel.run_panel(
                [panel.PanelJob(model="codex", prompt="p", diff="", round_no=3)],
                Path(d), 30,
            )
    finally:
        panel.resolve_backend = old_resolve

    assert results[0].returncode == 0
    assert captured["round_no"] == 3, "PanelJob.round_no was not threaded into the backend call"

    # And a brainstorm-shaped log set (round>=1) is inferred as brainstorm by the parser.
    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        _write_call_log(ld, "20260601T120000_000000", "codex", 1, "round one\n", exit_code=0)
        _write_call_log(ld, "20260601T120010_000000", "gemini", 1, "round one\n", exit_code=0)
        sessions = p.load_sessions(ld, gap_seconds=90)
        assert len(sessions) == 1
        assert sessions[0].mode == "brainstorm", "round>=1 logs must infer brainstorm mode"


def test_every_resolvable_backend_accepts_panel_round_no_dispatch():
    """(HYP-742 rebase regression) run_panel dispatches EVERY backend uniformly as
    ``backend(model, prompt, diff, cwd, timeout, round_no)`` (panel.py). After rebasing
    the dashboard onto post-HYP-741 main, the new keyed REST backends (review_zai,
    review_commandcode) and the split claude dispatcher must all accept that 6th
    positional ``round_no`` arg — otherwise the panel raises TypeError the moment a
    board includes a z.ai / commandcode seat. Guard the whole resolvable surface by
    signature so a future backend that forgets round_no fails here, not at runtime."""
    import inspect

    from reviewlib import backends as b

    # Every backend resolve_backend() can return, plus the claude-cli leaf the dispatcher
    # forwards to. review_claude_api is intentionally excluded: it is internal-only
    # (called by the review_claude dispatcher), never returned by resolve_backend.
    resolvable = [
        b.review_codex, b.review_gemini, b.review_zai, b.review_commandcode,
        b.review_claude, b.review_claude_cli, b.review_opencode,
    ]
    for fn in resolvable:
        params = list(inspect.signature(fn).parameters)
        assert "round_no" in params, f"{fn.__name__} is missing the round_no parameter"
        # round_no must be reachable as the 6th positional arg the panel passes.
        assert len(params) >= 6, f"{fn.__name__} cannot take round_no positionally: {params}"
        assert params[5] == "round_no", f"{fn.__name__} 6th param is {params[5]!r}, not 'round_no'"


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


def _post(base, path, obj, *, headers=None, raw=None):
    data = raw if raw is not None else json.dumps(obj).encode("utf-8")
    hdrs = {"Content-Type": "application/json"}
    if headers is not None:
        hdrs = dict(headers)
    req = urllib.request.Request(base + path, data=data, headers=hdrs, method="POST")
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


def test_write_endpoints_reject_csrf_and_bad_input():
    """(HYP-742 finding 1) Writes are hardened against a malicious web page fetching the
    loopback port: foreign Origin -> 403, non-JSON Content-Type -> 415, oversized body ->
    413, unknown session id -> 404. A same-origin (loopback Origin) JSON write to a real
    session still succeeds."""
    with tempfile.TemporaryDirectory() as logd, tempfile.TemporaryDirectory() as stored:
        os.environ["REVIEW_LOG_DIR"] = logd
        os.environ["REVIEW_DASHBOARD_STORE"] = str(Path(stored) / "dashboard.json")
        try:
            _seed_logs(Path(logd))
            from reviewlib.dashboard import server

            httpd = server.make_server(0)
            port = httpd.server_address[1]
            base = f"http://127.0.0.1:{port}"
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            try:
                # a real session id to target
                _, runs = _get(base, "/api/runs")
                sid = runs[0]["session_id"]

                def expect_status(headers, obj, want, *, raw=None, sid_override=None):
                    target = sid_override if sid_override is not None else sid
                    try:
                        _post(base, f"/api/runs/{target}/feedback", obj, headers=headers, raw=raw)
                        raise AssertionError(f"expected {want}")
                    except urllib.error.HTTPError as e:
                        assert e.code == want, f"got {e.code}, want {want}"

                # 1. Foreign Origin -> 403 (cross-site CSRF write).
                expect_status(
                    {"Content-Type": "application/json", "Origin": "https://evil.example.com"},
                    {"feedback": "pwned"}, 403,
                )
                # 2. Non-JSON Content-Type -> 415 (a simple-request form post).
                expect_status(
                    {"Content-Type": "text/plain"},
                    {"feedback": "pwned"}, 415,
                )
                # 3. Oversized body -> 413.
                big = json.dumps({"feedback": "x" * (70 * 1024)}).encode("utf-8")
                expect_status(
                    {"Content-Type": "application/json"},
                    None, 413, raw=big,
                )
                # 4. Unknown session id -> 404 (can't plant a record for an arbitrary id).
                expect_status(
                    {"Content-Type": "application/json"},
                    {"feedback": "x"}, 404, sid_override="sess-does-not-exist",
                )
                # 5. Loopback Origin + JSON + real session -> success.
                st, r = _post(
                    base, f"/api/runs/{sid}/feedback", {"feedback": "legit same-origin"},
                    headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"},
                )
                assert st == 200 and r["annotation"]["feedback"] == "legit same-origin"
                # 6. No Origin / no Referer (curl, the dashboard's own fetch) -> allowed.
                st, r = _post(base, f"/api/runs/{sid}/conscious", {"conscious": True})
                assert st == 200 and r["annotation"]["conscious"] is True
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
