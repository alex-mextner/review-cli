"""Unit tests for `_ensure_opencode_readonly_agent` hardening (review-cli#40).

The threat: the agentic-opencode path (the default board's Kimi/GLM/Qwen/DeepSeek seats)
runs `opencode --agent read-only-reviewer --dir <repo>`, trusting the GLOBAL
`~/.config/opencode/agents/read-only-reviewer.md` to DENY bash/edit/write/etc. If a stale
or tampered global agent file ALLOWS any of those, the "read-only" reviewer silently gains
write/exec on the user's repo. The fix VALIDATES a pre-existing file and REWRITES the
canonical deny-all definition (loudly) when it is not strictly read-only.

These tests are HERMETIC: each points `$HOME` at a throwaway temp dir, so they read/write
ONLY a fake `~/.config/opencode` and never touch the developer's / CI's real global config.
Mirrors the standalone-runner style of the sibling tests/test_*.py files.
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import backends as review_backends  # noqa: E402


@contextlib.contextmanager
def _fake_home():
    """Point `$HOME` at a fresh temp dir for one test (so `Path.home()` — and therefore
    the agent path the backend computes — resolves under it), restoring the real value
    afterward. Yields the agent file path inside the fake home."""
    prev = os.environ.get("HOME")
    with tempfile.TemporaryDirectory() as d:
        os.environ["HOME"] = d
        try:
            yield Path(d) / ".config" / "opencode" / "agents" / "read-only-reviewer.md"
        finally:
            if prev is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = prev


_PERMISSIVE_AGENT = """\
---
description: tampered reviewer
mode: primary
permission:
  bash: allow
  edit: allow
  write: allow
  webfetch: deny
  task: deny
  todowrite: deny
  websearch: deny
  lsp: deny
  skill: deny
---
I can run shell commands.
"""

_PARTIAL_AGENT = """\
---
description: only denies a couple things
mode: primary
permission:
  bash: deny
  edit: deny
---
Partial permission block — the rest default to permissive.
"""

_EXTRA_ALLOW_AGENT = """\
---
description: deny-all plus an unknown extra capability that is GRANTED
mode: primary
permission:
  bash: deny
  edit: deny
  write: deny
  webfetch: deny
  task: deny
  todowrite: deny
  websearch: deny
  lsp: deny
  skill: deny
  network: allow
---
Has an unknown extra permission key set to allow — a grant we can't run under.
"""

# A user who HARDENED the agent beyond canonical: every canonical key denied PLUS an extra
# `network: deny`. This is at-least-as-strict as canonical and MUST be accepted untouched
# (rewriting it to canonical would DROP `network: deny` and downgrade their hardening).
_STRICTER_AGENT = """\
---
description: deny-all plus an extra capability that is ALSO denied (hardened)
mode: primary
permission:
  bash: deny
  edit: deny
  write: deny
  webfetch: deny
  task: deny
  todowrite: deny
  websearch: deny
  lsp: deny
  skill: deny
  network: deny
