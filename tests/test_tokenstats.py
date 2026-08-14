#!/usr/bin/env python3
"""`reviewlib.dashboard.tokenstats` — the per-harness/per-model usage + health
aggregation behind `review stat`.

Covers the concrete findings from the 2026-08 token-burn investigation this module
answers: real token extraction is scoped strictly to the REST backends that emit it
(never scraped from an agentic CLI body, which the investigation found can carry a
DIFFERENT seat's quoted usage line — a real cross-contamination case), the SKILL.md/
MEMORY.md context-pollution signal, the Fable dispatch/failure pattern (both from call
logs and from the retry-event sidecars), and the largest-call outlier ranking.
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib.dashboard import tokenstats as ts  # noqa: E402


def _write(d: Path, name: str, content: str) -> Path:
    p = d / name
    p.write_text(content, encoding="utf-8")
    return p


def _zai_log(
    d: Path, stamp="20260813T100000_000000Z", prompt=120, output=45, task="T1"
) -> Path:
    return _write(
        d,
        f"{stamp}-z.ai-r0.log",
        f"[review-cli] z.ai: z.ai API glm-5.2 (args redacted) task={task}\n"
        f"Looks fine.\n\nprompt_tokens={prompt} output_tokens={output}\n"
        "[review-cli] EXIT 0\n",
    )


def _codex_log(
    d: Path, stamp="20260813T100100_000000Z", extra_body="", task="T1"
) -> Path:
    return _write(
        d,
        f"{stamp}-codex-r0.log",
        f"[review-cli] codex: codex (args redacted) task={task}\n"
        + extra_body
        + "review text\n"
        "[review-cli] EXIT 0\n",
    )


def _claude_paywall_log(d: Path, stamp="20260813T100200_000000Z", task="T1") -> Path:
    return _write(
        d,
        f"{stamp}-claude-r0.log",
        f"[review-cli] claude: claude-p (args redacted) task={task}\n"
        "Claude Fable 5 is currently unavailable. Learn more: https://example.com\n"
        "[review-cli] EXIT 0\n",
    )


def _retry_log(
    d: Path,
    stamp="20260813T100300_000000Z",
    detail="You've hit your session limit",
    exit_code="1",
) -> Path:
    return _write(
        d,
        f"{stamp}-Fable_promote-retry-0001.log",
        "[review-cli] RETRY-EVENT kind=promote model=Fable [architect]->commandcode:zai-org/GLM-5.2 "
        f"delay=0.00s exit={exit_code}\n"
        f"[detail] {detail}\n",
    )


def _cached_skip_log(d: Path, stamp="20260813T100400_000000Z", task="T1") -> Path:
    """Reproduces the EXACT shape `backends._cooldown_skip_result` writes via
    `_emit_rest_log("claude", "seat-cooldown skip (claude)", ...)` — used to pin
    compute_fable_report's cached-skip exclusion (three independent review passes
    converged on this bug: a cached skip was double-counted as a real dispatch AND a
    paywall failure)."""
    return _write(
        d,
        f"{stamp}-claude-r0.log",
        f"[review-cli] claude: seat-cooldown skip (claude) (args redacted) task={task}\n"
        "claude:claude-fable-5 is currently unavailable (cached: session limit; skip "
        "expires in 300s — reviewlib.seat_cooldown).\n"
        "[review-cli] EXIT 0\n",
    )


# ---- filename-timestamp parsing / listing --------------------------------------------
def test_list_call_logs_ignores_non_matching_files():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _zai_log(d)
        _write(d, "not-a-call-log.txt", "junk")
        _write(d, "20260813T110000_000000Z-brainstorm.md", "# not a call log either\n")
        calls = ts.list_call_logs(d)
        assert len(calls) == 1
        assert calls[0].backend == "z.ai"


def test_list_call_logs_since_filters_by_filename_timestamp():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _zai_log(d, stamp="20260101T000000_000000Z")
        _codex_log(d, stamp="20260813T100100_000000Z")
        since = datetime(2026, 6, 1, tzinfo=timezone.utc)
        calls = ts.list_call_logs(d, since=since)
        assert len(calls) == 1
        assert calls[0].backend == "codex"


def test_list_retry_events_ignores_call_logs():
    """A call-log filename (`-r0.log`) must never be mistaken for a retry sidecar
    (`-retry-NNNN.log`) — the two filename shapes must not collide."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _zai_log(d)
        _retry_log(d)
        events = ts.list_retry_events(d)
        assert len(events) == 1
        assert events[0].kind == "promote"


