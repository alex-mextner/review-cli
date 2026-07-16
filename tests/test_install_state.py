#!/usr/bin/env python3
"""install-* commands must show their INSTALLED state (ROADMAP CTO 2026-06-16).

`install-skill` / `install-commit-hook` / `register-module` must INDICATE current state, not
just offer the action: when the thing is already set up, a green ✓ + "already configured —
nothing to do" (idempotent, like rig doctor); when it (re)writes, a "+ wrote/updated". So a
user re-running install-* sees what's done vs pending at a glance. Pinned here:

  * install-skill: a fresh run reports "+ wrote/updated" per target; a SECOND run on the
    same HOME reports "✓ already configured" for every target + "nothing to do".
  * install-commit-hook: a second run reports the gate "already configured — nothing to do".
  * register-module: a second registration of the same manifest reports "already registered".

All HOME / git-config / registry state is isolated to temp dirs (never the real machine).
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import install  # noqa: E402
from reviewlib.features.visual.registry import RegistryEnv, register_module  # noqa: E402


def _capture(fn, *a, **k) -> tuple[int, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        rc = fn(*a, **k)
    return rc, out.getvalue()


def _isolated_home(tmp: str) -> dict:
    """An env with HOME (and HOME-derived paths) pointed at a temp dir + a `.claude` so the
    harness-detection writes somewhere isolated. Returns the saved env to restore.

    Also pins GIT_CONFIG_GLOBAL to a temp file: `install_commit_hook` shells out to
    `git config --global`, which writes $GIT_CONFIG_GLOBAL when set (overriding HOME) — so
    without this a CI/dev env with that var set would mutate the REAL global git config
    despite the temp HOME (codex review). With it, the global config is fully isolated.

    Also isolates `install_commit_hook` from a REAL `rig` on this machine: `install_commit_hook`
    now delegates to `rig apply` when rig is present (agent-tools#282's shared
    `agenttools_rig_delegate` helper — see `test_install_commit_hook_rig_delegate.py`). These
    tests pin the DIRECT installer's own edge cases (foreign hook, unwritable, exec-bit
    repair, …), so rig is forced ABSENT — otherwise a dev machine with rig actually installed
    (this one included) would silently delegate here instead of exercising the code under test.
    A non-executable `RIG_BIN` short-circuits find_rig to None (robust to the helper's
    well-known-bin probes like /opt/homebrew/bin/rig that a mere PATH trim would miss); PATH is
    trimmed too as belt-and-suspenders (git still resolves at /usr/bin/git)."""
    keys = ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "GIT_CONFIG_GLOBAL", "RIG_BIN", "PATH")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ["HOME"] = tmp
    os.environ["XDG_CONFIG_HOME"] = str(Path(tmp) / ".config")
    os.environ["XDG_DATA_HOME"] = str(Path(tmp) / ".local" / "share")
    os.environ["GIT_CONFIG_GLOBAL"] = str(Path(tmp) / ".gitconfig")
    os.environ["RIG_BIN"] = str(Path(tmp) / "no-rig-here")  # non-executable -> find_rig None
    os.environ["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    (Path(tmp) / ".claude").mkdir(parents=True, exist_ok=True)
    return saved


def _restore(saved: dict) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_write_if_changed_returns_false_and_does_not_rewrite_identical_content():
    """`_write_if_changed`: identical content -> returns False AND performs NO write; a
    different content -> True and writes. The no-write claim is pinned by spying on
    `Path.write_text` (not mtime, which some filesystems don't preserve precisely — glm
    review), so a spurious rewrite is caught deterministically."""
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "f.txt"
        assert install._write_if_changed(p, "hello\n") is True  # first write
        with mock.patch.object(Path, "write_text", autospec=True) as spy:
            assert install._write_if_changed(p, "hello\n") is False  # identical -> no write
            spy.assert_not_called()
        assert install._write_if_changed(p, "changed\n") is True  # different -> writes
        assert p.read_text(encoding="utf-8") == "changed\n"


def test_write_if_changed_handles_non_utf8_existing_file():
    """`_write_if_changed` must not crash when the existing target holds non-UTF-8 bytes
    (UnicodeDecodeError is a ValueError, NOT an OSError) — it treats the unreadable file as
    "needs write", returns True, and overwrites with the new content (glm review)."""
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "f.txt"
        p.write_bytes(b"\xff\xfe\x00\x01 not utf-8")
        assert install._write_if_changed(p, "clean\n") is True
        assert p.read_text(encoding="utf-8") == "clean\n"


def test_install_skill_non_utf8_harness_file_is_a_conflict_not_a_crash_or_overwrite():
    """A detected harness file (~/.claude/CLAUDE.md) holding non-UTF-8 bytes must be a
    CONFLICT: `install_agent_skill` leaves it byte-for-byte intact (no data loss), does NOT
    crash mid-loop (later targets still get configured), exits non-zero, and never says
    "nothing to do" (glm review)."""
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        try:
            claude_md = Path(tmp) / ".claude" / "CLAUDE.md"
            claude_md.parent.mkdir(parents=True, exist_ok=True)
            original = b"\xff\xfe user content, not utf-8"
            claude_md.write_bytes(original)
            rc, out = _capture(install.install_agent_skill, "review", "SKILL", "- blurb")
            # The user's file is untouched.
            assert claude_md.read_bytes() == original, "non-UTF-8 user file must be left intact"
            # Reported as a conflict, run is non-zero, no false "nothing to do".
            cm_lines = [ln for ln in out.splitlines() if str(claude_md) in ln]
            assert any("conflict" in ln for ln in cm_lines), (cm_lines, out)
            assert rc != 0, (rc, out)
            assert "nothing to do" not in out, out
            # Did NOT crash mid-loop: a later, unrelated target (the SKILL.md we own) was still
            # written.
            assert (Path(tmp) / ".agents" / "skills" / "review" / "SKILL.md").exists(), out
        finally:
            _restore(saved)


def test_append_marked_change_detection():
    """`_append_marked`: identical blurb -> False (no change); a different blurb, and a STALE
    existing block, both -> True (the regex-substitute-then-compare path is subtle)."""
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "AGENTS.md"
        assert install._append_marked(p, "review", "blurb v1") is True   # fresh insert
        assert install._append_marked(p, "review", "blurb v1") is False  # identical -> no change
        assert install._append_marked(p, "review", "blurb v2") is True   # changed content
        # A hand-edited stale block must be detected as a change and refreshed.
        body = p.read_text(encoding="utf-8").replace("blurb v2", "STALE HAND EDIT")
        p.write_text(body, encoding="utf-8")
        assert install._append_marked(p, "review", "blurb v2") is True
        assert "STALE HAND EDIT" not in p.read_text(encoding="utf-8")


def test_sessionstart_hook_present_degrades_on_odd_shapes():
    """`_sessionstart_hook_present` must degrade to False (not crash) on the shapes the diff
    comments call out: a SessionStart list of non-dict entries, and `hooks` present but
    `SessionStart` absent (glm review)."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        (home / ".claude").mkdir(parents=True, exist_ok=True)
        settings = home / ".claude" / "settings.json"
        # SessionStart is a list of non-dict entries.
        settings.write_text('{"hooks": {"SessionStart": ["not-a-dict", 42]}}', encoding="utf-8")
        assert install._sessionstart_hook_present(home) is False
        # hooks present but SessionStart absent.
        settings.write_text('{"hooks": {"PreToolUse": []}}', encoding="utf-8")
        assert install._sessionstart_hook_present(home) is False
        # A genuine marker IS detected.
        marker = install._HOOK_MARKER
        settings.write_text(
            json.dumps({"hooks": {"SessionStart": [
                {"hooks": [{"type": "command", "command": f"x {marker} y"}]}
            ]}}),
            encoding="utf-8",
        )
        assert install._sessionstart_hook_present(home) is True


