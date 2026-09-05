#!/usr/bin/env python3
"""Diff-identity binding (reviewlib.stats "Diff-identity binding" section).

Three real incidents in one session (2026-08-11) showed the quorum store's
task-code-only keying let one diff's real reviews silently count toward a
completely different diff's self-merge-authority quorum:

  1. PR #151 (hyper-ext-e2e): task code HYP-1185 showed "14 passed iterations"
     that had actually reviewed an unrelated diff in a DIFFERENT REPO
     (hyperide's PreviewPanel.ts).
  2. PR #131 (hyper-ext-e2e): a deliberate task-code swap substituted
     HYP-1014's history (iterations from TWO DIFFERENT PRs sharing a parent
     ticket, SAME repo) for the PR's own honest, quorum-short code.
  3. HYP-858/PR #390 (agent-tools): stored history had years of unrelated
     cross-repo iterations mixed into one task code's history, and `gh ship`'s
     own automated Guard-B gate trusted the polluted count at merge time.

Each test below is traceable to one of these shapes: cross-repo (#1/#3),
same-repo-different-diff (#2), and the "history predates identity, still
counts" backward-compat guarantee the fix must preserve.

Same harness style as test_run_stats.py: plain test_* functions, run by
pytest OR the __main__ block; the stats store is redirected to a temp file
via $REVIEW_STATS_FILE so the real ~/.config store is never touched.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import cli as _cli  # noqa: E402
from reviewlib import stats as _stats  # noqa: E402

# Reuse test_run_stats.py's fixtures (temp JSONL store, throwaway git repo with an
# uncommitted diff, stdout capture) instead of duplicating them — pytest's rootdir-
# relative import inserts tests/ onto sys.path (no tests/__init__.py in this repo).
from test_run_stats import (  # noqa: E402
    _capture_stdout,
    _git_init_with_diff,
    _TmpStore,
)

TASK = "HYP-742"


# ---------------------------------------------------------------------------
# pure helpers: extract_diff_files / normalize_repo_remote / diff_content_hash
# ---------------------------------------------------------------------------
_TWO_FILE_DIFF = """diff --git a/src/a.py b/src/a.py
index 111..222 100644
--- a/src/a.py
+++ b/src/a.py
@@ -1 +1 @@
-old
+new
diff --git a/src/b.py b/src/b.py
index 333..444 100644
--- a/src/b.py
+++ b/src/b.py
@@ -1 +1 @@
-old2
+new2
"""


def test_extract_diff_files_basic():
    assert _stats.extract_diff_files(_TWO_FILE_DIFF) == ["src/a.py", "src/b.py"]


def test_extract_diff_files_empty_diff():
    assert _stats.extract_diff_files("") == []
    assert _stats.extract_diff_files(None) == []  # type: ignore[arg-type]


def test_extract_diff_files_rename_credits_both_sides():
    diff = (
        "diff --git a/old_name.py b/new_name.py\n"
        "similarity index 100%\n"
        "rename from old_name.py\n"
        "rename to new_name.py\n"
    )
    assert _stats.extract_diff_files(diff) == ["new_name.py", "old_name.py"]


def test_extract_diff_files_real_git_delete_header():
    # A real `git diff` delete header keeps the SAME path on both a/ and b/ sides
    # of `diff --git` (only the body's `+++` line becomes /dev/null) -- so the
    # "exclude /dev/null" guard in extract_diff_files exists for defense in depth
    # (a hand-built/foreign diff literally putting /dev/null in the header), not
    # because real git output needs it here.
    diff = "diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n"
    assert _stats.extract_diff_files(diff) == ["gone.py"]


def test_normalize_repo_remote_https_strips_credentials_and_git_suffix():
    url = "https://x-access-token:ghp_abcdef@github.com/hyperide/hyper-ext-e2e.git"
    assert _stats.normalize_repo_remote(url) == "github.com/hyperide/hyper-ext-e2e"


def test_normalize_repo_remote_ssh_scp_style_matches_https_equivalent():
    https = _stats.normalize_repo_remote(
        "https://github.com/hyperide/hyper-ext-e2e.git"
    )
    ssh = _stats.normalize_repo_remote("git@github.com:hyperide/hyper-ext-e2e.git")
    assert https == ssh == "github.com/hyperide/hyper-ext-e2e"


def test_normalize_repo_remote_different_repos_never_collide():
    a = _stats.normalize_repo_remote("git@github.com:hyperide/hyper-ext-e2e.git")
    b = _stats.normalize_repo_remote("git@github.com:hyperide/hyperide.git")
    assert a != b


def test_normalize_repo_remote_empty_and_unparseable_return_none():
    assert _stats.normalize_repo_remote("") is None
    assert _stats.normalize_repo_remote(None) is None  # type: ignore[arg-type]
    assert _stats.normalize_repo_remote("not a url at all") is None


def test_normalize_repo_remote_host_case_insensitive():
    """Regression (Fable/Opus review finding): DNS hostnames are case-insensitive,
    so 'GitHub.com' and 'github.com' must normalize to the SAME id -- otherwise a
    task recorded via a differently-cased remote URL spuriously repo_mismatches."""
    mixed = _stats.normalize_repo_remote(
        "https://GitHub.com/hyperide/hyper-ext-e2e.git"
    )
    lower = _stats.normalize_repo_remote(
        "https://github.com/hyperide/hyper-ext-e2e.git"
    )
    assert mixed == lower == "github.com/hyperide/hyper-ext-e2e"


def test_normalize_repo_remote_default_ssh_port_matches_portless():
    """Regression (Fable/Opus review finding): an explicit default SSH port (:22)
    names the SAME remote as the portless form -- must normalize identically, or a
    task recorded over one form spuriously repo_mismatches against the other."""
    with_port = _stats.normalize_repo_remote(
        "ssh://git@github.com:22/hyperide/hyper-ext-e2e.git"
    )
    without_port = _stats.normalize_repo_remote(
        "git@github.com:hyperide/hyper-ext-e2e.git"
    )
    assert with_port == without_port == "github.com/hyperide/hyper-ext-e2e"


def test_normalize_repo_remote_nondefault_ssh_port_kept_distinct():
    """A NON-default port plausibly names a different remote (e.g. a self-hosted
    Gitea on a nonstandard port) -- must NOT be stripped like the default :22 is."""
    custom_port = _stats.normalize_repo_remote(
        "ssh://git@git.internal:2222/org/repo.git"
    )
    portless = _stats.normalize_repo_remote("git@git.internal:org/repo.git")
    assert custom_port != portless


def test_diff_content_hash_deterministic_and_content_sensitive():
    h1 = _stats.diff_content_hash(_TWO_FILE_DIFF)
    h2 = _stats.diff_content_hash(_TWO_FILE_DIFF)
    h3 = _stats.diff_content_hash(_TWO_FILE_DIFF + "\n")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 64  # sha256 hex


# ---------------------------------------------------------------------------
# quorum_check: repo/diff mismatch classification (library level, no CLI)
# ---------------------------------------------------------------------------
def _record_passed(*, models, repo_id=None, diff_files=None):
    _stats.record_run(
        task_code=TASK,
        mode="review",
        models=models,
        duration_seconds=1.0,
        ok_count=len(models),
        fail_count=0,
        passed=True,
        repo_id=repo_id,
        diff_files=diff_files,
    )


def test_quorum_check_without_context_preserves_pre_v4_shape_and_behavior():
    """The regression guard for backward compat: a caller that never passes
    repo_id/diff_files (every direct library caller before this feature existed)
    gets the EXACT pre-v4 return shape -- no new keys, no filtering -- even when
    the stored iterations are, in fact, from totally different repos."""
    with _TmpStore():
        _record_passed(models=["codex"], repo_id="github.com/org/repo-a")
        _record_passed(models=["gemini"], repo_id="github.com/org/repo-b")
        _record_passed(models=["fable5"], repo_id="github.com/org/repo-c")
        result = _stats.quorum_check(TASK, min_iter=3, min_models=3)
        assert result["passed"] is True
        assert result["passed_iterations"] == 3
        assert set(result) == {
            "task_code",
            "passed_iterations",
            "total_iterations",
            "distinct_models_passed",
            "models",
            "min_iter",
            "min_models",
            "passed",
        }


def test_quorum_check_cross_repo_iterations_excluded_incident_1_and_3_shape():
    """Regression for incidents #1 (PR #151) and #3 (HYP-858): iterations recorded
    against a DIFFERENT repo than the one currently being checked must be excluded
    from the quorum count, not silently trusted."""
    with _TmpStore():
        this_repo = "github.com/hyperide/hyper-ext-e2e"
        other_repo = "github.com/hyperide/hyperide"
        # 3 "passed" iterations exist, but only 1 actually reviewed THIS repo.
        _record_passed(models=["codex"], repo_id=this_repo, diff_files=["a.py"])
        _record_passed(models=["gemini"], repo_id=other_repo, diff_files=["b.py"])
        _record_passed(models=["fable5"], repo_id=other_repo, diff_files=["c.py"])
        result = _stats.quorum_check(
            TASK,
            min_iter=3,
            min_models=3,
            repo_id=this_repo,
            diff_files=["a.py"],
        )
        assert result["passed"] is False
        assert result["passed_iterations"] == 1  # only the real one counts
        assert result["excluded_mismatched_iterations"] == 2
        assert result["verified_iterations"] == 1
        assert result["unverifiable_iterations"] == 0
        reasons = {d["reason"] for d in result["mismatch_details"]}
        assert reasons == {"repo_mismatch"}
        assert "error" in result and "excluded" in result["error"]


def test_quorum_check_same_repo_disjoint_diff_excluded_incident_2_shape():
    """Regression for incident #2 (PR #131): a deliberate/accidental task-code
    reuse between two DIFFERENT PRs in the SAME repo -- repo_id matches, but the
    touched files share nothing -- must also be excluded (repo alone isn't
    enough; the diff itself has to be plausibly the same change)."""
    with _TmpStore():
        repo = "github.com/hyperide/hyper-ext-e2e"
        # This task's real PR touches auth.ts; the polluted history is a
        # DIFFERENT PR (same repo, same parent ticket) touching unrelated files.
        _record_passed(models=["codex"], repo_id=repo, diff_files=["auth.ts"])
        _record_passed(
            models=["gemini"], repo_id=repo, diff_files=["billing.ts", "invoice.ts"]
        )
        _record_passed(models=["fable5"], repo_id=repo, diff_files=["billing.ts"])
        result = _stats.quorum_check(
            TASK,
            min_iter=3,
            min_models=3,
            repo_id=repo,
            diff_files=["auth.ts"],
        )
        assert result["passed"] is False
        assert result["passed_iterations"] == 1
        assert result["excluded_mismatched_iterations"] == 2
        reasons = {d["reason"] for d in result["mismatch_details"]}
        assert reasons == {"diff_mismatch"}


def test_quorum_check_overlapping_files_across_iterations_still_counts():
    """NOT a regression: the normal review-fix-review loop, where the diff's exact
    TEXT changes between iterations (findings get fixed) but the file set stays
    the same or overlaps, must still satisfy the gate -- this is the reason
    matching is file-set overlap, not diff-content-hash equality."""
    with _TmpStore():
        repo = "github.com/hyperide/hyper-ext-e2e"
        # Round 1 touched auth.ts + helpers.ts; round 2 (after a fix) only auth.ts
        # (helpers.ts's finding got reverted); round 3 touched auth.ts + a NEW
        # file. All three share at least "auth.ts" with the current diff.
        _record_passed(
            models=["codex"], repo_id=repo, diff_files=["auth.ts", "helpers.ts"]
        )
        _record_passed(models=["gemini"], repo_id=repo, diff_files=["auth.ts"])
        _record_passed(
            models=["fable5"], repo_id=repo, diff_files=["auth.ts", "new_file.ts"]
        )
        result = _stats.quorum_check(
            TASK,
            min_iter=3,
            min_models=3,
            repo_id=repo,
            diff_files=["auth.ts"],
        )
        assert result["passed"] is True
        assert result["passed_iterations"] == 3
        assert result["excluded_mismatched_iterations"] == 0


def test_quorum_check_diffless_iterations_verified_by_repo_alone():
    """just-ask/quorum/brainstorm can run with NO diff at all (a question, not a
    change) -- diff_files == [] on both sides. repo_id match alone must still
    verify these (there is no file-set signal to compare)."""
    with _TmpStore():
        repo = "github.com/hyperide/hyper-ext-e2e"
        _record_passed(models=["codex"], repo_id=repo, diff_files=[])
        _record_passed(models=["gemini"], repo_id=repo, diff_files=[])
        result = _stats.quorum_check(
            TASK, min_iter=2, min_models=2, repo_id=repo, diff_files=[]
        )
        assert result["passed"] is True
        assert result["excluded_mismatched_iterations"] == 0


def test_quorum_check_legacy_iterations_without_identity_are_unverifiable_not_mismatched():
    """History written before this feature existed (or a run with no resolvable
    repo) has NO repo_id at all -- it must be treated as unverifiable (still
    counts, preserving the old "any passed iteration counts" behavior for old
    data), never auto-flagged as a mismatch just because it lacks the field."""
    with _TmpStore():
        this_repo = "github.com/hyperide/hyper-ext-e2e"
        _record_passed(models=["codex"])  # legacy shape: no repo_id/diff_files
        _record_passed(models=["gemini"], repo_id=this_repo, diff_files=["a.py"])
        result = _stats.quorum_check(
            TASK,
            min_iter=2,
            min_models=2,
            repo_id=this_repo,
            diff_files=["a.py"],
        )
        assert result["passed"] is True
        assert result["passed_iterations"] == 2
        assert result["verified_iterations"] == 1
        assert result["unverifiable_iterations"] == 1
        assert result["excluded_mismatched_iterations"] == 0


def test_quorum_check_all_mismatched_fails_bar_despite_raw_count_meeting_floor():
    """The exact HYP-858 shape: enough RAW passed iterations exist to clear the
    floor, but every single one is cross-repo pollution -- the bar must NOT be
    met (0 verified/unverifiable, not 11 polluted ones)."""
    with _TmpStore():
        this_repo = "github.com/hyperide/agent-tools"
        for i, model in enumerate(("codex", "gemini", "fable5", "glm")):
            _record_passed(models=[model], repo_id=f"github.com/other-org/repo-{i}")
        result = _stats.quorum_check(
            TASK, min_iter=3, min_models=3, repo_id=this_repo, diff_files=None
        )
        assert result["passed"] is False
        assert result["passed_iterations"] == 0
        assert result["excluded_mismatched_iterations"] == 4


def test_quorum_check_stalled_models_coexists_with_mismatch_error():
    """review-cli#221 round-4 review finding (k3/Opus): a mismatch-error result is NOT
    an early return (unlike the invalid-task-code/unreadable-store/no-iterations cases)
    -- `_finalize_quorum_result` sets `result["error"]` on the ALREADY-CONSTRUCTED
    result dict, after the `stalled_models` block already ran. cli.py's own comment
    claims this combination is real ("stalled_models can genuinely be populated
    alongside a mismatch error"), but no test drove it before this one -- a regression
    reintroducing the early `if "error" in result: ... return 1` guard removed in this
    diff would pass the full suite silently while breaking this exact case."""
    from reviewlib import seat_cooldown as _sc

    with tempfile.TemporaryDirectory() as d, _TmpStore():
        saved_cd_file = os.environ.get("REVIEW_SEAT_COOLDOWN_FILE")
        saved_cd_ttl = os.environ.get("REVIEW_SEAT_COOLDOWN_SECONDS")
        os.environ["REVIEW_SEAT_COOLDOWN_FILE"] = str(Path(d) / "seat-cooldown.json")
        os.environ.pop("REVIEW_SEAT_COOLDOWN_SECONDS", None)
        try:
            this_repo = "github.com/hyperide/agent-tools"
            # A polluted, cross-repo passed iteration -- excluded, drives the bar to
            # NOT met via the mismatch path (result["error"] gets set).
            _record_passed(models=["codex"], repo_id="github.com/other-org/repo")
            # A SEPARATE, real (unpassed) attempt at THIS task/repo by a seat that is
            # currently cooling down -- this is what stalled_models must still surface.
            _stats.record_run(
                task_code=TASK,
                mode="review",
                models=["oc:zai/glm-5.2"],
                duration_seconds=1.0,
                ok_count=0,
                fail_count=1,
                passed=False,
                repo_id=this_repo,
            )
            _sc.record_cooldown("oc:zai/glm-5.2", "timed out", now=time.time())

            result = _stats.quorum_check(
                TASK, min_iter=3, min_models=3, repo_id=this_repo, diff_files=None
            )
            assert result["passed"] is False
            assert "error" in result  # the mismatch-error path
            assert "excluded" in result["error"] or "mismatch" in result["error"]
            assert "stalled_models" in result, (
                "stalled_models must survive the mismatch-error branch, not just the "
                "bare-ratio-not-met branch"
            )
            assert result["stalled_models"][0]["model"] == "oc:zai/glm-5.2"
        finally:
            if saved_cd_file is None:
                os.environ.pop("REVIEW_SEAT_COOLDOWN_FILE", None)
            else:
                os.environ["REVIEW_SEAT_COOLDOWN_FILE"] = saved_cd_file
            if saved_cd_ttl is None:
                os.environ.pop("REVIEW_SEAT_COOLDOWN_SECONDS", None)
            else:
                os.environ["REVIEW_SEAT_COOLDOWN_SECONDS"] = saved_cd_ttl


def test_quorum_check_stalled_models_present_when_mismatch_exclusion_drops_below_floor():
    """review-cli#221 round-4 review finding (Opus/Fable): the prior coexistence test
    used raw counts already below the floor, so `result["passed"]` was False the moment
    the dict literal was built -- it couldn't distinguish "stalled_models runs on the
    final passed value" from "stalled_models runs on a since-superseded early value".
    This drives the OTHER shape both reviewers asked for: raw passed-iteration count
    alone would CLEAR the floor, but mismatch (diff-identity) exclusion drops the
    GATE-COUNTED total below it -- `gate_iterations`/`models` (what `result["passed"]`
    is actually computed from) are already the POST-exclusion sets by the time the
    dict literal runs, so this must behave identically to the simpler case."""
    from reviewlib import seat_cooldown as _sc

    with tempfile.TemporaryDirectory() as d, _TmpStore():
        saved_cd_file = os.environ.get("REVIEW_SEAT_COOLDOWN_FILE")
        saved_cd_ttl = os.environ.get("REVIEW_SEAT_COOLDOWN_SECONDS")
        os.environ["REVIEW_SEAT_COOLDOWN_FILE"] = str(Path(d) / "seat-cooldown.json")
        os.environ.pop("REVIEW_SEAT_COOLDOWN_SECONDS", None)
        try:
            this_repo = "github.com/hyperide/agent-tools"
            # 3 raw PASSED iterations -- enough to clear min_iter=3 on a raw count --
            # but every one is cross-repo pollution, so post-exclusion gate_iterations
            # is 0. The HYP-858 shape from the sibling test above, just with a cooling
            # seat added.
            for i, model in enumerate(("codex", "gemini", "fable5")):
                _record_passed(models=[model], repo_id=f"github.com/other-org/repo-{i}")
            # A separate, real (unpassed) attempt at THIS task/repo, currently cooling.
            _stats.record_run(
                task_code=TASK,
                mode="review",
                models=["oc:zai/glm-5.2"],
                duration_seconds=1.0,
                ok_count=0,
                fail_count=1,
                passed=False,
                repo_id=this_repo,
            )
            _sc.record_cooldown("oc:zai/glm-5.2", "timed out", now=time.time())

            result = _stats.quorum_check(
                TASK, min_iter=3, min_models=3, repo_id=this_repo, diff_files=None
            )
            # Raw count (3) meets min_iter (3), but post-exclusion it's 0 -- must fail.
            assert result["passed"] is False
            assert result["excluded_mismatched_iterations"] == 3
            assert "stalled_models" in result
            assert result["stalled_models"][0]["model"] == "oc:zai/glm-5.2"
        finally:
            if saved_cd_file is None:
                os.environ.pop("REVIEW_SEAT_COOLDOWN_FILE", None)
            else:
                os.environ["REVIEW_SEAT_COOLDOWN_FILE"] = saved_cd_file
            if saved_cd_ttl is None:
                os.environ.pop("REVIEW_SEAT_COOLDOWN_SECONDS", None)
            else:
                os.environ["REVIEW_SEAT_COOLDOWN_SECONDS"] = saved_cd_ttl


def test_quorum_check_min_floor_still_validated_with_context():
    """The min_iter/min_models >= 1 fail-closed floor validation runs BEFORE any
    repo_id/diff_files classification -- passing a check context must not bypass
    it (mirrors test_quorum_check_rejects_zero_or_negative_thresholds_directly in
    test_run_stats.py, extended to the new kwargs)."""
    with _TmpStore():
        _record_passed(models=["codex"], repo_id="github.com/org/repo")
        result = _stats.quorum_check(
            TASK,
            min_iter=0,
            min_models=1,
            repo_id="github.com/org/repo",
            diff_files=[],
        )
        assert result["passed"] is False
        assert "error" in result


# ---------------------------------------------------------------------------
# CLI level: `review diff` records repo_id/diff_files; `review task --check -C`
# end-to-end excludes a cross-repo iteration through the real dispatch path.
# ---------------------------------------------------------------------------
def test_cli_review_run_records_repo_id_and_diff_files():
    """A real `review diff` run (mocked backends, real git repo/diff) must land
    repo_id (the `path:` fallback -- the fixture repo has no remote) and
    diff_files (the one file the fixture's diff touches) in the stat record."""
    from test_run_stats import _stub_resolve_backend, _with_backend_stub

    with _TmpStore() as store:
        d = _git_init_with_diff()
        restore = _with_backend_stub(_stub_resolve_backend(0))
        log = tempfile.mkdtemp()
        os.environ["REVIEW_LOG_DIR"] = log
        try:
            err = io.StringIO()
            with redirect_stderr(err), _capture_stdout():
                rc = _cli.main(
                    ["diff", "--task", TASK, "-C", d.name, "-m", "codex,gemini"]
                )
            assert rc == 0, rc
            recs = store.records()
            assert len(recs) == 1, recs
            r = recs[0]
            assert r["repo_id"] == f"path:{Path(d.name).resolve()}", r
            assert r["diff_files"] == ["f.txt"], r
            assert "diff_sha256" in r and len(r["diff_sha256"]) == 64
        finally:
            restore()
            os.environ.pop("REVIEW_LOG_DIR", None)
            d.cleanup()


def test_cli_review_run_records_diff_files_correctly_on_noprefix_machine():
    """Regression (Fable review finding, round 3): the changelog's headline claim
    is that `_git_diff`'s `--src-prefix=a/ --dst-prefix=b/` pin fixes a LIVE bug
    on `diff.noprefix=true` machines -- but every existing noprefix test only
    exercised the CHECK-time `--name-only` path, which is immune by
    construction. This pins the actual RECORD-time path that had the live bug:
    `review diff` on a noprefix machine must still record the correct
    `diff_files`, not an empty list. Someone could drop the `_git_diff` prefix
    pin and this suite would stay green without this test."""
    import subprocess

    from test_run_stats import _stub_resolve_backend, _with_backend_stub

    with _TmpStore() as store:
        d = _git_init_with_diff()
        subprocess.run(
            ["git", "config", "diff.noprefix", "true"], cwd=d.name, check=True
        )
        restore = _with_backend_stub(_stub_resolve_backend(0))
        log = tempfile.mkdtemp()
        os.environ["REVIEW_LOG_DIR"] = log
        try:
            with redirect_stderr(io.StringIO()), _capture_stdout():
                rc = _cli.main(["diff", "--task", TASK, "-C", d.name, "-m", "codex"])
            assert rc == 0, rc
            recs = store.records()
            assert len(recs) == 1, recs
            assert recs[0]["diff_files"] == ["f.txt"], recs[0]
        finally:
            restore()
            os.environ.pop("REVIEW_LOG_DIR", None)
            d.cleanup()


def test_cli_staged_review_stamp_survives_concurrent_index_mutation():
    """End-to-end regression (round-5 review finding, k3+Opus): the pre-commit
    stamp must certify the diff the models ACTUALLY reviewed (captured at
    dispatch time), not whatever happens to be staged by the time the panel
    finishes -- even when a concurrent index mutation happens DURING the
    review (the AGENTS.md-documented shared-checkout race). Simulated by
    staging an EXTRA unrelated file from inside the stubbed backend call
    itself (standing in for a concurrent agent/session), then asserting the
    written stamp still matches the ORIGINAL staged diff, not the mutated one."""
    import subprocess

    from reviewlib.backends import ReviewResult
    from test_run_stats import _with_backend_stub

    with _TmpStore():
        d = tempfile.TemporaryDirectory()
        try:
            common = _git_common_config(d.name)
            (Path(d.name) / "f.txt").write_text("base\n")
            subprocess.run(["git", "add", "-A"], **common)
            subprocess.run(["git", "commit", "-qm", "init", "--no-verify"], **common)
            (Path(d.name) / "f.txt").write_text("base\nreviewed change\n")
            subprocess.run(["git", "add", "-A"], **common)

            # The hash of the diff as staged RIGHT NOW -- what the models see.
            original_hash = _cli._stamp_hash_for_staged_diff(Path(d.name))

            def resolver(model):
                def backend(m, prompt, diff, cwd, timeout, round_no=0, effort=None):
                    # A concurrent mutation DURING the "review" (standing in for a
                    # second agent/session in a shared checkout) -- happens AFTER
                    # dispatch captured the diff, BEFORE the stamp is written.
                    (Path(d.name) / "unrelated.py").write_text("concurrent edit\n")
                    subprocess.run(["git", "add", "-A"], **common)
                    return ReviewResult(
                        model=m, command="stub", returncode=0, stdout="ok", stderr=""
                    )

                return backend

            restore = _with_backend_stub(resolver)
            os.environ["REVIEW_LOG_DIR"] = tempfile.mkdtemp()
            try:
                with redirect_stderr(io.StringIO()), _capture_stdout():
                    rc = _cli.main(
                        [
                            "diff",
                            "--staged",
                            "--task",
                            TASK,
                            "-C",
                            d.name,
                            "-m",
                            "codex",
                        ]
                    )
                assert rc == 0, rc
                # The index DID change during the review (sanity check the fixture).
                mutated_hash = subprocess.run(
                    ["git", "-C", d.name, "diff", "--no-ext-diff", "--cached"],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout
                assert "unrelated.py" in mutated_hash

                # The stamp must certify the ORIGINALLY reviewed diff, NOT the
                # mutated one present when the stamp was actually written.
                assert _stamp_read(d.name) == original_hash
            finally:
                restore()
                os.environ.pop("REVIEW_LOG_DIR", None)
        finally:
            d.cleanup()


def _git_common_config(repo_dir: str) -> dict:
    """Env + the config calls every throwaway repo in this file needs: disable the
    global review-before-commit hook (core.hooksPath /dev/null, matching
    test_run_stats.py's _git_init_with_diff) and REVIEW_SKIP=1 for the fixture's
    OWN internal commits, which are not the diff under test."""
    import subprocess

    env = {**os.environ, "REVIEW_SKIP": "1"}
    common = dict(cwd=repo_dir, check=True, env=env)
    subprocess.run(["git", "init", "-q"], **common)
    subprocess.run(["git", "config", "core.hooksPath", "/dev/null"], **common)
    subprocess.run(["git", "config", "user.email", "t@t"], **common)
    subprocess.run(["git", "config", "user.name", "t"], **common)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], **common)
    return common


def _git_repo_ahead_of_origin_main(
    *, noprefix: bool
) -> tuple[tempfile.TemporaryDirectory, tempfile.TemporaryDirectory]:
    """A repo with `origin/main` tracked locally (via a bare "remote") and ONE
    commit ahead of it on a CLEAN working tree -- the exact `gh ship` post-push
    shape `_current_diff_files_for_check`'s default-branch fallback targets.
    `noprefix` sets `diff.noprefix=true` on the work repo to reproduce the live
    bug this fixture exists to regression-test (the fallback used to build its OWN
    unpinned `git diff`, so a noprefix machine got an empty diff_files list)."""
    import subprocess

    bare = tempfile.TemporaryDirectory()
    subprocess.run(["git", "init", "-q", "--bare", bare.name], check=True)

    work = tempfile.TemporaryDirectory()
    common = _git_common_config(work.name)
    if noprefix:
        subprocess.run(["git", "config", "diff.noprefix", "true"], **common)
    (Path(work.name) / "base.txt").write_text("base\n")
    subprocess.run(["git", "add", "-A"], **common)
    subprocess.run(["git", "commit", "-qm", "init", "--no-verify"], **common)
    subprocess.run(["git", "remote", "add", "origin", bare.name], **common)
    subprocess.run(["git", "push", "-q", "-u", "origin", "HEAD:main"], **common)
    # The PR's own commit, ahead of origin/main -- working tree is clean afterward,
    # matching the post-push `gh ship` check-time state.
    (Path(work.name) / "changed.py").write_text("print('hi')\n")
    subprocess.run(["git", "add", "-A"], **common)
    subprocess.run(["git", "commit", "-qm", "the PR's change", "--no-verify"], **common)
    return work, bare


def test_current_diff_files_for_check_default_branch_fallback_immune_to_noprefix():
    """Regression (codex/GLM/opus/fable review finding): the post-push `gh ship`
    fallback path must resolve the branch's changed files correctly EVEN on a
    machine with `diff.noprefix=true` globally set -- the exact live bug that made
    the first cut of this feature silently disable file-level matching on this
    repo's own dev machine, in precisely the fallback path this test targets."""
    work, bare = _git_repo_ahead_of_origin_main(noprefix=True)
    try:
        files = _cli._current_diff_files_for_check(Path(work.name))
        assert files == ["changed.py"], files
    finally:
        work.cleanup()
        bare.cleanup()


def test_current_diff_files_for_check_default_branch_fallback_normal_prefix():
    work, bare = _git_repo_ahead_of_origin_main(noprefix=False)
    try:
        files = _cli._current_diff_files_for_check(Path(work.name))
        assert files == ["changed.py"], files
    finally:
        work.cleanup()
        bare.cleanup()


def test_current_diff_files_for_check_covers_staged_and_unstaged_together():
    """Regression (codex/GLM/opus/fable review finding): the first cut probed
    unstaged then staged, first-non-empty-wins -- a repo with BOTH staged AND
    unstaged changes only saw whichever probe ran first. `git diff --name-only
    HEAD` (the fix) must return the UNION of both in one call."""
    import subprocess

    d = tempfile.TemporaryDirectory()
    try:
        common = _git_common_config(d.name)
        (Path(d.name) / "a.txt").write_text("base\n")
        (Path(d.name) / "b.txt").write_text("base\n")
        subprocess.run(["git", "add", "-A"], **common)
        subprocess.run(["git", "commit", "-qm", "init", "--no-verify"], **common)
        # a.txt is STAGED; b.txt is edited but left UNSTAGED.
        (Path(d.name) / "a.txt").write_text("base\nstaged change\n")
        subprocess.run(["git", "add", "a.txt"], **common)
        (Path(d.name) / "b.txt").write_text("base\nunstaged change\n")

        files = _cli._current_diff_files_for_check(Path(d.name))
        assert files == ["a.txt", "b.txt"], files
    finally:
        d.cleanup()


def _write_iteration_for_other_repo(other_repo_dir: str) -> None:
    """Record one PASSED iteration as if it reviewed `other_repo_dir` (a totally
    different repo from whatever `-C` the --check call below will use)."""
    other_repo_id = f"path:{Path(other_repo_dir).resolve()}"
    _stats.record_run(
        task_code=TASK,
        mode="review",
        models=["glm"],
        duration_seconds=1.0,
        ok_count=1,
        fail_count=0,
        passed=True,
        repo_id=other_repo_id,
        diff_files=["unrelated.py"],
    )


def test_cli_check_end_to_end_excludes_cross_repo_iteration():
    """The literal incident #1/#3 shape driven through the REAL CLI: two
    iterations were recorded (one for repo A, one for a completely different
    repo B); `review task CODE --check -C <repo A>` must count only the one that
    actually matches, print a visible warning, and (below the floor) fail."""
    from test_run_stats import _stub_resolve_backend, _with_backend_stub

    with _TmpStore() as store:
        repo_a = _git_init_with_diff()
        repo_b = _git_init_with_diff()
        restore = _with_backend_stub(_stub_resolve_backend(0))
        os.environ["REVIEW_LOG_DIR"] = tempfile.mkdtemp()
        try:
            # One real iteration for repo A, via the actual CLI dispatch path.
            with redirect_stderr(io.StringIO()), _capture_stdout():
                rc = _cli.main(
                    ["diff", "--task", TASK, "-C", repo_a.name, "-m", "codex"]
                )
            assert rc == 0, rc
            assert len(store.records()) == 1
            # One polluted iteration claiming the SAME task code but really
            # reviewed repo B (the cross-repo contamination pattern).
            _write_iteration_for_other_repo(repo_b.name)
            assert len(store.records()) == 2

            out = io.StringIO()
            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(out):
                rc = _cli.main(
                    [
                        "task",
                        TASK,
                        "--check",
                        "-C",
                        repo_a.name,
                        "--min-iter",
                        "2",
                        "--min-models",
                        "1",
                        "--json",
                    ]
                )
            # 2 raw passed iterations exist, but only 1 verifies against repo A ->
            # short of --min-iter 2 -> the bar must NOT be met.
            assert rc != 0, rc
            stderr_text = err.getvalue()
            assert "excluded 1 recorded iteration" in stderr_text
            assert json.loads(out.getvalue())["identity_verification"] == "ran"
        finally:
            restore()
            os.environ.pop("REVIEW_LOG_DIR", None)
            repo_a.cleanup()
            repo_b.cleanup()


def test_cli_check_error_branch_prints_header_and_stalled_lines_on_same_stream():
    """review-cli#221 round-4 review finding (k3/Fable): the mismatch-error branch's
    header and its `stalled:` detail lines must land on the SAME stream (both stderr,
    matching the ratio branch's both-stdout choice) -- a caller capturing only one
    stream must never see a bare header with no detail, or bare detail with no
    context. Drives the REAL CLI end to end (not just quorum_check directly, which the
    stats-level coexistence test already covers) in TEXT mode specifically, since
    --json returns before this print logic runs at all."""
    from reviewlib import seat_cooldown as _sc
    from test_run_stats import _stub_resolve_backend, _with_backend_stub

    with _TmpStore():
        repo_a = _git_init_with_diff()
        repo_b = _git_init_with_diff()
        restore = _with_backend_stub(_stub_resolve_backend(0))
        os.environ["REVIEW_LOG_DIR"] = tempfile.mkdtemp()
        saved_cd_file = os.environ.get("REVIEW_SEAT_COOLDOWN_FILE")
        saved_cd_ttl = os.environ.get("REVIEW_SEAT_COOLDOWN_SECONDS")
        cd_dir = tempfile.mkdtemp()
        os.environ["REVIEW_SEAT_COOLDOWN_FILE"] = str(
            Path(cd_dir) / "seat-cooldown.json"
        )
        os.environ.pop("REVIEW_SEAT_COOLDOWN_SECONDS", None)
        try:
            # One real, matching iteration for repo A -- not enough alone (min-iter 2).
            with redirect_stderr(io.StringIO()), _capture_stdout():
                rc = _cli.main(
                    ["diff", "--task", TASK, "-C", repo_a.name, "-m", "codex"]
                )
            assert rc == 0, rc
            # A cross-repo polluted PASSED iteration -- drives the mismatch-error path.
            _write_iteration_for_other_repo(repo_b.name)
            # A separate, real (unpassed) ATTEMPT at THIS task/repo by the cooling
            # seat -- attempted_models scoping requires an actual recorded iteration,
            # not just a cooldown entry (see the sibling stats-level test above).
            _stats.record_run(
                task_code=TASK,
                mode="review",
                models=["oc:zai/glm-5.2"],
                duration_seconds=1.0,
                ok_count=0,
                fail_count=1,
                passed=False,
                repo_id=f"path:{Path(repo_a.name).resolve()}",
            )
            _sc.record_cooldown("oc:zai/glm-5.2", "timed out", now=time.time())

            out = io.StringIO()
            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(out):
                rc = _cli.main(
                    [
                        "task",
                        TASK,
                        "--check",
                        "-C",
                        repo_a.name,
                        "--min-iter",
                        "2",
                        "--min-models",
                        "1",
                    ]
                )
            assert rc != 0, rc
            stdout_text = out.getvalue()
            stderr_text = err.getvalue()
            assert "review bar NOT met" in stderr_text
            assert "stalled: oc:zai/glm-5.2" in stderr_text
            # Neither line leaked onto stdout -- a stdout-only capture would otherwise
            # see the bare, contextless `stalled:` line the finding described.
            assert "review bar NOT met" not in stdout_text
            assert "stalled:" not in stdout_text
        finally:
            restore()
            os.environ.pop("REVIEW_LOG_DIR", None)
            if saved_cd_file is None:
                os.environ.pop("REVIEW_SEAT_COOLDOWN_FILE", None)
            else:
                os.environ["REVIEW_SEAT_COOLDOWN_FILE"] = saved_cd_file
            if saved_cd_ttl is None:
                os.environ.pop("REVIEW_SEAT_COOLDOWN_SECONDS", None)
            else:
                os.environ["REVIEW_SEAT_COOLDOWN_SECONDS"] = saved_cd_ttl
            repo_a.cleanup()
            repo_b.cleanup()


def test_cli_check_no_verify_identity_restores_legacy_count():
    """`--no-verify-identity` is the documented escape hatch: with it, the
    cross-repo polluted iteration from the test above counts again exactly like
    pre-v4 review-cli -- proving the flag is wired end to end, not just parsed."""
    with _TmpStore() as store:
        repo_a = _git_init_with_diff()
        repo_b = _git_init_with_diff()
        try:
            _stats.record_run(
                task_code=TASK,
                mode="review",
                models=["codex"],
                duration_seconds=1.0,
                ok_count=1,
                fail_count=0,
                passed=True,
                repo_id=f"path:{Path(repo_a.name).resolve()}",
                diff_files=["f.txt"],
            )
            _write_iteration_for_other_repo(repo_b.name)
            assert len(store.records()) == 2

            out = io.StringIO()
            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(out):
                rc = _cli.main(
                    [
                        "task",
                        TASK,
                        "--check",
                        "-C",
                        repo_a.name,
                        "--no-verify-identity",
                        "--min-iter",
                        "2",
                        "--min-models",
                        "2",
                        "--json",
                    ]
                )
            assert rc == 0, rc
            payload = json.loads(out.getvalue())
            assert payload["passed"] is True
            assert payload["passed_iterations"] == 2
            # Legacy shape: no diagnostic keys when verification is disabled.
            assert "excluded_mismatched_iterations" not in payload
            # Review finding: "verified" must never be silently indistinguishable
            # from "verification never ran" -- a visible warning AND a
            # machine-readable JSON field either way (round-3 review finding:
            # stderr alone isn't parseable by a --json-only caller like gh ship).
            assert "verification disabled" in err.getvalue()
            assert payload["identity_verification"] == "disabled"
        finally:
            repo_a.cleanup()
            repo_b.cleanup()


def test_cli_check_unresolvable_cwd_warns_and_falls_back_to_legacy():
    """Review finding: a `-C` that isn't even a real directory must NOT silently
    fall back to legacy counting -- it must warn on stderr just like
    --no-verify-identity does, so a machine caller can tell "verified" apart from
    "verification never ran" either way."""
    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(out):
            rc = _cli.main(
                [
                    "task",
                    TASK,
                    "--check",
                    "-C",
                    "/definitely/does/not/exist/xyz",
                    "--min-iter",
                    "1",
                    "--min-models",
                    "1",
                    "--json",
                ]
            )
        assert rc == 0, rc  # the one legacy (no-repo_id) record still counts
        assert "could not resolve a repo" in err.getvalue()
        assert "falling back to legacy counting" in err.getvalue()
        # Round-3 review finding: a --json-only caller (never sees stderr) needs
        # a machine-readable field to tell "verification ran" apart from this.
        assert (
            json.loads(out.getvalue())["identity_verification"]
            == "skipped_unresolvable"
        )


def test_mismatch_details_diff_mismatch_includes_recorded_diff_files():
    """Review finding: `recorded_repo_id` alone tells an operator nothing about WHY
    a same-repo iteration was excluded as `diff_mismatch` (the repo matched by
    definition) -- `recorded_diff_files` must be present so the actual evidence is
    visible, not just the (uninformative, matching) repo id."""
    with _TmpStore():
        repo = "github.com/hyperide/hyper-ext-e2e"
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
            repo_id=repo,
            diff_files=["billing.ts"],
        )
        result = _stats.quorum_check(
            TASK, min_iter=1, min_models=1, repo_id=repo, diff_files=["auth.ts"]
        )
        assert result["excluded_mismatched_iterations"] == 1
        detail = result["mismatch_details"][0]
        assert detail["reason"] == "diff_mismatch"
        assert detail["recorded_diff_files"] == ["billing.ts"]
        # Review finding (Opus/Fable, round 2): no test asserted the documented
        # `"iteration": int` shape wasn't actually always null -- pin it concretely.
        assert detail["iteration"] == 1
        assert isinstance(detail["iteration"], int)


