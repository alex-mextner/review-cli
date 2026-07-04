#!/usr/bin/env python3
"""REAL end-to-end tests for resumable brainstorm SESSIONS.

WHAT MAKES THESE "REAL" (vs tests/test_sessions.py)
---------------------------------------------------
test_sessions.py stubs `run_panel`/`run_moderator` IN-PROCESS. These tests instead spawn
the actual `bin/review` CLI as a SUBPROCESS and drive the genuine
`cli.py -> modes/brainstorm -> panel -> backends.resolve_backend` path. The ONLY thing
faked is the leaf model call: `REVIEW_FAKE_BACKEND=1` swaps every backend for the
deterministic, network-free `review_fake` (see reviewlib/backends.py). Nothing else is
mocked — argument parsing, the round loop, the discussion-log writer, the parser, the
resume seeding, and the exit codes all run for real. No network, no real CLIs, no keys.

THREE scenarios:
  1. test_e2e_resume_from_interrupted_log (ALWAYS runs, deterministic, CI-safe): a partial
     discussion log is planted on disk (exactly the shape a killed run leaves — rounds done,
     no synthesis), then a real `review sessions -s <id>` subprocess RESUMES it. Asserts it
     CONTINUES (does not restart at round 1), appends to the SAME log, and writes a final
     synthesis; and that the resumed log == pre-resume rounds + the new ones.
  2. test_e2e_refused_resume_exit_code (ALWAYS runs): a real `review sessions -s
     <completed-id>` without --force exits NON-ZERO (the codex-P2 refusal contract), while
     --force exits 0.
  3. test_e2e_spawn_kill_resume (KILL-based; runs by default, SKIPS gracefully if the
     timing window can't be hit, or is force-skipped with REVIEW_E2E_SKIP_KILL=1): spawns a
     real `review brainstorm`, polls its discussion log until >=2 rounds land, SIGTERM->
     SIGKILLs it mid-run, asserts the session lists as INTERRUPTED, then RESUMES it and
     asserts a final synthesis with the pre-kill rounds preserved.

Same harness style as the other tests: plain `test_*` functions invoked by the __main__
block; $REVIEW_LOG_DIR points the log dir at a throwaway temp dir so the real
~/Library/Logs/review-cli is never touched.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_BIN = REPO_ROOT / "bin" / "review"
sys.path.insert(0, str(REPO_ROOT))

_LOG_GLOB = "*-brainstorm.md"


def _env(log_dir: Path, *, fake_delay: float | None = None) -> dict[str, str]:
    """A child env with the fake backend ON and the log/stats dirs redirected to temp."""
    env = dict(os.environ)
    env["REVIEW_FAKE_BACKEND"] = "1"
    env["REVIEW_LOG_DIR"] = str(log_dir)
    env["REVIEW_STATS_FILE"] = str(log_dir / "stats.jsonl")
    env["REVIEW_TASK_CODE"] = "TEST-1"
    # Keep the run deterministic: pin the panel so resolution doesn't depend on host CLIs.
    if fake_delay is not None:
        env["REVIEW_FAKE_DELAY"] = str(fake_delay)
    return env


def _run_cli(args: list[str], env: dict[str, str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REVIEW_BIN), *args],
        env=env, capture_output=True, text=True, timeout=timeout,
    )


def _only_log(log_dir: Path) -> Path:
    logs = list(log_dir.glob(_LOG_GLOB))
    assert len(logs) == 1, [p.name for p in logs]
    return logs[0]


def _count_rounds(text: str) -> int:
    """Count `# Round N` heading-shaped lines. Used ONLY to poll a live WRITER log (which we
    control — no adversarial persona output there) to know how many rounds have landed before
    the kill; it is a coarse progress signal, not a spoof-resistant structural count."""
    return len(re.findall(r"(?m)^# Round \d+\s*$", text))


# --- 1. Deterministic resume (always runs) -----------------------------------------------
def test_e2e_resume_from_interrupted_log():
    """A REAL `review sessions -s <id>` subprocess resumes a planted interrupted log: it
    continues from the next round (never restarts at 1), appends to the SAME log, and writes
    a final synthesis. Fully deterministic — no kill, no timing."""
    d = Path(tempfile.mkdtemp(prefix="e2e-resume-"))
    env = _env(d)
    stamp = "20260616T200000_000001Z"
    # An INTERRUPTED log as a killed run leaves it: 2 rounds done, moderator said CONTINUE,
    # NO final synthesis. Legacy shape (no sentinel) on purpose — proves a pre-sentinel log
    # is still resumable end-to-end and the resumed half gets the new nonce'd sentinels.
    interrupted = (
        "# Brainstorm: e2e resume topic\n\npanel=codex moderator=codex rounds>=5 max=6\n"
        "# Round 1\n#### codex\npre-kill idea one\n\n## Moderator (round 1)\nok\nDECISION: CONTINUE\n"
        "# Round 2\n#### codex\npre-kill idea two\n\n## Moderator (round 2)\nok\nDECISION: CONTINUE\n"
    )
    log = d / f"{stamp}-brainstorm.md"
    log.write_text(interrupted, encoding="utf-8")
    pre_text = log.read_text(encoding="utf-8")
    pre_rounds = _count_rounds(pre_text)
    assert pre_rounds == 2, pre_rounds

    # Sanity: it lists as INTERRUPTED before the resume.
    listed = _run_cli(["sessions", "-a"], env)
    assert listed.returncode == 0, listed.stderr
    assert "20260616T200000" in listed.stdout, listed.stdout
    assert "interrupted" in listed.stdout, listed.stdout

    # RESUME via a real subprocess.
    res = _run_cli(["sessions", "-s", "20260616T200000", "-m", "codex"], env, timeout=90)
    assert res.returncode == 0, f"resume failed rc={res.returncode}\nSTDERR:\n{res.stderr}"

    # The SAME log was appended to (still exactly one log file) and now completes.
    post_log = _only_log(d)
    assert post_log == log, (post_log, log)
    post_text = post_log.read_text(encoding="utf-8")

    # CONTINUED, did not restart: the pre-kill rounds are still verbatim at the top, and new
    # rounds (>= round 3) were appended.
    assert "pre-kill idea one" in post_text and "pre-kill idea two" in post_text, post_text
    assert post_text.startswith(pre_text), "resume must APPEND, not rewrite the head"
    assert _count_rounds(post_text) > pre_rounds, "resume produced no new rounds"
    # The resumed rounds are genuinely round 3+ (no duplicate round 1/2).
    assert re.search(r"(?m)^# Round 3\s*$", post_text), post_text

    # A final synthesis landed (the run completed). Re-parse with the REAL parser to confirm
    # the session now reads as COMPLETED and the round count is the continued total.
    import reviewlib.sessions as S
    os.environ["REVIEW_LOG_DIR"] = str(d)
    try:
        sess = S.parse_log(post_log)
    finally:
        os.environ.pop("REVIEW_LOG_DIR", None)
    assert sess.completed is True, post_text
    assert sess.completed_rounds >= 3, (sess.completed_rounds, post_text)
    # The first two parsed rounds are the pre-kill ones (transcript preserved across resume).
    assert "pre-kill idea one" in sess.transcript_blocks()[0]
    assert "pre-kill idea two" in sess.transcript_blocks()[1]


# --- 2. Refused-resume exit code (always runs) -------------------------------------------
def test_e2e_refused_resume_exit_code():
    """REAL subprocess: `review sessions -s <completed-id>` WITHOUT --force exits NON-ZERO
    (codex P2 refusal contract); --force on the same id exits 0."""
    d = Path(tempfile.mkdtemp(prefix="e2e-refuse-"))
    env = _env(d)
    completed = (
        "# Brainstorm: already done\n<!-- review:session abc123def456abc123def456 -->\n\n"
        "panel=codex moderator=codex rounds>=5 max=6\n"
        "# Round 1\n<!-- review:round 1 nonce=abc123def456abc123def456 -->\n"
        "#### codex\nx\n## Moderator (round 1)\nok\nDECISION: STOP\n"
        "# Final synthesis\n<!-- review:final nonce=abc123def456abc123def456 -->\ndone\n"
    )
    (d / "20260616T210000_000002Z-brainstorm.md").write_text(completed, encoding="utf-8")

    refused = _run_cli(["sessions", "-s", "20260616T210000"], env)
    assert refused.returncode != 0, "refused resume must be non-zero"
    assert "already completed" in refused.stderr, refused.stderr

    forced = _run_cli(["sessions", "-s", "20260616T210000", "--force", "-m", "codex"], env, timeout=90)
    assert forced.returncode == 0, f"--force should succeed, rc={forced.returncode}\n{forced.stderr}"


# --- 3. Spawn -> kill -> resume (skips gracefully if timing can't be hit) -----------------
def test_e2e_spawn_kill_resume():
    """Spawn a REAL `review brainstorm`, KILL it mid-run, assert INTERRUPTED, then RESUME and
    assert a final synthesis with the pre-kill rounds preserved.

    Kill timing is the one genuinely non-deterministic part. The fake backend's
    REVIEW_FAKE_DELAY slows each call so the round loop is observable; we poll the live log
    until >=2 rounds land, then SIGTERM->SIGKILL. If the window can't be hit within the
    deadline (a slow/odd CI box) or REVIEW_E2E_SKIP_KILL=1 is set, the KILL scenario SKIPS
    (prints SKIP, returns) — the deterministic resume above still covers spawn+resume."""
    if os.environ.get("REVIEW_E2E_SKIP_KILL", "").strip() not in ("", "0", "false", "no"):
        print("    SKIP test_e2e_spawn_kill_resume (REVIEW_E2E_SKIP_KILL set)")
        return

    d = Path(tempfile.mkdtemp(prefix="e2e-kill-"))
    # ~0.4s/call * 3 personas + moderator ~= >1s/round, so 2 rounds take a couple seconds —
    # a comfortable window to poll-and-kill, while still finishing fast on resume.
    env = _env(d, fake_delay=0.4)

    proc = subprocess.Popen(
        [sys.executable, str(REVIEW_BIN), "brainstorm", "kill-me topic",
         "--rounds", "5", "--max-rounds", "6", "-m", "codex"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,  # own process group so we can kill the whole tree
    )
    try:
        deadline = time.time() + 30
        killed_after_rounds = 0
        while time.time() < deadline:
            if proc.poll() is not None:
                break  # process exited before we could kill it — window missed
            logs = list(d.glob(_LOG_GLOB))
            if logs:
                rounds = _count_rounds(logs[0].read_text(encoding="utf-8", errors="replace"))
                if rounds >= 2:
                    killed_after_rounds = rounds
                    break
            time.sleep(0.1)

        early_exit = proc.poll()
        if early_exit is not None and early_exit != 0:
            # A NON-ZERO early exit is a REAL failure (import/argparse regression, crash),
            # NOT a timing miss — surface it loudly instead of skipping (codex P2). Drain the
            # child's output so the assertion carries the actual error.
            try:
                out, err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                out, err = "", "(could not drain child output)"
            raise AssertionError(
                f"`review brainstorm` exited {early_exit} before the kill window — a real "
                f"startup/run failure, not a timing miss.\nSTDOUT:\n{out}\nSTDERR:\n{err}"
            )
        if killed_after_rounds < 2 or early_exit is not None:
            # Either the window was missed (too slow) or the run cleanly FINISHED early
            # (exit 0 before 2 rounds — too fast). Neither is a product bug, and the
            # deterministic resume test already proves resume — so skip on timing only.
            _terminate(proc)
            print(f"    SKIP test_e2e_spawn_kill_resume (could not hit kill window; "
                  f"rounds_seen={killed_after_rounds}, clean_early_exit={early_exit == 0})")
            return

        # KILL mid-run: SIGTERM the whole group, then SIGKILL if it lingers.
        _terminate(proc)
    finally:
        _terminate(proc)

    log = _only_log(d)
    pre_text = log.read_text(encoding="utf-8")
    pre_rounds = _count_rounds(pre_text)
    assert pre_rounds >= 2, pre_rounds
    assert "# Final synthesis" not in pre_text, "kill should land BEFORE synthesis"

    # The killed session lists as INTERRUPTED.
    listed = _run_cli(["sessions", "-a"], env)
    assert listed.returncode == 0, listed.stderr
    assert "interrupted" in listed.stdout, listed.stdout
    m = re.search(r"(\d{8}T\d{6})\s+\[interrupted", listed.stdout) or re.search(
        r"^\s+(\d{8}T\d{6})\b", listed.stdout, re.M)
    assert m, f"no interrupted session id in:\n{listed.stdout}"
    sess_id = m.group(1)

    # RESUME the killed session — it must continue and synthesize.
    res = _run_cli(["sessions", "-s", sess_id, "-m", "codex"], env, timeout=90)
    assert res.returncode == 0, f"resume after kill failed rc={res.returncode}\n{res.stderr}"

    post_text = _only_log(d).read_text(encoding="utf-8")
    assert post_text.startswith(pre_text), "resume must append to the killed log"
    assert _count_rounds(post_text) >= pre_rounds, "resume lost rounds"
    assert "# Final synthesis" in post_text, "resume did not synthesize"
    # The pre-kill rounds survived into the completed session.
    import reviewlib.sessions as S
    os.environ["REVIEW_LOG_DIR"] = str(d)
    try:
        sess = S.parse_log(_only_log(d))
    finally:
        os.environ.pop("REVIEW_LOG_DIR", None)
    assert sess.completed is True, post_text
    assert sess.completed_rounds >= pre_rounds, (sess.completed_rounds, pre_rounds)


def _terminate(proc: subprocess.Popen) -> None:
    """SIGTERM the child's whole process group, escalate to SIGKILL, never leak a process."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {exc!r}")
            import traceback
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
