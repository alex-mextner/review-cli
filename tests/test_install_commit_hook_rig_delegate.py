#!/usr/bin/env python3
"""`review install-commit-hook` delegates to `rig apply` when rig is present.

Background: `review install-commit-hook` writes a GLOBAL git pre-commit hook
(``core.hooksPath``) that gates every commit on a review-stamp check. When ``rig`` is also
installed, its opt-in ``git_hooks.dispatcher`` provisions the SAME mechanism as one stage
(``review-gate``) of its composed global pre-commit — literally the content that used to be
installed by hand via this exact command (see
``agent-tools/git-hooks/global-dispatcher/hooks/review-gate``, whose header reads
"originally installed by `review install-commit-hook`"). Two tools writing the same
``core.hooksPath`` is the double-write class the shared `agenttools_rig_delegate` helper
exists to remove: rig present -> delegate (`rig apply` owns the hook, single source of
truth); rig absent -> review's own direct installer runs unchanged (today's behavior,
covered by ``tests/test_install_state.py``).

NOTE: `install-skill`'s SessionStart hook (`_ensure_sessionstart_hook`) does NOT delegate.
rig's own `tools:` provisioning (`riglib/tools.py`) runs THIS repo's `install.sh`, which in
turn calls `review install-skill` — i.e. rig is a CONSUMER of `install-skill`, not an
independent provider of the same hook. Delegating `install-skill` to `rig apply` would risk
a `review install-skill` -> `rig apply` -> (tools: block) -> `install.sh` -> `review
install-skill` cycle. `install_hook_tg` is unrelated to rig entirely (a tg-cli descriptor;
rig has no equivalent). Only `install_commit_hook` is a genuine duplicate-hook installer.

Pinned here:
  * rig present (a runnable stub on RIG_BIN) -> `install_commit_hook` shells out to
    `rig apply` and returns ITS exit code; the direct installer never runs (no pre-commit
    file written, no core.hooksPath set).
  * rig present but `rig apply` FAILS (non-zero exit) -> that exit code is surfaced as-is;
    the direct installer is NOT used as a fallback (a present-but-failing rig is a real
    failure to report, not "rig is absent").
  * rig absent (no RIG_BIN, no candidate binary under an isolated HOME) -> the direct
    installer runs exactly as before (writes the hook, sets core.hooksPath).
  * the shared `agenttools_rig_delegate` helper not being importable at all (agent-tools not
    installed on this machine) -> degrades to the direct installer, not a crash.
"""

from __future__ import annotations

import importlib.util
import io
import os
import stat
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import install  # noqa: E402


def _ensure_helper_importable() -> bool:
    """Make the in-ecosystem `agenttools_rig_delegate` helper importable if an agent-tools
    checkout is reachable, so the delegation integration tests below actually exercise the
    real helper. It is an editable in-ecosystem dep (not on PyPI), so a bare CI without an
    agent-tools checkout can't import it — those two tests then SKIP cleanly (the helper's
    own behavior is fully covered by agent-tools#282); the rig-absent / helper-missing tests
    still run everywhere."""
    if importlib.util.find_spec("agenttools_rig_delegate") is not None:
        return True
    candidates = []
    src = os.environ.get("RIG_AGENT_TOOLS_SOURCE")
    if src:
        candidates.append(Path(src) / "lib")
    candidates.append(Path.home() / "xp" / "agent-tools" / "lib")
    for lib in candidates:
        if (lib / "agenttools_rig_delegate" / "__init__.py").is_file():
            sys.path.insert(0, str(lib))
            importlib.invalidate_caches()
            return importlib.util.find_spec("agenttools_rig_delegate") is not None
    return False


_HELPER_AVAILABLE = _ensure_helper_importable()
_needs_helper = pytest.mark.skipif(
    not _HELPER_AVAILABLE,
    reason="agenttools_rig_delegate (agent-tools#282) not installed; delegation covered there",
)