def test_malformed_filename_date_is_skipped_not_a_crash():
    """kimi review finding: a filename can match the REGEX shape (8 digits + T + 6
    digits) while embedding an impossible calendar date (month 99) — `strptime` raises
    ValueError in that case. Every function here promises "never raises" (report-only
    tooling must not crash `review stat` on a malformed/foreign file); pins that a
    `--since` window silently skips such a file instead of propagating the crash."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _zai_log(d)  # one real, valid call
        _write(d, "20269999T999999_000000Z-codex-r0.log", "junk\n")  # impossible date
        _write(
            d,
            "20269999T999999_000000Z-Fable_promote-retry-0001.log",
            "[review-cli] RETRY-EVENT kind=promote model=Fable delay=0.00s exit=1\n",
        )
        since = datetime(2020, 1, 1, tzinfo=timezone.utc)
        calls = ts.list_call_logs(d, since=since)  # must not raise
        events = ts.list_retry_events(d, since=since)  # must not raise
        assert len(calls) == 1
        assert calls[0].backend == "z.ai"
        assert events == []
        # Also exercised with no `since` (the code path that calls parse_retry_log
        # directly, which internally guards its own _parse_stamp call).
        assert ts.list_retry_events(d) == []


def test_malformed_filename_date_skipped_with_no_since_filter_too():
    """codex/kimi review finding (round 5): the fix above only covered the `since`-set
    path. `list_call_logs` with `since=None` (`review stat --days 0`, the documented
    "all recorded history" mode) used to skip the stamp check entirely and call the
    EXTERNAL `parser.parse_call_log` unguarded — which raises the exact same ValueError.
    Pins that the no-`since` path is equally safe."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _zai_log(d)
        _write(d, "20269999T999999_000000Z-codex-r0.log", "junk\n")
        calls = ts.list_call_logs(d)  # since=None — must not raise
        assert len(calls) == 1
        assert calls[0].backend == "z.ai"


# ---- real token extraction: scoped to REST backends only -----------------------------
def test_extract_usage_tokens_from_zai_call():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        path = _zai_log(d, prompt=200, output=80)
        from reviewlib.dashboard.parser import parse_call_log

        call = parse_call_log(path)
        assert ts.extract_usage_tokens(call) == (200, 80)


def test_extract_usage_tokens_none_for_codex_even_with_a_usage_shaped_line():
    """The investigation's real cross-contamination case: a DIFFERENT seat's quoted
    usage line appearing inside a codex call's own body must NOT be attributed to
    codex — extract_usage_tokens must return None for any non-REST backend, full stop,
    even when the body's last line happens to look exactly like a usage line."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        contaminated = (
            "some agent reasoning here\n"
            "quoting another seat's output:\nprompt_tokens=999 output_tokens=999\n"
        )
        path = _codex_log(d, extra_body=contaminated)
        from reviewlib.dashboard.parser import parse_call_log

        call = parse_call_log(path)
        assert ts.extract_usage_tokens(call) is None


def test_extract_usage_tokens_claude_api_mode_eligible():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        path = _write(
            d,
            "20260813T100000_000000Z-claude-r0.log",
            "[review-cli] claude: Anthropic API claude-opus-4-8 (args redacted) task=T1\n"
            "A real review.\n\ninput_tokens=50 output_tokens=20\n"
            "[review-cli] EXIT 0\n",
        )
        from reviewlib.dashboard.parser import parse_call_log

        call = parse_call_log(path)
        assert ts.extract_usage_tokens(call) == (50, 20)


def test_extract_usage_tokens_claude_cli_mode_not_eligible():
    """claude in CLI mode (argv0 == 'claude-p', not 'Anthropic API ...') must never be
    treated as a REST call even if its body happens to contain a usage-shaped line."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        path = _write(
            d,
            "20260813T100000_000000Z-claude-r0.log",
            "[review-cli] claude: claude-p (args redacted) task=T1\n"
            "input_tokens=50 output_tokens=20\n"
            "[review-cli] EXIT 0\n",
        )
        from reviewlib.dashboard.parser import parse_call_log

        call = parse_call_log(path)
        assert ts.extract_usage_tokens(call) is None