def test_quorum_check_mismatch_details_capped_but_count_stays_exact():
    """Review finding (GLM): a task with thousands of polluted iterations (the
    exact HYP-858 shape) must not balloon --check --json into an unbounded
    payload. The DETAIL LIST is capped; excluded_mismatched_iterations (the count
    the gate math and a machine caller like gh ship actually rely on) is always
    the TRUE total, never truncated."""
    with _TmpStore():
        repo = "github.com/hyperide/hyper-ext-e2e"
        n = _stats._MISMATCH_DETAILS_CAP + 5
        for i in range(n):
            _stats.record_run(
                task_code=TASK,
                mode="review",
                models=[f"model-{i}"],
                duration_seconds=1.0,
                ok_count=1,
                fail_count=0,
                passed=True,
                repo_id=f"github.com/other-org/repo-{i}",
            )
        result = _stats.quorum_check(
            TASK, min_iter=1, min_models=1, repo_id=repo, diff_files=[]
        )
        assert result["excluded_mismatched_iterations"] == n
        assert len(result["mismatch_details"]) == _stats._MISMATCH_DETAILS_CAP
        assert result["mismatch_details_truncated"] is True


def test_current_diff_files_for_check_unions_dirty_tree_with_branch_diff():
    """Regression (Opus + Fable review finding, round 2 -- the highest-severity
    finding this feature's own dogfooding caught): the post-push `gh ship` case
    with an UNRELATED dirty file present must NOT let that stray file shadow the
    branch's real PR diff. A first cut returned first-non-empty-wins (HEAD probe
    before the branch fallback), so a single stray edit made every legitimate
    iteration look like a diff_mismatch and spuriously blocked a real merge. The
    fix returns the UNION -- the branch's real files must still be present even
    with unrelated local dirt around."""
    work, bare = _git_repo_ahead_of_origin_main(noprefix=False)
    try:
        # An UNRELATED stray edit, present at check time (a version bump in
        # progress, a config tweak) -- the same "clean tree" precondition the
        # first cut of the fallback silently assumed.
        (Path(work.name) / "base.txt").write_text("base\nstray unrelated edit\n")
        files = _cli._current_diff_files_for_check(Path(work.name))
        # The real branch file must still be present (not shadowed by the dirt),
        # so a recorded iteration touching ONLY "changed.py" still verifies.
        assert "changed.py" in files, files
        assert "base.txt" in files, files  # the union also includes the stray file
    finally:
        work.cleanup()
        bare.cleanup()