def _capture(fn, *a, **k) -> tuple[int, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        rc = fn(*a, **k)
    return rc, out.getvalue()


def _isolated_home(tmp: str) -> dict:
    """Isolate HOME (+ git's global config) so a direct-install fallback never touches the
    real machine, mirroring `tests/test_install_state.py::_isolated_home`."""
    keys = ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "GIT_CONFIG_GLOBAL", "RIG_BIN", "PATH")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ["HOME"] = tmp
    os.environ["XDG_CONFIG_HOME"] = str(Path(tmp) / ".config")
    os.environ["XDG_DATA_HOME"] = str(Path(tmp) / ".local" / "share")
    os.environ["GIT_CONFIG_GLOBAL"] = str(Path(tmp) / ".gitconfig")
    # Force "rig absent" ROBUSTLY: a RIG_BIN pointing at a non-existent (hence non-executable)
    # path short-circuits find_rig to None — so the helper's well-known-bin probes
    # (/usr/local/bin/rig, /opt/homebrew/bin/rig) can NOT let a real machine rig leak in.
    # Delegation tests override RIG_BIN with their own executable stub.
    os.environ["RIG_BIN"] = str(Path(tmp) / "no-rig-here")
    (Path(tmp) / ".claude").mkdir(parents=True, exist_ok=True)
    return saved


