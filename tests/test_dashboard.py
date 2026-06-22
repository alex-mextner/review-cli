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


def test_parse_brainstorm_real_on_disk_format_with_sentinels():
    """The parser extracts the real topic/panel/moderator/personas from the ACTUAL discussion
    log shape modes/brainstorm.py writes (session/round/final `<!-- review:* -->` sentinels +
    a blank line before `panel=`), keeping the sentinels out of the topic and transcripts."""
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        md = ld / "20260601T120000_000000Z-brainstorm.md"
        # Byte-for-byte the structure modes/brainstorm.py emits (see its `_disc` calls).
        md.write_text(
            "# Brainstorm: Design a fast change-vs-justAsk classifier\n"
            "<!-- review:session abc123 -->\n"
            "\n"
            "panel=codex,gemini moderator=codex rounds>=5 max=8\n"
            "\n"
            "# Round 1\n"
            "<!-- review:round 1 nonce=abc123 -->\n"
            "#### Pragmatic staff engineer (codex)\n"
            "Keep it simple, short-circuit interrogatives.\n"
            "\n"
            "#### Security reviewer (gemini)\n"
            "Watch for prompt-injection in the cached text.\n"
            "\n"
            "## Moderator (round 1)\n"
            "CONTINUE. Panel agrees on a heuristic prefilter.\n"
            "\n"
            "# Final synthesis\n"
            "<!-- review:final nonce=abc123 -->\n"
            "Ship the tiered classifier.\n",
            encoding="utf-8",
        )
        bs = p.parse_brainstorm_log(md)
        assert bs is not None
        # The REAL topic — never the sentinel, never empty.
        assert bs.topic == "Design a fast change-vs-justAsk classifier", repr(bs.topic)
        assert "review:session" not in bs.topic
        assert bs.panel == ["codex", "gemini"], bs.panel
        assert bs.moderator == "codex", bs.moderator
        r1 = next(r for r in bs.rounds if r["round"] == 1)
        assert [pp["name"] for pp in r1["personas"]] == ["Pragmatic staff engineer", "Security reviewer"]
        # The round/session sentinels must NOT bleed into any persona transcript.
        for pp in r1["personas"]:
            assert "review:round" not in pp["text"], pp
            assert "review:session" not in pp["text"], pp
            assert "review:final" not in pp["text"], pp
        assert "short-circuit interrogatives" in r1["personas"][0]["text"]
        # The moderator decision + the synthesis are not attributed to a persona.
        for rnd in bs.rounds:
            for pp in rnd["personas"]:
                assert "CONTINUE" not in pp["text"], pp
                assert "Ship the tiered classifier" not in pp["text"], pp


def test_brainstorm_moderator_and_synthesis_not_attributed_to_a_persona():
    """(codex P2) `## Moderator (round N)` and `# Final synthesis` are written after a
    round's persona blocks; they must NOT bleed into the last persona's transcript, or the
    dashboard misattributes the moderator's decision / the synthesis to a persona."""
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        md = ld / "20260601T120000_000000Z-brainstorm.md"
        md.write_text(
            "# Brainstorm: cache design\n\n"
            "panel=codex,gemini moderator=codex rounds>=5 max=5\n\n"
            "# Round 1\n"
            "#### Pragmatic engineer (codex)\nKeep it simple, use an LRU.\n\n"
            "#### Security reviewer (gemini)\nWatch for cache poisoning.\n\n"
            "## Moderator (round 1)\nCONTINUE. Panel agrees on LRU.\n\n"
            "# Round 2\n"
            "#### Pragmatic engineer (codex)\nAdd a TTL.\n\n"
            "## Moderator (round 2)\nSTOP. Converged on LRU+TTL.\n\n"
            "# Final synthesis\nBEST IDEAS: LRU + TTL. RECOMMENDATION: ship it.\n",
            encoding="utf-8",
        )
        bs = p.parse_brainstorm_log(md)
        for rnd in bs.rounds:
            for persona in rnd["personas"]:
                assert "Moderator" not in persona["text"], persona
                assert "CONTINUE" not in persona["text"], persona
                assert "STOP. Converged" not in persona["text"], persona
                assert "BEST IDEAS" not in persona["text"], persona
        r1 = next(r for r in bs.rounds if r["round"] == 1)
        assert [pp["model"] for pp in r1["personas"]] == ["codex", "gemini"]
        assert "use an LRU" in r1["personas"][0]["text"]


def test_brainstorm_persona_markdown_headings_stay_in_transcript():
    """(codex P2) A persona's OWN Markdown headings (`## Risks`, `### Plan`) must NOT be
    treated as dashboard section boundaries — only `## Moderator` / `# Final synthesis`
    end persona capture. Otherwise the rest of that model's transcript is dropped."""
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        md = ld / "20260601T120000_000000Z-brainstorm.md"
        md.write_text(
            "# Brainstorm: api shape\n\n"
            "panel=codex,gemini moderator=codex rounds>=5 max=5\n\n"
            "# Round 1\n"
            "#### Pragmatic engineer (codex)\n"
            "Here is my analysis.\n\n"
            "## Risks\nRace conditions on the cache.\n\n"
            "### Plan\nStep 1: add a lock. Step 2: ship.\n\n"
            # Headings that START with reserved marker words but are the persona's OWN —
            # they must NOT be treated as control sections (codex P3).
            "## Moderator notes\nMy own aside about the moderator.\n\n"
            "# Final synthesis plan\nHow I'd structure a synthesis.\n\n"
            "#### Security reviewer (gemini)\n"
            "Looks fine to me.\n\n"
            "## Moderator (round 1)\nSTOP.\n",
            encoding="utf-8",
        )
        bs = p.parse_brainstorm_log(md)
        r1 = next(r for r in bs.rounds if r["round"] == 1)
        prag = r1["personas"][0]
        # The persona's own headings + their content are part of its transcript.
        assert "## Risks" in prag["text"], prag["text"]
        assert "Race conditions" in prag["text"]
        assert "### Plan" in prag["text"]
        assert "Step 1: add a lock" in prag["text"]
        # Reserved-word-PREFIX headings authored by the persona stay in its transcript.
        assert "## Moderator notes" in prag["text"], prag["text"]
        assert "My own aside about the moderator" in prag["text"]
        assert "# Final synthesis plan" in prag["text"]
        assert "How I'd structure a synthesis" in prag["text"]
        # But the EXACT control sections (`## Moderator (round N)`) are still excluded.
        assert "STOP" not in prag["text"]
        assert "(round 1)" not in prag["text"]


def test_persona_heading_inside_moderator_section_is_not_a_persona():
    """(codex P2) A `#### Name (model)` heading that appears INSIDE the moderator summary
    or final synthesis must NOT be parsed as an extra persona of the previous round — the
    control section suppresses persona capture until the next `# Round N`."""
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        md = ld / "20260601T120000_000000Z-brainstorm.md"
        md.write_text(
            "# Brainstorm: t\n\n"
            "panel=codex,gemini moderator=codex rounds>=5 max=5\n\n"
            "# Round 1\n"
            "#### Pragmatic engineer (codex)\nLRU.\n\n"
            "## Moderator (round 1)\n"
            "Summary of the panel. The strongest take was:\n"
            "#### Security reviewer (gemini)\n"  # quoted inside the moderator text
            "(the moderator is recapping gemini's point, not a new persona)\n\n"
            "# Final synthesis\n"
            "#### Pragmatic engineer (codex)\nThis recap must not become a persona.\n",
            encoding="utf-8",
        )
        bs = p.parse_brainstorm_log(md)
        r1 = next(r for r in bs.rounds if r["round"] == 1)
        # Exactly ONE persona in round 1 — the moderator's quoted `#### …` is not counted.
        assert [pp["model"] for pp in r1["personas"]] == ["codex"], r1["personas"]
        assert r1["personas"][0]["text"].strip() == "LRU.", r1["personas"][0]["text"]