def test_install_skill_reports_already_configured_on_second_run():
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        try:
            rc1, out1 = _capture(install.install_agent_skill, "review", "SKILL-MD", "- blurb")
            assert rc1 == 0, rc1
            assert "+ wrote/updated" in out1, out1
            rc2, out2 = _capture(install.install_agent_skill, "review", "SKILL-MD", "- blurb")
            assert rc2 == 0, rc2
            # Second run: every target is already configured, nothing changed.
            assert "+ wrote/updated" not in out2, out2
            assert "nothing to do" in out2, out2
            # Count, don't just substring-probe: EVERY per-target line must say "already
            # configured", and there must be as many as the first run wrote. A regression that
            # silently DROPS a target (e.g. the SessionStart branch stops appending) would keep
            # the substring present and emit no "+ wrote/updated", so a loose `in` check would
            # miss it — the per-target counts catching the drop is the point (codex review).
            target_lines_1 = [ln for ln in out1.splitlines() if "+ wrote/updated" in ln]
            target_lines_2 = [ln for ln in out2.splitlines() if "✓ already configured" in ln]
            assert target_lines_2, out2
            assert len(target_lines_2) == len(target_lines_1), (target_lines_1, target_lines_2)
        finally:
            _restore(saved)


def test_install_skill_reports_update_when_content_changes():
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        try:
            _capture(install.install_agent_skill, "review", "SKILL-MD", "- blurb")
            # A changed SKILL.md / blurb -> those targets report "+ wrote/updated" again.
            _rc, out = _capture(install.install_agent_skill, "review", "SKILL-MD-v2", "- blurb-v2")
            assert "+ wrote/updated" in out, out
        finally:
            _restore(saved)