def _restore(saved: dict) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _write_fake_rig(
    path: Path,
    *,
    exit_code: int = 0,
    record: "Path | None" = None,
    provision_gate: bool = False,
    provision_unrelated: bool = False,
) -> None:
    """A stub `rig` executable that records its argv and exits `exit_code`. When
    `provision_gate` is set it SETS `core.hooksPath` + writes a COMPOSING pre-commit that runs
    an executable `review-gate` sibling — faithfully simulating rig's `git_hooks.dispatcher`
    installing the REVIEW gate, so the caller's postcondition (`_commit_gate_active`) passes.
    When `provision_unrelated` is set it instead leaves an UNRELATED executable pre-commit (no
    `review-gate` sibling, no marker) — a user's pre-existing global hook — which the
    postcondition must REJECT (it is not the review gate)."""
    record_line = f'echo "$@" >> "{record}"\n' if record is not None else ""
    provision = ""
    if provision_gate:
        provision = (
            'hd="$HOME/.config/git/hooks"; mkdir -p "$hd"; '
            'printf "#!/bin/sh\\nexec \\"$(dirname \\"$0\\")/review-gate\\" \\"$@\\"\\n" '
            '> "$hd/pre-commit"; chmod +x "$hd/pre-commit"; '
            'printf "#!/bin/sh\\nexit 0\\n" > "$hd/review-gate"; chmod +x "$hd/review-gate"; '
            'git config --global core.hooksPath "$hd";\n'
        )
    elif provision_unrelated:
        provision = (
            'hd="$HOME/.config/git/hooks"; mkdir -p "$hd"; '
            'printf "#!/bin/sh\\nexit 0\\n" > "$hd/pre-commit"; chmod +x "$hd/pre-commit"; '
            'git config --global core.hooksPath "$hd";\n'
        )
    path.write_text(
        "#!/bin/sh\n"
        f"{record_line}"
        f"{provision}"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _core_hooks_path() -> str:
    return subprocess.run(
        ["git", "config", "--global", "--get", "core.hooksPath"],
        capture_output=True, text=True, env=os.environ,
    ).stdout.strip()


# --- rig present: delegates, direct installer never runs -------------------------------


@_needs_helper
def test_install_commit_hook_delegates_scoped_and_rig_owns_the_gate():
    """rig present AND it provisions the gate -> delegate SCOPED (`apply --only git_hooks`),
    the direct installer never runs (rig owns the hook, single source of truth)."""
    if not _HELPER_AVAILABLE:  # standalone __main__ runner ignores the pytest mark above
        pytest.skip("agenttools_rig_delegate not installed; delegation covered by agent-tools#282")
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        try:
            rig_bin = Path(tmp) / "rig"
            record = Path(tmp) / "rig-calls.log"
            _write_fake_rig(rig_bin, exit_code=0, record=record, provision_gate=True)
            os.environ["RIG_BIN"] = str(rig_bin)

            with mock.patch.object(install, "_install_commit_hook_direct") as direct_stub:
                rc, _out = _capture(install.install_commit_hook)

            assert rc == 0
            direct_stub.assert_not_called()
            # Delegation is SCOPED so it never reconciles unrelated areas as a side effect.
            assert record.read_text(encoding="utf-8").strip() == "apply --only git_hooks"
        finally:
            _restore(saved)


@_needs_helper
def test_install_commit_hook_falls_back_when_rig_succeeds_but_provisions_no_gate():
    """rig present, exits 0, BUT installs no commit gate (e.g. no `git_hooks:` block in this
    repo) -> the direct installer runs so the user still gets the hook they asked for. This is
    NOT the same as a rig failure; it is 'rig doesn't manage this gate here'."""
    if not _HELPER_AVAILABLE:  # standalone __main__ runner ignores the pytest mark above
        pytest.skip("agenttools_rig_delegate not installed; delegation covered by agent-tools#282")
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        try:
            rig_bin = Path(tmp) / "rig"
            _write_fake_rig(rig_bin, exit_code=0, provision_gate=False)  # no gate provisioned
            os.environ["RIG_BIN"] = str(rig_bin)

            rc, out = _capture(install.install_commit_hook)

            assert rc == 0, out
            # the direct installer ran: it sets core.hooksPath + prints its banner
            assert _core_hooks_path() != "", "fallback direct installer must set core.hooksPath"
            assert "review: commit gate active" in out, out
        finally:
            _restore(saved)


@_needs_helper
def test_install_commit_hook_falls_back_when_rig_leaves_only_an_unrelated_hook():
    """rig exits 0 and `core.hooksPath` points at an executable pre-commit — but it is an
    UNRELATED hook (no `review-gate` sibling, no marker), e.g. a user's pre-existing global
    pre-commit. The postcondition REJECTS it (not the review gate) so delegation falls back to
    the direct installer instead of falsely reporting 'rig owns the gate'. The direct installer
    then finds a FOREIGN hook occupying the path and refuses to clobber it — a `NOT ours`
    conflict (rc 1), NEVER a silent double-write (codex review). The foreign hook is preserved.
    This drives the REAL fallback (no stub) so the asserted behavior is the production one."""
    if not _HELPER_AVAILABLE:  # standalone __main__ runner ignores the pytest mark above
        pytest.skip("agenttools_rig_delegate not installed; delegation covered by agent-tools#282")
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        try:
            rig_bin = Path(tmp) / "rig"
            _write_fake_rig(rig_bin, exit_code=0, provision_unrelated=True)
            os.environ["RIG_BIN"] = str(rig_bin)

            rc, out = _capture(install.install_commit_hook)

            # Fallback ran; the direct installer refused to overwrite the foreign hook.
            assert rc == 1, out
            assert "NOT ours" in out, out
            foreign = Path(tmp) / ".config" / "git" / "hooks" / "pre-commit"
            assert foreign.read_text(encoding="utf-8") == "#!/bin/sh\nexit 0\n"
        finally:
            _restore(saved)


@_needs_helper
def test_install_commit_hook_surfaces_a_failing_rigs_exit_code_without_falling_back():
    """A present-but-failing rig (non-zero) must NOT trigger the fallback — that would silently
    re-create the double-write the delegation exists to prevent."""
    if not _HELPER_AVAILABLE:  # standalone __main__ runner ignores the pytest mark above
        pytest.skip("agenttools_rig_delegate not installed; delegation covered by agent-tools#282")
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        try:
            rig_bin = Path(tmp) / "rig"
            _write_fake_rig(rig_bin, exit_code=2)
            os.environ["RIG_BIN"] = str(rig_bin)

            with mock.patch.object(install, "_install_commit_hook_direct") as direct_stub:
                rc, _out = _capture(install.install_commit_hook)

            assert rc == 2
            direct_stub.assert_not_called()
        finally:
            _restore(saved)


def test_install_commit_hook_consumer_contract_with_injected_helper():
    """Hermetic contract test — runs in CI WITHOUT the real helper installed. Inject a fake
    `agenttools_rig_delegate` into sys.modules and assert install_commit_hook drives it per the
    contract: rig present -> `delegate(["apply", "--only", "git_hooks"])`, and rig's non-zero
    exit is surfaced (not swallowed). Guards the production delegation branch that the two
    real-helper tests above SKIP when the helper is absent."""
    import types

    calls = {"args": None}

    fake = types.ModuleType("agenttools_rig_delegate")

    def _rig_available():
        return True

    class _Result:
        def __init__(self, rc):
            self.returncode = rc

    def _delegate(args):
        calls["args"] = list(args)
        return _Result(3)  # non-zero: must be surfaced, no fallback

    fake.rig_available = _rig_available  # type: ignore[attr-defined]
    fake.delegate = _delegate  # type: ignore[attr-defined]

    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        try:
            with mock.patch.dict(sys.modules, {"agenttools_rig_delegate": fake}):
                with mock.patch.object(install, "_install_commit_hook_direct") as direct_stub:
                    rc, _out = _capture(install.install_commit_hook)
            assert calls["args"] == ["apply", "--only", "git_hooks"]
            assert rc == 3, "a non-zero rig exit must be surfaced as-is"
            direct_stub.assert_not_called()
        finally:
            _restore(saved)


# --- _commit_gate_active(): the delegation postcondition, tested DIRECTLY --------------------
# These need NO `agenttools_rig_delegate` helper, so they run UNCONDITIONALLY in clean CI (the
# helper-gated integration tests above SKIP there) — closing the "regression guard silently
# skipped" gap the codex review flagged. Each seeds `core.hooksPath` + a hook shape and asserts
# whether the postcondition recognizes it as the REVIEW gate.


def _seed_hookspath(tmp: str) -> Path:
    hooks_dir = Path(tmp) / ".config" / "git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "config", "--global", "core.hooksPath", str(hooks_dir)],
        capture_output=True, text=True, env=os.environ,
    )
    return hooks_dir


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def test_precommit_template_carries_the_marker():
    """Pin the invariant the accept-path relies on: the direct pre-commit template embeds the
    marker `_commit_gate_active()` looks for. If the marker is ever refactored out of the body,
    this fails loudly instead of silently breaking the accept-path (opus review)."""
    assert install._PRECOMMIT_MARKER in install._PRECOMMIT