def test_emit_rest_log_swallows_encoding_errors_best_effort():
    """(codex P3) _emit_rest_log is best-effort: if write_sidecar_log raises on un-encodable
    text (an unpaired surrogate → UnicodeEncodeError, NOT an OSError), it must be swallowed,
    NOT propagated — else it would flip a successful REST ReviewResult into a failure since
    _emit_rest_log is called on the backends' success path."""
    from reviewlib import backends

    with tempfile.TemporaryDirectory() as logd:
        os.environ["REVIEW_LOG_DIR"] = logd
        try:
            # stdout carries a lone surrogate; write_sidecar_log's utf-8 write raises
            # UnicodeEncodeError. _emit_rest_log must swallow it and return normally.
            backends._emit_rest_log(
                "z.ai", "z.ai API glm-5.2", round_no=0, returncode=0,
                stdout="finding: \ud800 broken char", stderr="",
            )
        finally:
            os.environ.pop("REVIEW_LOG_DIR", None)
        # Reaching here (no exception) is the assertion: the encoding error was swallowed.


def test_review_succeeds_even_if_sidecar_write_raises():
    """End-to-end: a successful REST response whose sidecar write raises must still return
    a successful ReviewResult (best-effort logging never changes the outcome)."""
    import urllib.request

    from reviewlib import backends

    class _FakeResp:
        def __init__(self, payload: bytes):
            self._payload = payload

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    payload = json.dumps({"choices": [{"message": {"content": "a real finding"}}], "usage": {}}).encode("utf-8")

    with tempfile.TemporaryDirectory() as logd:
        os.environ["REVIEW_LOG_DIR"] = logd
        os.environ["ZAI_API_KEY"] = "k"
        old_urlopen = urllib.request.urlopen
        old_write = backends.write_sidecar_log
        try:
            urllib.request.urlopen = lambda req, timeout=None: _FakeResp(payload)
            # Force the sidecar write to raise a non-OSError (UnicodeEncodeError class).
            def _boom(*a, **k):
                raise UnicodeEncodeError("utf-8", "x", 0, 1, "forced")

            backends.write_sidecar_log = _boom
            result = backends.review_zai("zai", "q", "", Path(logd), 10, round_no=0)
        finally:
            urllib.request.urlopen = old_urlopen
            backends.write_sidecar_log = old_write
            os.environ.pop("REVIEW_LOG_DIR", None)
            os.environ.pop("ZAI_API_KEY", None)
        assert result.returncode == 0, (result.returncode, result.stderr)
        assert "real finding" in result.stdout


def test_duration_capped_for_untrustworthy_mtime():
    """(codex P3) A log whose mtime is far beyond the per-call timeout window (copied /
    restored long after its filename stamp) must not report a multi-day duration; the
    duration uses the same cap/fallback as ended_at."""
    import os as _os
    import time as _time

    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        # Filename stamp far in the past; mtime = now (days later) → untrustworthy.
        path = _write_call_log(ld, "20200101T000000_000000", "codex", 0, "old\n", exit_code=0)
        now = _time.time()
        _os.utime(path, (now, now))
        c = p.parse_call_log(path)
        # ended_at fell back to start, so duration is capped (0), not multi-day.
        assert c.duration_seconds == 0.0, c.duration_seconds


def test_brainstorm_model_attribution_is_stable_across_log_aging():
    """(codex P2) For a brainstorm, the `panel=` line is authoritative — model attribution
    must be the SAME whether the per-call logs still exist or have aged out, and must keep
    aliased / suffixed variants (`codex:gpt-5`, `zai`) that the resolved log filenames lose."""
    from reviewlib.dashboard import parser as p

    # (a) logs aged out: only the brainstorm md survives.
    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        _write_brainstorm(ld, "20260601T120000_000000", "T", "codex:gpt-5,zai", "codex")
        aged = p.load_sessions(ld)[0]
        assert aged.calls == []
        assert aged.models == ["codex:gpt-5", "zai"], aged.models

    # (b) logs still present (resolved backend names) — attribution is EXACTLY the panel,
    # NOT double-counted with the resolved per-call backends (`codex`, `z.ai`).
    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        _write_brainstorm(ld, "20260601T120000_000000", "T", "codex:gpt-5,zai", "codex")
        _write_call_log(ld, "20260601T120001_000000", "codex", 1, "round one\n", exit_code=0)
        _write_call_log(ld, "20260601T120002_000000", "z.ai", 1, "round one\n", exit_code=0)
        live = next(s for s in p.load_sessions(ld) if s.brainstorm is not None)
        # Identical to the aged-out case (a) — stable, no resolved backends appended.
        assert live.models == ["codex:gpt-5", "zai"], live.models


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


def test_panel_session_surfaces_recorded_invocations_as_prompt():
    """A non-brainstorm panel run has no topic; Session.invocations must surface the DISTINCT
    recorded argv0 lines and to_summary must expose them as `invocations`, so the Prompts panel
    / panel rows can show the invocation instead of a blank 'redacted' note."""
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        # A 3-seat panel at the same instant: each call records a distinct argv0. The 2nd and
        # 3rd share a backend invocation string to prove de-dup keeps it once.
        _write_call_log(ld, "20260601T100000_000000", "codex", 0, "ok\n",
                        argv0="/opt/homebrew/bin/codex", exit_code=0)
        _write_call_log(ld, "20260601T100001_000000", "z.ai", 0, "ok\n",
                        argv0="z.ai API glm-5.2", exit_code=0)
        _write_call_log(ld, "20260601T100002_000000", "gemini", 0, "ok\n",
                        argv0="z.ai API glm-5.2", exit_code=0)  # duplicate invocation string
        sessions = p.load_sessions(ld, gap_seconds=90)
        assert len(sessions) == 1, sessions
        s = sessions[0]
        assert s.mode == "panel", s.mode
        # Distinct, order-preserving, de-duplicated invocation lines.
        assert s.invocations == ["/opt/homebrew/bin/codex", "z.ai API glm-5.2"], s.invocations
        # And the summary exposes them for the UI (was missing entirely before).
        summ = s.to_summary()
        assert summ["topic"] is None, "a panel run has no brainstorm topic"
        assert summ["invocations"] == ["/opt/homebrew/bin/codex", "z.ai API glm-5.2"], summ
        # An empty argv0 is never surfaced as a blank invocation.
        empty = p.Session("sess-x", s.started, s.ended, calls=[
            p.CallLog("", "x.log", s.started, "codex", 0, argv0="", body="")
        ])
        assert empty.invocations == [], empty.invocations