def test_install_commit_hook_reports_already_configured_on_second_run():
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        try:
            # Isolate git's global config to HOME (git reads $HOME/.gitconfig).
            rc1, out1 = _capture(install.install_commit_hook)
            assert rc1 == 0, (rc1, out1)
            assert "wrote" in out1 or "+ " in out1, out1
            # Confirm git actually points at our hooks dir (so the second run sees it).
            cur = subprocess.run(
                ["git", "config", "--global", "--get", "core.hooksPath"],
                capture_output=True, text=True, env=os.environ,
            ).stdout.strip()
            assert cur, "core.hooksPath was not set"
            rc2, out2 = _capture(install.install_commit_hook)
            assert rc2 == 0, (rc2, out2)
            assert "already configured" in out2, out2
            assert "nothing to do" in out2, out2
        finally:
            _restore(saved)


def test_install_commit_hook_repairs_non_executable_hook_not_already_configured():
    """A pre-commit hook with our EXACT content but mode 0644 is SKIPPED by git — so it must
    NOT be reported "already configured" (a false claim). The installer re-chmods it instead
    (codex review). Pins: the exec bit is set after a rerun, and the rerun did NOT report the
    hook as already-configured."""
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        try:
            _capture(install.install_commit_hook)
            hooks_path = subprocess.run(
                ["git", "config", "--global", "--get", "core.hooksPath"],
                capture_output=True, text=True, env=os.environ,
            ).stdout.strip()
            pre_commit = Path(hooks_path) / "pre-commit"
            pre_commit.chmod(0o644)  # strip the exec bit
            _rc, out = _capture(install.install_commit_hook)
            assert os.access(pre_commit, os.X_OK), "the rerun must repair the exec bit"
            # The hook line must NOT claim already-configured (it was non-executable).
            hook_line = next(ln for ln in out.splitlines() if str(pre_commit) in ln)
            assert "already configured" not in hook_line, hook_line
        finally:
            _restore(saved)


def test_install_skill_reports_conflict_not_already_for_wrong_link():
    """If `~/.claude/skills/<name>` is a regular file (or a symlink to the wrong target), the
    skill link must be reported as a CONFLICT, never a silent "already configured — nothing
    to do" (codex review): Claude would otherwise be wired to the wrong thing."""
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        try:
            skills = Path(tmp) / ".claude" / "skills"
            skills.mkdir(parents=True, exist_ok=True)
            (skills / "review").write_text("NOT our symlink\n", encoding="utf-8")  # wrong file
            rc, out = _capture(install.install_agent_skill, "review", "SKILL", "- blurb")
            link_lines = [ln for ln in out.splitlines() if str(skills / "review") in ln]
            assert link_lines, out
            assert any("conflict" in ln for ln in link_lines), link_lines
            assert not any("already configured" in ln for ln in link_lines), link_lines
            # A conflict means a target is unconfigured: the run must NOT claim "nothing to
            # do" and must return non-zero (codex review).
            assert "nothing to do" not in out, out
            assert rc != 0, (rc, out)
        finally:
            _restore(saved)


