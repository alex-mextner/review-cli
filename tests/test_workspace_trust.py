#!/usr/bin/env python3
"""Unit tests for _ensure_workspace_trusted (claude/opus headless auto-trust).

The claude-p backend drives the interactive claude TUI under a PTY; in an
untrusted cwd it blocks on the "Quick safety check / Do you trust this folder?"
prompt and the PTY auto-accept is non-deterministic (it also fails outright
under the bypass/skip permission gates). _ensure_workspace_trusted seeds the
trust flag in ~/.claude.json directly so the prompt never fires. These tests
pin its contract without spawning claude.

Same harness style as tests/test_streaming.py: plain test_* functions run by the
__main__ block; no pytest dependency.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import backends as _backends  # noqa: E402
from reviewlib.backends import _ensure_workspace_trusted, _remove_workspace_trust  # noqa: E402


@contextlib.contextmanager
def _home(tmp: Path):
    """Point ~ at a throwaway home for the duration of the block."""
    home = tmp / "home"
    home.mkdir()
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        yield home
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


def test_seeds_trust_for_absent_project():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        with _home(tmp) as home:
            cfg = home / ".claude.json"
            cfg.write_text(json.dumps({"projects": {}}))
            proj = tmp / "repo"
            proj.mkdir()
            _ensure_workspace_trusted(proj)
            data = json.loads(cfg.read_text())
            key = os.path.realpath(str(proj))
            assert data["projects"][key]["hasTrustDialogAccepted"] is True
            assert data["projects"][key]["hasCompletedProjectOnboarding"] is True


def test_idempotent_when_already_trusted():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        with _home(tmp) as home:
            proj = tmp / "repo"
            proj.mkdir()
            key = os.path.realpath(str(proj))
            cfg = home / ".claude.json"
            cfg.write_text(json.dumps({"projects": {key: {"hasTrustDialogAccepted": True, "sentinel": 1}}}))
            before = cfg.read_text()
            _ensure_workspace_trusted(proj)
            # Already trusted → no rewrite (keeps the no-race-window guarantee;
            # never clobbers a concurrent claude session's state).
            assert cfg.read_text() == before


def test_key_is_the_resolved_real_path():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        with _home(tmp) as home:
            cfg = home / ".claude.json"
            cfg.write_text(json.dumps({"projects": {}}))
            real = tmp / "real"
            real.mkdir()
            link = tmp / "link"
            os.symlink(real, link)
            _ensure_workspace_trusted(link)
            data = json.loads(cfg.read_text())
            # claude canonicalises cwd, so the entry must be keyed by the real
            # path, not the symlink we passed (else the seed silently misses).
            assert data["projects"][os.path.realpath(str(real))]["hasTrustDialogAccepted"] is True
            assert str(link) not in data["projects"]


def test_missing_config_is_noop():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        with _home(tmp) as home:
            proj = tmp / "repo"
            proj.mkdir()
            _ensure_workspace_trusted(proj)  # no ~/.claude.json present
            # Best-effort: never fabricate a config the user didn't have.
            assert not (home / ".claude.json").exists()


def test_garbage_config_is_left_untouched():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        with _home(tmp) as home:
            cfg = home / ".claude.json"
            cfg.write_text("{ this is not json")
            proj = tmp / "repo"
            proj.mkdir()
            _ensure_workspace_trusted(proj)
            assert cfg.read_text() == "{ this is not json"


def test_preserves_other_projects_and_top_level_keys():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        with _home(tmp) as home:
            cfg = home / ".claude.json"
            other = os.path.realpath(str(tmp / "other"))
            cfg.write_text(json.dumps({
                "numStartups": 42,
                "projects": {other: {"hasTrustDialogAccepted": True, "lastCost": 0.5}},
            }))
            proj = tmp / "repo"
            proj.mkdir()
            _ensure_workspace_trusted(proj)
            data = json.loads(cfg.read_text())
            assert data["numStartups"] == 42  # top-level state untouched
            assert data["projects"][other] == {"hasTrustDialogAccepted": True, "lastCost": 0.5}
            assert data["projects"][os.path.realpath(str(proj))]["hasTrustDialogAccepted"] is True


def test_forces_onboarding_true_on_partial_untrusted_entry():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        with _home(tmp) as home:
            cfg = home / ".claude.json"
            proj = tmp / "repo"
            proj.mkdir()
            key = os.path.realpath(str(proj))
            # A prior blocked/headless attempt can leave a partial entry that is
            # NOT trusted and has onboarding=false; seeding must force BOTH true,
            # else claude still blocks on the onboarding gate.
            cfg.write_text(json.dumps({
                "projects": {key: {"hasTrustDialogAccepted": False, "hasCompletedProjectOnboarding": False}},
            }))
            _ensure_workspace_trusted(proj)
            entry = json.loads(cfg.read_text())["projects"][key]
            assert entry["hasTrustDialogAccepted"] is True
            assert entry["hasCompletedProjectOnboarding"] is True


def test_review_claude_does_not_trust_when_claude_cli_is_missing():
    # The binary is resolved before trust is touched, so a host with NEITHER `claude`
    # nor `claude-p` must raise WITHOUT permanently trusting the repo in the user's
    # global config. Mock backends._which_optional (the new resolution path) to report both absent.
    def _which_none(name):
        return None  # neither claude nor claude-p on PATH

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        with _home(tmp) as home:
            cfg = home / ".claude.json"
            cfg.write_text(json.dumps({"projects": {}}))
            proj = tmp / "repo"
            proj.mkdir()
            before = cfg.read_text()
            old_which = _backends._which_optional
            _backends._which_optional = _which_none
            try:
                raised = False
                try:
                    # CLI variant specifically: review_claude() is now a dispatcher
                    # that can route to the API backend; the CLI path is the one that
                    # must resolve the binary before touching trust.
                    _backends.review_claude_cli("claude:opus", "p", "", proj, 5)
                except RuntimeError:
                    raised = True
                assert raised, "review_claude_cli must raise when no claude CLI is present"
            finally:
                _backends._which_optional = old_which
            # No trust granted on a run that never launched the CLI.
            assert cfg.read_text() == before


def test_remove_trust_reaps_a_review_seeded_entry():
    """The seed/reap round-trip: _ensure_workspace_trusted adds an entry, _remove_workspace_trust
    drops it — so an ephemeral qa worktree leaves no dead trust path behind (review-cli#60)."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        with _home(tmp) as home:
            cfg = home / ".claude.json"
            cfg.write_text(json.dumps({"projects": {}}))
            proj = tmp / "wt"
            proj.mkdir()
            key = os.path.realpath(str(proj))
            _ensure_workspace_trusted(proj)
            assert key in json.loads(cfg.read_text())["projects"]
            _remove_workspace_trust(proj)
            assert key not in json.loads(cfg.read_text())["projects"]


def test_remove_trust_leaves_an_enriched_entry_intact():
    """If a real interactive claude session enriched the entry (extra keys beyond the trust
    flags review seeds), reap LEAVES it — we only delete what we created (no data loss)."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        with _home(tmp) as home:
            proj = tmp / "wt"
            proj.mkdir()
            key = os.path.realpath(str(proj))
            cfg = home / ".claude.json"
            enriched = {"hasTrustDialogAccepted": True, "history": ["x"], "mcpServers": {}}
            cfg.write_text(json.dumps({"projects": {key: enriched}}))
            _remove_workspace_trust(proj)
            assert json.loads(cfg.read_text())["projects"][key] == enriched


def test_remove_trust_is_noop_when_absent_or_no_config():
    """Reap is best-effort: no ~/.claude.json, or no entry for the path, is a silent no-op."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        with _home(tmp) as home:
            proj = tmp / "wt"
            proj.mkdir()
            # No config file at all -> no-op, no crash.
            _remove_workspace_trust(proj)
            # Config with an UNRELATED entry -> the unrelated entry is untouched.
            cfg = home / ".claude.json"
            other = os.path.realpath(str(tmp / "other"))
            cfg.write_text(json.dumps({"projects": {other: {"hasTrustDialogAccepted": True}}}))
            _remove_workspace_trust(proj)
            assert other in json.loads(cfg.read_text())["projects"]


def test_is_review_seeded_trust_entry_predicate():
    """The reap-safety predicate: only an entry whose keys are a subset of the seeded flags
    qualifies; anything richer (or a non-dict) does not."""
    assert _backends._is_review_seeded_trust_entry({"hasTrustDialogAccepted": True})
    assert _backends._is_review_seeded_trust_entry(
        {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True})
    assert _backends._is_review_seeded_trust_entry({})  # empty subset
    assert not _backends._is_review_seeded_trust_entry(
        {"hasTrustDialogAccepted": True, "history": []})
    assert not _backends._is_review_seeded_trust_entry("nope")


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