def test_extract_usage_tokens_from_openrouter_call():
    """codex review finding: `openrouter` was missing from `_REST_USAGE_BACKENDS` even
    though `review_openrouter` dispatches through the exact same `_openai_compatible_
    request` helper z.ai/commandcode use — the one that appends the `prompt_tokens=N
    output_tokens=N` line this function parses. Pins that a real OpenRouter call's
    token count is now extracted, same as its z.ai/commandcode siblings."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        path = _write(
            d,
            "20260813T100000_000000Z-openrouter-r0.log",
            "[review-cli] openrouter: openrouter API anthropic/claude-3.5-sonnet "
            "(args redacted) task=T1\n"
            "A real review.\n\nprompt_tokens=300 output_tokens=90\n"
            "[review-cli] EXIT 0\n",
        )
        from reviewlib.dashboard.parser import parse_call_log

        call = parse_call_log(path)
        assert ts.extract_usage_tokens(call) == (300, 90)
        assert "openrouter" in ts._REST_USAGE_BACKENDS


def test_extract_usage_tokens_fabricated_zero_zero_reads_as_absent():
    """codex/kimi review finding: the REST emitters always append a `prompt_tokens=N
    output_tokens=N` line, even when the provider's response had no usable `usage`
    object — in that case they synthesize `0 0` (`_parse_openai_usage`'s own docstring:
    '0/0 on any wrong shape'). Before this fix a `(0, 0)` match was reported as a REAL
    token count, flipping `tokens_real: true` on a call whose usage was never actually
    known — the exact honesty conflation the module promises to avoid. A genuinely
    completed call never legitimately has zero prompt tokens."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        path = _zai_log(d, prompt=0, output=0)
        from reviewlib.dashboard.parser import parse_call_log

        call = parse_call_log(path)
        assert ts.extract_usage_tokens(call) is None


def test_extract_usage_tokens_partial_zero_also_reads_as_absent():
    """codex review finding, round 2: `_parse_openai_usage` defaults EACH field to 0
    INDEPENDENTLY on a partially-malformed `usage` object (e.g. `prompt_tokens` present
    but `completion_tokens` missing) — the ORIGINAL `prompt == 0 and output == 0` guard
    only caught the case where BOTH were fabricated, so a real `prompt_tokens=120` next
    to a fabricated `output_tokens=0` was reported as an EXACT zero-output measurement.
    The caller (`_openai_compatible_request`) already fails closed (rc=1, no usage line
    at all) whenever the response text is empty, so a real call reaching this line
    always produced non-empty output — a genuine zero output-token count is impossible
    here, meaning a `0` on either side is always a defaulted field, never a real
    measurement. Pins BOTH partial-zero shapes (prompt-only-zero, output-only-zero) as
    absent, not just the all-zero case above."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        from reviewlib.dashboard.parser import parse_call_log

        path_a = _zai_log(d, stamp="20260813T100000_000000Z", prompt=120, output=0)
        path_b = _zai_log(d, stamp="20260813T100001_000000Z", prompt=0, output=45)
        assert ts.extract_usage_tokens(parse_call_log(path_a)) is None
        assert ts.extract_usage_tokens(parse_call_log(path_b)) is None


# ---- per-harness aggregation -----------------------------------------------------------
def test_compute_harness_stats_groups_by_backend_and_tracks_bytes_and_tokens():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _zai_log(d)
        _codex_log(d, extra_body="reading SKILL.md and MEMORY.md\n")
        calls = ts.list_call_logs(d)
        stats = ts.compute_harness_stats(calls)
        assert set(stats) == {"z.ai", "codex"}
        assert stats["z.ai"].calls == 1
        assert stats["z.ai"].calls_with_real_tokens == 1
        assert stats["z.ai"].tokens_prompt == 120
        assert stats["codex"].calls_with_real_tokens == 0  # no real tokens for codex
        assert stats["codex"].skill_md_calls == 1
        assert stats["codex"].memory_md_calls == 1


def test_compute_harness_stats_counts_a_paywall_sentinel_as_fail_not_ok():
    """codex review finding: the ok/fail split used to key on `call.has_error`, which is
    authoritative on the raw EXIT CODE only — a Fable "is currently unavailable"
    sentinel is EXIT 0, so it counted as `ok` in the per-harness table while the SAME
    call's `classify_call` (used by the Fable section right below it in the report)
    buckets it as `HEALTH_PAYWALL`. A report showing "claude: ok=1 fail=0" next to a
    Fable section reporting that exact call as a paywall failure was genuinely
    confusing. Pins that the harness table's `ok`/`fail` now agrees with `classify_call`
    (and therefore with the Fable section) for the identical call."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _claude_paywall_log(d)
        calls = ts.list_call_logs(d)
        stats = ts.compute_harness_stats(calls)
        assert stats["claude"].ok == 0
        assert stats["claude"].fail == 1