def test_install_skill_reports_conflict_for_wrong_target_symlink():
    """`~/.claude/skills/<name>` as a symlink to the WRONG target is a conflict (not "already
    configured"), the run exits non-zero, and the summary never says "nothing to do"."""
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        try:
            skills = Path(tmp) / ".claude" / "skills"
            skills.mkdir(parents=True, exist_ok=True)
            (skills / "review").symlink_to(Path("/tmp/some-wrong-target"))
            rc, out = _capture(install.install_agent_skill, "review", "SKILL", "- blurb")
            link_lines = [ln for ln in out.splitlines() if str(skills / "review") in ln]
            assert any("conflict" in ln for ln in link_lines), (link_lines, out)
            assert not any("already configured" in ln for ln in link_lines), link_lines
            assert "nothing to do" not in out, out
            assert rc != 0, (rc, out)
        finally:
            _restore(saved)


def test_install_skill_symlink_creation_failure_is_a_conflict():
    """If creating the Claude skill symlink fails (OSError), it must be a CONFLICT (exit
    non-zero, no "nothing to do") — not a silent skip that a rerun calls "already configured"
    (codex review). The stub only raises for the EXPECTED skill-link target (`~/.claude/
    skills/review`), so adding a future, unrelated symlink target elsewhere in the install
    path can't silently flip this to a false pass."""
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        # The symlink branch only runs when ~/.claude/skills exists.
        skill_link = Path(tmp) / ".claude" / "skills" / "review"
        skill_link.parent.mkdir(parents=True, exist_ok=True)
        orig = Path.symlink_to

        def _boom(self, *a, **k):
            # Only fail the skill link under test; defer every other symlink to the real impl
            # so the stub stays scoped to the call site this test is about.
            if self == skill_link:
                raise OSError("simulated symlink failure")
            return orig(self, *a, **k)

        # patch.object auto-restores even on KeyboardInterrupt / a runner that bypasses a bare
        # `finally`, so the builtin can't leak into sibling tests in the same process (glm
        # review). autospec keeps the bound-method signature intact.
        try:
            with mock.patch.object(Path, "symlink_to", autospec=True, side_effect=_boom):
                rc, out = _capture(install.install_agent_skill, "review", "SKILL", "- blurb")
            assert "conflict" in out, out
            assert "nothing to do" not in out, out
            assert rc != 0, (rc, out)
        finally:
            _restore(saved)


def test_install_skill_tolerates_malformed_settings_json():
    """install-skill must not CRASH when ~/.claude/settings.json is valid JSON but a malformed
    shape (`{"hooks": "bad"}`) — `_sessionstart_hook_present` / `_ensure_sessionstart_hook`
    degrade to not-present rather than raising on `.get()` (codex review). It returns a
    structured CONFLICT exit for the unconfigurable hook (covered in detail by
    `test_install_skill_sessionstart_unwritable_is_a_conflict`), but the guarantee pinned HERE
    is "no traceback / no exception", whatever the exit code."""
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        try:
            settings = Path(tmp) / ".claude" / "settings.json"
            settings.write_text('{"hooks": "bad"}', encoding="utf-8")
            # The point: this call completes WITHOUT raising (a crash would propagate here).
            _rc, _out = _capture(install.install_agent_skill, "review", "SKILL", "- blurb")
        finally:
            _restore(saved)


def test_register_module_reports_already_registered_on_second_run():
    with tempfile.TemporaryDirectory() as tmp:
        manifest = Path(tmp) / "manifest.json"
        manifest.write_text('{"name": "t", "modules": []}\n', encoding="utf-8")
        env = RegistryEnv(global_registry_path=Path(tmp) / "registry.json")
        rc1, out1 = _capture(register_module, str(manifest), env=env)
        assert rc1 == 0, rc1
        assert "+ registered" in out1, out1
        rc2, out2 = _capture(register_module, str(manifest), env=env)
        assert rc2 == 0, rc2
        assert "already registered" in out2, out2
        assert "nothing to do" in out2, out2


def test_register_module_registers_a_second_different_manifest():
    """A DIFFERENT second manifest must register fresh (not be falsely reported "already
    registered") — guards against a membership check on the wrong field (glm review)."""
    with tempfile.TemporaryDirectory() as tmp:
        env = RegistryEnv(global_registry_path=Path(tmp) / "registry.json")
        m1 = Path(tmp) / "m1.json"
        m2 = Path(tmp) / "m2.json"
        m1.write_text('{"name": "a", "modules": []}\n', encoding="utf-8")
        m2.write_text('{"name": "b", "modules": []}\n', encoding="utf-8")
        _capture(register_module, str(m1), env=env)
        rc, out = _capture(register_module, str(m2), env=env)
        assert rc == 0, rc
        assert "+ registered" in out, out
        assert "already registered" not in out, out


