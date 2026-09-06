#!/usr/bin/env python3
"""Unit tests for the review-cli#180 codex execpolicy recursion guard
(`install.install_codex_recursion_guard`).

codex's ONLY safety mechanism around the review-cli backend call was `-s read-only`, a
filesystem/network sandbox — it does NOT restrict codex's shell/exec tool (codex's core
capability IS running shell commands; there is no `--tools ""`-style built-in
tool-disable like the claude backend, or an explicit `bash: deny` permission block like
the opencode backend). codex DOES have a real command-level restriction: execpolicy
`.rules` files (Starlark `prefix_rule(pattern=[...], decision="forbidden")`), loaded by
default from `$CODEX_HOME/rules/` for every `codex exec` unless `--ignore-rules` is
passed — a `forbidden` decision hard-blocks the command before it runs.

These tests cover:
  * `install_codex_recursion_guard()` writes the expected rules file, is idempotent
    (second call is a no-op), and honors `$CODEX_HOME`;
  * the generated content covers every backend binary review-cli itself shells out to
    (`review`, `codex`, `claude`, `opencode`, `omp`) — single-source-of-truth check
    against `CODEX_RECURSION_GUARD_FORBIDDEN_COMMANDS`;
  * `backends._ensure_codex_recursion_guard()` runs the install at most once per
    process (cheap re-entry) and never raises even when the write target is
    unwritable;
  * an INTEGRATION check (skipped when the real `codex` binary is absent, same style
    as the ImageMagick-gated visual-verification suite) that feeds the ACTUAL
    generated file to `codex execpolicy check` and asserts codex's own policy engine
    resolves `review …` / `codex …` / `claude …` / `opencode …` / `omp …` to
    `"decision": "forbidden"`, while an unrelated command (`git status`) is
    unaffected — proof this is a real, codex-verified restriction, not just a file
    that looks right.

Same harness style as the other test_* files: plain test_* functions run by the
__main__ block (and pytest-collectable). No live model/API call is ever made.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import backends as b  # noqa: E402
from reviewlib import install  # noqa: E402


class _Env:
    """Set/clear env vars for one test, restoring exactly afterward."""

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


# === codex_home() resolution ======================================================
def test_codex_home_defaults_to_dot_codex_under_real_home():
    with _Env(CODEX_HOME=None):
        assert install.codex_home() == Path.home() / ".codex"


def test_codex_home_honors_env_override():
    with _Env(CODEX_HOME="/tmp/some-codex-home"):
        assert install.codex_home() == Path("/tmp/some-codex-home")


def test_codex_home_treats_empty_string_as_unset():
    """`CODEX_HOME=""` (present but empty) falls back to `~/.codex` like a genuinely
    unset var, rather than resolving to `Path("")` — the untested edge case the
    docstring calls out (review-cli#180 review round 3, Opus)."""
    with _Env(CODEX_HOME=""):
        assert install.codex_home() == Path.home() / ".codex"


# === install_codex_recursion_guard(): write + idempotency =========================
def test_install_writes_the_rules_file_under_codex_home_rules():
    with tempfile.TemporaryDirectory() as tmp:
        with _Env(CODEX_HOME=tmp):
            changed = install.install_codex_recursion_guard()
            path = (
                install.codex_home() / "rules" / install._CODEX_RECURSION_GUARD_FILENAME
            )
            assert changed is True
            assert path.is_file()
            content = path.read_text(encoding="utf-8")
            assert "review-cli#180" in content
            assert "prefix_rule" in content


def test_install_is_idempotent_second_call_reports_unchanged():
    with tempfile.TemporaryDirectory() as tmp:
        with _Env(CODEX_HOME=tmp):
            assert install.install_codex_recursion_guard() is True
            assert install.install_codex_recursion_guard() is False


def test_install_rewrites_a_stale_or_tampered_copy():
    with tempfile.TemporaryDirectory() as tmp:
        with _Env(CODEX_HOME=tmp):
            path = (
                install.codex_home() / "rules" / install._CODEX_RECURSION_GUARD_FILENAME
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "# stale content from a previous version\n", encoding="utf-8"
            )
            changed = install.install_codex_recursion_guard()
            assert changed is True
            assert "review-cli#180" in path.read_text(encoding="utf-8")


def test_install_never_raises_on_an_unwritable_target():
    """A locked-down $CODEX_HOME must not break a review run — a failed write returns
    False rather than propagating."""
    with tempfile.TemporaryDirectory() as tmp:
        blocked = Path(tmp) / "rules-parent-is-a-file"
        blocked.write_text("not a directory", encoding="utf-8")
        with _Env(CODEX_HOME=str(blocked)):
            assert install.install_codex_recursion_guard() is False


def test_install_never_raises_when_codex_home_raises_runtimeerror():
    """`codex_home()` can raise `RuntimeError` (via `Path.home()`, when $HOME is unset
    AND the pwd-database fallback also fails — e.g. a minimal container with no passwd
    entry for the UID; NOT reliably reproducible by just unsetting $HOME on a normal
    dev machine, since the pwd fallback usually still resolves there). The docstring
    documents this as a handled case (review-cli#180 review round 2, glm-5.2) — force
    it directly by swapping the `codex_home` FUNCTION (a plain module attribute, the
    smallest possible blast radius — review-cli#180 review round 6, Opus: an earlier
    version of this test patched the `Path` class instead, which was narrower than
    patching the real `pathlib.Path` but still wider than necessary)."""
    saved_codex_home = install.codex_home
    install.codex_home = lambda: (_ for _ in ()).throw(
        RuntimeError("simulated: no HOME and no pwd-database entry")
    )
    try:
        assert install.install_codex_recursion_guard() is False
    finally:
        install.codex_home = saved_codex_home


# === content covers every review-cli backend binary ===============================
def test_generated_rules_forbid_every_backend_binary():
    content = install._codex_recursion_guard_rules()
    for cmd in install.CODEX_RECURSION_GUARD_FORBIDDEN_COMMANDS:
        assert f'pattern = ["{cmd}"]' in content, (cmd, content)
        assert content.count(f'pattern = ["{cmd}"]') == 1, (cmd, content)
    # single-source-of-truth: `review` and `codex` (the two proven-live vectors) MUST
    # be in the constant, not just in the file — a future edit that drops one from the
    # tuple would silently reopen the exact review-cli#180 hole.
    assert "review" in install.CODEX_RECURSION_GUARD_FORBIDDEN_COMMANDS
    assert "codex" in install.CODEX_RECURSION_GUARD_FORBIDDEN_COMMANDS


# === backends._ensure_codex_recursion_guard(): cached, never raises ===============
def test_ensure_prints_a_notice_only_when_the_file_actually_changes():
    """The write into the user's $CODEX_HOME is a side effect of an ordinary review
    run, so it must not be silent (review-cli#180 review finding, glm-5.2) — but it
    also must not spam a notice on the common no-op ("already up to date") path."""
    saved_ensured = b._codex_recursion_guard_ensured
    saved_install = b.install_codex_recursion_guard

    b.install_codex_recursion_guard = lambda: True
    b._codex_recursion_guard_ensured = False
    captured_err = io.StringIO()
    try:
        with contextlib.redirect_stderr(captured_err):
            b._ensure_codex_recursion_guard()
    finally:
        b.install_codex_recursion_guard = saved_install
        b._codex_recursion_guard_ensured = saved_ensured
    assert "review-cli#180" in captured_err.getvalue()

    b.install_codex_recursion_guard = lambda: False
    b._codex_recursion_guard_ensured = False
    captured_err2 = io.StringIO()
    try:
        with contextlib.redirect_stderr(captured_err2):
            b._ensure_codex_recursion_guard()
    finally:
        b.install_codex_recursion_guard = saved_install
        b._codex_recursion_guard_ensured = saved_ensured
    assert captured_err2.getvalue() == ""


def test_ensure_runs_the_install_at_most_once_per_process():
    saved_ensured = b._codex_recursion_guard_ensured
    calls = {"n": 0}
    saved_install = b.install_codex_recursion_guard
    b.install_codex_recursion_guard = lambda: (
        calls.__setitem__("n", calls["n"] + 1) or True
    )
    b._codex_recursion_guard_ensured = False
    try:
        b._ensure_codex_recursion_guard()
        b._ensure_codex_recursion_guard()
        b._ensure_codex_recursion_guard()
        assert calls["n"] == 1, calls
    finally:
        b.install_codex_recursion_guard = saved_install
        b._codex_recursion_guard_ensured = saved_ensured


def test_ensure_never_raises_even_if_install_blows_up():
    saved_ensured = b._codex_recursion_guard_ensured
    saved_install = b.install_codex_recursion_guard
    b.install_codex_recursion_guard = lambda: (_ for _ in ()).throw(
        OSError("simulated disk failure")
    )
    b._codex_recursion_guard_ensured = False
    try:
        b._ensure_codex_recursion_guard()  # must not raise
        assert (
            b._codex_recursion_guard_ensured is True
        )  # still marks done — no retry storm
    finally:
        b.install_codex_recursion_guard = saved_install
        b._codex_recursion_guard_ensured = saved_ensured


def test_ensure_is_thread_safe_under_concurrent_first_call():
    """`_codex_recursion_guard_lock` exists specifically because panel mode calls
    several backends (including possibly multiple codex seats/rounds) from separate
    threads at once (review-cli#65's ThreadPoolExecutor). Spawn N threads that all
    race `_ensure_codex_recursion_guard()` on its very first (uninstalled) call and
    assert the double-checked-locking actually holds: the real install function runs
    EXACTLY once, not once per racing thread."""
    saved_ensured = b._codex_recursion_guard_ensured
    saved_install = b.install_codex_recursion_guard
    calls = {"n": 0}
    counter_lock = threading.Lock()

    def _counting_install() -> bool:
        with counter_lock:
            calls["n"] += 1
        return True

    b.install_codex_recursion_guard = _counting_install
    b._codex_recursion_guard_ensured = False
    try:
        threads = [
            threading.Thread(target=b._ensure_codex_recursion_guard) for _ in range(16)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert calls["n"] == 1, calls
        assert b._codex_recursion_guard_ensured is True
    finally:
        b.install_codex_recursion_guard = saved_install
        b._codex_recursion_guard_ensured = saved_ensured


# === integration: codex's OWN policy engine resolves the file to "forbidden" ======
def _codex_execpolicy_decision(rules_path: Path, *command: str) -> str | None:
    codex_bin = shutil.which("codex")
    if not codex_bin:
        return None
    proc = subprocess.run(
        [codex_bin, "execpolicy", "check", "-r", str(rules_path), "--", *command],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if proc.returncode not in (0, 1) or not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout).get("decision")
    except json.JSONDecodeError:
        return None


def test_codex_execpolicy_engine_resolves_forbidden_commands_as_forbidden():
    codex_bin = shutil.which("codex")
    if not codex_bin:
        print("SKIP: codex CLI not on PATH — install.py logic already covered above")
        return
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / install._CODEX_RECURSION_GUARD_FILENAME
        path.write_text(install._codex_recursion_guard_rules(), encoding="utf-8")
        for cmd in install.CODEX_RECURSION_GUARD_FORBIDDEN_COMMANDS:
            decision = _codex_execpolicy_decision(path, cmd, "diff")
            assert decision == "forbidden", (cmd, decision)
        # a command NOT in the forbidden list must be unaffected (no false-positive
        # blast radius on ordinary reviewer commands like `git status`).
        decision = _codex_execpolicy_decision(path, "git", "status")
        assert decision != "forbidden", decision


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