def test_harness_stats_to_dict_bytes_percentiles():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        for i in range(5):
            _zai_log(d, stamp=f"20260813T10{i:02d}00_000000Z", prompt=1, output=1)
        calls = ts.list_call_logs(d)
        stats = ts.compute_harness_stats(calls)
        row = stats["z.ai"].to_dict()
        assert row["calls"] == 5
        assert row["bytes_max"] >= row["bytes_p90"] >= row["bytes_p50"] >= 0


# ---- Fable-specific report --------------------------------------------------------------
def test_fable_report_counts_dispatch_and_classifies_reason():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _claude_paywall_log(d)
        _retry_log(d, detail="You've hit your session limit · resets 7:30pm")
        calls = ts.list_call_logs(d)
        events = ts.list_retry_events(d)
        report = ts.compute_fable_report(calls, events)
        assert report["dispatch_attempts"] == 1
        assert report["paywall_sentinel_calls"] == 1
        assert report["failure_rate"] == 1.0
        assert report["retry_events"] == 1
        assert report["retry_event_reasons"]["session_limit"] == 1


def test_fable_report_auth_reason_not_fooled_by_author_or_authoritative():
    """kimi review finding: `"auth" in low` mis-bucketed a retry detail into "auth"
    whenever it happened to contain "author"/"authoritative" — plausible prose in a
    quoted model response, not an auth failure. Pins that a detail containing only
    those words lands in "other", while a genuine auth failure (the investigation's own
    real observed shapes: "Not logged in", "authentication failed") still lands in
    "auth"."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _retry_log(
            d,
            stamp="20260813T100300_000000Z",
            detail="The author's authoritative claim is unverified.",
        )
        _retry_log(
            d, stamp="20260813T100301_000000Z", detail="Error: authentication failed"
        )
        _retry_log(
            d,
            stamp="20260813T100302_000000Z",
            detail="Not logged in — run `claude login`",
        )
        events = ts.list_retry_events(d)
        report = ts.compute_fable_report([], events)
        assert report["retry_event_reasons"]["auth"] == 2
        assert report["retry_event_reasons"]["other"] == 1


def test_fable_report_auth_reason_not_fooled_by_401_in_free_text():
    """glm review finding, round 2: a bare `\\b401\\b` regex over `event.detail` (free
    text — quoted provider prose or stderr) matched a literal "401" ANYWHERE, the same
    false-positive class the word-marker fix above already closed for "author"/
    "authoritative". A detail that merely MENTIONS 401 (e.g. quoting a line number, or a
    reviewed diff's own error-handling code) must NOT be bucketed as `auth` — only the
    STRUCTURED `exit_code` field (populated from the real HTTP status a REST backend's
    `returncode` carries) is authoritative. Pins both directions: free-text "401" with a
    non-401 real exit code reads as `other`; a genuine `exit=401` reads as `auth` even
    with no auth-shaped wording in the detail at all."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _retry_log(
            d,
            stamp="20260813T100310_000000Z",
            detail="...failing at line 401 of server.py, unrelated to auth",
            exit_code="1",
        )
        _retry_log(
            d,
            stamp="20260813T100311_000000Z",
            detail="request rejected",
            exit_code="401",
        )
        events = ts.list_retry_events(d)
        report = ts.compute_fable_report([], events)
        assert report["retry_event_reasons"]["auth"] == 1
        assert report["retry_event_reasons"]["other"] == 1


