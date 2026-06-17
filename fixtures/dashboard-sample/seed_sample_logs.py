#!/usr/bin/env python3
"""Seed a realistic review-cli log directory for the dashboard's VISUAL QA.

The dashboard reads real on-disk log artifacts from ``REVIEW_LOG_DIR``. A live ``~/Library/
Logs/review-cli`` is noisy (and historically polluted with smoke runs whose brainstorm topic
was the LITERAL string "topic"), which is exactly the failure the dashboard's visual QA must
NOT reproduce. This script writes a SELF-CONTAINED sample directory with REAL topics, varied
models, real error/timeout shapes, and multiple sessions across several days — enough to make
every dashboard tab (Overview / Chat logs / Stats / Models & roles / Metrics / Overseer
feedback / Modes / Errors / Tasks / Prompts / PRs & tickets) render real data.

Usage:
    python fixtures/dashboard-sample/seed_sample_logs.py [DEST_DIR]
    # then:
    REVIEW_LOG_DIR=DEST_DIR review dashboard --port 7878 start

Idempotent: a fresh DEST_DIR is created (existing *.log / *.md are cleared first) so re-running
gives the same corpus. DEST_DIR defaults to ./fixtures/dashboard-sample/logs.
"""
from __future__ import annotations

import sys
from pathlib import Path


def _call_log(
    log_dir: Path,
    stamp: str,
    backend: str,
    round_no: int,
    body: str,
    *,
    argv0: str = "/opt/homebrew/bin/codex",
    exit_code: int | None = 0,
) -> None:
    """Write one per-call streamed log: ``{stamp}Z-{backend}-r{round}.log``."""
    name = f"{stamp}Z-{backend}-r{round_no}.log"
    header = f"[review-cli] {backend}: {argv0} (args redacted)\n"
    footer = f"[review-cli] EXIT {exit_code}\n" if exit_code is not None else ""
    (log_dir / name).write_text(header + body + footer, encoding="utf-8")


def _brainstorm(log_dir: Path, stamp: str, topic: str, panel: str, moderator: str, body: str) -> None:
    """Write a brainstorm discussion md: ``{stamp}Z-brainstorm.md`` with a REAL topic header."""
    name = f"{stamp}Z-brainstorm.md"
    content = f"# Brainstorm: {topic}\n\npanel={panel} moderator={moderator} rounds>=3 max=5\n\n{body}"
    (log_dir / name).write_text(content, encoding="utf-8")