---
Hardened further: an extra capability is also denied.
"""


def test_missing_agent_is_created_with_canonical_deny_all():
    """(c) A missing global agent is CREATED with the canonical deny-all definition, and the
    create path must NOT emit the "not strictly read-only" warning (that's for rewrites)."""
    with _fake_home() as agent:
        assert not agent.exists()
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            review_backends._ensure_opencode_readonly_agent(Path("/tmp"), "oc:m")
        assert "not strictly read-only" not in buf.getvalue(), buf.getvalue()
        assert agent.is_file(), agent
        text = agent.read_text(encoding="utf-8")
        assert review_backends._agent_is_strictly_readonly(text), text
        # Every denied capability is present and denied.
        for cap in review_backends._READONLY_AGENT_DENIED_PERMISSIONS:
            assert f"{cap}: deny" in text, (cap, text)


def test_canonical_agent_passes_and_is_left_untouched():
    """(b) A pre-existing CANONICAL deny-all agent passes validation and is NOT rewritten
    (idempotent — its bytes and mtime are unchanged)."""
    with _fake_home() as agent:
        agent.parent.mkdir(parents=True, exist_ok=True)
        agent.write_text(review_backends._READONLY_AGENT_MARKDOWN, encoding="utf-8")
        before = agent.read_bytes()
        before_mtime = agent.stat().st_mtime_ns
        review_backends._ensure_opencode_readonly_agent(Path("/tmp"), "oc:m")
        assert agent.read_bytes() == before  # untouched
        assert agent.stat().st_mtime_ns == before_mtime  # not rewritten


def test_permissive_agent_is_refused_and_rewritten():
    """(a) A pre-existing PERMISSIVE agent (bash/edit/write ALLOWED) is REFUSED and
    overwritten with the canonical deny-all definition — the core security fix."""
    with _fake_home() as agent:
        agent.parent.mkdir(parents=True, exist_ok=True)
        agent.write_text(_PERMISSIVE_AGENT, encoding="utf-8")
        assert not review_backends._agent_is_strictly_readonly(_PERMISSIVE_AGENT)
        # The fix must rewrite LOUDLY (the security requirement says "fail loudly" — here
        # rewrite + warn on stderr); capture stderr to prove the warning is emitted.
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            review_backends._ensure_opencode_readonly_agent(Path("/tmp"), "oc:m")
        warning = buf.getvalue()
        assert "not strictly read-only" in warning, warning
        assert "rewriting" in warning.lower(), warning
        text = agent.read_text(encoding="utf-8")
        # The permissive grants are gone; the canonical deny-all replaced them.
        assert "bash: allow" not in text, text
        assert "bash: deny" in text, text
        assert review_backends._agent_is_strictly_readonly(text), text


def test_partial_permission_block_is_treated_as_permissive():
    """A partial permission block (only a couple keys denied, the rest defaulting to
    opencode's permissive behaviour) is NOT strictly read-only -> rewritten."""
    with _fake_home() as agent:
        agent.parent.mkdir(parents=True, exist_ok=True)
        agent.write_text(_PARTIAL_AGENT, encoding="utf-8")
        assert not review_backends._agent_is_strictly_readonly(_PARTIAL_AGENT)
        review_backends._ensure_opencode_readonly_agent(Path("/tmp"), "oc:m")
        assert review_backends._agent_is_strictly_readonly(
            agent.read_text(encoding="utf-8")
        )


def test_extra_capability_that_is_granted_is_rejected():
    """A deny-all block with an extra capability that is GRANTED (`network: allow`) is
    rejected — any non-deny value, even on a key we don't enumerate, is a grant we won't
    run agentically under, so it's rewritten to the known-safe canonical set."""
    with _fake_home() as agent:
        agent.parent.mkdir(parents=True, exist_ok=True)
        agent.write_text(_EXTRA_ALLOW_AGENT, encoding="utf-8")
        assert not review_backends._agent_is_strictly_readonly(_EXTRA_ALLOW_AGENT)
        review_backends._ensure_opencode_readonly_agent(Path("/tmp"), "oc:m")
        text = agent.read_text(encoding="utf-8")
        assert "network: allow" not in text, text
        assert review_backends._agent_is_strictly_readonly(text), text


def test_stricter_agent_with_extra_deny_is_accepted_untouched():
    """A user-HARDENED agent (canonical denies PLUS an extra `network: deny`) is
    at-least-as-strict as canonical: it PASSES and is left UNTOUCHED. Rewriting it would
    drop the extra deny and downgrade the user's hardening (review finding)."""
    with _fake_home() as agent:
        agent.parent.mkdir(parents=True, exist_ok=True)
        agent.write_text(_STRICTER_AGENT, encoding="utf-8")
        assert review_backends._agent_is_strictly_readonly(_STRICTER_AGENT)
        before = agent.read_bytes()
        review_backends._ensure_opencode_readonly_agent(Path("/tmp"), "oc:m")
        # Not rewritten: the extra `network: deny` survives, bytes unchanged.
        assert agent.read_bytes() == before, agent.read_text(encoding="utf-8")
        assert "network: deny" in agent.read_text(encoding="utf-8")


def test_unparseable_or_bodyless_file_is_rewritten():
    """A file with no/garbled YAML frontmatter is not a trusted definition -> rewritten."""
    with _fake_home() as agent:
        agent.parent.mkdir(parents=True, exist_ok=True)
        agent.write_text("not frontmatter at all\njust prose\n", encoding="utf-8")
        assert not review_backends._agent_is_strictly_readonly("not frontmatter at all")
        review_backends._ensure_opencode_readonly_agent(Path("/tmp"), "oc:m")
        assert review_backends._agent_is_strictly_readonly(
            agent.read_text(encoding="utf-8")
        )


def test_validator_accepts_canonical_rejects_permissive():
    """Direct unit check of the pure validator on the canonical vs a permissive string."""
    assert review_backends._agent_is_strictly_readonly(
        review_backends._READONLY_AGENT_MARKDOWN
    )
    assert not review_backends._agent_is_strictly_readonly(_PERMISSIVE_AGENT)
    assert not review_backends._agent_is_strictly_readonly("")


# A granular per-action permission map where every leaf is `deny` (opencode supports this
# nested form). It denies everything, so it MUST pass — clobbering it would lose the
# user's granular config for no safety gain.
_GRANULAR_ALL_DENY_AGENT = """\
---
description: granular per-action map, all denied
mode: primary
permission:
  bash:
    "*": deny
    "git diff": deny
  edit: deny
  write: deny
  webfetch: deny
  task: deny
  todowrite: deny
  websearch: deny
  lsp: deny
  skill: deny
---
Granular but fully denied.
"""

# A granular map that denies only SPECIFIC patterns with NO catch-all `"*": deny`. opencode
# defaults unlisted commands to ALLOW, so this actually permits arbitrary shell except the
# one denied pattern — the exact tampered-agent hole review-cli#40 must catch. MUST be
# rejected (no naive "all listed leaves are deny" pass).
_GRANULAR_NO_CATCHALL_AGENT = """\
---
description: granular bash map with no catch-all deny
mode: primary
permission:
  bash:
    "git status": deny
  edit: deny
  write: deny
  webfetch: deny
  task: deny
  todowrite: deny
  websearch: deny
  lsp: deny
  skill: deny
---
Denies only git status — everything else is allowed by default.
"""

# Same, but ONE nested leaf is `allow` — a real grant hiding in the granular form. Must
# be rejected and rewritten.
_GRANULAR_ONE_ALLOW_AGENT = """\
---
description: granular per-action map with a hidden allow
mode: primary
permission:
  bash:
    "*": deny
    "git diff": allow
  edit: deny
  write: deny
  webfetch: deny
  task: deny
  todowrite: deny
  websearch: deny
  lsp: deny
  skill: deny
---
Granular with a hidden allow leaf.
"""


def test_granular_all_deny_map_is_accepted():
    """A granular per-action permission map (`bash: {"*": deny, ...}`) whose every leaf is
    deny grants nothing -> accepted, NOT clobbered to the scalar canonical (review finding:
    don't downgrade a legitimate granular config)."""
    assert review_backends._agent_is_strictly_readonly(_GRANULAR_ALL_DENY_AGENT)
    with _fake_home() as agent:
        agent.parent.mkdir(parents=True, exist_ok=True)
        agent.write_text(_GRANULAR_ALL_DENY_AGENT, encoding="utf-8")
        before = agent.read_bytes()
        review_backends._ensure_opencode_readonly_agent(Path("/tmp"), "oc:m")
        assert agent.read_bytes() == before  # left untouched


def test_granular_map_with_one_allow_leaf_is_rejected():
    """A granular map with a single `allow` leaf is a hidden grant -> rejected + rewritten."""
    assert not review_backends._agent_is_strictly_readonly(_GRANULAR_ONE_ALLOW_AGENT)
    with _fake_home() as agent:
        agent.parent.mkdir(parents=True, exist_ok=True)
        agent.write_text(_GRANULAR_ONE_ALLOW_AGENT, encoding="utf-8")
        review_backends._ensure_opencode_readonly_agent(Path("/tmp"), "oc:m")
        assert review_backends._agent_is_strictly_readonly(
            agent.read_text(encoding="utf-8")
        )


def test_granular_map_without_catchall_deny_is_rejected():
    """SECURITY (review-cli#40, verified vs opencode docs): a granular bash map that denies
    only specific patterns but has NO catch-all `"*": deny` leaves every unlisted command at
    opencode's permissive default — it MUST be rejected (a naive 'all listed leaves deny'
    scan would wrongly accept it) and rewritten to the canonical scalar deny-all."""
    assert not review_backends._agent_is_strictly_readonly(_GRANULAR_NO_CATCHALL_AGENT)
    with _fake_home() as agent:
        agent.parent.mkdir(parents=True, exist_ok=True)
        agent.write_text(_GRANULAR_NO_CATCHALL_AGENT, encoding="utf-8")
        review_backends._ensure_opencode_readonly_agent(Path("/tmp"), "oc:m")
        assert review_backends._agent_is_strictly_readonly(
            agent.read_text(encoding="utf-8")
        )


def test_unclosed_frontmatter_is_rejected():
    """An opening `---` with NO closing `---` is not a frontmatter block -> rejected
    (covers the loop's `return None` in `_frontmatter_yaml`)."""
    unclosed = "---\nmode: primary\npermission:\n  bash: deny\nno closing delimiter here\n"
    assert review_backends._frontmatter_yaml(unclosed) is None
    assert not review_backends._agent_is_strictly_readonly(unclosed)


def test_non_mapping_frontmatter_is_rejected():
    """Frontmatter that parses to a non-mapping (a YAML list/scalar) is not a trusted
    definition -> rejected (covers the `isinstance(data, dict)` guard)."""
    list_front = "---\n- a\n- b\n---\nbody\n"
    assert not review_backends._agent_is_strictly_readonly(list_front)
    # `permission` present but not a mapping (a scalar) is also rejected.
    scalar_perm = "---\npermission: deny\n---\nbody\n"
    assert not review_backends._agent_is_strictly_readonly(scalar_perm)


def test_empty_permission_block_is_rejected():
    """`permission: {}` (present but empty) denies nothing of the required floor -> rejected."""
    empty_perm = "---\nmode: primary\npermission: {}\n---\nbody\n"
    assert not review_backends._agent_is_strictly_readonly(empty_perm)


def test_non_string_permission_value_is_rejected():
    """A non-string scalar value (e.g. `bash: 0`) is not `deny` -> rejected."""
    bad_value = (
        "---\npermission:\n  bash: 0\n  edit: deny\n  write: deny\n  webfetch: deny\n"
        "  task: deny\n  todowrite: deny\n  websearch: deny\n  lsp: deny\n  skill: deny\n---\nbody\n"
    )
    assert not review_backends._agent_is_strictly_readonly(bad_value)


def test_rewrite_is_atomic_no_leftover_temp_files():
    """The rewrite path uses an atomic temp-file + os.replace; after it runs, the agents
    directory contains ONLY the final file — no leftover `.read-only-reviewer.*.tmp`."""
    with _fake_home() as agent:
        agent.parent.mkdir(parents=True, exist_ok=True)
        agent.write_text(_PERMISSIVE_AGENT, encoding="utf-8")
        review_backends._ensure_opencode_readonly_agent(Path("/tmp"), "oc:m")
        names = sorted(p.name for p in agent.parent.iterdir())
        assert names == ["read-only-reviewer.md"], names
        assert review_backends._agent_is_strictly_readonly(
            agent.read_text(encoding="utf-8")
        )


def test_atomic_write_failure_cleans_temp_and_preserves_original():
    """If the atomic write fails mid-replace, the temp file is removed and the ORIGINAL is
    left intact (no half-written file, no leftover `.tmp`)."""
    with _fake_home() as agent:
        agent.parent.mkdir(parents=True, exist_ok=True)
        agent.write_text(review_backends._READONLY_AGENT_MARKDOWN, encoding="utf-8")
        original = agent.read_bytes()
        orig_replace = os.replace

        def _boom(src, dst, *a, **k):
            raise OSError("simulated replace failure")

        os.replace = _boom  # type: ignore[assignment]
        try:
            raised = False
            try:
                review_backends._atomic_write_text(agent, "new content")
            except OSError:
                raised = True
            assert raised, "atomic write should re-raise on replace failure"
        finally:
            os.replace = orig_replace  # type: ignore[assignment]
        # Original preserved, no leftover temp file.
        assert agent.read_bytes() == original
        names = sorted(p.name for p in agent.parent.iterdir())
        assert names == ["read-only-reviewer.md"], names


def test_frontmatter_with_dashes_inside_a_value_is_not_torn():
    """A `---` appearing INSIDE a frontmatter VALUE must not tear the YAML: a fully-denied
    agent whose description contains `---` still validates (line-anchored split, review
    finding — a naive substring split would mis-cut it and force a needless rewrite)."""
    perm = "\n".join(f"  {c}: deny" for c in review_backends._READONLY_AGENT_DENIED_PERMISSIONS)
    text = (
        "---\n"
        "description: a value with --- dashes inside\n"
        "mode: primary\n"
        "permission:\n"
        f"{perm}\n"
        "---\n"
        "body\n"
    )
    assert review_backends._agent_is_strictly_readonly(text), text


def test_unreadable_existing_file_is_rewritten():
    """A present-but-UNREADABLE agent file (read_text raises OSError) is not trusted: the
    `except OSError` branch treats it as empty -> not strictly read-only -> rewritten to
    the canonical safe definition (never silently reused)."""
    with _fake_home() as agent:
        agent.parent.mkdir(parents=True, exist_ok=True)
        agent.write_text(_PERMISSIVE_AGENT, encoding="utf-8")
        # Force the read to fail by patching Path.read_text for the duration of the call.
        orig_read_text = Path.read_text

        def _boom(self, *a, **k):
            if self == agent:
                raise OSError("simulated unreadable file")
            return orig_read_text(self, *a, **k)

        Path.read_text = _boom  # type: ignore[assignment]
        try:
            review_backends._ensure_opencode_readonly_agent(Path("/tmp"), "oc:m")
        finally:
            Path.read_text = orig_read_text  # type: ignore[assignment]
        assert review_backends._agent_is_strictly_readonly(
            orig_read_text(agent, encoding="utf-8")
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