def test_commit_gate_active_false_without_hookspath():
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        try:
            assert install._commit_gate_active() is False
        finally:
            _restore(saved)


def test_commit_gate_active_rejects_an_unrelated_executable_hook():
    """A user's pre-existing global pre-commit that is NOT the review gate (no marker, no
    `review-gate` sibling) must NOT satisfy the postcondition (codex review)."""
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        try:
            hooks_dir = _seed_hookspath(tmp)
            _write_exec(hooks_dir / "pre-commit", "#!/bin/sh\n# my own linter\nexit 0\n")
            assert install._commit_gate_active() is False
        finally:
            _restore(saved)


def test_commit_gate_active_accepts_the_direct_marker_gate():
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        try:
            hooks_dir = _seed_hookspath(tmp)
            _write_exec(hooks_dir / "pre-commit", install._PRECOMMIT)
            assert install._commit_gate_active() is True
        finally:
            _restore(saved)


def test_commit_gate_active_accepts_rig_composer_with_executable_review_gate():
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        try:
            hooks_dir = _seed_hookspath(tmp)
            _write_exec(
                hooks_dir / "pre-commit",
                '#!/bin/sh\nexec "$(dirname "$0")/review-gate" "$@"\n',
            )
            _write_exec(hooks_dir / "review-gate", "#!/bin/sh\nexit 0\n")
            assert install._commit_gate_active() is True
        finally:
            _restore(saved)


