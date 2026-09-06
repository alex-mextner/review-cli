"""Unit tests for review_opencode running in the REAL repo, read-only (Task 2a).

The point of the change: opencode used to run in an empty `tempfile.TemporaryDirectory`
+ `git init`, so it only ever saw the diff embedded in the prompt — same blindness as
the raw-API seats. Now it runs in the REAL `cwd` (like `codex exec -C <cwd>`), under the
read-only-reviewer agent (bash/edit/write/webfetch DENIED), so it can READ any project
file while never mutating the repo.

These tests pin the dispatch contract WITHOUT a live opencode (non-deterministic): we
patch BOTH `_run_streamed` (to capture the argv + cwd the backend would launch) AND
`_which` (so the tests are HERMETIC — they pass on a CI box with no `opencode` binary on
PATH, which is the whole point of mocking).
  * inside a git repo  -> opencode runs in the REAL cwd, with `--dir <cwd>`;
  * outside a git repo -> falls back to an isolated empty temp dir (nothing to read);
  * the read-only agent (no write/edit/bash) is what keeps the real-repo run SAFE.

Mock harness style mirrors tests/test_streaming.py.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import backends as review_backends  # noqa: E402


class _FakeProc:
    def __init__(
        self,
        returncode=0,
        stdout="ok",
        stderr="",
        timeout_kind=None,
        stall_bound_clamped=False,
    ):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timeout_kind = timeout_kind
        self.stall_bound_clamped = stall_bound_clamped


def _stalled_proc() -> _FakeProc:
    """A `_run_streamed`-shaped result matching what `_opencode_call_stalled` looks for
    (review-cli#153/#159/#179) -- the real `timeout_kind` attribute `_run_streamed`
    stamps on a result that timed out via the liveness/stall path, not a text marker."""
    return _FakeProc(
        returncode=124,
        stdout="\n[review-cli] TIMEOUT after 300s waiting for first output — partial output above]\n",
        timeout_kind="waiting for first output",
    )


class _Captured:
    """Stand-in for `_run_streamed`; records EVERY call (so a retry-loop test can
    inspect each attempt) and its argv/cwd/etc. Returns a queued sequence of responses
    (popped in order, the last one repeats once exhausted) so a test can script a
    stall-then-succeed or stall-x3 scenario. Defaults to always-success, matching every
    pre-existing test's expectation of a single successful call."""

    def __init__(self, responses=None) -> None:
        self._responses = list(responses) if responses else [_FakeProc()]
        self.calls: list[dict] = []
        self.argv: list[str] | None = None
        self.cwd: Path | None = None
        self.timeout: int | None = None
        self.header_argv0: str | None = None
        self.liveness_timeout: int | None = None
        self.true_silence_timeout: int | None = None

    def __call__(
        self,
        argv,
        cwd,
        timeout,
        backend,
        round_no=0,
        announce=False,
        header_argv0=None,
        liveness_timeout=None,
        true_silence_timeout=None,
    ):
        self.true_silence_timeout = true_silence_timeout
        self.argv = list(argv)
        self.cwd = Path(cwd)
        self.timeout = timeout
        self.header_argv0 = header_argv0
        self.liveness_timeout = liveness_timeout
        self.calls.append(
            {"argv": list(argv), "cwd": Path(cwd), "liveness_timeout": liveness_timeout}
        )
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


@contextlib.contextmanager
def _capture_opencode(responses=None):
    """Patch `_run_streamed` (capture, optionally scripted via `responses`), `_which`
    (hermetic — no real opencode binary needed), `_ensure_opencode_readonly_agent` (so
    the unit tests do NOT touch the developer's / CI's global ~/.config/opencode), AND
    the cooldown/stall env vars (review_opencode now consults `seat_cooldown` before
    every dispatch — without redirecting $REVIEW_SEAT_COOLDOWN_FILE too, a test run
    would read/write the developer's real ~/.config/review-cli/seat-cooldown.json,
    exactly the leak `test_seat_cooldown.py`'s own `_with_store` helper exists to
    prevent; and without CLEARING $REVIEW_SEAT_COOLDOWN_SECONDS /
    $REVIEW_OPENCODE_STALL_SECONDS, a developer/CI environment that happens to export
    either -- e.g. set to 0 to disable a cooldown/stall check for some other purpose --
    would make these cooldown/liveness assertions fail, codex review finding). Restores
    everything afterward — a single restore point means a typo can't leak the mock into
    sibling tests (board feedback)."""
    cap = _Captured(responses)
    orig_run = review_backends._run_streamed
    orig_which = review_backends._which
    orig_ensure = review_backends._ensure_opencode_readonly_agent
    saved_env = {
        k: os.environ.get(k)
        for k in (
            "REVIEW_SEAT_COOLDOWN_FILE",
            "REVIEW_SEAT_COOLDOWN_SECONDS",
            "REVIEW_OPENCODE_STALL_SECONDS",
            "REVIEW_OPENCODE_STALL_MODELS",
        )
    }
    review_backends._run_streamed = cap  # type: ignore[assignment]
    review_backends._which = lambda name: f"/fake/bin/{name}"  # type: ignore[assignment]
    review_backends._ensure_opencode_readonly_agent = lambda *_a, **_k: None  # type: ignore[assignment]
    os.environ.pop("REVIEW_SEAT_COOLDOWN_SECONDS", None)
    os.environ.pop("REVIEW_OPENCODE_STALL_SECONDS", None)
    os.environ.pop("REVIEW_OPENCODE_STALL_MODELS", None)
    with tempfile.TemporaryDirectory() as cooldown_dir:
        os.environ["REVIEW_SEAT_COOLDOWN_FILE"] = str(
            Path(cooldown_dir) / "seat-cooldown.json"
        )
        try:
            yield cap
        finally:
            review_backends._run_streamed = orig_run
            review_backends._which = orig_which
            review_backends._ensure_opencode_readonly_agent = orig_ensure
            for key, saved in saved_env.items():
                if saved is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = saved


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)