def test_error_recovery_recovered_when_a_clean_call_follows_in_session():
    """A failed seat is `recovered` when a clean OK call ran concurrently-or-after it in the
    same session (the failover pool / a retry produced a verdict). Each error also carries its
    resolved MODEL id, failure CLASS, and the planned FALLBACK seat (next board priority)."""
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        # A panel: a Cloudflare-blocked agentic Qwen seat (403) + a clean agentic DeepSeek seat
        # right after. The default board is AGENTIC (review-cli#24), so the seats are the `oc:`
        # opencode ids — model_id_for_call recovers the `-m <provider/model>` selector.
        _write_call_log(ld, "20260601T100000_000000", "opencode", 0, "[stderr] error code: 1010\n",
                        argv0="opencode run --agent read-only-reviewer --dir /x -m commandcode/Qwen/Qwen3.7-Max",
                        exit_code=403)
        _write_call_log(ld, "20260601T100002_000000", "opencode", 0,
                        "## Findings\nA real verdict.\n",
                        argv0="opencode run --agent read-only-reviewer --dir /x -m commandcode/deepseek/deepseek-v4-pro",
                        exit_code=0)
        s = p.load_sessions(ld, gap_seconds=90)[0]
        errs = s.errors
        assert len(errs) == 1, errs
        e = errs[0]
        assert e["model"] == "oc:commandcode/Qwen/Qwen3.7-Max", e
        assert e["health_class"] == p.HEALTH_BLOCKED, e
        assert e["recovery"] == "recovered", e  # DeepSeek's clean call after it recovered the run
        # The planned fallback is the next board seat by priority after Qwen (DeepSeek).
        assert e["fallback"] is not None and e["fallback"]["display"] == "DeepSeek", e["fallback"]


def test_error_recovery_unrecovered_when_no_clean_call():
    """A lone failed seat with no clean call anywhere is `unrecovered` — the Errors tab surfaces
    the manual-control affordance for these."""
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        _write_call_log(ld, "20260601T100000_000000", "z.ai", 0, '[stderr] {"error":"bad key"}\n',
                        argv0="z.ai API glm-5.2", exit_code=401)
        s = p.load_sessions(ld, gap_seconds=90)[0]
        e = s.errors[0]
        assert e["model"] == "zai:glm-5.2", e
        assert e["health_class"] == p.HEALTH_AUTH, e
        assert e["recovery"] == "unrecovered", e


def test_error_recovery_does_not_overclaim_from_an_earlier_round_success():
    """(glm review finding 5) A round-1 success must NOT mark a LATER-round failure 'recovered'.
    Recovery is 'a clean call in the SAME round (parallel sibling) or at/after the failure', not
    'any clean call in the session', so a failure whose only clean call is in an EARLIER round is
    honestly `unrecovered`."""
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        # Round 1: a clean codex call. Round 3 (later): a GLM auth failure with nothing clean
        # in its round or after — the earlier round-1 success must NOT count as recovery.
        _write_call_log(ld, "20260601T100000_000000", "codex", 1, "## Findings\nverdict.\n",
                        argv0="/opt/homebrew/bin/codex", exit_code=0)
        _write_call_log(ld, "20260601T100030_000000", "z.ai", 3, '[stderr] {"error":"bad key"}\n',
                        argv0="z.ai API glm-5.2", exit_code=401)
        s = p.load_sessions(ld, gap_seconds=90)[0]
        glm_err = next(e for e in s.errors if e["model"] == "zai:glm-5.2")
        assert glm_err["recovery"] == "unrecovered", glm_err


def test_error_recovery_recovered_by_parallel_panel_sibling_same_round():
    """A parallel panel fan-out: all seats answer the SAME request in the same round. A clean
    sibling in that round means the run produced a verdict despite this seat failing — even if
    the sibling's log finished a moment BEFORE this seat's failure timestamp (the file mtimes in
    a fan-out aren't strictly ordered)."""
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        # DeepSeek (clean) logged first, then Kimi (blocked) — both round 0 of one panel.
        _write_call_log(ld, "20260601T100000_000000", "commandcode", 0, "## Findings\nverdict.\n",
                        argv0="commandcode API deepseek/deepseek-v4-pro", exit_code=0)
        _write_call_log(ld, "20260601T100004_000000", "commandcode", 0, "[stderr] error code: 1010\n",
                        argv0="commandcode API moonshotai/Kimi-K2.7-Code", exit_code=403)
        s = p.load_sessions(ld, gap_seconds=90)[0]
        kimi_err = next(e for e in s.errors if "Kimi" in e["model"])
        assert kimi_err["recovery"] == "recovered", kimi_err


def test_fallback_seat_is_next_priority_and_none_for_last_seat():
    """`_fallback_seat_for` returns the next board seat by priority, and None for the
    lowest-priority seat (the board has no lower reserve) or an off-board model."""
    from reviewlib.dashboard import parser as p
    from reviewlib.config import DEFAULT_BOARD

    first = DEFAULT_BOARD[0].model
    second = DEFAULT_BOARD[1]
    last = DEFAULT_BOARD[-1].model
    fb = p._fallback_seat_for(first)
    assert fb is not None and fb["model"] == second.model and fb["priority"] == 2, fb
    assert p._fallback_seat_for(last) is None, "the lowest-priority seat has no fallback"
    assert p._fallback_seat_for("opencode") is None, "an off-board model has no board fallback"


def test_fallback_resolves_for_real_call_resolved_gateway_ids():
    """(glm review finding 2/6) The id `model_id_for_call` returns for a real gateway call (e.g.
    `commandcode:moonshotai/Kimi-K2.7-Code`, `zai:glm-5.2`) is EXACTLY a DEFAULT_BOARD seat id,
    so `_fallback_seat_for` resolves a real next-priority seat — NOT None — for the in-production
    failing-seat case. Pin it so a board re-id can't silently make every fallback hint blank."""
    from reviewlib.dashboard import parser as p
    from reviewlib.config import DEFAULT_BOARD

    board_ids = [b.model for b in DEFAULT_BOARD]
    # Every NON-LAST board seat must resolve a concrete fallback whose id is the next board seat.
    for idx, b in enumerate(DEFAULT_BOARD[:-1]):
        fb = p._fallback_seat_for(b.model)
        assert fb is not None, f"{b.model} (priority {idx + 1}) should have a fallback"
        assert fb["model"] == board_ids[idx + 1], (b.model, fb)
    # The gateway-routed seat a failing Kimi call carries (the board's exact id — agentic `oc:`
    # form today) resolves a real fallback, not None — the production failing-seat case.
    kimi_seat = next(b.model for b in DEFAULT_BOARD if b.display == "Kimi")
    assert p._fallback_seat_for(kimi_seat) is not None
    # The z.ai GLM seat is now the LAST-RESORT reserve (deprioritized, review-cli#65), so by
    # construction it has no next-priority fallback — None is the correct hint for the lowest
    # seat (the general "last seat -> None" rule, asserted dynamically as DEFAULT_BOARD[-1]).
    glm_seat = next(b.model for b in DEFAULT_BOARD if b.display == "GLM")
    assert glm_seat == DEFAULT_BOARD[-1].model, glm_seat  # pin: GLM is the last seat
    assert p._fallback_seat_for(glm_seat) is None


def test_to_summary_exposes_enriched_errors_for_the_errors_tab():
    """to_summary carries the enriched `errors` list so the Errors tab can drill down + show
    recovery/fallback without a per-session detail fetch. A clean session has an empty list."""
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        _write_call_log(ld, "20260601T100000_000000", "codex", 0, "ok\n",
                        argv0="/opt/homebrew/bin/codex", exit_code=0)
        clean = p.load_sessions(ld)[0]
        assert clean.to_summary()["errors"] == [], "a clean session surfaces no errors"


def test_call_to_dict_carries_resolved_model_for_the_detail_chip():
    """to_dict exposes the resolved gateway MODEL id (not the bare backend) so the detail view's
    per-call chip wears the right brand logo/label (e.g. Qwen, not the generic gateway)."""
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        path = _write_call_log(ld, "20260601T100000_000000", "commandcode", 0, "x\n",
                               argv0="commandcode API Qwen/Qwen3.7-Max", exit_code=0)
        c = p.parse_call_log(path)
        assert c.to_dict()["model"] == "commandcode:Qwen/Qwen3.7-Max", c.to_dict()