def test_install_skill_unwritable_target_is_a_conflict_not_a_crash():
    """If writing an OWN target (SKILL.md) fails with OSError (read-only FS / EPERM / ENOSPC),
    `install_agent_skill` records a `! conflict` and exits non-zero rather than crashing
    mid-loop with a traceback (glm review). The stub fails ONLY the SKILL.md write, so other
    targets still proceed."""
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        skill_md = Path(tmp) / ".agents" / "skills" / "review" / "SKILL.md"
        orig = Path.write_text

        def _boom(self, *a, **k):
            if self == skill_md:
                raise OSError("simulated read-only filesystem")
            return orig(self, *a, **k)

        try:
            with mock.patch.object(Path, "write_text", autospec=True, side_effect=_boom):
                rc, out = _capture(install.install_agent_skill, "review", "SKILL", "- blurb")
            sk_lines = [ln for ln in out.splitlines() if str(skill_md) in ln]
            assert any("conflict" in ln for ln in sk_lines), (sk_lines, out)
            assert rc != 0, (rc, out)
            assert "nothing to do" not in out, out
        finally:
            _restore(saved)


def test_install_skill_sessionstart_write_failure_is_a_conflict_not_a_crash():
    """If `_ensure_sessionstart_hook` can PARSE settings.json but cannot WRITE it (locked /
    read-only / failed .bak write -> OSError), `install_agent_skill` must report a `! conflict`
    + non-zero exit, not abort with a traceback (codex review). Stub the settings write to
    raise; the parse path (read) still succeeds."""
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        settings = Path(tmp) / ".claude" / "settings.json"
        settings.write_text("{}", encoding="utf-8")  # valid, parseable
        orig = Path.write_text

        def _boom(self, *a, **k):
            # Fail writing settings.json (and its .bak), leave all other writes alone.
            if self.name in ("settings.json", "settings.json.bak"):
                raise OSError("simulated locked settings.json")
            return orig(self, *a, **k)

        try:
            with mock.patch.object(Path, "write_text", autospec=True, side_effect=_boom):
                rc, out = _capture(install.install_agent_skill, "review", "SKILL", "- blurb")
            sess_lines = [ln for ln in out.splitlines() if "SessionStart hook" in ln]
            assert any("conflict" in ln for ln in sess_lines), (sess_lines, out)
            # Exactly one SessionStart conflict line (the write-error path must not ALSO trigger
            # the not-present `else` branch — no duplicate conflict).
            assert sum("SessionStart hook" in ln and "conflict" in ln
                       for ln in out.splitlines()) == 1, out
            assert rc != 0, (rc, out)
            assert "nothing to do" not in out, out
        finally:
            _restore(saved)


def test_install_commit_hook_git_config_failure_is_a_conflict():
    """If `git config --global core.hooksPath` fails (locked/corrupt global config), the hook
    file exists but git never points at it — so the installer must NOT claim "gate active":
    it reports a `! conflict` and exits non-zero (codex review)."""
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        orig_run = subprocess.run

        def _fake_run(cmd, *a, **k):
            # Let the "--get core.hooksPath" probe return empty (unset); make the SET fail.
            if cmd[:3] == ["git", "config", "--global"] and "--get" not in cmd and len(cmd) >= 5:
                class _R:
                    returncode = 1
                    stdout = ""
                    stderr = "fatal: could not lock config file"
                return _R()
            return orig_run(cmd, *a, **k)

        try:
            with mock.patch.object(subprocess, "run", side_effect=_fake_run):
                rc, out = _capture(install.install_commit_hook)
            assert rc != 0, (rc, out)
            assert "conflict" in out, out
            assert "gate active" not in out, out
        finally:
            _restore(saved)


def test_install_skill_non_utf8_settings_json_does_not_crash():
    """A `~/.claude/settings.json` holding non-UTF-8 bytes must not crash install-skill:
    `_ensure_sessionstart_hook` / `_sessionstart_hook_present` degrade to "could not write"
    (UnicodeDecodeError is a ValueError, not OSError), so the SessionStart target becomes a
    `! conflict` (non-zero exit), no traceback, no "nothing to do" (glm review)."""
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        try:
            settings = Path(tmp) / ".claude" / "settings.json"
            settings.write_bytes(b"\xff\xfe not utf-8 at all")
            # The call completes without raising (a crash would propagate here).
            rc, out = _capture(install.install_agent_skill, "review", "SKILL", "- blurb")
            sess_lines = [ln for ln in out.splitlines() if "SessionStart hook" in ln]
            assert any("conflict" in ln for ln in sess_lines), (sess_lines, out)
            assert rc != 0, (rc, out)
            assert "nothing to do" not in out, out
        finally:
            _restore(saved)