def test_runs_in_real_repo_with_dir_flag():
    with _capture_opencode() as cap:
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            _git_init(repo)
            res = review_backends.review_opencode(
                "oc:opencode/deepseek-v4-flash-free",
                "Review.",
                "some diff",
                repo,
                60,
            )
        assert res.returncode == 0, res
        argv = cap.argv or []
        # opencode was pointed at the REAL repo, not a temp dir.
        assert "--dir" in argv, argv
        dir_value = argv[argv.index("--dir") + 1]
        assert Path(dir_value) == repo, (dir_value, repo)
        # the subprocess cwd is the real repo too.
        assert cap.cwd == repo, (cap.cwd, repo)
        # still the read-only-reviewer agent (the safety boundary).
        assert "read-only-reviewer" in argv, argv
        # The sidecar log header carries the model SELECTOR (not the bare binary path), so
        # the dashboard attributes the call to its `oc:` board seat (review-cli#24). It is
        # the `oc_model` (everything after `oc:`), and must NOT carry the prompt/diff.
        assert cap.header_argv0 == "opencode -m opencode/deepseek-v4-flash-free", (
            cap.header_argv0
        )
        assert "some diff" not in (cap.header_argv0 or ""), cap.header_argv0


def test_real_repo_message_invites_reading_files():
    with _capture_opencode() as cap:
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            _git_init(repo)
            review_backends.review_opencode("oc:m", "Review.", "DIFFBODY", repo, 60)
        message = (cap.argv or [])[-1]
        # The model is told it can read project files, AND the diff is the focus.
        assert "read" in message.lower(), message
        assert "DIFFBODY" in message, message


def test_glm52_opencode_keeps_requested_timeout_like_other_models():
    with _capture_opencode() as cap:
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            _git_init(repo)
            review_backends.review_opencode(
                "oc:zai/glm-5.2", "Review.", "DIFF", repo, 1200
            )
        assert cap.timeout == 1200, cap.timeout


def test_opencode_seat_passes_a_stall_bound_distinct_from_the_full_timeout():
    """review-cli#153/#159/#179: opencode's zai/glm seat hangs at 0% CPU with ZERO
    output when the provider's quota is exhausted. review_opencode must ask
    `_run_streamed` for a stall (liveness) bound that is much shorter than the full
    call timeout, so a dead seat is detected in minutes, not the whole requested
    timeout."""
    with _capture_opencode() as cap:
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            _git_init(repo)
            review_backends.review_opencode(
                "oc:zai/glm-5.2", "Review.", "DIFF", repo, 1200
            )
        assert cap.liveness_timeout is not None, "no stall bound was requested"
        assert cap.liveness_timeout < cap.timeout, (cap.liveness_timeout, cap.timeout)


