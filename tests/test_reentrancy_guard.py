#!/usr/bin/env python3
"""Unit tests for the REVIEW_CLI_ACTIVE reentrancy guard (review-cli#180).

The threat: the codex backend's ONLY safety mechanism was `-s read-only`, a
filesystem/network sandbox that does NOT restrict codex's shell/exec tool. A codex
reviewer could re-invoke `review diff` on the same worktree as a plain shell command,
which spawns another codex backend, which can do it again — an unbounded
self-reinvocation loop. Live-confirmed 2026-08-11: 40+ live `codex exec` processes and
11 `review diff` processes across 4 worktrees, swap at 88.5%, load average 60+.

The existing process-GROUP kill/backstop machinery (`process._run_streamed`,
`reviewlib.backstop`) cannot bound this, because each backend child is spawned with
`start_new_session=True` (needed so a per-call timeout can kill just that call's own
tree without also taking down the CLI's own group) — every recursive level therefore
re-roots into a BRAND NEW OS session, invisible to a `killpg` rooted at an earlier
level. `$REVIEW_CLI_ACTIVE` is an env var, which survives `exec`/`setsid` regardless
of how many session boundaries the recursion crosses, so it is the mechanism that
actually closes the loop.

These tests cover:
  * `cli._reject_if_reentrant` in isolation (set / unset);
  * `cli.main()` sets the var for the duration of a run and clears it in `finally`,
    including on a non-zero exit — so back-to-back non-nested CLI calls in the SAME
    process (as pytest does when collecting several test_* functions here) never
    false-trip each other;
  * a REAL nested `bin/review` subprocess invocation is refused fast (no dispatch, no
    backend touched) when it inherits an already-set $REVIEW_CLI_ACTIVE;
  * the review-cli#180 acceptance-criterion regression test: a STUBBED codex binary
    that attempts to re-invoke `review` (a real subprocess, not a mock) is blocked by
    this guard — proven by inspecting the nested call's actual exit code/stderr, with
    a companion control run (guard var absent) showing the SAME nested call would
    otherwise have succeeded, so the block is attributable to the guard and not to some
    unrelated failure.

Same harness style as the other test_* files: plain test_* functions run by the
__main__ block (and pytest-collectable). No live model/API call is ever made — the
"codex" in the last test is a stubbed local Python script, not the real CLI.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_BIN = REPO_ROOT / "bin" / "review"
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import backends as b  # noqa: E402
from reviewlib import cli  # noqa: E402


class _Env:
    """Set/clear env vars for one test, restoring exactly afterward (no monkeypatch
    fixture, so the standalone __main__ runner works)."""

    def __init__(self, **env: str | None) -> None:
        self._env = env

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in self._env}
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *exc):
        for k, old in self._saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old
        return False


# === _reject_if_reentrant() in isolation =========================================
def test_reject_if_reentrant_returns_none_when_unset():
    with _Env(REVIEW_CLI_ACTIVE=None):
        assert cli._reject_if_reentrant([]) is None


def test_reject_if_reentrant_returns_nonzero_when_set():
    with _Env(REVIEW_CLI_ACTIVE="1"):
        rc = cli._reject_if_reentrant([])
        assert rc is not None and rc != 0


def test_reject_if_reentrant_treats_empty_string_as_set():
    """review-cli#180 review finding (glm-5.2): `REVIEW_CLI_ACTIVE= review diff` (a
    shell setting the var to an EMPTY string for the child, e.g. via `VAR= cmd`
    syntax) must still trip the guard. A bare `if os.environ.get(...)` treats `""` as
    falsy and would silently let this recursion through — the check must test
    PRESENCE (`is not None`), not truthiness."""
    with _Env(REVIEW_CLI_ACTIVE=""):
        rc = cli._reject_if_reentrant([])
        assert rc is not None and rc != 0


def test_reject_if_reentrant_treats_falsy_looking_strings_as_set():
    """Any PRESENT value — including ones that look "off" to a human, like "0" or
    "false" — must still refuse. Only a genuinely ABSENT var is a fresh top-level run."""
    for value in ("0", "false", "no"):
        with _Env(REVIEW_CLI_ACTIVE=value):
            rc = cli._reject_if_reentrant([])
            assert rc is not None and rc != 0, value


# === main() sets + clears the guard across a run =================================
def test_main_sets_env_var_during_dispatch_and_clears_after():
    """Patch _dispatch_with_backstop to observe the var IS set while main() is inside
    its own dispatch, then confirm it is cleared once main() returns."""
    saved_dispatch = cli._dispatch_with_backstop
    observed: dict[str, str | None] = {}

    def _spy(raw, output_path):
        observed["value"] = os.environ.get(cli.REVIEW_CLI_ACTIVE_ENV)
        return 0

    cli._dispatch_with_backstop = _spy
    try:
        with _Env(REVIEW_CLI_ACTIVE=None):
            rc = cli.main(["--help"])
            assert rc == 0
            assert observed["value"] == "1"
            assert os.environ.get(cli.REVIEW_CLI_ACTIVE_ENV) is None
    finally:
        cli._dispatch_with_backstop = saved_dispatch


def test_main_clears_env_var_even_on_a_raising_dispatch():
    """The var must not leak past a run that raises — otherwise ONE crashed run would
    permanently wedge every later invocation in the same process (e.g. pytest reusing
    interpreter state, or a long-lived dashboard-like host process)."""
    saved_dispatch = cli._dispatch_with_backstop
    cli._dispatch_with_backstop = lambda raw, output_path: (_ for _ in ()).throw(
        RuntimeError("simulated dispatch crash")
    )
    try:
        with _Env(REVIEW_CLI_ACTIVE=None):
            try:
                cli.main(["--help"])
                raised = False
            except RuntimeError:
                raised = True
            assert raised
            assert os.environ.get(cli.REVIEW_CLI_ACTIVE_ENV) is None
    finally:
        cli._dispatch_with_backstop = saved_dispatch


def test_back_to_back_non_nested_main_calls_never_false_trip():
    """Two SEQUENTIAL (not nested) main() calls in one process — exactly what running
    this file's own tests does — must both succeed. Only a call that starts WHILE an
    outer one is still active (genuine nesting) may be refused.

    Uses `--list-defaults` rather than `--help`: argparse's own `--help` handler calls
    `sys.exit(0)` (SystemExit), which would abort THIS test file's plain except-Exception
    runner loop instead of asserting cleanly — `--list-defaults` is real dispatch (proves
    the env var is genuinely being set/cleared around live work, not a mocked path) that
    returns an ordinary int."""
    with _Env(REVIEW_CLI_ACTIVE=None):
        assert cli.main(["--list-defaults"]) == 0
        assert cli.main(["--list-defaults"]) == 0
        assert os.environ.get(cli.REVIEW_CLI_ACTIVE_ENV) is None


# === a real nested subprocess is refused fast ====================================
def test_nested_subprocess_invocation_is_refused():
    env = dict(os.environ)
    env["REVIEW_CLI_ACTIVE"] = "1"
    proc = subprocess.run(
        [sys.executable, str(REVIEW_BIN), "--help"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "review-cli#180" in proc.stderr, proc.stderr
    # It must refuse WITHOUT printing the normal --help usage text — proof it never
    # reached dispatch at all.
    assert "subcommands:" not in proc.stdout, proc.stdout


def test_non_nested_subprocess_invocation_still_works():
    """Control: the SAME command with $REVIEW_CLI_ACTIVE absent must behave normally —
    proves the refusal above is attributable to the guard, not to some unrelated
    breakage in the subprocess path."""
    env = {k: v for k, v in os.environ.items() if k != "REVIEW_CLI_ACTIVE"}
    proc = subprocess.run(
        [sys.executable, str(REVIEW_BIN), "--help"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "subcommands:" in proc.stdout, proc.stdout


# === AC#4 regression: a stubbed codex binary attempting self-reinvocation ========
# The fake binary also records the RAW value of $REVIEW_CLI_ACTIVE it itself received
# (record + ".own_env") — direct proof that `review_codex()`'s real `Popen(env=None)`
# call (via `process._run_streamed`) inherits the parent's environment all the way
# into the spawned codex-equivalent process, rather than inferring propagation only
# from the nested `review` call's behavior (review-cli#180 review finding: Opus and
# glm-5.2 both flagged that the guard's efficacy depends on this inheritance, and
# neither test nor code made it explicit).
_FAKE_CODEX_SCRIPT = """#!/usr/bin/env python3
import os
import subprocess
import sys