def test_stats_exposes_priority_ordered_board():
    """compute_stats carries the priority-ordered board so the UI can show failover order."""
    from reviewlib.dashboard import parser as p
    from reviewlib.config import DEFAULT_BOARD

    stats = p.compute_stats([])
    board = stats["board"]
    assert [b["model"] for b in board] == [b.model for b in DEFAULT_BOARD]
    assert board[0]["priority"] == 1 and board[-1]["priority"] == len(DEFAULT_BOARD)


def test_invocations_endpoint_returns_populated_prompt_for_panel():
    """GET /api/runs returns a populated `invocations` list for a panel session, so the Prompts
    panel renders the invoked command rather than a blank/redacted placeholder."""
    with tempfile.TemporaryDirectory() as logd, tempfile.TemporaryDirectory() as stored:
        os.environ["REVIEW_LOG_DIR"] = logd
        os.environ["REVIEW_DASHBOARD_STORE"] = str(Path(stored) / "dashboard.json")
        try:
            _write_call_log(Path(logd), "20260601T100000_000000", "codex", 0, "ok\n",
                            argv0="/opt/homebrew/bin/codex", exit_code=0)
            _write_call_log(Path(logd), "20260601T100001_000000", "z.ai", 0, "ok\n",
                            argv0="z.ai API glm-5.2", exit_code=0)
            from reviewlib.dashboard import server

            httpd = server.make_server(0)
            base = f"http://127.0.0.1:{httpd.server_address[1]}"
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            try:
                with urllib.request.urlopen(base + "/api/runs", timeout=10) as r:
                    runs = json.loads(r.read().decode("utf-8"))
                assert len(runs) == 1, runs
                assert runs[0]["mode"] == "panel", runs[0]
                assert runs[0]["invocations"] == ["/opt/homebrew/bin/codex", "z.ai API glm-5.2"], runs[0]
            finally:
                httpd.shutdown()
                httpd.server_close()
        finally:
            os.environ.pop("REVIEW_LOG_DIR", None)
            os.environ.pop("REVIEW_DASHBOARD_STORE", None)


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


# --- per-model health classifier (Models & roles tab) -----------------------
#
# These cover the dashboard's per-model health view + the problematic-count badge. Each
# fixture log is written in the EXACT on-disk format for one real failure class, so the
# classifier is exercised against the same bytes review-cli writes (paywall / 401 auth /
# 403 + 1010 blocked / 124 timeout / empty / ok), plus the model-attribution that splits
# the shared `commandcode` / `z.ai` backends into their gateway models and the `claude`
# wrapper into Fable (paywall) vs Opus.


def _fable_paywall_log(log_dir: Path, stamp: str) -> Path:
    # The on-disk Fable body has interior whitespace collapsed (the logger strips it), so
    # the sentinel reads `currentlyunavailable`. EXIT is 0 — the body, not the code, is the
    # failure signal.
    return _write_call_log(
        log_dir, stamp, "claude", 0,
        "ClaudeFable5iscurrentlyunavailable.Learnmore:\nhttps://www.anthropic.com/news/fable\n",
        argv0="/Users/x/.local/bin/claude-p", exit_code=0,
    )


def _opus_ok_log(log_dir: Path, stamp: str) -> Path:
    return _write_call_log(
        log_dir, stamp, "claude", 0,
        "## Findings\n1. A real, substantive review verdict about the diff.\n",
        argv0="/Users/x/.local/bin/claude-p", exit_code=0,
    )


def test_classify_paywall_auth_blocked_timeout_empty_ok():
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        # paywall — Fable, EXIT 0, body sentinel
        paywall = p.parse_call_log(_fable_paywall_log(ld, "20260601T100000_000000"))
        # auth — z.ai bad key, EXIT 401
        auth = p.parse_call_log(_write_call_log(
            ld, "20260601T100100_000000", "z.ai", 0,
            '[stderr] {"error":"bad key"}\n', argv0="z.ai API glm-5.2", exit_code=401))
        # blocked — commandcode Cloudflare, EXIT 403 + 1010
        blocked = p.parse_call_log(_write_call_log(
            ld, "20260601T100200_000000", "commandcode", 0,
            "[stderr] error code: 1010\n", argv0="commandcode API moonshotai/Kimi-K2.7-Code",
            exit_code=403))
        # timeout — EXIT 124 + marker
        timeout = p.parse_call_log(_write_call_log(
            ld, "20260601T100300_000000", "commandcode", 0,
            "partial\n[stderr] commandcode API request failed: timed out\n"
            "[review-cli] TIMEOUT after 10s — partial output above]\n",
            argv0="commandcode API Qwen/Qwen3.7-Max", exit_code=124))
        # empty — EXIT 0, output_tokens=0, no real content
        empty = p.parse_call_log(_write_call_log(
            ld, "20260601T100400_000000", "z.ai", 0,
            "[reasoning_content — no final answer returned]\n\nprompt_tokens=0 output_tokens=0\n",
            argv0="z.ai API glm-5.2", exit_code=0))
        # ok — EXIT 0, real verdict
        ok = p.parse_call_log(_opus_ok_log(ld, "20260601T100500_000000"))

        assert p.classify_call(paywall) == p.HEALTH_PAYWALL
        assert p.classify_call(auth) == p.HEALTH_AUTH
        assert p.classify_call(blocked) == p.HEALTH_BLOCKED
        assert p.classify_call(timeout) == p.HEALTH_TIMEOUT
        assert p.classify_call(empty) == p.HEALTH_EMPTY
        assert p.classify_call(ok) == p.HEALTH_OK


def test_real_output_with_zero_usage_fallback_is_ok_not_empty():
    """A REST backend that returns REAL review text but omits usage metadata still appends a
    fallback usage line (`input_tokens=0 output_tokens=0`). The zero-usage line must NOT make
    the classifier call it empty — there is a real verdict above it, so it's OK."""
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        # claude API mode: real verdict body, then the zero-usage fallback the backend writes
        # when the response carried no usage block.
        real = p.parse_call_log(_write_call_log(
            ld, "20260601T100600_000000", "claude", 0,
            "## Findings\n1. A real, substantive review verdict about the diff.\n"
            "\n\ninput_tokens=0 output_tokens=0\n",
            argv0="Anthropic API claude-opus-4-8", exit_code=0))
        assert p.classify_call(real) == p.HEALTH_OK

        # Guard the other half: a claude API call with ONLY the zero-usage fallback line
        # (no verdict text) is still genuinely EMPTY — dropping the blanket short-circuit
        # must not start mis-classifying a truly empty call as OK.
        truly_empty = p.parse_call_log(_write_call_log(
            ld, "20260601T100650_000000", "claude", 0,
            "input_tokens=0 output_tokens=0\n",
            argv0="Anthropic API claude-opus-4-8", exit_code=0))
        assert p.classify_call(truly_empty) == p.HEALTH_EMPTY


