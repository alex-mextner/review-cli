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
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import backends as review_backends  # noqa: E402


class _Captured:
    """Stand-in for the CompletedProcess `_run_streamed` returns; also records the
    argv/cwd of the call so the test can assert how opencode was launched."""

    def __init__(self) -> None:
        self.argv: list[str] | None = None
        self.cwd: Path | None = None

    def __call__(self, argv, cwd, timeout, backend, round_no=0, announce=False):
        self.argv = list(argv)
        self.cwd = Path(cwd)

        class _Proc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return _Proc()


@contextlib.contextmanager
def _capture_opencode():
    """Patch `_run_streamed` (capture), `_which` (hermetic — no real opencode binary
    needed) AND `_ensure_opencode_readonly_agent` (so the unit tests do NOT touch the
    developer's / CI's global ~/.config/opencode) for the duration of one test,
    restoring all three afterward. A single restore point means a typo can't leak the
    mock into sibling tests (board feedback)."""
    cap = _Captured()
    orig_run = review_backends._run_streamed
    orig_which = review_backends._which
    orig_ensure = review_backends._ensure_opencode_readonly_agent
    review_backends._run_streamed = cap  # type: ignore[assignment]
    review_backends._which = lambda name: f"/fake/bin/{name}"  # type: ignore[assignment]
    review_backends._ensure_opencode_readonly_agent = lambda *_a, **_k: None  # type: ignore[assignment]
    try:
        yield cap
    finally:
        review_backends._run_streamed = orig_run
        review_backends._which = orig_which
        review_backends._ensure_opencode_readonly_agent = orig_ensure


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)


def test_runs_in_real_repo_with_dir_flag():
    with _capture_opencode() as cap:
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            _git_init(repo)
            res = review_backends.review_opencode(
                "oc:opencode/deepseek-v4-flash-free", "Review.", "some diff", repo, 60,
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
    # codex is the agentic route for the #3 GPT-5.5/codex seat — it reads the whole repo
    # (the diff-only `commandcode:gpt-5.5` route for the SAME model was retired). Unlike
    # opencode, codex's scope label does NOT gate on the repo bit (the helper returns True
    # for review_codex unconditionally), so it stays `agentic` regardless of cwd_is_repo.
    assert _seat_reads_repo("codex", True) is True
    assert _seat_reads_repo("codex", False) is True
    # commandcode / z.ai are diff-only regardless of the repo bit (keyed HTTP, no workspace).
    assert _seat_reads_repo("commandcode:moonshotai/Kimi-K2.7-Code", True) is False
    assert _seat_reads_repo("zai:glm-5.2", True) is False


class _CapturedCodex:
    """Like `_Captured`, but its `__call__` accepts review_codex's `input_text=` kwarg
    (the codex backend pipes the payload over stdin, unlike opencode's argv-only call)."""

    def __init__(self) -> None:
        self.argv: list[str] | None = None
        self.cwd: Path | None = None
        self.input_text: str | None = None

    def __call__(self, argv, cwd, timeout, backend, round_no=0, announce=False, input_text=""):
        self.argv = list(argv)
        self.cwd = Path(cwd)
        self.input_text = input_text

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
    """The #3 board seat is the bare `codex` string -> the AGENTIC codex CLI route:
    `codex exec -s read-only -C <cwd> --ephemeral -`. Bare `codex` (no `:model`) pins
    NO `-m` flag (the codex CLI default model), and the run is read-only inside the real
    repo, so it can read any project file — not just the diff."""
    with _capture_codex() as cap:
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            _git_init(repo)
            res = review_backends.review_codex("codex", "Review.", "some diff", repo, 60)
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