def test_fable_report_excludes_cached_skip_originated_retry_events():
    """Fable/kimi review finding (round 2): a cached-cooldown-skip result mimics the
    live paywall sentinel shape ON PURPOSE, so `panel.result_is_usable` treats it as a
    real failure and it DOES reach the failover loop — logged TWICE per skip
    (`retry.classify_failure` writes a `seat-fatal` event, then the reserve backfill
    writes a separate `promote` event), both carrying the skip's own cached-reason text
    in `detail`. An earlier version of this report's own docstring incorrectly claimed
    this never happens. Without the exclusion, every review during an active cooldown
    window would count as a FRESH session-limit occurrence. Pins that both event kinds,
    reproducing the exact detail text `backends._cooldown_skip_result` writes, are
    excluded from `retry_events`/`retry_event_reasons` and surfaced instead via
    `cached_skip_retry_events_excluded` — alongside one genuine (non-cached) session
    limit event, which must still count normally."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        cached_detail = (
            "claude:claude-fable-5 is currently unavailable (cached: session limit / "
            "usage credits; skip expires in 300s — reviewlib.seat_cooldown)."
        )
        _write(
            d,
            "20260813T100300_000000Z-Fable_seat-fatal-retry-0001.log",
            "[review-cli] RETRY-EVENT kind=seat-fatal model=Fable [architect] delay=0.00s exit=0\n"
            f"[detail] {cached_detail}\n",
        )
        _write(
            d,
            "20260813T100301_000000Z-Fable_promote-retry-0002.log",
            "[review-cli] RETRY-EVENT kind=promote model=Fable [architect]->commandcode:zai-org/GLM-5.2 "
            "delay=0.00s exit=0\n"
            f"[detail] {cached_detail}\n",
        )
        # One genuine (non-cached) session-limit event must still count normally.
        _retry_log(
            d,
            stamp="20260813T100302_000000Z",
            detail="You've hit your session limit · resets 7:30pm",
        )
        events = ts.list_retry_events(d)
        assert len(events) == 3  # all three parse — the exclusion is report-level
        report = ts.compute_fable_report([], events)
        assert report["retry_events"] == 1
        assert report["retry_event_reasons"]["session_limit"] == 1
        assert report["cached_skip_retry_events_excluded"] == 2


def test_fable_report_zero_when_no_fable_activity():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _zai_log(d)
        calls = ts.list_call_logs(d)
        events = ts.list_retry_events(d)
        report = ts.compute_fable_report(calls, events)
        assert report["dispatch_attempts"] == 0
        assert report["failure_rate"] is None


def test_fable_report_excludes_cached_skips_from_dispatch_and_failures():
    """Three independent review passes (Opus, codex, kimi-code/k3) converged on this
    exact bug: a cached-cooldown skip (backends._cooldown_skip_result) was counted as
    BOTH a real dispatch attempt AND a paywall failure — so after the cooldown fix
    ships and starts AVOIDING real dispatches, the report kept reporting the SAME high
    failure rate the fix exists to reduce. Pins the fix: a cached skip counts ONLY
    toward `cached_skips`, never `dispatch_attempts`/`paywall_sentinel_calls`."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _cached_skip_log(d)
        calls = ts.list_call_logs(d)
        report = ts.compute_fable_report(calls, [])
        assert report["cached_skips"] == 1
        assert report["dispatch_attempts"] == 0
        assert report["paywall_sentinel_calls"] == 0
        assert report["failure_rate"] is None  # no REAL dispatch -> no rate to report


def test_fable_report_mixes_a_real_failure_with_a_cached_skip():
    """A cached skip alongside a genuine failed dispatch: the skip must not inflate
    dispatch_attempts, and the real failure alone determines the failure rate."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _claude_paywall_log(d)
        _cached_skip_log(d)
        calls = ts.list_call_logs(d)
        report = ts.compute_fable_report(calls, [])
        assert report["cached_skips"] == 1
        assert report["dispatch_attempts"] == 1  # only the real paywall call
        assert report["failure_rate"] == 1.0  # the one real dispatch failed


def test_fable_report_failure_rate_never_exceeds_one():
    """codex/kimi review finding: classify_call checks the paywall sentinel BEFORE the
    has_error branches, so a call CAN be both HEALTH_PAYWALL and has_error==True (a
    non-zero-exit paywall body). Summing both predicates would push failure_rate above
    1.0; the union must cap it at exactly 1.0 for an all-failing set."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        # A paywall body with a NON-ZERO exit code: has_error AND HEALTH_PAYWALL both True.
        _write(
            d,
            "20260813T100500_000000Z-claude-r0.log",
            "[review-cli] claude: claude-p (args redacted) task=T1\n"
            "Claude Fable 5 is currently unavailable. Learn more: https://example.com\n"
            "[review-cli] EXIT 1\n",
        )
        calls = ts.list_call_logs(d)
        report = ts.compute_fable_report(calls, [])
        assert report["dispatch_attempts"] == 1
        assert report["failure_rate"] == 1.0  # not 2.0