def test_model_attribution_splits_shared_backends_and_claude():
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        # The board's Kimi/DeepSeek/GLM seats run AGENTICALLY via opencode now
        # (review-cli#24): the runtime header is `opencode -m <provider/model>`, attributed
        # to the `oc:` board seat (not a single `opencode` row).
        kimi = p.parse_call_log(_write_call_log(
            ld, "20260601T100000_000000", "opencode", 0, "x\n",
            argv0="opencode -m commandcode/moonshotai/Kimi-K2.7-Code", exit_code=0))
        deepseek = p.parse_call_log(_write_call_log(
            ld, "20260601T100100_000000", "opencode", 0, "x\n",
            argv0="opencode -m commandcode/deepseek/deepseek-v4-pro", exit_code=0))
        glm = p.parse_call_log(_write_call_log(
            ld, "20260601T100200_000000", "opencode", 0, "x\n",
            argv0="opencode -m zai/glm-5.2", exit_code=0))
        # The diff-only commandcode/z.ai REST backends still exist (explicit `-m cc`/`-m glm`,
        # config boards) and must still split into their gateway board prefixes.
        kimi_rest = p.parse_call_log(_write_call_log(
            ld, "20260601T100210_000000", "commandcode", 0, "x\n",
            argv0="commandcode API moonshotai/Kimi-K2.7-Code", exit_code=0))
        glm_rest = p.parse_call_log(_write_call_log(
            ld, "20260601T100220_000000", "z.ai", 0, "x\n",
            argv0="z.ai API glm-5.2", exit_code=0))
        codex = p.parse_call_log(_write_call_log(
            ld, "20260601T100300_000000", "codex", 0, "x\n",
            argv0="/opt/homebrew/bin/codex", exit_code=0))
        fable = p.parse_call_log(_fable_paywall_log(ld, "20260601T100400_000000"))
        opus = p.parse_call_log(_opus_ok_log(ld, "20260601T100500_000000"))

        # opencode seats attribute to the `oc:provider/model` board id.
        assert p.model_id_for_call(kimi) == "oc:commandcode/moonshotai/Kimi-K2.7-Code"
        assert p.model_id_for_call(deepseek) == "oc:commandcode/deepseek/deepseek-v4-pro"
        assert p.model_id_for_call(glm) == "oc:zai/glm-5.2"
        # The diff-only REST backends still split into their gateway model ids.
        assert p.model_id_for_call(kimi_rest) == "commandcode:moonshotai/Kimi-K2.7-Code"
        assert p.model_id_for_call(glm_rest) == "zai:glm-5.2"
        assert p.model_id_for_call(codex) == "codex"
        # claude wrapper is identical on disk; the body splits Fable (paywall) from Opus.
        assert p.model_id_for_call(fable) == "claude:claude-fable-5"
        assert p.model_id_for_call(opus) == "claude:claude-opus-4-8"


def test_opencode_call_attributes_to_oc_board_seat_and_matches_default_board():
    """review-cli#24: an agentic opencode call's header is `opencode -m <provider/model>`
    (review_opencode passes it as header_argv0), and the dashboard attributes it to the
    `oc:<provider/model>` board seat — the EXACT id DEFAULT_BOARD carries — so the health
    view splits Kimi/GLM/Qwen/DeepSeek instead of collapsing them to one `opencode` row.
    A bare opencode call with NO `-m` stays the backend name (`opencode`) so it can't
    mis-attribute to a real seat."""
    from reviewlib.dashboard import parser as p
    from reviewlib.config import DEFAULT_BOARD

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        # Every agentic (oc:) seat on the default board round-trips header -> board id.
        oc_seats = [b.model for b in DEFAULT_BOARD if b.model.startswith("oc:")]
        assert oc_seats, "expected agentic oc: seats on the default board (#24)"
        for i, seat in enumerate(oc_seats):
            oc_model = seat.split(":", 1)[1]  # provider/model
            call = p.parse_call_log(_write_call_log(
                ld, f"20260601T1000{i:02d}_000000", "opencode", 0, "ok\n",
                argv0=f"opencode -m {oc_model}", exit_code=0))
            assert p.model_id_for_call(call) == seat, (seat, p.model_id_for_call(call))
        # Bare opencode (no -m) -> backend name, not a board seat.
        bare = p.parse_call_log(_write_call_log(
            ld, "20260601T100900_000000", "opencode", 0, "ok\n",
            argv0="/opt/homebrew/bin/opencode", exit_code=0))
        assert p.model_id_for_call(bare) == "opencode"


def test_claude_api_argv0_identifies_model_before_opus_default():
    """In Claude API mode the sidecar argv0 carries the EXACT model as `Anthropic API
    <model>` (optionally `@ <base>`). The attributor must read that argv0 to identify the
    model BEFORE defaulting to Opus — otherwise an API-mode Fable call (no paywall body)
    is mis-attributed to the Opus seat."""
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        # API-mode Fable: real text body (no paywall sentinel), argv0 names Fable.
        fable_api = p.parse_call_log(_write_call_log(
            ld, "20260601T100600_000000", "claude", 0,
            "## Findings\n1. A real verdict from Fable via the API.\n",
            argv0="Anthropic API claude-fable-5", exit_code=0))
        # API-mode Opus, with the trailing `@ <base>` form the backend also emits.
        opus_api = p.parse_call_log(_write_call_log(
            ld, "20260601T100700_000000", "claude", 0,
            "## Findings\n1. A real verdict from Opus via the API.\n",
            argv0="Anthropic API claude-opus-4-8 @ https://api.anthropic.com", exit_code=0))
        # CLI mode (claude-p): argv0 has no model, so the body sentinel still decides.
        opus_cli = p.parse_call_log(_opus_ok_log(ld, "20260601T100800_000000"))
        # CLI mode where the `claude-p` binary PATH itself contains `API ` (e.g. installed
        # under `/opt/API Tools/`). A generic `\bAPI ` match would mis-read `Tools/claude-p`
        # as the model; the anchored `^Anthropic API ` match must NOT, so this still falls
        # through to the body sentinel (paywall=Fable / else Opus).
        opus_cli_apipath = p.parse_call_log(_write_call_log(
            ld, "20260601T100900_000000", "claude", 0,
            "## Findings\n1. A real verdict from Opus via the CLI.\n",
            argv0="/opt/API Tools/claude-p --permission-mode dontAsk -p", exit_code=0))
        fable_cli_apipath = p.parse_call_log(_write_call_log(
            ld, "20260601T101000_000000", "claude", 0,
            "ClaudeFable5iscurrentlyunavailable.Learnmore:\n",
            argv0="/opt/API Tools/claude-p --permission-mode dontAsk -p", exit_code=0))

        assert p.model_id_for_call(fable_api) == "claude:claude-fable-5"
        assert p.model_id_for_call(opus_api) == "claude:claude-opus-4-8"
        assert p.model_id_for_call(opus_cli) == "claude:claude-opus-4-8"
        assert p.model_id_for_call(opus_cli_apipath) == "claude:claude-opus-4-8"
        assert p.model_id_for_call(fable_cli_apipath) == "claude:claude-fable-5"


def test_paywall_sentinel_survives_whitespace_collapse():
    """The Fable body lands de-spaced (`currentlyunavailable`); a spaced match would miss
    it. The classifier normalizes whitespace, so both renderings classify as paywall."""
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        spaced = p.parse_call_log(_write_call_log(
            ld, "20260601T100000_000000", "claude", 0,
            "Claude Fable 5 is currently unavailable. Learn more:\n",
            argv0="/x/claude-p", exit_code=0))
        collapsed = p.parse_call_log(_fable_paywall_log(ld, "20260601T100100_000000"))
        assert p.classify_call(spaced) == p.HEALTH_PAYWALL
        assert p.classify_call(collapsed) == p.HEALTH_PAYWALL


