#!/usr/bin/env python3
"""`review stat` — the bare CLI subcommand wiring for the per-harness usage report.

Regression this pins: `review stat --json` (and any other subcommand-scoped flag) used
to be misrouted to the "no subcommand given, use `review diff`" migration pointer,
because `stat` was missing from `_BARE_SUBCOMMANDS` — the registry
`_reject_subcommand_only_flag_without_verb` consults to know a flag like `--json`
belongs to a recognized bare management subcommand (like `task`/`dashboard`/`sessions`)
rather than being a stray mode-only flag with no verb. Caught by actually invoking the
real CLI with `--json`, not just `--days 0` alone (which happened to work).

Driven through the real `bin/review` subprocess (the faithful end-to-end path) against a
throwaway `$REVIEW_LOG_DIR` with a couple of fixture call/retry logs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

REVIEW = str(REPO_ROOT / "bin" / "review")


def _write_fixture_logs(d: Path) -> None:
    (d / "20260813T100000_000000Z-z.ai-r0.log").write_text(
        "[review-cli] z.ai: z.ai API glm-5.2 (args redacted) task=T1\n"
        "Looks fine.\n\nprompt_tokens=120 output_tokens=45\n"
        "[review-cli] EXIT 0\n",
        encoding="utf-8",
    )
    (d / "20260813T100100_000000Z-codex-r0.log").write_text(
        "[review-cli] codex: codex (args redacted) task=T1\n"
        "reading SKILL.md\ndiff --git a/x b/x\nreview text\n"
        "[review-cli] EXIT 0\n",
        encoding="utf-8",
    )
    (d / "20260813T100200_000000Z-claude-r0.log").write_text(
        "[review-cli] claude: claude-p (args redacted) task=T1\n"
        "Claude Fable 5 is currently unavailable. Learn more: https://example.com\n"
        "[review-cli] EXIT 0\n",
        encoding="utf-8",
    )
    (d / "20260813T100300_000000Z-Fable_promote-retry-0001.log").write_text(
        "[review-cli] RETRY-EVENT kind=promote model=Fable [architect]->commandcode:zai-org/GLM-5.2 "
        "delay=0.00s exit=1\n"
        "[detail] You've hit your session limit · resets 7:30pm (Europe/Belgrade)\n",
        encoding="utf-8",
    )


def _run(argv, *, log_dir: Path) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as fake_home:
        env = dict(os.environ)
        env["HOME"] = fake_home
        env["XDG_CONFIG_HOME"] = str(Path(fake_home) / ".config")
        env["REVIEW_LOG_DIR"] = str(log_dir)
        return subprocess.run(
            [sys.executable, REVIEW, "stat", *argv],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )


def test_stat_days_zero_text_report():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _write_fixture_logs(d)
        proc = _run(["--days", "0"], log_dir=d)
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        assert "Traceback" not in proc.stdout + proc.stderr
        assert "calls: 3" in proc.stdout
        assert "codex" in proc.stdout
        assert "z.ai" in proc.stdout
        assert "Fable (priority-1 board seat) pattern" in proc.stdout
        assert "session_limit=1" in proc.stdout


def test_stat_json_flag_does_not_trigger_no_subcommand_error():
    """The exact regression: `--json` combined with the bare `stat` subcommand must
    dispatch normally, not fall through to the removed-bare-review migration pointer."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _write_fixture_logs(d)
        proc = _run(["--days", "0", "--json"], log_dir=d)
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        assert "no subcommand given" not in proc.stdout + proc.stderr
        payload = json.loads(proc.stdout)
        assert payload["call_count"] == 3
        assert payload["retry_event_count"] == 1
        assert set(payload["harnesses"]) == {"z.ai", "codex", "claude"}
        assert payload["fable"]["dispatch_attempts"] == 1
        assert payload["fable"]["failure_rate"] == 1.0


def test_stat_harness_filter():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _write_fixture_logs(d)
        proc = _run(["--days", "0", "--harness", "codex", "--json"], log_dir=d)
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        payload = json.loads(proc.stdout)
        assert set(payload["harnesses"]) == {"codex"}
        # The overall call_count / fable section stay unscoped (global context), only
        # the harness table itself is filtered.
        assert payload["call_count"] == 3