def test_opencode_stall_bound_also_applies_outside_a_repo():
    """The temp-dir fallback path (non-repo cwd) must get the same stall bound as
    the real-repo path -- both go through the same hung-subprocess failure mode."""
    with _capture_opencode() as cap:
        with tempfile.TemporaryDirectory() as d:
            plain = Path(d) / "scratch"
            plain.mkdir()  # NOT a git repo
            review_backends.review_opencode(
                "oc:zai/glm-5.2", "Answer.", "", plain, 1200
            )
        assert cap.liveness_timeout is not None, "no stall bound was requested"


def test_opencode_stall_seconds_env_override():
    """$REVIEW_OPENCODE_STALL_SECONDS overrides the default (5 minutes); <=0 disables
    the check, mirroring $REVIEW_IDLE_TIMEOUT_SECONDS's own convention."""
    saved = os.environ.get("REVIEW_OPENCODE_STALL_SECONDS")
    try:
        os.environ["REVIEW_OPENCODE_STALL_SECONDS"] = "45"
        assert review_backends._opencode_stall_seconds() == 45
        os.environ["REVIEW_OPENCODE_STALL_SECONDS"] = "0"
        assert review_backends._opencode_stall_seconds() is None
        os.environ["REVIEW_OPENCODE_STALL_SECONDS"] = "-5"
        assert review_backends._opencode_stall_seconds() is None
        os.environ.pop("REVIEW_OPENCODE_STALL_SECONDS")
        assert (
            review_backends._opencode_stall_seconds()
            == review_backends._OPENCODE_DEFAULT_STALL_SECONDS
        )
        assert review_backends._OPENCODE_DEFAULT_STALL_SECONDS == 300
    finally:
        if saved is None:
            os.environ.pop("REVIEW_OPENCODE_STALL_SECONDS", None)
        else:
            os.environ["REVIEW_OPENCODE_STALL_SECONDS"] = saved


# ---- stall retry + cooldown (Alex, 2026-08-14 design directive) ---------------------------
def test_opencode_retries_a_stall_and_succeeds_on_a_later_attempt():
    """A transient stall (e.g. a network hiccup) must not sideline the seat -- it gets
    retried up to `_OPENCODE_MAX_STALL_RETRIES` times before being treated as dead."""
    responses = [
        _stalled_proc(),
        _stalled_proc(),
        _FakeProc(returncode=0, stdout="real answer"),
    ]
    with _capture_opencode(responses) as cap:
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            _git_init(repo)
            result = review_backends.review_opencode(
                "oc:zai/glm-5.2", "Review.", "DIFF", repo, 1200
            )
        assert len(cap.calls) == 3, cap.calls
        assert result.returncode == 0, result
        assert result.stdout == "real answer", result
        # No cooldown recorded -- the seat eventually answered.
        from reviewlib.seat_cooldown import active_cooldown

        assert active_cooldown("oc:zai/glm-5.2", access_method="opencode") is None


def test_opencode_exhausting_stall_retries_records_a_cooldown():
    """After `_OPENCODE_MAX_STALL_RETRIES` consecutive stalls, the seat is cooled down
    (so the NEXT invocation skips the real dispatch) and the call still returns a
    bounded (stalled) result -- never hangs the caller."""
    assert review_backends._OPENCODE_MAX_STALL_RETRIES == 3
    responses = [_stalled_proc(), _stalled_proc(), _stalled_proc()]
    with _capture_opencode(responses) as cap:
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            _git_init(repo)
            result = review_backends.review_opencode(
                "oc:zai/glm-5.2", "Review.", "DIFF", repo, 1200
            )
        assert len(cap.calls) == 3, cap.calls
        assert result.returncode == 124, result
        from reviewlib.seat_cooldown import active_cooldown

        cooldown = active_cooldown("oc:zai/glm-5.2", access_method="opencode")
        assert cooldown is not None, "no cooldown was recorded after exhausting retries"
        assert "stalled" in cooldown["reason"], cooldown
        # Exactly ONE record for the whole 3-attempt cycle -- a second writer (e.g.
        # `_record_true_silence_if_needed` also matching the stalled proc) would
        # escalate this to 2 (round-2 review finding, Fable).
        assert cooldown["fail_count"] == 1, cooldown