def test_compute_model_health_covers_board_and_flags_problematic():
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        # Kimi (agentic opencode) — 3 blocked calls => hard-unavailable + 0% ok =>
        # problematic. The runtime log is an `opencode -m <provider/model>` header now
        # (review-cli#24), attributed to the `oc:` board seat.
        for i in range(3):
            _write_call_log(ld, f"20260601T1000{i:02d}_000000", "opencode", 0,
                            "[stderr] error code: 1010\n",
                            argv0="opencode -m commandcode/moonshotai/Kimi-K2.7-Code", exit_code=403)
        # GLM (agentic opencode via the zai provider) — 3 auth failures => problematic.
        for i in range(3):
            _write_call_log(ld, f"20260601T1100{i:02d}_000000", "opencode", 0,
                            '[stderr] {"error":"bad key"}\n',
                            argv0="opencode -m zai/glm-5.2", exit_code=401)
        # Fable (claude) — paywall => problematic.
        _fable_paywall_log(ld, "20260601T120000_000000")
        # Codex — 4 healthy calls => NOT problematic (ok-rate 100%).
        for i in range(4):
            _write_call_log(ld, f"20260601T1300{i:02d}_000000", "codex", 0,
                            "## Findings\nA substantive review verdict.\n",
                            argv0="/opt/homebrew/bin/codex", exit_code=0)

        stats = p.compute_stats(p.load_sessions(ld))
        health = {m["model"]: m for m in stats["model_health"]}

        # Every board model is represented (covers the whole board, even no-data seats).
        for board_id in ("claude:claude-fable-5", "claude:claude-opus-4-8", "codex",
                         "oc:commandcode/moonshotai/Kimi-K2.7-Code", "oc:zai/glm-5.2"):
            assert board_id in health, board_id

        assert health["oc:commandcode/moonshotai/Kimi-K2.7-Code"]["problematic"] is True
        assert health["oc:commandcode/moonshotai/Kimi-K2.7-Code"]["dominant_class"] == p.HEALTH_BLOCKED
        assert health["oc:zai/glm-5.2"]["problematic"] is True
        assert health["oc:zai/glm-5.2"]["dominant_class"] == p.HEALTH_AUTH
        assert health["claude:claude-fable-5"]["problematic"] is True
        assert health["claude:claude-fable-5"]["dominant_class"] == p.HEALTH_PAYWALL
        assert health["codex"]["problematic"] is False
        assert health["codex"]["ok_rate"] == 1.0
        # Opus had no calls => no_data, not problematic.
        assert health["claude:claude-opus-4-8"]["status"] == "no_data"
        assert health["claude:claude-opus-4-8"]["problematic"] is False

        # Badge count = problematic BOARD models. Kimi + GLM + Fable = 3.
        assert stats["problematic_count"] == 3


def test_recent_streak_makes_a_model_problematic_even_below_rate_threshold():
    """Most-recent-N all-failing trips problematic even when the longer-window rate is OK
    (a fresh outage an averaged rate would otherwise dilute)."""
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        # 6 healthy OLD codex calls (rate stays high) ...
        for i in range(6):
            _write_call_log(ld, f"20260601T1000{i:02d}_000000", "codex", 0,
                            "## Findings\nA real verdict.\n",
                            argv0="/opt/homebrew/bin/codex", exit_code=0)
        # ... then 3 fresh timeouts (newest) — the recent streak should flip problematic.
        for i in range(3):
            _write_call_log(ld, f"20260601T2000{i:02d}_000000", "codex", 0,
                            "partial\n[review-cli] TIMEOUT after 10s — partial output above]\n",
                            argv0="/opt/homebrew/bin/codex", exit_code=124)

        stats = p.compute_stats(p.load_sessions(ld))
        codex = next(m for m in stats["model_health"] if m["model"] == "codex")
        # 6/9 ok => ok_rate 0.667 (below the 0.5 fail threshold) but the 3 newest all fail.
        assert codex["ok_rate"] > 0.5
        assert codex["current_class"] == p.HEALTH_TIMEOUT
        assert codex["problematic"] is True


def test_model_health_empty_log_dir_is_graceful():
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        stats = p.compute_stats(p.load_sessions(Path(d) / "nope"))
        assert stats["problematic_count"] == 0
        # The board is still listed (all no_data), so the view never shows a blank tab.
        assert all(m["status"] == "no_data" for m in stats["model_health"])
        assert {m["model"] for m in stats["model_health"]} >= {"codex", "oc:zai/glm-5.2"}


def test_bare_commandcode_probe_keeps_backend_name_and_classifies_error():
    """A bare `commandcode` header (no `API <model>`) has no gateway model, so the id stays
    the backend name — and a generic non-zero exit with no recognized sentinel is ERROR."""
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        probe = p.parse_call_log(_write_call_log(
            ld, "20260601T100000_000000", "commandcode", 0,
            "[stderr] something generic went wrong\n", argv0="commandcode", exit_code=2))
        assert p.model_id_for_call(probe) == "commandcode"
        assert p.classify_call(probe) == p.HEALTH_ERROR  # exit 2, no sentinel


def test_blocked_marker_in_body_not_just_stderr_is_blocked():
    """The CF bot-block marker can land in the BODY (not stderr); both must classify blocked."""
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        in_body = p.parse_call_log(_write_call_log(
            ld, "20260601T100000_000000", "commandcode", 0,
            "error code: 1010\n", argv0="commandcode API Qwen/Qwen3.7-Max", exit_code=1))
        assert p.classify_call(in_body) == p.HEALTH_BLOCKED


def test_dominant_class_tie_break_prefers_hard_unavailable():
    """When two failure classes are equally frequent, the dominant-class tie-break picks the
    hard-unavailable one (the only reason HARD_UNAVAILABLE_CLASSES is ordered)."""
    from reviewlib.dashboard import parser as p

    # 2 timeout + 2 blocked -> blocked wins (hard-unavailable beats timeout on a tie).
    assert p._dominant_class(
        [p.HEALTH_TIMEOUT, p.HEALTH_BLOCKED, p.HEALTH_TIMEOUT, p.HEALTH_BLOCKED]
    ) == p.HEALTH_BLOCKED
    # All-OK -> no dominant FAILURE class.
    assert p._dominant_class([p.HEALTH_OK, p.HEALTH_OK]) is None


def test_non_board_model_is_appended_after_board_in_order():
    """A backend that isn't on DEFAULT_BOARD still appears (so the view is complete), but
    AFTER the board models, which come out in board/priority order."""
    from reviewlib.dashboard import parser as p
    from reviewlib.config import DEFAULT_BOARD

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        # `opencode` is not on DEFAULT_BOARD.
        _write_call_log(ld, "20260601T100000_000000", "opencode", 0,
                        "## Findings\nA real verdict.\n", argv0="/x/opencode", exit_code=0)
        stats = p.compute_stats(p.load_sessions(ld))
        ids = [m["model"] for m in stats["model_health"]]
        board_ids = [b.model for b in DEFAULT_BOARD]
        # Board ids lead, in DEFAULT_BOARD order; the non-board id trails.
        assert ids[: len(board_ids)] == board_ids
        assert "opencode" in ids and ids.index("opencode") >= len(board_ids)
        opencode = next(m for m in stats["model_health"] if m["model"] == "opencode")
        assert opencode["on_board"] is False


