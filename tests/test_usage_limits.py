#!/usr/bin/env python3
"""Unit tests for reviewlib.usage_limits — the tg-ctl usage-percent bridge used
by board/panel reuse-and-exclusion (see test_pool_reuse.py for the consumer).

All tests drive `usage_percent_for_model` via its `samples=` parameter (a plain
list of dicts, matching tg-ctl's `usage-latest.json` shape) — no real
filesystem/env dependency, so these never touch a live ~/.config/tg-cli file.
`test_reads_real_usage_file`/`test_merges_samples_across_multiple_chat_files`
are the exceptions: they prove the actual file-reading path against throwaway
files via $REVIEW_USAGE_LIMITS_FILE (single-file case) and a real
~/.config/tg-cli-shaped glob (multi-file merge case).

Plain-script harness (mirrors tests/test_moderator.py): each test_* is run by
__main__, and also pytest-discoverable.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import os  # noqa: E402

from reviewlib.usage_limits import (  # noqa: E402
    load_snapshot,
    usage_percent_for_model,
)

# A fixed epoch anchored to real wall-clock time at import time (NOT a hardcoded
# constant) — every test that needs a "sample taken right now" derives it from
# this, so none of them silently go stale as real time passes (codex/Fable
# review finding: a hardcoded NOW + no now= at the call site is a time bomb).
import time as _time  # noqa: E402

NOW = _time.time()


def _sample(
    agent: str, limit_name: str, percent: float, age_seconds: float = 0.0
) -> dict:
    return {
        "agent": agent,
        "limitName": limit_name,
        "percent": percent,
        "resetAt": None,
        "sampledAt": (NOW - age_seconds) * 1000,
    }


def test_unmapped_provider_is_unknown():
    samples = [_sample("claude", "weekly", 99)]
    assert usage_percent_for_model("zai:glm-5.2", samples=samples, now=NOW) is None
    assert usage_percent_for_model("gemini", samples=samples, now=NOW) is None


def test_claude_prefix_maps_to_claude_family():
    samples = [_sample("claude", "weekly", 82)]
    assert (
        usage_percent_for_model("claude:claude-opus-4-8", samples=samples, now=NOW)
        == 82
    )
    assert (
        usage_percent_for_model("claude:claude-fable-5", samples=samples, now=NOW) == 82
    )


def test_bare_codex_maps_to_codex_family():
    samples = [_sample("codex", "5-hour", 55)]
    assert usage_percent_for_model("codex", samples=samples, now=NOW) == 55


def test_context_window_limit_is_ignored():
    # "context window" is a per-call size limit, not a rate/usage budget — it must
    # never feed the near-limit check even if it happens to read high.
    samples = [_sample("claude", "context window", 95), _sample("claude", "weekly", 10)]
    assert (
        usage_percent_for_model("claude:claude-opus-4-8", samples=samples, now=NOW)
        == 10
    )


def test_max_of_5hour_and_weekly_wins():
    samples = [_sample("claude", "5-hour", 20), _sample("claude", "weekly", 88)]
    assert (
        usage_percent_for_model("claude:claude-opus-4-8", samples=samples, now=NOW)
        == 88
    )


def test_stale_sample_is_ignored():
    stale = _sample("claude", "weekly", 99, age_seconds=48 * 3600)
    assert (
        usage_percent_for_model("claude:claude-opus-4-8", samples=[stale], now=NOW)
        is None
    )


def test_freshest_sample_wins_over_higher_but_stale_duplicate():
    # load_snapshot() merges multiple tg-ctl chat files -- two samples for
    # the SAME (agent, limitName) key can both survive the 24h trust window.
    # The FRESHER one must win, even though the STALER one reads higher.
    stale_high = _sample("claude", "weekly", 90, age_seconds=3600)
    fresh_low = _sample("claude", "weekly", 15, age_seconds=60)
    samples = [stale_high, fresh_low]
    assert (
        usage_percent_for_model("claude:claude-opus-4-8", samples=samples, now=NOW)
        == 15
    )
    # order-independent
    assert (
        usage_percent_for_model(
            "claude:claude-opus-4-8", samples=list(reversed(samples)), now=NOW
        )
        == 15
    )


def test_reset_at_in_the_past_is_ignored_even_within_trust_window():
    # A "5-hour" sample taken 6 hours ago whose OWN resetAt already passed is
    # describing a window that no longer applies, regardless of the 24h
    # sampledAt trust window.
    expired = _sample("claude", "5-hour", 95, age_seconds=6 * 3600)
    expired["resetAt"] = (NOW - 3600) * 1000  # reset already happened
    assert (
        usage_percent_for_model("claude:claude-opus-4-8", samples=[expired], now=NOW)
        is None
    )


def test_reset_at_in_the_future_is_honored():
    still_active = _sample("claude", "5-hour", 95)
    still_active["resetAt"] = (NOW + 3600) * 1000  # resets an hour from now
    assert (
        usage_percent_for_model(
            "claude:claude-opus-4-8", samples=[still_active], now=NOW
        )
        == 95
    )


def test_codex_prefixed_seat_maps_to_codex_family():
    # e.g. config.SOL_SEAT = "codex:gpt-5.6-sol" -- shares the codex account
    # quota with the bare "codex" seat.
    samples = [_sample("codex", "weekly", 77)]
    assert usage_percent_for_model("codex:gpt-5.6-sol", samples=samples, now=NOW) == 77


def test_malformed_samples_never_raise():
    junk = [
        "not a dict",
        {"agent": "claude"},  # missing limitName/percent/sampledAt
        {
            "agent": "claude",
            "limitName": "weekly",
            "percent": "not-a-number",
            "sampledAt": NOW * 1000,
        },
        None,
    ]
    assert (
        usage_percent_for_model("claude:claude-opus-4-8", samples=junk, now=NOW) is None
    )


def test_reads_real_usage_file(tmp_path=None):
    tmp_dir = Path(tempfile.mkdtemp())
    usage_file = tmp_dir / "usage-latest.json"
    usage_file.write_text(
        json.dumps(
            {
                "version": 1,
                "samples": [_sample("codex", "weekly", 91)],
            }
        ),
        encoding="utf-8",
    )
    old_override = os.environ.get("REVIEW_USAGE_LIMITS_FILE")
    os.environ["REVIEW_USAGE_LIMITS_FILE"] = str(usage_file)
    try:
        assert usage_percent_for_model("codex") == 91
    finally:
        if old_override is None:
            os.environ.pop("REVIEW_USAGE_LIMITS_FILE", None)
        else:
            os.environ["REVIEW_USAGE_LIMITS_FILE"] = old_override


def test_missing_file_is_unknown():
    old_override = os.environ.get("REVIEW_USAGE_LIMITS_FILE")
    os.environ["REVIEW_USAGE_LIMITS_FILE"] = "/nonexistent/path/usage-latest.json"
    try:
        assert usage_percent_for_model("codex") is None
    finally:
        if old_override is None:
            os.environ.pop("REVIEW_USAGE_LIMITS_FILE", None)
        else:
            os.environ["REVIEW_USAGE_LIMITS_FILE"] = old_override


def test_merges_samples_across_multiple_chat_files():
    # Two different tg-ctl chat ids each get their own usage-latest.json; one
    # tracks only claude, the other only codex. A "pick the freshest file"
    # strategy would silently drop whichever family's file is less recently
    # touched — load_snapshot() must MERGE both instead.
    tmp_dir = Path(tempfile.mkdtemp())
    (tmp_dir / "tg-ctl.111.usage-latest.json").write_text(
        json.dumps({"version": 1, "samples": [_sample("claude", "weekly", 40)]}),
        encoding="utf-8",
    )
    (tmp_dir / "tg-ctl.222.usage-latest.json").write_text(
        json.dumps({"version": 1, "samples": [_sample("codex", "weekly", 60)]}),
        encoding="utf-8",
    )
    old_dir = os.environ.get("REVIEW_USAGE_LIMITS_DIR")
    old_file = os.environ.pop("REVIEW_USAGE_LIMITS_FILE", None)
    os.environ["REVIEW_USAGE_LIMITS_DIR"] = str(tmp_dir)
    try:
        snapshot = load_snapshot()
        assert (
            usage_percent_for_model("claude:claude-opus-4-8", samples=snapshot, now=NOW)
            == 40
        )
        assert usage_percent_for_model("codex", samples=snapshot, now=NOW) == 60
    finally:
        if old_dir is None:
            os.environ.pop("REVIEW_USAGE_LIMITS_DIR", None)
        else:
            os.environ["REVIEW_USAGE_LIMITS_DIR"] = old_dir
        if old_file is not None:
            os.environ["REVIEW_USAGE_LIMITS_FILE"] = old_file


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