def test_clamped_stall_is_not_retried_and_records_no_cooldown():
    """A stall whose liveness bound was clamped below the requested window (board
    deadline / small idle window -- `stall_bound_clamped=True` on the result) is an
    honest bounded failure, NOT the quota-exhaustion signature: no retry, no cooldown
    (round-2 review finding, Fable: under deadline pressure a merely slow-to-first-byte
    seat would otherwise rack up three sub-minute "stalls" and be benched)."""
    clamped = _FakeProc(
        returncode=124,
        stdout="\n[review-cli] TIMEOUT after 40s waiting for first output]\n",
        timeout_kind="waiting for first output",
        stall_bound_clamped=True,
    )
    with _capture_opencode([clamped, clamped, clamped]) as cap:
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            _git_init(repo)
            result = review_backends.review_opencode(
                "oc:zai/glm-5.2", "Review.", "DIFF", repo, 1200
            )
        assert len(cap.calls) == 1, cap.calls
        assert result.returncode == 124, result
        from reviewlib.seat_cooldown import active_cooldown

        assert active_cooldown("oc:zai/glm-5.2", access_method="opencode") is None


def test_stall_seconds_zero_keeps_true_silence_for_watched_model():
    """$REVIEW_OPENCODE_STALL_SECONDS=0 disables the liveness watchdog; the watched
    model must then fall back to the registry-driven true-silence bound instead of
    losing zero-output protection entirely (round-2 review finding, Fable)."""
    from reviewlib import model_behavior

    with _capture_opencode() as cap:
        os.environ["REVIEW_OPENCODE_STALL_SECONDS"] = "0"
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            _git_init(repo)
            review_backends.review_opencode(
                "oc:zai/glm-5.2", "Review.", "DIFF", repo, 1200
            )
        assert cap.liveness_timeout is None
        assert cap.true_silence_timeout == model_behavior.true_silence_timeout_seconds(
            "oc:zai/glm-5.2"
        )
        assert cap.true_silence_timeout is not None


def test_stall_watchdog_owns_zero_output_for_watched_model():
    """For a WATCHED model the liveness watchdog must be the SOLE zero-output detector:
    `true_silence_timeout` is NOT forwarded to `_run_streamed` (round-1 review finding,
    Opus + Fable: the registry's true-silence value for zai/glm equals the 300s stall
    default, so without this the "retry 3x then cooldown" policy vs "cooldown on first
    silence" was decided by poll-loop check order). An UNWATCHED opencode seat keeps
    the registry-driven true-silence bound exactly as before."""
    from reviewlib import model_behavior

    with _capture_opencode() as cap:
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            _git_init(repo)
            review_backends.review_opencode(
                "oc:zai/glm-5.2", "Review.", "DIFF", repo, 1200
            )
            assert cap.liveness_timeout == review_backends._OPENCODE_DEFAULT_STALL_SECONDS
            assert cap.true_silence_timeout is None, cap.true_silence_timeout
            unwatched = "oc:moonshotai/kimi-k2.5"
            assert not review_backends._opencode_model_needs_stall_watchdog(unwatched)
            review_backends.review_opencode(unwatched, "Review.", "DIFF", repo, 1200)
            assert cap.liveness_timeout is None
            assert cap.true_silence_timeout == model_behavior.true_silence_timeout_seconds(
                unwatched
            )
            assert cap.true_silence_timeout is not None


def test_opencode_skips_real_dispatch_while_cooling_down():
    """Once a cooldown is active for (model, opencode), review_opencode must return a
    synthetic skip WITHOUT spawning any real opencode call."""
    with _capture_opencode() as cap:
        from reviewlib.seat_cooldown import record_cooldown

        record_cooldown(
            "oc:zai/glm-5.2",
            "opencode stalled with no output after 3 attempts",
            ttl_seconds=600.0,
            access_method="opencode",
        )
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            _git_init(repo)
            result = review_backends.review_opencode(
                "oc:zai/glm-5.2", "Review.", "DIFF", repo, 1200
            )
        assert cap.calls == [], "real dispatch was NOT skipped while cooling down"
        assert result.returncode == 0, result
        assert "is currently unavailable" in result.stdout, result
        assert "cached:" in result.stdout, result