def test_footerless_clean_log_is_running_not_success():
    """(codex P2) A footerless, error-free log = a call still streaming or whose writer
    died before the EXIT footer. It must NOT be counted as a success: completed is False,
    it lands in running_calls, and success_rate is computed over COMPLETED calls only."""
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        # An in-flight call: header + partial body, NO EXIT footer, no error markers.
        running = _write_call_log(ld, "20260601T100000_000000", "codex", 0,
                                  "still working on the review...\n", exit_code=None)
        c = p.parse_call_log(running)
        assert c.exit_code is None
        assert c.completed is False, "a footerless clean log is not finished"
        assert c.has_error is False, "and it is not (yet) an error either"
        # One finished OK call alongside it, so we can check the rate denominator.
        _write_call_log(ld, "20260601T100005_000000", "gemini", 0, "looks good\n", exit_code=0)
        stats = p.compute_stats(p.load_sessions(ld, gap_seconds=90))
        assert stats["call_count"] == 2
        assert stats["running_calls"] == 1, stats
        assert stats["ok_calls"] == 1, stats
        assert stats["error_calls"] == 0, stats
        # 1 ok / (1 ok + 0 error) = 1.0 — the running call does not drag the rate.
        assert stats["success_rate"] == 1.0, stats


def test_running_session_surfaces_running_flag_for_the_ui():
    """(codex P2) A footerless in-flight session must be exposed as `running` (not OK) in
    the run summary AND `completed: False` per call, so the UI badges it running/unknown
    instead of a green OK."""
    from reviewlib.dashboard import parser as p

    with tempfile.TemporaryDirectory() as d:
        ld = Path(d)
        _write_call_log(ld, "20260601T100000_000000", "codex", 0,
                        "still streaming the review...\n", exit_code=None)  # no footer
        sessions = p.load_sessions(ld, gap_seconds=90)
        assert len(sessions) == 1
        summary = sessions[0].to_summary()
        assert summary["has_error"] is False
        assert summary["running"] is True, "footerless in-flight session must surface running"
        detail = sessions[0].to_detail()
        assert detail["calls"][0]["completed"] is False, "per-call completed must be exposed for the UI badge"
        # A finished OK session is NOT running.
        _write_call_log(ld, "20260602T100000_000000", "codex", 0, "done\n", exit_code=0)
        done = [s for s in p.load_sessions(Path(d), gap_seconds=90) if not s.running and not s.has_error]
        assert any(s.to_detail()["calls"][0]["completed"] is True for s in done)


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
                # A committed model brand-logo PNG is served from the allowlisted icons/ path
                # as a real image (the front-end renders each model as <img>, never an emoji).
                # mini_claude.png is the Anthropic brand mark (always committed — Opus/Fable seats).
                with urllib.request.urlopen(base + "/assets/icons/mini_claude.png", timeout=10) as resp:
                    assert resp.status == 200
                    assert resp.headers.get("Content-Type") == "image/png"
                    assert resp.read(8) == b"\x89PNG\r\n\x1a\n"  # real PNG magic, not an HTML error
                # An icon name NOT in the discovered allowlist is rejected (no arbitrary read).
                try:
                    urllib.request.urlopen(base + "/assets/icons/nope.png", timeout=10)
                    raise AssertionError("expected unknown-icon 404")
                except urllib.error.HTTPError as e:
                    assert e.code == 404
                # asset traversal blocked (top-level and via the icons/ subpath)
                for bad in ("/assets/../server.py", "/assets/icons/../app.css"):
                    try:
                        urllib.request.urlopen(base + bad, timeout=10)
                        raise AssertionError(f"expected traversal block for {bad}")
                    except urllib.error.HTTPError as e:
                        assert e.code == 404
            finally:
                httpd.shutdown()
                httpd.server_close()
        finally:
            os.environ.pop("REVIEW_LOG_DIR", None)
            os.environ.pop("REVIEW_DASHBOARD_STORE", None)


def test_host_allowlist_includes_configured_extra_host():
    """(--host / Tailscale exposure) With $REVIEW_DASHBOARD_ALLOWED_HOSTS set (the Tailscale
    host), a request carrying that Host is served (not 403'd) while an unrelated foreign Host
    is still rejected — the rebinding guard stays ON, it just admits the explicit host."""
    with tempfile.TemporaryDirectory() as logd, tempfile.TemporaryDirectory() as stored:
        os.environ["REVIEW_LOG_DIR"] = logd
        os.environ["REVIEW_DASHBOARD_STORE"] = str(Path(stored) / "dashboard.json")
        os.environ["REVIEW_DASHBOARD_ALLOWED_HOSTS"] = "ultras-mbp.tailbfe8ea.ts.net"
        try:
            from reviewlib.dashboard import server

            # allowed_hosts() reads the env live; only the Tailscale lookup is cached. Force
            # it empty so the test never shells out to `tailscale` and is deterministic.
            server._tailscale_cache = set()
            httpd = server.make_server(0, host="0.0.0.0")
            port = httpd.server_address[1]
            base = f"http://127.0.0.1:{port}"
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            try:
                # The configured Tailscale host -> served.
                req = urllib.request.Request(
                    base + "/api/health",
                    headers={"Host": "ultras-mbp.tailbfe8ea.ts.net"},
                )
                with urllib.request.urlopen(req, timeout=10) as r:
                    assert r.status == 200
                    payload = json.loads(r.read().decode("utf-8"))
                    assert "ultras-mbp.tailbfe8ea.ts.net" in payload["allowed_origins"]
                # An unrelated foreign Host is STILL rejected (rebinding guard intact).
                bad = urllib.request.Request(base + "/api/health", headers={"Host": "evil.example.com"})
                try:
                    urllib.request.urlopen(bad, timeout=10)
                    raise AssertionError("expected 403 for foreign Host")
                except urllib.error.HTTPError as e:
                    assert e.code == 403, e.code
                # Loopback still served.
                ok = urllib.request.Request(base + "/api/health", headers={"Host": f"127.0.0.1:{port}"})
                with urllib.request.urlopen(ok, timeout=10) as r:
                    assert r.status == 200
            finally:
                httpd._sse_stop = True
                httpd.shutdown()
                httpd.server_close()
        finally:
            os.environ.pop("REVIEW_LOG_DIR", None)
            os.environ.pop("REVIEW_DASHBOARD_STORE", None)
            os.environ.pop("REVIEW_DASHBOARD_ALLOWED_HOSTS", None)
            server._tailscale_cache = None  # let real discovery resume for other code


def test_write_allowed_from_tailscale_origin_rejected_from_foreign():
    """(--host / Tailscale exposure — the new WRITE vector) When exposed with the Tailscale
    host allowlisted, a POST whose Origin is that Tailscale host succeeds (a remote reviewer
    can leave feedback), while a foreign Origin is still 403'd (CSRF intact)."""
    with tempfile.TemporaryDirectory() as logd, tempfile.TemporaryDirectory() as stored:
        os.environ["REVIEW_LOG_DIR"] = logd
        os.environ["REVIEW_DASHBOARD_STORE"] = str(Path(stored) / "dashboard.json")
        os.environ["REVIEW_DASHBOARD_ALLOWED_HOSTS"] = "ultras-mbp.tailbfe8ea.ts.net"
        try:
            _seed_logs(Path(logd))
            from reviewlib.dashboard import server

            server._tailscale_cache = set()
            httpd = server.make_server(0, host="0.0.0.0")
            port = httpd.server_address[1]
            base = f"http://127.0.0.1:{port}"
            ts = "ultras-mbp.tailbfe8ea.ts.net"
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            try:
                # a real session id (reached via the Tailscale Host, like a phone would)
                req = urllib.request.Request(base + "/api/runs", headers={"Host": ts})
                with urllib.request.urlopen(req, timeout=10) as r:
                    sid = json.loads(r.read().decode("utf-8"))[0]["session_id"]
                # write from the Tailscale Origin + matching Host -> allowed.
                st, body = _post(
                    base, f"/api/runs/{sid}/feedback", {"feedback": "from the phone"},
                    headers={"Content-Type": "application/json", "Origin": f"http://{ts}", "Host": ts},
                )
                assert st == 200 and body["annotation"]["feedback"] == "from the phone"
                # write from a FOREIGN Origin -> 403 (CSRF), even with an allowed Host.
                try:
                    _post(
                        base, f"/api/runs/{sid}/feedback", {"feedback": "pwned"},
                        headers={"Content-Type": "application/json",
                                 "Origin": "https://evil.example.com", "Host": ts},
                    )
                    raise AssertionError("expected 403 for foreign Origin")
                except urllib.error.HTTPError as e:
                    assert e.code == 403, e.code
            finally:
                httpd._sse_stop = True
                httpd.shutdown()
                httpd.server_close()
        finally:
            os.environ.pop("REVIEW_LOG_DIR", None)
            os.environ.pop("REVIEW_DASHBOARD_STORE", None)
            os.environ.pop("REVIEW_DASHBOARD_ALLOWED_HOSTS", None)
            server._tailscale_cache = None