def test_register_module_repairs_a_corrupt_manifests_shape():
    """A registry whose `manifests` is not a list (corrupt/legacy shape) is normalized to a
    valid list and the result is persisted (glm review: normalization must reach disk)."""
    with tempfile.TemporaryDirectory() as tmp:
        reg = Path(tmp) / "registry.json"
        reg.write_text('{"manifests": "garbage-not-a-list"}\n', encoding="utf-8")
        env = RegistryEnv(global_registry_path=reg)
        manifest = Path(tmp) / "m.json"
        manifest.write_text('{"name": "t", "modules": []}\n', encoding="utf-8")
        rc, out = _capture(register_module, str(manifest), env=env)
        assert rc == 0, (rc, out)
        on_disk = json.loads(reg.read_text(encoding="utf-8"))
        assert isinstance(on_disk["manifests"], list), on_disk
        assert str(manifest.resolve()) in on_disk["manifests"], on_disk


def test_install_commit_hook_refreshes_a_stale_marked_hook():
    """A pre-commit hook that carries OUR marker but has STALE body must be refreshed: the
    rerun reports "+ wrote" (not "already configured") and the on-disk content becomes the
    current `_PRECOMMIT` (glm review: pins the `body != _PRECOMMIT` update path)."""
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        try:
            install.install_commit_hook()  # fresh install
            hooks_path = subprocess.run(
                ["git", "config", "--global", "--get", "core.hooksPath"],
                capture_output=True, text=True, env=os.environ,
            ).stdout.strip()
            pre_commit = Path(hooks_path) / "pre-commit"
            # Replace the body with a stale-but-marked version.
            stale = install._PRECOMMIT_MARKER + "\n# STALE OLD BODY\nexit 0\n"
            pre_commit.write_text(stale, encoding="utf-8")
            _rc, out = _capture(install.install_commit_hook)
            hook_line = next(ln for ln in out.splitlines() if str(pre_commit) in ln)
            assert "wrote" in hook_line, hook_line
            assert "already configured" not in hook_line, hook_line
            assert pre_commit.read_text(encoding="utf-8") == install._PRECOMMIT
        finally:
            _restore(saved)


def test_install_commit_hook_unwritable_is_a_conflict_not_a_crash():
    """If writing the pre-commit hook fails with OSError (read-only FS / EPERM / ENOSPC),
    `install_commit_hook` reports a `! conflict` and exits non-zero rather than crashing with a
    traceback and falsely printing "gate active" (glm review — same contract as install-skill)."""
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        orig = Path.write_text

        def _boom(self, *a, **k):
            if self.name == "pre-commit":
                raise OSError("simulated read-only filesystem")
            return orig(self, *a, **k)

        try:
            with mock.patch.object(Path, "write_text", autospec=True, side_effect=_boom):
                rc, out = _capture(install.install_commit_hook)
            assert rc != 0, (rc, out)
            assert "conflict" in out, out
            assert "gate active" not in out, out
        finally:
            _restore(saved)


def test_register_module_unwritable_registry_is_a_conflict_not_a_crash():
    """An unwritable registry must be a `! conflict` + non-zero exit, not a traceback (glm
    review — same install-* contract)."""
    with tempfile.TemporaryDirectory() as tmp:
        manifest = Path(tmp) / "m.json"
        manifest.write_text('{"name": "t", "modules": []}\n', encoding="utf-8")
        env = RegistryEnv(global_registry_path=Path(tmp) / "registry.json")
        orig = Path.write_text

        def _boom(self, *a, **k):
            if self.name == "registry.json":
                raise OSError("simulated read-only filesystem")
            return orig(self, *a, **k)

        with mock.patch.object(Path, "write_text", autospec=True, side_effect=_boom):
            rc, _out = _capture(register_module, str(manifest), env=env)
        assert rc == 1, rc