def test_is_cached_skip_not_fooled_by_quoted_prose_in_a_real_review():
    """kimi review finding — a genuine self-review trap: `_is_cached_skip` used to
    substring-match the BODY for "cached:"/"seat_cooldown", but those are ordinary
    English words that appear verbatim in THIS diff's own source/docs. A real Claude
    review of this feature that quotes `_cooldown_skip_result`'s code (exactly what
    happened during this PR's own review) would contain both substrings in its body —
    while its argv0 is a REAL dispatch command, not the skip path's. Pins the fix:
    classification is anchored on argv0, so quoted prose can never trigger it."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _write(
            d,
            "20260813T100600_000000Z-claude-r0.log",
            "[review-cli] claude: claude-p (args redacted) task=T1\n"
            "Reviewing this diff: the cooldown skip writes a stdout like "
            "'cached: session limit; ... reviewlib.seat_cooldown' via _emit_rest_log.\n"
            "This looks correct.\n"
            "[review-cli] EXIT 0\n",
        )
        calls = ts.list_call_logs(d)
        assert len(calls) == 1
        assert not ts._is_cached_skip(calls[0])


# ---- oversized-call ranking --------------------------------------------------------------
def test_top_oversized_calls_ranks_by_size_and_flags_diff_signal():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _zai_log(d, stamp="20260813T100000_000000Z")
        big_diff = "\n".join(["diff --git a/x b/x"] * 5)
        _codex_log(d, stamp="20260813T100100_000000Z", extra_body=big_diff + "\n")
        calls = ts.list_call_logs(d)
        top = ts.top_oversized_calls(calls, limit=5)
        assert top[0]["backend"] == "codex"  # the bigger log ranks first
        assert top[0]["diff_git_files"] == 5


# ---- top-level report assembly (compute_stat_report) -------------------------------------
def test_compute_stat_report_end_to_end():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _zai_log(d)
        _codex_log(d, extra_body="reading SKILL.md\n" + "diff --git a/x b/x\n" * 3)
        _claude_paywall_log(d)
        _retry_log(d)
        report = ts.compute_stat_report(d)
        assert report["call_count"] == 3
        assert report["retry_event_count"] == 1
        assert set(report["harnesses"]) == {"z.ai", "codex", "claude"}
        assert report["fable"]["dispatch_attempts"] == 1
        assert report["retry_events_by_kind"] == {"promote": 1}
        assert len(report["top_oversized_calls"]) == 3


def test_compute_stat_report_since_filters_out_old_calls():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _zai_log(d, stamp="20260101T000000_000000Z")
        _codex_log(d, stamp="20260813T100100_000000Z")
        # Both fixture stamps are fixed dates; assert against an explicit cutoff between them.
        cutoff = datetime(2026, 6, 1, tzinfo=timezone.utc)
        report = ts.compute_stat_report(d, since=cutoff)
        assert report["call_count"] == 1
        assert "codex" in report["harnesses"]
        assert "z.ai" not in report["harnesses"]


def test_format_bytes():
    assert ts.format_bytes(0) == "0B"
    assert ts.format_bytes(999) == "999B"
    assert ts.format_bytes(1024) == "1.0KB"
    assert ts.format_bytes(1024 * 1024) == "1.0MB"
    assert ts.format_bytes(int(6.5 * 1024 * 1024)) == "6.5MB"
    assert ts.format_bytes(1024**4) == "1.0TB"


def test_format_bytes_pb_scale_is_not_mislabeled_tb():
    """Opus review finding: the KB/MB/GB/TB loop only divides 4 times, so falling
    through to the PB return line without one more division printed the TB-scale
    value with a "PB" label (e.g. exactly 1 PB read as "1024.0PB"). Effectively
    unreachable for a real per-call log, but a real off-by-one in the unit math."""
    assert ts.format_bytes(1024**5) == "1.0PB"


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