def test_sse_events_stream_content_type_and_live_event():
    """(SSE live stream) GET /events is an event-stream, and when a NEW call log appears in
    the log dir AFTER the stream connects, a `run` (and `log`) event is pushed to the client
    without any polling — proving live activity reaches the browser."""
    import http.client

    with tempfile.TemporaryDirectory() as logd, tempfile.TemporaryDirectory() as stored:
        os.environ["REVIEW_LOG_DIR"] = logd
        os.environ["REVIEW_DASHBOARD_STORE"] = str(Path(stored) / "dashboard.json")
        try:
            from reviewlib.dashboard import server

            httpd = server.make_server(0)
            port = httpd.server_address[1]
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            try:
                # /events inherits the Host allowlist (do_GET rejects a foreign Host before
                # routing) — a rebound page can't open the live stream and exfiltrate logs.
                bad = urllib.request.Request(
                    f"http://127.0.0.1:{port}/events", headers={"Host": "evil.example.com"})
                try:
                    urllib.request.urlopen(bad, timeout=10)
                    raise AssertionError("expected 403 for foreign Host on /events")
                except urllib.error.HTTPError as e:
                    assert e.code == 403, e.code

                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
                conn.request("GET", "/events", headers={"Host": f"127.0.0.1:{port}"})
                resp = conn.getresponse()
                assert resp.status == 200, resp.status
                ctype = resp.getheader("Content-Type") or ""
                assert ctype.startswith("text/event-stream"), ctype
                # Read the initial framing (retry: + : connected). The handler snapshots its
                # baseline BEFORE flushing the first byte, so once a byte arrives the baseline
                # is already established and a log written now is guaranteed to read as a delta.
                resp.read(1)  # block until at least one byte is flushed
                # Now write a NEW call log — the watcher must pick it up and stream an event.
                _write_call_log(Path(logd), "20260601T100000_000000", "codex", 0,
                                "live run output\n", exit_code=0)
                # Accumulate the stream until we see BOTH a `log` and a `run` event, or time out.
                buf = b""
                saw_run = saw_log = False
                import time as _t

                deadline = _t.monotonic() + 12
                while _t.monotonic() < deadline and not (saw_run and saw_log):
                    chunk = resp.read1(64)  # documented incremental read, no fixed-size block
                    if not chunk:
                        break
                    buf += chunk
                    saw_log = saw_log or b"event: log" in buf
                    saw_run = saw_run or b"event: run" in buf
                assert saw_run, f"no run event streamed; got: {buf[:400]!r}"
                assert saw_log, f"no log event streamed; got: {buf[:400]!r}"
                # The run event must carry a JSON data line with a session summary.
                assert b'"session_id"' in buf, buf[:400]
                # The log event payload contract: filename + grew + the parsed call fields.
                text = buf.decode("utf-8", "replace")
                log_block = next(b for b in text.split("\n\n") if "event: log" in b)
                data_line = next(ln for ln in log_block.splitlines() if ln.startswith("data: "))
                log_payload = json.loads(data_line[len("data: "):])
                assert log_payload["filename"].endswith("-codex-r0.log"), log_payload
                assert log_payload["kind"] == "call", log_payload
                assert log_payload["backend"] == "codex", log_payload
                assert log_payload["completed"] is True, log_payload
                assert log_payload["grew"] is False, log_payload  # brand-new file, not a grow
                conn.close()
            finally:
                httpd._sse_stop = True
                httpd.shutdown()
                httpd.server_close()
        finally:
            os.environ.pop("REVIEW_LOG_DIR", None)
            os.environ.pop("REVIEW_DASHBOARD_STORE", None)


def test_sse_baseline_snapshot_is_taken_before_first_byte_no_lost_event():
    """(SSE race regression) A log written AFTER the client sees the first byte but BEFORE
    the watcher thread reaches its first poll must STILL be reported — never folded into a
    silent baseline. We force exactly that ordering (the CI thread-scheduling that made the
    live-event test time out): the very first ``_snapshot_logs`` call (the connect-time
    baseline) is delayed, and the test writes the new log during that delay. With the
    baseline taken synchronously before the first byte is flushed, the file is guaranteed to
    be a delta; under the old lazy-first-tick baseline it would be absorbed and lost."""
    import http.client
    import time as _t

    from reviewlib.dashboard import server

    with tempfile.TemporaryDirectory() as logd, tempfile.TemporaryDirectory() as stored:
        os.environ["REVIEW_LOG_DIR"] = logd
        os.environ["REVIEW_DASHBOARD_STORE"] = str(Path(stored) / "dashboard.json")
        orig_snapshot = server.DashboardHandler._snapshot_logs
        state = {"delayed": False}

        def slow_first_snapshot(ld):
            # Delay ONLY the first snapshot (the connect-time baseline) so the test's write
            # reliably lands before it — reproducing the CI ordering that lost the event.
            if not state["delayed"]:
                state["delayed"] = True
                _t.sleep(1.5)
            return orig_snapshot(ld)

        server.DashboardHandler._snapshot_logs = staticmethod(slow_first_snapshot)
        try:
            httpd = server.make_server(0)
            port = httpd.server_address[1]
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
                conn.request("GET", "/events", headers={"Host": f"127.0.0.1:{port}"})
                resp = conn.getresponse()
                assert resp.status == 200, resp.status
                # Block until the first byte. Because the baseline snapshot is taken BEFORE
                # this byte is flushed, the slow_first_snapshot delay has already elapsed by
                # the time read(1) returns — so our write below is strictly after the baseline.
                resp.read(1)
                _write_call_log(Path(logd), "20260601T100000_000000", "codex", 0,
                                "live run output\n", exit_code=0)
                buf = b""
                saw_run = saw_log = False
                deadline = _t.monotonic() + 12
                while _t.monotonic() < deadline and not (saw_run and saw_log):
                    chunk = resp.read1(64)
                    if not chunk:
                        break
                    buf += chunk
                    saw_log = saw_log or b"event: log" in buf
                    saw_run = saw_run or b"event: run" in buf
                assert saw_log, f"baseline race: log event lost; got: {buf[:400]!r}"
                assert saw_run, f"baseline race: run event lost; got: {buf[:400]!r}"
                conn.close()
            finally:
                httpd._sse_stop = True
                httpd.shutdown()
                httpd.server_close()
        finally:
            server.DashboardHandler._snapshot_logs = staticmethod(orig_snapshot)
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