def seed(dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    for old in list(dest.glob("*.log")) + list(dest.glob("*.md")):
        old.unlink()

    # --- Session A: a 3-model panel review (diff mode), all OK. Day 1. -----------------------
    _call_log(dest, "20260610T090000_000000", "opus-4-8", 0,
              "The change is correct. One nit: the retry loop should cap at 5 attempts.\n",
              argv0="/Users/dev/.claude/local/claude")
    _call_log(dest, "20260610T090004_000000", "codex", 0,
              "LGTM. The error path returns the structured exit code as expected.\n")
    _call_log(dest, "20260610T090009_000000", "gemini", 0,
              "Looks good. Consider adding a test for the offline-bootstrap branch.\n",
              argv0="/opt/homebrew/bin/gemini")

    # --- Session B: a single review that surfaced a real error in the body. Day 1, later. ----
    _call_log(dest, "20260610T113000_000000", "codex", 0,
              "There's an issue with the selected model.\n"
              "[stderr] error: model 'glm-5.2' currently unavailable (paywall)\n",
              exit_code=0)

    # --- Session C: a timed-out call (the TIMEOUT marker + exit 124). Day 1. -----------------
    _call_log(dest, "20260610T140000_000000", "qwen", 0,
              "partial reasoning about the cache layer...\n"
              "[review-cli] TIMEOUT after 240s — partial output above]\n",
              argv0="/opt/homebrew/bin/opencode", exit_code=124)

    # --- Session D: a hard auth failure (bad key -> exit 401). Day 2. ------------------------
    _call_log(dest, "20260611T101500_000000", "glm-5.2", 0,
              '[stderr] {"error":{"code":"invalid_api_key","message":"bad key"}}\n',
              argv0="/opt/homebrew/bin/opencode", exit_code=401)

    # --- Session E: a 5-round brainstorm with a REAL topic + 3 personas/round. Day 2. --------
    bs_body = (
        "# Round 1\n"
        "#### Pragmatic staff engineer (codex)\n"
        "Cache at the edge with a short TTL; invalidate on write. Keep it boring.\n\n"
        "#### Security reviewer (opus-4-8)\n"
        "Watch for cache poisoning on the shared key; scope keys per-tenant.\n\n"
        "#### Performance specialist (gemini)\n"
        "Measure the hit-rate before adding a second tier; one tier is usually enough.\n\n"
        "## Moderator (round 1)\n"
        "Consensus: single short-TTL tier, per-tenant keys, measure first.\n\n"
        "# Round 2\n"
        "#### Pragmatic staff engineer (codex)\n"
        "Agreed — add a metric for hit-rate and a kill-switch env var.\n\n"
        "#### Security reviewer (opus-4-8)\n"
        "Per-tenant keys resolve the poisoning risk; ship it.\n\n"
        "# Final synthesis\n"
        "#### Synthesis (opus-4-8)\n"
        "Single edge cache, short TTL, per-tenant keys, hit-rate metric, env kill-switch.\n"
    )
    _call_log(dest, "20260611T154500_000000", "codex", 1, "round one persona output\n")
    _call_log(dest, "20260611T154512_000000", "opus-4-8", 1, "round one persona output\n",
              argv0="/Users/dev/.claude/local/claude")
    _call_log(dest, "20260611T154524_000000", "gemini", 1, "round one persona output\n")
    _brainstorm(dest, "20260611T154530_000000",
                "How should we cache API responses in the multi-tenant gateway?",
                "codex,opus-4-8,gemini", "opus-4-8", bs_body)

    # --- Session F: a quorum decision (just-ask style) — 2 models agree. Day 3. --------------
    _call_log(dest, "20260612T084500_000000", "opus-4-8", 0,
              "Recommendation: Option A (path-based dep) — fewer moving parts, no extra runtime.\n",
              argv0="/Users/dev/.claude/local/claude")
    _call_log(dest, "20260612T084509_000000", "codex", 0,
              "Concur with Option A. The editable install keeps the lib import working in tests.\n")

    # --- Session G: a second brainstorm, different REAL topic. Day 3. ------------------------
    bs2 = (
        "# Round 1\n"
        "#### Distributed-systems engineer (codex)\n"
        "Use a single shared service-manager lib; per-tool copies always drift.\n\n"
        "#### Pragmatic staff engineer (opus-4-8)\n"
        "Agreed. launchd on macOS, systemd --user on Linux, a no-op fallback elsewhere.\n\n"
        "# Final synthesis\n"
        "#### Synthesis (opus-4-8)\n"
        "One agenttools_service lib; run/start/status/stop/enable/disable; OS autostart shared.\n"
    )
    _call_log(dest, "20260612T161000_000000", "codex", 1, "round one persona output\n")
    _call_log(dest, "20260612T161015_000000", "opus-4-8", 1, "round one persona output\n",
              argv0="/Users/dev/.claude/local/claude")
    _brainstorm(dest, "20260612T161030_000000",
                "Should every long-running agent-tools server share one service-manager lib?",
                "codex,opus-4-8", "opus-4-8", bs2)

    return dest


def main(argv: list[str]) -> int:
    default = Path(__file__).resolve().parent / "logs"
    dest = Path(argv[1]).expanduser() if len(argv) > 1 else default
    out = seed(dest)
    n_logs = len(list(out.glob("*.log")))
    n_md = len(list(out.glob("*.md")))
    print(f"seeded {n_logs} call logs + {n_md} brainstorm logs into {out}")
    print(f"  REVIEW_LOG_DIR={out} review dashboard --port 7878 start")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