def test_opencode_cooldown_does_not_affect_other_opencode_models():
    """A cooldown recorded for the zai/glm seat must not skip a DIFFERENT opencode
    model's dispatch -- the cooldown key includes the model, not just the access
    method."""
    with _capture_opencode() as cap:
        from reviewlib.seat_cooldown import record_cooldown

        record_cooldown(
            "oc:zai/glm-5.2",
            "opencode stalled",
            ttl_seconds=600.0,
            access_method="opencode",
        )
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            _git_init(repo)
            result = review_backends.review_opencode(
                "oc:opencode/deepseek-v4-flash-free", "Review.", "DIFF", repo, 1200
            )
        assert len(cap.calls) == 1, "an unrelated model's dispatch was wrongly skipped"
        assert result.returncode == 0, result


def test_other_opencode_models_keep_requested_timeout():
    with _capture_opencode() as cap:
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            _git_init(repo)
            review_backends.review_opencode(
                "oc:commandcode/deepseek/deepseek-v4-pro", "Review.", "DIFF", repo, 1200
            )
        assert cap.timeout == 1200, cap.timeout


def test_other_opencode_models_get_no_stall_watchdog():
    """codex review finding, round 2: the default board runs Kimi/Qwen/DeepSeek
    AGENTIC seats through opencode besides zai/glm -- there is no evidence any of them
    share the zero-output hang failure mode, so the stall/liveness bound (and its
    retry-then-cooldown behavior) must stay scoped to the confirmed-bad route, not
    blanket-applied to every opencode dispatch."""
    for model in (
        "oc:commandcode/deepseek/deepseek-v4-pro",
        "oc:commandcode/Qwen/Qwen3.7-Max",
        "oc:kimi-code/k3",
    ):
        with _capture_opencode() as cap:
            with tempfile.TemporaryDirectory() as d:
                repo = Path(d) / "repo"
                repo.mkdir()
                _git_init(repo)
                review_backends.review_opencode(model, "Review.", "DIFF", repo, 1200)
            assert cap.liveness_timeout is None, (model, cap.liveness_timeout)
            assert len(cap.calls) == 1, (model, cap.calls)


def test_opencode_model_needs_stall_watchdog_matcher():
    assert review_backends._opencode_model_needs_stall_watchdog("oc:zai/glm-5.2")
    assert not review_backends._opencode_model_needs_stall_watchdog(
        "oc:commandcode/deepseek/deepseek-v4-pro"
    )
    saved = os.environ.get("REVIEW_OPENCODE_STALL_MODELS")
    try:
        os.environ["REVIEW_OPENCODE_STALL_MODELS"] = "some-other-model"
        assert not review_backends._opencode_model_needs_stall_watchdog(
            "oc:zai/glm-5.2"
        )
        assert review_backends._opencode_model_needs_stall_watchdog(
            "oc:some-other-model"
        )
    finally:
        if saved is None:
            os.environ.pop("REVIEW_OPENCODE_STALL_MODELS", None)
        else:
            os.environ["REVIEW_OPENCODE_STALL_MODELS"] = saved


def test_non_repo_cwd_falls_back_to_temp_dir():
    with _capture_opencode() as cap:
        with tempfile.TemporaryDirectory() as d:
            plain = Path(d) / "scratch"
            plain.mkdir()  # NOT a git repo
            review_backends.review_opencode("oc:m", "Answer.", "", plain, 60)
        argv = cap.argv or []
        # No --dir into the scratch dir; the run is isolated in a temp dir instead.
        assert "--dir" not in argv, argv
        assert cap.cwd is not None and cap.cwd != plain, cap.cwd
        # The fallback path also stamps the model selector header for dashboard attribution.
        assert cap.header_argv0 == "opencode -m m", cap.header_argv0