def test_install_commit_hook_refuses_a_foreign_pre_commit():
    """A pre-existing pre-commit hook that is NOT ours must NOT be overwritten — the installer
    refuses (exit 1) and leaves the foreign hook intact (glm review: this path was untested)."""
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        try:
            hooks_dir = Path(tmp) / ".config" / "git" / "hooks"
            hooks_dir.mkdir(parents=True, exist_ok=True)
            foreign = hooks_dir / "pre-commit"
            foreign.write_text("#!/bin/sh\necho not-ours\n", encoding="utf-8")
            subprocess.run(
                ["git", "config", "--global", "core.hooksPath", str(hooks_dir)],
                env=os.environ, check=True,
            )
            rc, out = _capture(install.install_commit_hook)
            assert rc == 1, (rc, out)
            assert "NOT ours" in out, out
            # The foreign hook must be left byte-for-byte intact.
            assert foreign.read_text(encoding="utf-8") == "#!/bin/sh\necho not-ours\n"
        finally:
            _restore(saved)


def test_install_skill_sessionstart_unwritable_is_a_conflict():
    """If the SessionStart hook can neither be written (settings.json is valid JSON but a
    malformed shape that blocks the hook write) nor be found present, that target is a
    CONFLICT — the run exits non-zero and never claims "nothing to do" (glm review: a silent
    drop falsely reported the install complete)."""
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        try:
            # `{"hooks": "bad"}` is valid JSON but a shape _ensure_sessionstart_hook refuses to
            # mutate (returns False) and _sessionstart_hook_present reads as not-present.
            settings = Path(tmp) / ".claude" / "settings.json"
            settings.write_text('{"hooks": "bad"}', encoding="utf-8")
            rc, out = _capture(install.install_agent_skill, "review", "SKILL", "- blurb")
            sess_lines = [ln for ln in out.splitlines() if "SessionStart hook" in ln]
            assert any("conflict" in ln for ln in sess_lines), (sess_lines, out)
            assert "nothing to do" not in out, out
            assert rc != 0, (rc, out)
        finally:
            _restore(saved)


def test_install_skill_relative_target_symlink_is_already_configured():
    """The common path: an existing `~/.claude/skills/<name>` symlink whose stored target is
    the exact relative `want` (`../../.agents/skills/<name>`, the form `symlink_to(want)`
    itself writes) reports "already configured" + rc 0. Guards the `points_at == want`
    comparison against a regression that "normalizes" the target before comparing (glm)."""
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        try:
            skills = Path(tmp) / ".claude" / "skills"
            skills.mkdir(parents=True, exist_ok=True)
            want = Path("..") / ".." / ".agents" / "skills" / "review"
            (skills / "review").symlink_to(want)  # the exact relative form
            rc, out = _capture(install.install_agent_skill, "review", "SKILL", "- blurb")
            link_lines = [ln for ln in out.splitlines() if str(skills / "review") in ln]
            assert link_lines, out
            assert any("already configured" in ln for ln in link_lines), (link_lines, out)
            assert not any("conflict" in ln for ln in link_lines), link_lines
            assert rc == 0, (rc, out)
        finally:
            _restore(saved)


def test_install_skill_absolute_target_symlink_is_already_configured_not_conflict():
    """A Claude skill symlink written with an ABSOLUTE target that resolves to the SAME
    directory as our relative `want` is already-configured, not a conflict (glm review:
    byte-equality on the stored target was too strict)."""
    with tempfile.TemporaryDirectory() as tmp:
        saved = _isolated_home(tmp)
        try:
            skills = Path(tmp) / ".claude" / "skills"
            skills.mkdir(parents=True, exist_ok=True)
            # The relative `want` resolves to ~/.agents/skills/review; point an ABSOLUTE
            # symlink at that exact resolved directory.
            abs_target = Path(tmp) / ".agents" / "skills" / "review"
            abs_target.mkdir(parents=True, exist_ok=True)
            (skills / "review").symlink_to(abs_target)
            rc, out = _capture(install.install_agent_skill, "review", "SKILL", "- blurb")
            link_lines = [ln for ln in out.splitlines() if str(skills / "review") in ln]
            assert link_lines, out
            assert not any("conflict" in ln for ln in link_lines), (link_lines, out)
            assert any("already configured" in ln for ln in link_lines), (link_lines, out)
            assert rc == 0, (rc, out)
        finally:
            _restore(saved)


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
