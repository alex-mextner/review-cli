#!/usr/bin/env python3
"""`review install-hook tg` — install/refresh the pre-send-photo review-visual gate.

Before this command existed, the descriptor + its hook script were placed by hand-copying
both files from the tg-cli repo into ~/.agents/hooks/tg/ — a second copy of the script that
silently desynced from tg-cli twice in one day (an unmerged fix landed on the live copy by
hand and was never carried forward when tg-cli moved on). The fix mirrors rig's own
`install_agent_hook` action: install ONLY the descriptor, with `cmd` rewritten to the
absolute path of the script INSIDE the source tg-cli checkout. There is no local copy of the
script to go stale — a `git pull` in that checkout IS the entire resync step. Pinned here:

  * a fresh run resolves a fake tg-cli checkout (via REVIEW_TG_CLI_SOURCE), writes ONLY the
    descriptor (never a copy of the script), and points `cmd` at the source checkout;
  * a second run on the same HOME + unchanged source reports "already configured";
  * `cmd` resolves INSIDE the source checkout (never under HOME), so a source-side script
    update (no re-install) is picked up automatically with zero re-provisioning;
  * a stale local .py copy (or a broken/dangling symlink) left over from the old manual-copy
    days is removed on install; a live WORKING symlink is left alone;
  * a missing/invalid REVIEW_TG_CLI_SOURCE, a missing/malformed/non-object descriptor, or a
    descriptor missing required fields (id/point/on_error) are each a `! conflict`
    (non-zero exit), never a crash or a silent no-op;
  * a harmless failure to remove the stale copy is a WARNING (rc 0) — not a conflict, since
    the descriptor is already correctly installed and working by that point;
  * the top-level `review install-hook` CLI dispatch (argv parsing) routes `tg` to the
    installer and anything else to a usage error, exit 2.

All HOME / source-checkout state is isolated to temp dirs (never the real machine).
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import cli, install  # noqa: E402


def _capture(fn, *a, **k) -> tuple[int, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        rc = fn(*a, **k)
    return rc, out.getvalue()


def _isolated_env(tmp: str) -> dict:
    keys = ("HOME", "REVIEW_TG_CLI_SOURCE")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ["HOME"] = tmp
    os.environ.pop("REVIEW_TG_CLI_SOURCE", None)
    return saved


def _restore(saved: dict) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


_DESCRIPTOR_TEMPLATE = {
    "id": "review-visual",
    "point": "pre-send-photo",
    "cmd": "/ABSOLUTE/PATH/TO/features/hooks/review-descriptor/pre_send_photo.py",
    "priority": 50,
    "timeout_ms": 60000,
    "on_error": "open",
    "description": "TEMPLATE",
}


def _hook_dir(root: Path) -> Path:
    return root / "features" / "hooks" / "review-descriptor"


def _make_fake_tg_cli(
    root: Path, script_body: str = "#!/usr/bin/env python3\nprint('hook')\n"
) -> Path:
    """Build a fake tg-cli checkout with the real relative layout the installer expects."""
    hook_dir = _hook_dir(root)
    hook_dir.mkdir(parents=True, exist_ok=True)
    (hook_dir / "pre_send_photo.py").write_text(script_body, encoding="utf-8")
    (hook_dir / "pre_send_photo.py").chmod(0o755)
    (hook_dir / "review-visual.pre-send-photo.json").write_text(
        json.dumps(_DESCRIPTOR_TEMPLATE, indent=2) + "\n", encoding="utf-8"
    )
    return root


def _descriptor_path(home: str) -> Path:
    return Path(home) / ".agents" / "hooks" / "tg" / "review-visual.pre-send-photo.json"


def _write_source_descriptor(source: Path, spec: dict) -> None:
    (_hook_dir(source) / "review-visual.pre-send-photo.json").write_text(
        json.dumps(spec), encoding="utf-8"
    )


# --- install_hook_tg (the installer itself) --------------------------------------------


def test_install_hook_tg_writes_descriptor_pointing_at_source_not_a_copy():
    """A fresh run: writes the descriptor (only), with `cmd` resolved to the absolute path of
    the script INSIDE the source checkout, and does NOT copy the script into ~/.agents/hooks/tg/."""
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as src:
        saved = _isolated_env(home)
        try:
            source = _make_fake_tg_cli(Path(src))
            os.environ["REVIEW_TG_CLI_SOURCE"] = str(source)
            rc, out = _capture(install.install_hook_tg)
            assert rc == 0, out
            target_dir = Path(home) / ".agents" / "hooks" / "tg"
            descriptor_path = _descriptor_path(home)
            assert descriptor_path.is_file(), out
            spec = json.loads(descriptor_path.read_text(encoding="utf-8"))
            expected_cmd = str((_hook_dir(source) / "pre_send_photo.py").resolve())
            assert spec["cmd"] == expected_cmd, (spec, out)
            # No local copy of the script — the descriptor is the ONLY file installed.
            assert not (target_dir / "pre_send_photo.py").exists(), (
                "install_hook_tg must not copy the script; cmd already points at the source"
            )
            assert "+ wrote/updated" in out, out
        finally:
            _restore(saved)


def test_install_hook_tg_second_run_reports_already_configured():
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as src:
        saved = _isolated_env(home)
        try:
            source = _make_fake_tg_cli(Path(src))
            os.environ["REVIEW_TG_CLI_SOURCE"] = str(source)
            rc1, out1 = _capture(install.install_hook_tg)
            assert rc1 == 0, out1
            rc2, out2 = _capture(install.install_hook_tg)
            assert rc2 == 0, out2
            assert "already configured" in out2, out2
            assert "already configured, nothing to do" in out2, out2
        finally:
            _restore(saved)


def test_install_hook_tg_rewrites_descriptor_when_the_source_checkout_moves():
    """An existing descriptor with a STALE `cmd` (the tg-cli checkout moved — a re-clone, a
    different machine, a relocated dev workspace) must be rewritten on the next run, not
    silently left pointing at a location that no longer holds the hook. Distinct from the
    "already configured" idempotence tests above: here the descriptor on disk genuinely is
    stale and `wrote_descriptor` must come back True."""
    with (
        tempfile.TemporaryDirectory() as home,
        tempfile.TemporaryDirectory() as src1,
        tempfile.TemporaryDirectory() as src2,
    ):
        saved = _isolated_env(home)
        try:
            source1 = _make_fake_tg_cli(Path(src1))
            os.environ["REVIEW_TG_CLI_SOURCE"] = str(source1)
            rc1, out1 = _capture(install.install_hook_tg)
            assert rc1 == 0, out1
            before_cmd = json.loads(_descriptor_path(home).read_text(encoding="utf-8"))[
                "cmd"
            ]

            source2 = _make_fake_tg_cli(Path(src2))
            os.environ["REVIEW_TG_CLI_SOURCE"] = str(source2)
            rc2, out2 = _capture(install.install_hook_tg)
            assert rc2 == 0, out2
            assert "+ wrote/updated" in out2, out2
            after_cmd = json.loads(_descriptor_path(home).read_text(encoding="utf-8"))[
                "cmd"
            ]
            assert after_cmd != before_cmd, "cmd must be rewritten to the new source"
            assert after_cmd.startswith(str(source2.resolve()) + os.sep), (
                after_cmd,
                source2,
            )
        finally:
            _restore(saved)


def test_install_hook_tg_unwritable_target_is_a_conflict_not_a_crash():
    """A `_write_if_changed` failure (e.g. an unwritable target directory) must surface as a
    `! conflict` + non-zero exit, not an uncaught OSError traceback."""
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as src:
        saved = _isolated_env(home)
        try:
            source = _make_fake_tg_cli(Path(src))
            os.environ["REVIEW_TG_CLI_SOURCE"] = str(source)
            target_dir = Path(home) / ".agents" / "hooks" / "tg"
            target_dir.mkdir(parents=True, exist_ok=True)
            target_dir.chmod(0o500)  # read+execute, no write — new file creation fails
            try:
                rc, out = _capture(install.install_hook_tg)
                assert rc == 1, out
                assert "conflict" in out, out
            finally:
                target_dir.chmod(
                    0o700
                )  # restore so TemporaryDirectory cleanup can remove it
        finally:
            _restore(saved)


def test_install_hook_tg_cmd_points_into_source_and_a_source_edit_needs_no_reinstall():
    """The whole point of the fix, proven end to end:
    1. `cmd` resolves to a path INSIDE the source checkout, never under HOME — there is no
       copy for a hook invocation to read stale content from.
    2. Editing the source script (simulating a `git pull` in tg-cli) is picked up with ZERO
       re-provisioning: the NEXT run of install-hook reports "already configured" (nothing
       needed to change) with the descriptor byte-for-byte unchanged, because `cmd` already
       pointed at the (now-updated) file all along. This is what the old manual-copy install
       could never do."""
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as src:
        saved = _isolated_env(home)
        try:
            source = _make_fake_tg_cli(Path(src), script_body="print('v1')\n")
            os.environ["REVIEW_TG_CLI_SOURCE"] = str(source)
            rc, out = _capture(install.install_hook_tg)
            assert rc == 0, out
            descriptor_path = _descriptor_path(home)
            before = descriptor_path.read_text(encoding="utf-8")
            cmd_path = Path(json.loads(before)["cmd"])
            assert str(cmd_path).startswith(str(source.resolve()) + os.sep), (
                cmd_path,
                source,
            )
            assert not str(cmd_path).startswith(str(Path(home).resolve())), (
                "cmd must point into the source checkout, never a copy under HOME"
            )

            # Simulate tg-cli evolving (a `git pull`) WITHOUT re-running install-hook.
            cmd_path.write_text("print('v2')\n", encoding="utf-8")
            assert cmd_path.read_text(encoding="utf-8") == "print('v2')\n"

            # Re-running install-hook is a no-op: `cmd` already points at the (now-updated)
            # source file, so nothing needs to change — the descriptor is untouched.
            rc2, out2 = _capture(install.install_hook_tg)
            assert rc2 == 0, out2
            assert "already configured" in out2, out2
            assert descriptor_path.read_text(encoding="utf-8") == before, (
                "a source-side edit must not require rewriting the descriptor"
            )
        finally:
            _restore(saved)


def test_install_hook_tg_removes_stale_local_script_copy():
    """A leftover local .py copy from the old hand-copy days is dead weight (the descriptor
    no longer reads it) and is exactly the trap that caused the original desync — remove it
    on install so there is only one file anyone could mistake for "the" hook script."""
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as src:
        saved = _isolated_env(home)
        try:
            source = _make_fake_tg_cli(Path(src))
            os.environ["REVIEW_TG_CLI_SOURCE"] = str(source)
            target_dir = Path(home) / ".agents" / "hooks" / "tg"
            target_dir.mkdir(parents=True, exist_ok=True)
            stale = target_dir / "pre_send_photo.py"
            stale.write_text("# stale manually-copied version\n", encoding="utf-8")

            rc, out = _capture(install.install_hook_tg)
            assert rc == 0, out
            assert not stale.exists(), "stale local copy must be removed"
            assert "removed stale local copy" in out, out
            # The removal itself IS a change — must not also claim "nothing to do".
            assert "nothing to do" not in out, out
        finally:
            _restore(saved)


def test_install_hook_tg_second_run_removes_stale_copy_without_masking_by_wrote_descriptor():
    """`removed_stale_copy` alone (NOT `wrote_descriptor`) must be what keeps the summary out
    of "nothing to do" — the only prior stale-copy-removal test was a FIRST run, where
    `wrote_descriptor=True` masks whether `removed_stale_copy` does anything at all (a
    refactor that dropped it from the `if` condition would still pass that test). Reproduce
    the realistic migration case instead: run #1 installs the descriptor (now up to date),
    run #2 finds a leftover stale copy with the descriptor UNCHANGED — summary must say
    "done", not "nothing to do" (review found this masking gap)."""
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as src:
        saved = _isolated_env(home)
        try:
            source = _make_fake_tg_cli(Path(src))
            os.environ["REVIEW_TG_CLI_SOURCE"] = str(source)
            rc0, out0 = _capture(install.install_hook_tg)
            assert rc0 == 0, out0

            target_dir = Path(home) / ".agents" / "hooks" / "tg"
            stale = target_dir / "pre_send_photo.py"
            stale.write_text(
                "# stale, added after the descriptor was already installed\n",
                encoding="utf-8",
            )

            rc, out = _capture(install.install_hook_tg)
            assert rc == 0, out
            assert "✓ already configured" in out, (
                out
            )  # the descriptor itself did NOT change
            assert not stale.exists(), "the stale copy must still be removed"
            assert "removed stale local copy" in out, out
            assert "nothing to do" not in out, out
        finally:
            _restore(saved)


def test_install_hook_tg_preserves_a_symlinked_local_copy():
    """A local .py that is already a SYMLINK (e.g. a user's own convenience link, or a
    future install layout) is left alone — only a stale REGULAR FILE copy is cleared."""
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as src:
        saved = _isolated_env(home)
        try:
            source = _make_fake_tg_cli(Path(src))
            os.environ["REVIEW_TG_CLI_SOURCE"] = str(source)
            target_dir = Path(home) / ".agents" / "hooks" / "tg"
            target_dir.mkdir(parents=True, exist_ok=True)
            link = target_dir / "pre_send_photo.py"
            link.symlink_to(_hook_dir(source) / "pre_send_photo.py")

            rc, out = _capture(install.install_hook_tg)
            assert rc == 0, out
            assert link.is_symlink(), "a symlinked local copy must not be removed"
            assert "removed stale local copy" not in out, out
        finally:
            _restore(saved)


def test_install_hook_tg_removes_a_broken_dangling_symlink():
    """A DANGLING symlink named pre_send_photo.py (target deleted) is neither a legitimate
    reference nor removable by the "regular file" branch (`exists()` follows the link and
    returns False for a broken one) — it must still be cleared, not silently ignored
    (review found: the original `exists() and not is_symlink()` gate skipped it entirely)."""
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as src:
        saved = _isolated_env(home)
        try:
            source = _make_fake_tg_cli(Path(src))
            os.environ["REVIEW_TG_CLI_SOURCE"] = str(source)
            target_dir = Path(home) / ".agents" / "hooks" / "tg"
            target_dir.mkdir(parents=True, exist_ok=True)
            link = target_dir / "pre_send_photo.py"
            gone = Path(src) / "this-target-does-not-exist.py"
            link.symlink_to(gone)
            assert link.is_symlink() and not link.exists()  # sanity: genuinely dangling

            rc, out = _capture(install.install_hook_tg)
            assert rc == 0, out
            assert not link.is_symlink() and not link.exists(), (
                "the broken symlink must be removed"
            )
            assert "removed broken symlink" in out, out
        finally:
            _restore(saved)


def test_install_hook_tg_missing_source_is_a_conflict_not_a_crash():
    with tempfile.TemporaryDirectory() as home:
        saved = _isolated_env(home)
        try:
            os.environ["REVIEW_TG_CLI_SOURCE"] = str(Path(home) / "does-not-exist")
            rc, out = _capture(install.install_hook_tg)
            assert rc == 1, out
            assert "conflict" in out, out
        finally:
            _restore(saved)


def test_install_hook_tg_missing_descriptor_is_a_conflict_not_a_crash():
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as src:
        saved = _isolated_env(home)
        try:
            source = _make_fake_tg_cli(Path(src))
            (_hook_dir(source) / "review-visual.pre-send-photo.json").unlink()
            os.environ["REVIEW_TG_CLI_SOURCE"] = str(source)
            rc, out = _capture(install.install_hook_tg)
            assert rc == 1, out
            assert "conflict" in out and "descriptor not found" in out, out
        finally:
            _restore(saved)


def test_install_hook_tg_malformed_descriptor_json_is_a_conflict_not_a_crash():
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as src:
        saved = _isolated_env(home)
        try:
            source = _make_fake_tg_cli(Path(src))
            (_hook_dir(source) / "review-visual.pre-send-photo.json").write_text(
                "{not json", encoding="utf-8"
            )
            os.environ["REVIEW_TG_CLI_SOURCE"] = str(source)
            rc, out = _capture(install.install_hook_tg)
            assert rc == 1, out
            assert "conflict" in out and "bad descriptor json" in out, out
        finally:
            _restore(saved)


def test_install_hook_tg_non_object_descriptor_json_is_a_conflict_not_a_crash():
    """A syntactically VALID but non-object descriptor (a bare list/string/null/number) must
    not crash with a TypeError on the `spec["cmd"] = ...` assignment (review found)."""
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as src:
        saved = _isolated_env(home)
        try:
            source = _make_fake_tg_cli(Path(src))
            (_hook_dir(source) / "review-visual.pre-send-photo.json").write_text(
                "[1, 2, 3]", encoding="utf-8"
            )
            os.environ["REVIEW_TG_CLI_SOURCE"] = str(source)
            rc, out = _capture(install.install_hook_tg)
            assert rc == 1, out
            assert "conflict" in out and "not a JSON object" in out, out
        finally:
            _restore(saved)


def test_install_hook_tg_descriptor_missing_id_is_a_conflict():
    """A descriptor object missing `id` must be rejected — matching tg-cli's OWN runtime
    `validateDescriptor()` contract (features/hooks/runner.ts), which silently SKIPS such a
    descriptor at load time. Installing it "successfully" would be exactly the
    looks-fine-does-nothing failure mode this command exists to prevent (review found)."""
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as src:
        saved = _isolated_env(home)
        try:
            source = _make_fake_tg_cli(Path(src))
            bad = dict(_DESCRIPTOR_TEMPLATE)
            del bad["id"]
            _write_source_descriptor(source, bad)
            os.environ["REVIEW_TG_CLI_SOURCE"] = str(source)
            rc, out = _capture(install.install_hook_tg)
            assert rc == 1, out
            assert "conflict" in out and "expected 'review-visual'" in out, out
        finally:
            _restore(saved)


def test_install_hook_tg_descriptor_missing_point_is_a_conflict():
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as src:
        saved = _isolated_env(home)
        try:
            source = _make_fake_tg_cli(Path(src))
            bad = dict(_DESCRIPTOR_TEMPLATE)
            bad["point"] = ""
            _write_source_descriptor(source, bad)
            os.environ["REVIEW_TG_CLI_SOURCE"] = str(source)
            rc, out = _capture(install.install_hook_tg)
            assert rc == 1, out
            assert "conflict" in out and "expected 'pre-send-photo'" in out, out
        finally:
            _restore(saved)


def test_install_hook_tg_descriptor_wrong_but_nonempty_point_is_a_conflict():
    """A right-SHAPED but WRONG `point` (e.g. "pre-send-message" instead of
    "pre-send-photo") must be rejected, not just an EMPTY one — tg's own dispatcher
    (features/hooks/run-photo-hooks.ts loadDescriptors) only fires a descriptor whose
    `point` matches "pre-send-photo" exactly, so a wrong-but-non-empty value would
    install "successfully" here and then silently never run (review found)."""
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as src:
        saved = _isolated_env(home)
        try:
            source = _make_fake_tg_cli(Path(src))
            bad = dict(_DESCRIPTOR_TEMPLATE)
            bad["point"] = "pre-send-message"
            _write_source_descriptor(source, bad)
            os.environ["REVIEW_TG_CLI_SOURCE"] = str(source)
            rc, out = _capture(install.install_hook_tg)
            assert rc == 1, out
            assert "conflict" in out and "expected 'pre-send-photo'" in out, out
        finally:
            _restore(saved)


def test_install_hook_tg_descriptor_wrong_but_nonempty_id_is_a_conflict():
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as src:
        saved = _isolated_env(home)
        try:
            source = _make_fake_tg_cli(Path(src))
            bad = dict(_DESCRIPTOR_TEMPLATE)
            bad["id"] = "some-other-hook"
            _write_source_descriptor(source, bad)
            os.environ["REVIEW_TG_CLI_SOURCE"] = str(source)
            rc, out = _capture(install.install_hook_tg)
            assert rc == 1, out
            assert "conflict" in out and "expected 'review-visual'" in out, out
        finally:
            _restore(saved)


def test_install_hook_tg_descriptor_invalid_on_error_is_a_conflict():
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as src:
        saved = _isolated_env(home)
        try:
            source = _make_fake_tg_cli(Path(src))
            bad = dict(_DESCRIPTOR_TEMPLATE)
            bad["on_error"] = "sometimes"
            _write_source_descriptor(source, bad)
            os.environ["REVIEW_TG_CLI_SOURCE"] = str(source)
            rc, out = _capture(install.install_hook_tg)
            assert rc == 1, out
            assert "conflict" in out and "on_error" in out, out
        finally:
            _restore(saved)


def test_install_hook_tg_descriptor_without_on_error_key_installs_fine():
    """`on_error` is OPTIONAL in the agents-hooks/v1 contract (features/hooks/types.ts) — a
    descriptor that omits it entirely (not merely `null`) must install successfully, not be
    rejected by the on_error validation added for the invalid-value case."""
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as src:
        saved = _isolated_env(home)
        try:
            source = _make_fake_tg_cli(Path(src))
            good = dict(_DESCRIPTOR_TEMPLATE)
            del good["on_error"]
            _write_source_descriptor(source, good)
            os.environ["REVIEW_TG_CLI_SOURCE"] = str(source)
            rc, out = _capture(install.install_hook_tg)
            assert rc == 0, out
            assert "+ wrote/updated" in out, out
        finally:
            _restore(saved)


def test_install_hook_tg_warns_on_non_executable_source_script():
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as src:
        saved = _isolated_env(home)
        try:
            source = _make_fake_tg_cli(Path(src))
            (_hook_dir(source) / "pre_send_photo.py").chmod(0o644)
            os.environ["REVIEW_TG_CLI_SOURCE"] = str(source)
            rc, out = _capture(install.install_hook_tg)
            assert rc == 0, out
            assert "not executable" in out, out
        finally:
            _restore(saved)


def test_install_hook_tg_second_run_with_non_executable_script_warns_not_nothing_to_do():
    """The `elif warned_non_executable` summary branch is only reachable when the descriptor
    is ALREADY up to date (wrote_descriptor=False) — the single existing non-executable test
    is a first run, where `wrote_descriptor=True` masks it behind the "done" branch. Cover the
    second-run case directly: "already configured" summary must still surface the warning,
    never a bare "nothing to do" (review found this branch had no direct test)."""
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as src:
        saved = _isolated_env(home)
        try:
            source = _make_fake_tg_cli(Path(src))
            os.environ["REVIEW_TG_CLI_SOURCE"] = str(source)
            rc0, out0 = _capture(install.install_hook_tg)
            assert rc0 == 0, out0

            (_hook_dir(source) / "pre_send_photo.py").chmod(0o644)
            rc, out = _capture(install.install_hook_tg)
            assert rc == 0, out
            assert "not executable" in out, out
            assert "but see the warning above" in out, out
            assert "nothing to do" not in out, out
        finally:
            _restore(saved)


def test_install_hook_tg_stale_copy_removal_failure_is_a_warning_not_a_conflict():
    """A working, already-installed hook (descriptor written, `cmd` correct) must NOT be
    reported as a failure just because a harmless leftover .py copy couldn't be deleted
    (e.g. an unwritable directory) — the descriptor doesn't read that file at all
    (review found: this previously returned exit 1 for a fully-working install)."""
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as src:
        saved = _isolated_env(home)
        try:
            source = _make_fake_tg_cli(Path(src))
            os.environ["REVIEW_TG_CLI_SOURCE"] = str(source)
            rc0, out0 = _capture(install.install_hook_tg)
            assert rc0 == 0, out0
            target_dir = Path(home) / ".agents" / "hooks" / "tg"
            stale = target_dir / "pre_send_photo.py"
            stale.write_text("# stale\n", encoding="utf-8")
            target_dir.chmod(0o500)  # read+execute, no write — unlink-in-dir now fails
            try:
                rc, out = _capture(install.install_hook_tg)
                assert rc == 0, out
                assert "conflict" not in out, out
                assert "warning" in out, out
                assert stale.exists(), (
                    "the unremovable stale copy is still there (expected)"
                )
                # The warning must NOT be immediately contradicted by a "nothing to do"
                # summary — review found this exact contradiction on the first version of
                # this fix (the descriptor-write and stale-copy-REMOVED paths were already
                # guarded; a failed removal ATTEMPT was not).
                assert "nothing to do" not in out, out
            finally:
                target_dir.chmod(
                    0o700
                )  # restore so TemporaryDirectory cleanup can remove it
        finally:
            _restore(saved)


def test_resolve_tg_cli_source_prefers_configured_over_candidates():
    with tempfile.TemporaryDirectory() as src:
        source = _make_fake_tg_cli(Path(src))
        resolved = install.resolve_tg_cli_source(str(source))
        assert resolved == source.resolve()


def test_resolve_tg_cli_source_falls_back_to_candidate_paths_when_no_override_is_set():
    """With NO `configured` arg and NO `REVIEW_TG_CLI_SOURCE`, resolution must fall through to
    the fixed candidate list (~/.files/repos/tg-cli, else ~/xp/tg-cli) and find a checkout
    living there — not just the explicit-override path every other test exercises."""
    with tempfile.TemporaryDirectory() as home:
        saved = _isolated_env(home)
        try:
            os.environ.pop("REVIEW_TG_CLI_SOURCE", None)
            candidate = Path(home) / ".files" / "repos" / "tg-cli"
            _make_fake_tg_cli(candidate)
            resolved = install.resolve_tg_cli_source()
            assert resolved == candidate.resolve()
        finally:
            _restore(saved)


def test_resolve_tg_cli_source_no_override_and_no_candidate_is_a_clear_error():
    with tempfile.TemporaryDirectory() as home:
        saved = _isolated_env(home)
        try:
            os.environ.pop("REVIEW_TG_CLI_SOURCE", None)
            try:
                install.resolve_tg_cli_source()
                raise AssertionError(
                    "expected ValueError when no tg-cli checkout exists anywhere"
                )
            except ValueError as exc:
                assert "no tg-cli checkout found" in str(exc)
        finally:
            _restore(saved)


def test_resolve_tg_cli_source_prefers_files_repos_candidate_over_xp_candidate():
    """When BOTH default candidates exist (no override at all), `~/.files/repos/tg-cli` —
    the checkout the `tg` binary on PATH actually runs — must win over the `~/xp/tg-cli` dev
    workspace fallback, per the documented preference order."""
    with tempfile.TemporaryDirectory() as home:
        saved = _isolated_env(home)
        try:
            os.environ.pop("REVIEW_TG_CLI_SOURCE", None)
            files_candidate = _make_fake_tg_cli(
                Path(home) / ".files" / "repos" / "tg-cli"
            )
            _make_fake_tg_cli(Path(home) / "xp" / "tg-cli")
            resolved = install.resolve_tg_cli_source()
            assert resolved == files_candidate.resolve()
        finally:
            _restore(saved)


def test_resolve_tg_cli_source_configured_wins_over_env_var_with_correct_attribution():
    """When BOTH an explicit `configured` arg and REVIEW_TG_CLI_SOURCE are set, `configured`
    wins — and a bad `configured` value is attributed to "configured" in the error, not
    misattributed to the env var (the exact fix `resolve_tg_cli_source`'s (label, raw) pairing
    exists for; review found the original always blamed REVIEW_TG_CLI_SOURCE regardless of
    which one was actually bad)."""
    with (
        tempfile.TemporaryDirectory() as home,
        tempfile.TemporaryDirectory() as env_src,
        tempfile.TemporaryDirectory() as bad,
    ):
        saved = _isolated_env(home)
        try:
            _make_fake_tg_cli(Path(env_src))
            os.environ["REVIEW_TG_CLI_SOURCE"] = str(env_src)
            # A valid `configured` wins over a valid env var.
            configured_source = _make_fake_tg_cli(Path(home) / "configured-source")
            resolved = install.resolve_tg_cli_source(str(configured_source))
            assert resolved == configured_source.resolve()

            # An INVALID `configured` is rejected — and blamed on "configured", not the (here
            # perfectly valid) REVIEW_TG_CLI_SOURCE.
            try:
                install.resolve_tg_cli_source(str(Path(bad) / "not-tg-cli"))
                raise AssertionError("expected ValueError for a bad configured path")
            except ValueError as exc:
                assert str(exc).startswith("configured "), exc
                assert "REVIEW_TG_CLI_SOURCE" not in str(exc), exc
        finally:
            _restore(saved)


def test_resolve_tg_cli_source_rejects_a_path_that_is_not_tg_cli():
    with tempfile.TemporaryDirectory() as not_tg_cli:
        try:
            install.resolve_tg_cli_source(not_tg_cli)
            raise AssertionError("expected ValueError for a non-tg-cli path")
        except ValueError as exc:
            assert "not a tg-cli checkout" in str(exc)


# --- `review install-hook` CLI dispatch (argv parsing in cli.py) -----------------------


def test_cli_dispatch_install_hook_tg_calls_the_installer():
    with mock.patch.object(cli, "install_hook_tg", return_value=0) as stub:
        rc = cli._dispatch(["install-hook", "tg"])
    assert rc == 0
    stub.assert_called_once_with()


def test_cli_dispatch_install_hook_tg_propagates_installer_exit_code():
    with mock.patch.object(cli, "install_hook_tg", return_value=1):
        rc = cli._dispatch(["install-hook", "tg"])
    assert rc == 1


def test_cli_dispatch_install_hook_with_no_subtool_is_a_usage_error():
    with mock.patch.object(cli, "install_hook_tg") as stub:
        rc = cli._dispatch(["install-hook"])
    assert rc == 2
    stub.assert_not_called()


def test_cli_dispatch_install_hook_with_an_unknown_subtool_is_a_usage_error():
    with mock.patch.object(cli, "install_hook_tg") as stub:
        rc = cli._dispatch(["install-hook", "bogus"])
    assert rc == 2
    stub.assert_not_called()


def test_cli_dispatch_install_hook_tg_with_trailing_args_is_a_usage_error():
    with mock.patch.object(cli, "install_hook_tg") as stub:
        rc = cli._dispatch(["install-hook", "tg", "extra"])
    assert rc == 2
    stub.assert_not_called()
