#!/usr/bin/env python3
"""Unit tests for the operator-facing "panel/board padded" stderr notices and
quorum's per-seat role/lens assignment (reviewlib.cli._warn_if_panel_padded,
reviewlib.modes.review._warn_if_board_reused, reviewlib.modes.quorum's
`<model>#N [<persona>]` labelling + moderator note).

Covers, offline (no model call, no network):
  (a) the panel/board warnings fire iff a repeat is genuinely REUSE, not just
      "the output happens to contain a duplicate model" — a config board that
      legitimately lists the same model under two distinct roles must NOT
      false-positive (Fable/k3 review finding, review-cli#205 round 2);
  (b) every quorum seat gets a distinct persona from brainstorm's role
      rotation (Alex, 2026-08-18); repeated models keep a `<model>#N` prefix
      in their label and a moderator disclosure note so duplicate seats read
      as ONE opinion, not two independent ones, despite reasoning from
      different roles.

Plain-script harness (mirrors tests/test_pool_reuse.py): each test_* is run
by __main__, and also pytest-discoverable.
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stderr
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib.backends import ReviewResult  # noqa: E402
from reviewlib.cli import _warn_if_panel_padded  # noqa: E402
from reviewlib.config import BoardReviewer  # noqa: E402
from reviewlib.modes import PERSONAS  # noqa: E402
from reviewlib.modes import quorum as q_mod  # noqa: E402
from reviewlib.modes.review import _warn_if_board_reused  # noqa: E402

REPO_DIR = REPO_ROOT


def _captured_stderr(fn, *args) -> str:
    buf = io.StringIO()
    with redirect_stderr(buf):
        fn(*args)
    return buf.getvalue()


def test_panel_padded_warning_fires_on_real_reuse():
    out = _captured_stderr(_warn_if_panel_padded, ["fable", "glm", "fable", "glm"])
    assert "panel padded" in out
    assert "fable x2" in out


def test_panel_padded_warning_silent_when_all_distinct():
    out = _captured_stderr(_warn_if_panel_padded, ["fable", "glm", "sol"])
    assert out == ""


def test_board_reused_warning_fires_on_real_padding():
    board = [
        BoardReviewer("fable", "architect", "Fable"),
        BoardReviewer("glm", "performance", "GLM"),
    ]
    # A genuine padded pool: the second "Fable" seat is a NEW object (as
    # select_pool_with_reuse's `replace()` extras always are), not one of
    # `board`'s own seat objects.
    from dataclasses import replace

    pool = [board[0], replace(board[0], role="security")]
    out = _captured_stderr(_warn_if_board_reused, board, pool)
    assert "board padded" in out
    assert (
        "fable x1" in out or "fable x2" in out
    )  # exact count phrasing, either is fine


def test_board_reused_warning_silent_on_legit_duplicate_model_board():
    # A config board that legitimately lists the SAME model under two
    # DIFFERENT roles as two DISTINCT seats — no reuse happened, the pool is
    # just both of those original seats verbatim.
    board = [
        BoardReviewer("fable", "architect", "Fable-A"),
        BoardReviewer("fable", "security", "Fable-B"),
    ]
    pool = list(board)  # both ORIGINAL objects, unchanged — not a replace() copy
    out = _captured_stderr(_warn_if_board_reused, board, pool)
    assert out == ""


def test_board_reused_warning_silent_when_pool_equals_board():
    board = [
        BoardReviewer("fable", "architect", "Fable"),
        BoardReviewer("sol", "consistency", "Sol"),
    ]
    out = _captured_stderr(_warn_if_board_reused, board, list(board))
    assert out == ""


# --- quorum's <model>#N labelling + moderator disclosure --------------------------

# Shared fakes for the `mode_quorum` tests below (Fable review finding, round 5:
# 5 tests each copy-pasted an equivalent pair of fakes; a shared factory keeps a
# 6th quorum test from forking yet another copy). `captured` is a fresh dict per
# test, populated with `jobs` (the real PanelJob list dispatched, so a test can
# inspect `.label`/`.prompt`/`.model`) and `mod_prompt` (the moderator's prompt).


def _fake_quorum_panel(captured: dict):
    def _fake_run_panel(jobs, cwd, timeout):
        captured["jobs"] = jobs
        return [
            ReviewResult(
                model=j.label or j.model,
                command="fake",
                returncode=0,
                stdout=f"answer from {j.label or j.model}",
                stderr="",
            )
            for j in jobs
        ]

    return _fake_run_panel


def _fake_quorum_moderator(captured: dict):
    def _fake_run_moderator(candidates, prompt, cwd, timeout, diff="", round_no=0):
        captured["mod_prompt"] = prompt
        return ReviewResult(
            model=candidates[0],
            command="fake",
            returncode=0,
            stdout="summary",
            stderr="",
        )

    return _fake_run_moderator


def _run_quorum_with_fakes(models: list[str]) -> dict:
    """Dispatch `mode_quorum(models)` through the shared fakes and return
    `captured` (with `jobs`, `labels`, `mod_prompt`, `rc`)."""
    captured: dict = {}
    saved_panel, saved_mod = q_mod.run_panel, q_mod.run_moderator
    q_mod.run_panel = _fake_quorum_panel(captured)
    q_mod.run_moderator = _fake_quorum_moderator(captured)
    try:
        captured["rc"] = q_mod.mode_quorum("question", models, "", REPO_DIR, 5, ["mod"])
    finally:
        q_mod.run_panel = saved_panel
        q_mod.run_moderator = saved_mod
    captured["labels"] = [j.label for j in captured["jobs"]]
    return captured


def test_quorum_labels_duplicate_seats_and_notes_them_to_the_moderator():
    captured = _run_quorum_with_fakes(
        ["fable", "glm", "fable"]
    )  # fable padded in twice
    assert captured["rc"] == 0, captured["rc"]
    # Every seat gets a persona (Alex, 2026-08-18: quorum panels run "под разными
    # полями" -- distinct roles, not just distinct models). Persona is keyed on
    # PER-MODEL occurrence (offset by that model's own first seat index), not raw
    # seat index -- fable's 2nd seat lands on PERSONAS[(0+1)%6]=P1, which happens
    # to match glm's own P1; that's fine, the invariant is same-model seats never
    # repeat a lens, not global uniqueness across different models. The repeated
    # fable seats keep a "#N" prefix so they stay grep-able by model identity.
    assert captured["labels"] == [
        "fable#1 [Pragmatic staff engineer]",
        "glm [Security-paranoid reviewer]",
        "fable#2 [Security-paranoid reviewer]",
    ]
    assert "### Expert: fable#1 [Pragmatic staff engineer]" in captured["mod_prompt"]
    assert "### Expert: fable#2 [Security-paranoid reviewer]" in captured["mod_prompt"]
    assert "fable#1" in captured["mod_prompt"]
    assert "fable#2" in captured["mod_prompt"]
    assert "SINGLE opinion" in captured["mod_prompt"]


def test_quorum_labels_distinct_seats_with_roles_but_no_dedup_note():
    captured = _run_quorum_with_fakes(["fable", "glm"])
    # No duplicates -> no "#N" and no dedup note, but every seat still carries
    # a persona (Alex, 2026-08-18) -- distinct-model panels get roles too.
    assert captured["labels"] == [
        "fable [Pragmatic staff engineer]",
        "glm [Security-paranoid reviewer]",
    ]
    assert "SINGLE opinion" not in captured["mod_prompt"]
    # The lens notation is explained UNCONDITIONALLY (k3 review finding, round 2:
    # a distinct-model panel's moderator saw the `[<lens>]` bracket with no
    # explanation of what it means).
    assert "role/lens" in captured["mod_prompt"]


def test_quorum_reused_model_gets_a_different_persona_per_seat():
    """A model reused across 3 seats (short pool, `expand_flat_models_with_reuse`)
    must land on 3 DIFFERENT personas, not the same one repeated -- otherwise
    reuse gives quorum zero added diversity, which is exactly what Alex asked
    to fix ("под разными полями")."""
    captured = _run_quorum_with_fakes(["fable", "fable", "fable"])
    assert captured["labels"] == [
        "fable#1 [Pragmatic staff engineer]",
        "fable#2 [Security-paranoid reviewer]",
        "fable#3 [Developer-experience designer]",
    ]
    # The prompts themselves differ by role too, not just the label.
    prompts = [j.prompt for j in captured["jobs"]]
    assert len(set(prompts)) == 3
    assert "Pragmatic staff engineer" in prompts[0]
    assert "Security-paranoid reviewer" in prompts[1]
    assert "Developer-experience designer" in prompts[2]
    # The evidence-citing / INSUFFICIENT-EVIDENCE contract must survive the
    # persona rewrite (k3 review finding, round 3: `_expert_prompt` was written
    # from scratch and nothing pinned this -- quorum's whole "cited quorum"
    # value depends on it, and the moderator's ABSTAINED section reads it).
    for prompt in prompts:
        assert "INSUFFICIENT EVIDENCE" in prompt
        assert "Cite concrete evidence" in prompt


def test_quorum_persona_does_not_collide_past_six_seats():
    """Fable/k3 review finding: raw `PERSONAS[i % len(PERSONAS)]` (len==6) collided
    once a model's own repeats landed exactly 6 seats apart -- reachable via
    `expand_flat_models_with_reuse`'s cycling pad once a pool is down to 2
    reachable models (e.g. a 7-seat panel `[A,B,A,B,A,B,A]`). Assert every
    occurrence of the SAME model gets a distinct persona across a 7-seat panel."""
    captured = _run_quorum_with_fakes(
        ["fable", "glm", "fable", "glm", "fable", "glm", "fable"]
    )
    jobs = captured["jobs"]
    fable_personas = [j.prompt for i, j in enumerate(jobs) if jobs[i].model == "fable"]
    glm_personas = [j.prompt for i, j in enumerate(jobs) if jobs[i].model == "glm"]
    assert len(fable_personas) == 4
    assert len(set(fable_personas)) == 4, "fable's 4 seats must not repeat a persona"
    assert len(glm_personas) == 3
    assert len(set(glm_personas)) == 3, "glm's 3 seats must not repeat a persona"


def test_quorum_seat_label_persona_matches_the_seat_prompt_persona():
    """Fable/k3 review finding, round 2: the original version of this test only
    checked `_seat_assignments`' OWN return values against each other (label vs
    personas from the SAME call) -- tautologically true by construction, and
    blind to a real desync between `_seat_assignments` and the SEPARATE
    `_expert_prompt` call site in `mode_quorum` that actually builds what gets
    sent. This version routes every shape through the real `mode_quorum` (like
    the neighboring reuse/collision tests) and checks each dispatched job's
    label persona is the SAME persona that landed in that job's own prompt --
    a fix that edits one call site but not the other would fail this."""
    for models in (
        ["fable", "glm", "fable"],
        ["fable", "fable", "fable"],
        ["fable", "glm", "fable", "glm", "fable", "glm", "fable"],
        ["sol", "kimi", "glm", "codex"],
    ):
        captured = _run_quorum_with_fakes(models)
        for job in captured["jobs"]:
            # Extract the bracketed lens name from the label (e.g.
            # "fable#2 [Security-paranoid reviewer]" -> "Security-paranoid reviewer").
            persona_name = job.label.rsplit("[", 1)[1].rstrip("]")
            assert persona_name in job.prompt, (models, job.label, job.prompt)


def test_personas_pool_stays_large_enough_for_both_consumers():
    """Enforce the CROSS-MODE CONTRACT documented in modes/__init__.py as a real
    test, not just prose (Fable review finding, round 3) -- a future edit that
    shrinks the pool below brainstorm's minimum, or reintroduces a character that
    breaks quorum's label parsing, should fail loudly here instead of surfacing
    as a confusing brainstorm/quorum bug later."""
    assert len(PERSONAS) >= 5, "brainstorm's rotation needs a pool >= 5"
    names = [name for name, _bg in PERSONAS]
    assert len(set(names)) == len(names), "persona names must be unique"
    for name in names:
        # `[`/`]` are the LOAD-BEARING characters (Fable review finding, round 5:
        # the round-2 rename avoided `/`, but quorum's label format is
        # `<model> [<persona>]` and both `mode_quorum`'s tests and its own
        # `label.rsplit("[", 1)` parsing depend on the bracket pair being
        # unambiguous -- a persona name containing one would corrupt that, not
        # a stray `/`, which was already confirmed harmless in round 3).
        assert "[" not in name and "]" not in name, (
            f"persona name {name!r} would break quorum's <model> [<persona>] "
            "label parsing"
        )


def test_brainstorm_draws_from_the_shared_personas_pool():
    """Fable review finding, round 4: the PERSONAS move was only test-covered from
    the quorum side; nothing pinned that brainstorm still draws from the SAME
    tuple rather than a re-forked local copy. Identity (not just equality) --
    a future "brainstorm needs its own persona flavor" edit that re-forks the
    tuple would still be equal-by-value the moment it's copy-pasted, but this
    catches it immediately, before drift starts."""
    from reviewlib.modes import brainstorm as b_mod

    assert b_mod.PERSONAS is PERSONAS


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