# ---------------------------------------------------------------------------
# _write_review_stamp: must stay hash-compatible with the pre-commit hook's own
# INDEPENDENT, unprefixed `git diff --no-ext-diff --cached` recomputation, even
# though `_git_diff` (used to build the reviewed `diff` text) now pins
# `--src-prefix=a/ --dst-prefix=b/`. This is a real regression this PR's own
# `_git_diff` fix caused and caught live: on a `diff.noprefix=true` machine
# (this repo's own dev machine), hashing the PREFIXED `diff` string no longer
# matched the hook's UNPREFIXED recomputation, permanently blocking every
# commit's own pre-commit gate.
# ---------------------------------------------------------------------------
def test_write_review_stamp_matches_hook_hash_despite_prefixed_diff():
    import subprocess

    from reviewlib.install import _write_review_stamp

    d = tempfile.TemporaryDirectory()
    try:
        common = _git_common_config(d.name)
        subprocess.run(
            ["git", "config", "diff.noprefix", "true"], **common
        )  # the exact live bug's precondition
        (Path(d.name) / "f.txt").write_text("base\n")
        subprocess.run(["git", "add", "-A"], **common)
        subprocess.run(["git", "commit", "-qm", "init", "--no-verify"], **common)
        (Path(d.name) / "f.txt").write_text("base\nstaged change\n")
        subprocess.run(["git", "add", "-A"], **common)

        # What the HOOK independently computes (unprefixed, per its own script).
        hook_diff = subprocess.run(
            ["git", "-C", d.name, "diff", "--no-ext-diff", "--cached"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        hook_hash = __import__("hashlib").sha256(hook_diff.encode()).hexdigest()

        # What review-cli's OWN reviewed diff looks like on this noprefix machine
        # (prefixed, per `_git_diff`'s pin) -- deliberately DIFFERENT text from
        # `hook_diff` above, which is exactly the regression scenario.
        reviewed_diff = _cli._git_diff(Path(d.name), staged=True)
        assert reviewed_diff != hook_diff, "fixture didn't reproduce the divergence"

        _write_review_stamp(Path(d.name), reviewed_diff)
        stamp_path_out = subprocess.run(
            ["git", "-C", d.name, "rev-parse", "--git-path", "review-stamp"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        stamp_path = (
            Path(stamp_path_out)
            if os.path.isabs(stamp_path_out)
            else Path(d.name) / stamp_path_out
        )
        written_hash = stamp_path.read_text().strip()
        assert written_hash == hook_hash, (written_hash, hook_hash)
    finally:
        d.cleanup()


def _stamp_read(d: str) -> str:
    import subprocess as sp

    out = sp.run(
        ["git", "-C", d, "rev-parse", "--git-path", "review-stamp"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    stamp_path = Path(out) if os.path.isabs(out) else Path(d) / out
    return stamp_path.read_text().strip()


def test_write_review_stamp_prefers_caller_supplied_hash_over_recompute():
    """Regression (round-5 review finding, k3+Opus): a caller-supplied
    `stamp_diff_hash` must be used AS-IS, never overridden by a fresh recompute
    -- this is the TOCTOU fix itself. Proven by deliberately passing a hash that
    does NOT match the CURRENT index (simulating "the index changed after the
    diff was captured but before the stamp was written") and confirming the
    caller-supplied value wins, not a value that happens to match current state."""
    import subprocess

    from reviewlib.install import _write_review_stamp

    d = tempfile.TemporaryDirectory()
    try:
        common = _git_common_config(d.name)
        (Path(d.name) / "f.txt").write_text("base\n")
        subprocess.run(["git", "add", "-A"], **common)
        subprocess.run(["git", "commit", "-qm", "init", "--no-verify"], **common)
        (Path(d.name) / "f.txt").write_text("base\nstaged change\n")
        subprocess.run(["git", "add", "-A"], **common)

        deliberately_wrong_hash = "0" * 64  # would NOT match any real diff
        _write_review_stamp(
            Path(d.name),
            "irrelevant diff text",
            stamp_diff_hash=deliberately_wrong_hash,
        )
        assert _stamp_read(d.name) == deliberately_wrong_hash
    finally:
        d.cleanup()


def test_stamp_hash_for_staged_diff_matches_hook_recompute():
    """`cli._stamp_hash_for_staged_diff` (captured at dispatch time, the value
    threaded into `stamp_diff_hash`) must itself equal the hook's own
    recomputation -- proves the end-to-end wiring, not just `_write_review_stamp`
    in isolation."""
    import subprocess

    d = tempfile.TemporaryDirectory()
    try:
        common = _git_common_config(d.name)
        subprocess.run(["git", "config", "diff.noprefix", "true"], **common)
        (Path(d.name) / "f.txt").write_text("base\n")
        subprocess.run(["git", "add", "-A"], **common)
        subprocess.run(["git", "commit", "-qm", "init", "--no-verify"], **common)
        (Path(d.name) / "f.txt").write_text("base\nstaged change\n")
        subprocess.run(["git", "add", "-A"], **common)

        hook_diff = subprocess.run(
            ["git", "-C", d.name, "diff", "--no-ext-diff", "--cached"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        hook_hash = __import__("hashlib").sha256(hook_diff.encode()).hexdigest()

        assert _cli._stamp_hash_for_staged_diff(Path(d.name)) == hook_hash
    finally:
        d.cleanup()


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
    print(f"\n{'FAILED' if failures else 'OK'}: {failures} failure(s)")
    sys.exit(1 if failures else 0)