def test_repo_detection_helper():
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d) / "repo"
        repo.mkdir()
        _git_init(repo)
        assert review_backends._opencode_runs_in_repo(repo) is True
        plain = Path(d) / "plain"
        plain.mkdir()
        assert review_backends._opencode_runs_in_repo(plain) is False
        missing = Path(d) / "nope"
        assert review_backends._opencode_runs_in_repo(missing) is False


def test_real_repo_run_does_not_write_anything_into_repo():
    # The OLD temp-dir path wrote a `review.diff` into its scratch dir. The real-repo
    # path must NOT create ANY file ANYWHERE under the user's repo (read-only safety):
    # the diff travels in the prompt only, and `_ensure_opencode_readonly_agent` writes
    # the agent to the GLOBAL ~/.config/opencode, never the worktree. Snapshot the whole
    # tree recursively (not just the top level) so a stray `.opencode/…` would be caught.
    def _tree(root: Path) -> set[str]:
        return {str(p.relative_to(root)) for p in root.rglob("*")}

    with _capture_opencode():
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            _git_init(repo)
            before = _tree(repo)
            review_backends.review_opencode("oc:m", "Review.", "DIFF", repo, 60)
            after = _tree(repo)
        assert before == after, after - before  # no files added anywhere in the repo


def test_repo_with_opencode_config_is_not_run_agentically():
    # SECURITY: a reviewed repo that ships its own opencode config (.opencode/ or
    # opencode.json/jsonc) could redefine the read-only agent and re-enable write/bash.
    # Such a repo must NOT run agentically (no --dir into it) — it falls back to the
    # isolated temp-dir, diff-only posture. Verified live that the project agent DOES
    # override the global one, so this guard is the real mitigation.
    for marker in (".opencode", "opencode.json", "opencode.jsonc"):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            _git_init(repo)
            target = repo / marker
            if marker == ".opencode":
                target.mkdir()
            else:
                target.write_text("{}", encoding="utf-8")
            assert review_backends._opencode_runs_in_repo(repo) is False, marker
            # And the backend actually runs WITHOUT --dir for such a repo.
            with _capture_opencode() as cap:
                review_backends.review_opencode("oc:m", "Review.", "DIFF", repo, 60)
            assert "--dir" not in (cap.argv or []), (marker, cap.argv)


def test_show_board_scope_label_tracks_cwd_for_opencode():
    # The `--show-board` agentic/diff-only label for an opencode seat must mirror
    # review_opencode's own cwd check: agentic inside a real repo, diff-only outside.
    # `_seat_reads_repo` takes the precomputed repo bit (the caller resolves it once).
    from reviewlib.cli import _seat_reads_repo  # noqa: PLC0415

    # opencode: agentic iff cwd is a real repo.
    assert _seat_reads_repo("oc:opencode/m", True) is True
    assert _seat_reads_repo("oc:opencode/m", False) is False
    # codex is the agentic route every `codex:<model>` seat uses (Sol at #1, Astra at #5 —
    # GLM review finding, review-cli#286 round 4: the #5 slot was #6 before Fable's
    # demotion moved every subsequent seat up one slot — NOT "#3"/"two slots" as an
    # earlier fix here wrongly claimed) — it reads the whole repo (the diff-only
    # `commandcode:gpt-5.5` route for a codex-family model was retired). Unlike opencode,
    # codex's scope label does NOT gate on the repo bit (the helper returns True for
    # review_codex unconditionally), so it stays `agentic` regardless of cwd_is_repo, for
    # the bare `codex` seat tested below or any pinned `codex:<model>` seat alike.
    assert _seat_reads_repo("codex", True) is True
    assert _seat_reads_repo("codex", False) is True
    # commandcode / z.ai are diff-only regardless of the repo bit (keyed HTTP, no workspace).
    assert _seat_reads_repo("commandcode:moonshotai/Kimi-K2.7-Code", True) is False
    assert _seat_reads_repo("zai:glm-5.2", True) is False