def test_stat_harness_filter_accepts_common_aliases():
    """glm review finding, round 2: `--harness` used to filter on an EXACT match
    against the report's raw backend-name keys (`z.ai`, `opencode`, `commandcode`), not
    the short aliases `-m`/config actually teach (`-m glm`, `-m cc`) — so `--harness
    glm`/`zai`/`cc` all printed "no calls recorded" while the data sat in the report
    under a different spelling. Pins that the common aliases the report's own footnote
    (and `--harness`'s help text) advertise now actually resolve to the real harness."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _write_fixture_logs(d)
        for alias in ("glm", "zai", "GLM52"):  # case-insensitive too
            proc = _run(["--days", "0", "--harness", alias, "--json"], log_dir=d)
            assert proc.returncode == 0, (
                alias,
                proc.returncode,
                proc.stdout,
                proc.stderr,
            )
            payload = json.loads(proc.stdout)
            assert set(payload["harnesses"]) == {"z.ai"}, (alias, payload["harnesses"])


def test_stat_harness_filter_exact_name_is_case_and_whitespace_insensitive():
    """Opus review finding, round 4: `_normalize_harness_arg`'s fallback (for a token
    NOT in the alias dict) used to return the RAW value completely unchanged — every
    real backend key is lowercase with no surrounding whitespace, so `--harness Codex`
    (different casing) or `--harness ' codex '` (surrounding whitespace) fell through
    as a literal `'Codex'`/`' codex '`, which then never equals the report's `'codex'`
    key: a false "no calls recorded" for data that genuinely exists. Pins that an exact
    backend name now matches regardless of casing/whitespace, same as an alias already
    did."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _write_fixture_logs(d)
        for spelling in ("Codex", "CODEX", " codex ", "codex"):
            proc = _run(["--days", "0", "--harness", spelling, "--json"], log_dir=d)
            assert proc.returncode == 0, (
                spelling,
                proc.returncode,
                proc.stdout,
                proc.stderr,
            )
            payload = json.loads(proc.stdout)
            assert set(payload["harnesses"]) == {"codex"}, (
                spelling,
                payload["harnesses"],
            )


def test_stat_harness_filter_cmd_is_not_a_silently_accepted_alias():
    """`cmd` is NOT a real alias anywhere in this codebase (unlike `cc`, which genuinely
    resolves to `commandcode` via `config.MODEL_ALIASES`) — pins that `--harness cmd`
    stays an honest "no calls recorded" rather than silently normalizing to something
    real, which would misreport a request that was never actually a valid alias."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _write_fixture_logs(d)
        proc = _run(["--days", "0", "--harness", "cmd"], log_dir=d)
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        assert "No calls recorded for harness 'cmd'" in proc.stdout


def test_stat_invalid_since_is_a_usage_error():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        proc = _run(["--since", "not-a-date"], log_dir=d)
        assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
        assert "invalid --since value" in proc.stdout + proc.stderr


def test_stat_since_accepts_trailing_z_on_every_supported_python():
    """Opus/kimi review finding: `datetime.fromisoformat` only accepts a trailing `Z`
    (Zulu/UTC) shorthand from Python 3.11 onward, but this project declares
    `requires-python = ">=3.9"` and every call-log filename this command's own output
    is built from uses exactly that `...Z` stamp format — so a value copied straight
    out of `review stat`'s own report used to usage-error on 3.9/3.10. Pins that the
    exact same `--since` value the fixture stamps use (`...Z`) is accepted, on
    whichever Python actually runs this test."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _write_fixture_logs(d)
        proc = _run(["--since", "2026-08-13T10:00:00Z", "--json"], log_dir=d)
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        payload = json.loads(proc.stdout)
        assert payload["call_count"] == 3


def test_stat_help_works():
    with tempfile.TemporaryDirectory() as tmp:
        proc = _run(["--help"], log_dir=Path(tmp))
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        assert "usage: review stat" in proc.stdout
        assert "--harness" in proc.stdout
        assert "--json" in proc.stdout


def test_stat_empty_log_dir_reports_no_calls():
    with tempfile.TemporaryDirectory() as tmp:
        proc = _run(["--days", "0"], log_dir=Path(tmp))
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        assert "calls: 0" in proc.stdout
        assert "No calls recorded" in proc.stdout


def test_stat_negative_top_is_clamped_not_a_footgun():
    """kimi review finding: `--top -3` used to silently slice `sorted(...)[:-3]` — "all
    but the last 3" — under a "Top N oversized calls" heading. Pins the clamp: a
    negative --top behaves like --top 1, never the reversed-slice footgun."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _write_fixture_logs(d)
        proc = _run(["--days", "0", "--top", "-3", "--json"], log_dir=d)
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        payload = json.loads(proc.stdout)
        assert len(payload["top_oversized_calls"]) == 1  # clamped to 1, not 0


def test_stat_harness_filter_empty_message_names_the_harness():
    """kimi review finding: when calls exist but none match --harness, the text report
    must not claim "No calls recorded in this window" (false — calls DO exist, just not
    for this harness)."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _write_fixture_logs(d)
        proc = _run(["--days", "0", "--harness", "nonexistent-harness"], log_dir=d)
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        assert "calls: 3" in proc.stdout  # the header still shows the REAL total
        assert "No calls recorded for harness 'nonexistent-harness'" in proc.stdout
        # Opus review finding, round 3: the text report used to print this SAME message
        # a second time on stderr (redundant with the one in the report body above) —
        # pin that the text path shows it exactly once now, not twice.
        assert "no calls recorded for harness" not in proc.stderr.lower()


def test_stat_harness_filter_json_still_explains_an_empty_result_on_stderr():
    """The `--json` payload alone (an empty `harnesses` dict) doesn't explain WHY —
    unlike the text report, it has no in-payload message — so this is the one path that
    still needs the stderr note (see the de-duplication fix above, which removed it
    for the text path only)."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _write_fixture_logs(d)
        proc = _run(
            ["--days", "0", "--harness", "nonexistent-harness", "--json"], log_dir=d
        )
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        payload = json.loads(proc.stdout)
        assert payload["harnesses"] == {}
        assert (
            "no calls recorded for harness 'nonexistent-harness'" in proc.stderr.lower()
        )


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
