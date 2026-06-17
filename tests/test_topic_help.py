#!/usr/bin/env python3
"""Topic-based deep help: `review help <topic>` / `review --help <topic>`.

ROADMAP "Topic-based help across the ecosystem" (start with `review help config`): tools
ship DEEP help topics advertised from the main help. Pinned here:

  * the main `review --help` LISTS the available topics and POINTS at `review help config`;
  * `review help` (bare) lists the topics; `review help config` prints the config reference
    (config file path + cascade, keys/auth, the board) — the same content via the
    `review --help config` alias;
  * an unknown topic is a usage error (exit 2) that names the known topics;
  * the config topic stays in sync with behavior (names the real CONFIG_PATH, the keys, the
    selection cascade) — help-docs-sync.

In-process via the CLI dispatch (offline; no backend). Topic content is asserted on the
rendered text, robust to argparse line-wrapping where relevant.
"""
from __future__ import annotations

import io
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import cli  # noqa: E402
from reviewlib.config import CONFIG_PATH  # noqa: E402


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    rc = None
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = cli._dispatch(argv)
        except SystemExit as exc:  # the top-level --help path may exit
            rc = exc.code if isinstance(exc.code, int) else 0
    return rc, out.getvalue(), err.getvalue()


def test_main_help_lists_topics_and_points_at_config():
    help_text = cli._build_top_level_parser().format_help()
    assert "help topics" in help_text, help_text
    assert "review help config" in help_text, help_text


def test_help_bare_lists_topics():
    rc, out, _ = _run(["help"])
    assert rc == 0, rc
    assert "config" in out, out
    assert "review help" in out, out


def test_help_config_prints_the_config_reference():
    rc, out, _ = _run(["help", "config"])
    assert rc == 0, rc
    # Names the real config file path + the keys + the cascade + the keys/auth section.
    assert str(CONFIG_PATH) in out, out
    assert "models:" in out and "brainstorm_models:" in out and "board:" in out, out
    assert "SELECTION CASCADE" in out, out
    assert "COMMANDCODE_API_KEY" in out and "GEMINI_API_KEY" in out, out
    assert "review --show-board" in out, out
    # The board-entry shape must name the keys load_board ACTUALLY reads (model/role/name),
    # NOT a `display` key it ignores (codex review) — else a user can't set a seat label.
    assert "name" in out, out
    assert "{model, role, name}" in out, out
    assert "{model, role, display}" not in out, out


def test_help_config_alias_via_dashdash_help():
    rc_a, out_a, _ = _run(["help", "config"])
    rc_b, out_b, _ = _run(["--help", "config"])
    assert rc_a == 0 and rc_b == 0, (rc_a, rc_b)
    assert out_a == out_b, "review --help config must render the same as review help config"
    rc_c, out_c, _ = _run(["-h", "config"])
    assert rc_c == 0 and out_c == out_a, "the -h alias too"


def test_help_unknown_topic_is_usage_error_naming_known_topics():
    rc, _out, err = _run(["help", "bogus"])
    assert rc == 2, rc
    assert "unknown topic 'bogus'" in err, err
    assert "config" in err, err


def test_unknown_topic_via_dashdash_help_alias_also_errors():
    """A TYPO topic through the alias (`review --help confg`) must get the SAME unknown-topic
    usage error (exit 2) as `review help confg`, not fall through to argparse's normal help
    (exit 0) (codex review)."""
    for prefix in ("--help", "-h"):
        rc, _out, err = _run([prefix, "confg"])
        assert rc == 2, (prefix, rc)
        assert "unknown topic 'confg'" in err, (prefix, err)


def test_bare_dashdash_help_without_topic_is_normal_help_not_topic():
    # `review --help` (no topic) must still print the normal top-level help (exit 0), not be
    # swallowed by the topic alias.
    rc, out, _ = _run(["--help"])
    assert rc == 0, rc
    assert "subcommands:" in out, out


def test_help_extra_trailing_args_are_a_usage_error():
    """`review help config extra` / `review --help config --bogus` must NOT silently drop the
    tail and exit 0 — extra args after the topic are a usage error (exit 2) (codex review)."""
    for argv in (["help", "config", "extra"], ["--help", "config", "--bogus"]):
        rc, _out, err = _run(argv)
        assert rc == 2, (argv, rc)
        assert "extra arguments" in err, (argv, err)


def test_help_topics_registry_renderers_all_produce_text():
    """Every registered topic must render non-empty text (so the main-help listing never
    advertises a broken topic). Guards against a topic entry with a bad renderer."""
    for topic, (summary, render) in cli.HELP_TOPICS.items():
        assert summary, topic
        text = render()
        assert isinstance(text, str) and text.strip(), (topic, "empty topic render")


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