sys.stdin.read()  # drain the piped prompt/diff payload, like a real agent CLI would
review_bin = os.environ["FAKE_CODEX_REVIEW_BIN"]
record = os.environ["FAKE_CODEX_RECORD"]
with open(record + ".own_env", "w", encoding="utf-8") as fh:
    fh.write(repr(os.environ.get("REVIEW_CLI_ACTIVE")))
proc = subprocess.run(
    [sys.executable, review_bin, "--help"], capture_output=True, text=True
)
with open(record + ".rc", "w", encoding="utf-8") as fh:
    fh.write(str(proc.returncode))
with open(record + ".err", "w", encoding="utf-8") as fh:
    fh.write(proc.stderr)
print("fake-codex: attempted self-reinvocation of `review diff`", flush=True)
"""


def _write_fake_codex(tmp: Path) -> Path:
    script = tmp / "fake-codex"
    script.write_text(_FAKE_CODEX_SCRIPT, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _run_review_codex_with_fake_binary(tmp: Path, record: Path) -> None:
    """Call `backends.review_codex` for real (not mocked) with `_which_optional`
    patched so `codex` resolves to the fake binary above, `CODEX_HOME` pointed at a
    throwaway dir (so `_ensure_codex_recursion_guard` never touches the developer's
    real `~/.codex`), and `REVIEW_UNPAID_PROVIDERS` cleared (so the call is never
    short-circuited as an unpaid-provider skip before it reaches the fake binary)."""
    _write_fake_codex(tmp)
    saved_which = b._which_optional
    saved_ensured = b._codex_recursion_guard_ensured
    b._which_optional = lambda name: (
        str(tmp / "fake-codex") if name == "codex" else None
    )
    b._codex_recursion_guard_ensured = (
        False  # force the (harmless, redirected) install check
    )
    try:
        with _Env(
            REVIEW_UNPAID_PROVIDERS=None,
            CODEX_HOME=str(tmp / "codex-home"),
            FAKE_CODEX_REVIEW_BIN=str(REVIEW_BIN),
            FAKE_CODEX_RECORD=str(record),
        ):
            result = b.review_codex("codex", "Review this.", "", tmp, timeout=30)
    finally:
        b._which_optional = saved_which
        b._codex_recursion_guard_ensured = saved_ensured
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "self-reinvocation" in result.stdout, result.stdout


def test_review_codex_calls_ensure_codex_recursion_guard():
    """A future refactor that removes the `_ensure_codex_recursion_guard()` call site
    from `review_codex()` should fail a test, not silently reopen the install gap —
    for a board that actually runs a codex backend."""
    saved_ensure = b._ensure_codex_recursion_guard
    saved_which = b._which_optional
    saved_guard_ensured = b._codex_recursion_guard_ensured
    called = {"n": 0}

    def _spy():
        called["n"] += 1

    b._ensure_codex_recursion_guard = _spy
    b._which_optional = lambda name: "/usr/bin/codex"  # codex resolves on PATH
    try:
        with _Env(REVIEW_UNPAID_PROVIDERS=None):
            try:
                b.review_codex("codex", "prompt", "", Path("."), timeout=5)
            except Exception:
                pass  # the real codex subprocess spawn isn't under test here
    finally:
        b._ensure_codex_recursion_guard = saved_ensure
        b._which_optional = saved_which
        b._codex_recursion_guard_ensured = saved_guard_ensured
    assert called["n"] == 1, called


def test_review_codex_skips_guard_install_when_codex_binary_absent():
    """Opus finding on #279: installing the execpolicy guard writes into $HOME and
    prints a stderr notice. A board that lists a codex seat but has no `codex` binary
    on PATH (or the provider is disabled as unpaid) must not touch the user's home
    directory or announce a guard install for a backend that never actually runs."""
    saved_ensure = b._ensure_codex_recursion_guard
    saved_which = b._which_optional
    saved_guard_ensured = b._codex_recursion_guard_ensured
    called = {"n": 0}

    def _spy():
        called["n"] += 1

    b._ensure_codex_recursion_guard = _spy
    b._which_optional = lambda name: None  # -> _which("codex") raises RuntimeError
    try:
        with _Env(REVIEW_UNPAID_PROVIDERS=None):
            try:
                b.review_codex("codex", "prompt", "", Path("."), timeout=5)
            except RuntimeError:
                pass  # expected: no real `codex` binary resolved
    finally:
        b._ensure_codex_recursion_guard = saved_ensure
        b._which_optional = saved_which
        b._codex_recursion_guard_ensured = saved_guard_ensured
    assert called["n"] == 0, called


def test_review_codex_skips_guard_install_when_provider_is_unpaid():
    """Same Opus finding, the other short-circuit: a codex seat disabled via
    $REVIEW_UNPAID_PROVIDERS must not install the guard either."""
    saved_ensure = b._ensure_codex_recursion_guard
    saved_guard_ensured = b._codex_recursion_guard_ensured
    called = {"n": 0}

    def _spy():
        called["n"] += 1

    b._ensure_codex_recursion_guard = _spy
    try:
        with _Env(REVIEW_UNPAID_PROVIDERS="codex"):
            b.review_codex("codex", "prompt", "", Path("."), timeout=5)
    finally:
        b._ensure_codex_recursion_guard = saved_ensure
        b._codex_recursion_guard_ensured = saved_guard_ensured
    assert called["n"] == 0, called


def test_codex_backend_self_reinvocation_is_blocked_when_guard_is_active():
    """The review-cli#180 acceptance-criterion regression test: a codex backend that
    tries to shell out to `review` again is refused, because `review_codex` was
    itself called (as it always is in production, from `main()`'s guarded dispatch)
    with $REVIEW_CLI_ACTIVE already set."""
    with tempfile.TemporaryDirectory() as tmp_raw:
        tmp = Path(tmp_raw)
        record = tmp / "nested-call"
        with _Env(REVIEW_CLI_ACTIVE="1"):
            _run_review_codex_with_fake_binary(tmp, record)
        own_env = (record.with_suffix(".own_env")).read_text(encoding="utf-8")
        # Direct proof (not inference) that review_codex's real Popen(env=None) call
        # propagated $REVIEW_CLI_ACTIVE into the spawned codex-equivalent process.
        assert own_env == "'1'", own_env
        rc = (record.with_suffix(".rc")).read_text(encoding="utf-8").strip()
        err = (record.with_suffix(".err")).read_text(encoding="utf-8")
        assert rc != "0", f"nested `review` call was NOT blocked (rc={rc}): {err}"
        assert "review-cli#180" in err, err


def test_codex_backend_self_reinvocation_control_succeeds_without_the_guard():
    """Control for the test above: with $REVIEW_CLI_ACTIVE absent (simulating a world
    without the review-cli#180 fix), the exact same nested `review --help` call
    SUCCEEDS — proving the block above is attributable to the guard, not to the fake
    binary, the temp dir, or some unrelated subprocess failure."""
    with tempfile.TemporaryDirectory() as tmp_raw:
        tmp = Path(tmp_raw)
        record = tmp / "nested-call"
        with _Env(REVIEW_CLI_ACTIVE=None):
            _run_review_codex_with_fake_binary(tmp, record)
        rc = (record.with_suffix(".rc")).read_text(encoding="utf-8").strip()
        out_err = (record.with_suffix(".err")).read_text(encoding="utf-8")
        assert rc == "0", (
            f"control nested `review --help` call unexpectedly failed: {out_err}"
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