def test_commit_gate_active_rejects_composer_when_review_gate_sibling_is_missing():
    """A composer that MENTIONS review-gate but has no executable `review-gate` sibling is not a
    live gate — reject it, so a half-provisioned dispatcher can't masquerade as the review gate."""
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        try:
            hooks_dir = _seed_hookspath(tmp)
            _write_exec(
                hooks_dir / "pre-commit",
                '#!/bin/sh\nexec "$(dirname "$0")/review-gate" "$@"\n',
            )  # no review-gate file written
            assert install._commit_gate_active() is False
        finally:
            _restore(saved)


def test_commit_gate_active_rejects_unrelated_hook_beside_an_orphan_review_gate_file():
    """An UNRELATED pre-commit (its body never references `review-gate`) sitting next to a
    leftover/orphan executable `review-gate` file must NOT pass: the orphan file alone is not the
    gate if the composer doesn't invoke it (codex review, round 2). Both signals are required."""
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        try:
            hooks_dir = _seed_hookspath(tmp)
            _write_exec(hooks_dir / "pre-commit", "#!/bin/sh\n# my own linter\nexit 0\n")
            _write_exec(hooks_dir / "review-gate", "#!/bin/sh\nexit 0\n")  # orphan, not invoked
            assert install._commit_gate_active() is False
        finally:
            _restore(saved)


def test_commit_gate_active_false_when_pre_commit_not_executable():
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        try:
            hooks_dir = _seed_hookspath(tmp)
            (hooks_dir / "pre-commit").write_text(install._PRECOMMIT, encoding="utf-8")
            (hooks_dir / "pre-commit").chmod(0o644)  # correct content, but git would skip it
            assert install._commit_gate_active() is False
        finally:
            _restore(saved)


# --- rig absent: direct installer runs unchanged ----------------------------------------


def test_install_commit_hook_runs_direct_installer_when_rig_is_absent():
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        try:
            # _isolated_home already forces rig absent via a non-executable RIG_BIN (which
            # short-circuits find_rig to None). Trim PATH too as belt-and-suspenders so no stray
            # `rig` on the real PATH can leak in.
            os.environ["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"

            rc, out = _capture(install.install_commit_hook)

            assert rc == 0, out
            assert "review: commit gate active" in out, out
            assert _core_hooks_path() != "", "the direct installer must set core.hooksPath"
        finally:
            _restore(saved)


# --- helper not importable: degrades to the direct installer, no crash -----------------


def test_install_commit_hook_runs_direct_installer_when_the_shared_helper_is_missing():
    """Simulate `agenttools_rig_delegate` not being installed (agent-tools absent) by
    forcing the import to fail, even on a machine where the package genuinely IS
    importable. `install_commit_hook` must degrade to today's direct install, not crash."""
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        try:
            with mock.patch.dict(sys.modules, {"agenttools_rig_delegate": None}):
                rc, out = _capture(install.install_commit_hook)
            assert rc == 0, out
            assert "review: commit gate active" in out, out
            assert _core_hooks_path() != "", "the direct installer must set core.hooksPath"
        finally:
            _restore(saved)


if __name__ == "__main__":
    # Standalone runner (this is how tests/smoke.py executes the file: `python <file>`, exit 0
    # required). It ignores pytest marks, so the in-body `pytest.skip(...)` guards raise a
    # `Skipped` exception here — catch it as SKIP (not a failure) so a host without the shared
    # helper still exits 0.
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except BaseException as exc:  # noqa: BLE001
                if type(exc).__name__ == "Skipped":
                    print(f"SKIP {name}: {exc}")
                    continue
                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{'FAILED' if failures else 'OK'}: {failures} failure(s)")
    sys.exit(1 if failures else 0)