def test_show_board_scope_label_tracks_direct_claude_cli():
    from reviewlib.cli import _seat_reads_repo  # noqa: PLC0415

    saved_which = review_backends._which_optional
    saved_mode = os.environ.get("REVIEW_CLAUDE_MODE")
    review_backends._which_optional = lambda name: (
        "/bin/claude" if name == "claude" else None
    )
    os.environ.pop("REVIEW_CLAUDE_MODE", None)
    try:
        assert _seat_reads_repo("claude:claude-opus-4-8", True) is True
    finally:
        review_backends._which_optional = saved_which
        if saved_mode is None:
            os.environ.pop("REVIEW_CLAUDE_MODE", None)
        else:
            os.environ["REVIEW_CLAUDE_MODE"] = saved_mode


class _CapturedCodex:
    """Like `_Captured`, but its `__call__` accepts review_codex's `input_text=` kwarg
    (the codex backend pipes the payload over stdin, unlike opencode's argv-only call)."""

    def __init__(self) -> None:
        self.argv: list[str] | None = None
        self.cwd: Path | None = None
        self.input_text: str | None = None
        self.header_argv0: str | None = None

    def __call__(
        self,
        argv,
        cwd,
        timeout,
        backend,
        round_no=0,
        announce=False,
        input_text="",
        header_argv0=None,
    ):
        self.argv = list(argv)
        self.cwd = Path(cwd)
        self.input_text = input_text
        self.header_argv0 = header_argv0

        class _Proc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return _Proc()


@contextlib.contextmanager
def _capture_codex():
    """Hermetic capture of review_codex's launch (no real `codex` binary needed): patch
    `_run_streamed` to record argv/cwd and `_which` to a fake path. Single restore point."""
    cap = _CapturedCodex()
    orig_run = review_backends._run_streamed
    orig_which = review_backends._which
    review_backends._run_streamed = cap  # type: ignore[assignment]
    review_backends._which = lambda name: f"/fake/bin/{name}"  # type: ignore[assignment]
    try:
        yield cap
    finally:
        review_backends._run_streamed = orig_run
        review_backends._which = orig_which


def test_codex_bare_seat_runs_agentic_read_only_in_repo_no_model_flag():
    """The bare `codex` string (DEFAULT_MODELS' seat, the moderator fallback, and the
    manifest's `openai` route -- no longer any `DEFAULT_BOARD` seat since ASTRA_SEAT
    replaced its old #5 slot, see config.py) resolves to the AGENTIC codex CLI route:
    `codex exec -s read-only -C <cwd> --ephemeral -`. Bare `codex` (no `:model`) pins
    NO `-m` flag (the codex CLI default model), and the run is read-only inside the real
    repo, so it can read any project file — not just the diff."""
    with _capture_codex() as cap:
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            _git_init(repo)
            res = review_backends.review_codex(
                "codex", "Review.", "some diff", repo, 60
            )
        assert res.returncode == 0, res
        argv = cap.argv or []
        assert argv[0].endswith("/codex"), argv
        assert argv[1] == "exec", argv
        # read-only scope + the real repo as -C, never a write/agentic-edit posture.
        assert "-s" in argv and argv[argv.index("-s") + 1] == "read-only", argv
        assert "-C" in argv and argv[argv.index("-C") + 1] == str(repo), argv
        # Ephemeral session + the stdin marker `-` as the last arg (payload piped in).
        assert "--ephemeral" in argv, argv
        assert argv[-1] == "-", argv
        # Bare `codex` carries NO `-m` (uses the codex CLI default model).
        assert "-m" not in argv, argv
        assert cap.cwd == repo
        # The prompt AND the diff actually reach the model over stdin — a regression that
        # drops the payload would otherwise pass an argv-only check while reviewing nothing.
        assert "Review." in (cap.input_text or ""), cap.input_text
        assert "some diff" in (cap.input_text or ""), cap.input_text
        assert cap.header_argv0 is None


def test_codex_pinned_model_seat_passes_model_flag():
    """A `codex:<model>` spec DOES pin `-m <model>` (so a future board could pin a
    version), unlike the bare `codex` seat. Pins the argv contract both ways."""
    with _capture_codex() as cap:
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            _git_init(repo)
            review_backends.review_codex("codex:gpt-5.5", "Review.", "diff", repo, 60)
        argv = cap.argv or []
        assert "-m" in argv and argv[argv.index("-m") + 1] == "gpt-5.5", argv
        assert cap.header_argv0 == "codex -m gpt-5.5", cap.header_argv0


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
