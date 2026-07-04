#!/usr/bin/env python3
"""intent_mentions_tag — multilingual free-text tag matching (tg#6188).

Proves the fix: a Russian caption claiming an element is "selected"/"highlighted" must
be recognized as mentioning the "selection" tag, exactly like the English words already
were — so a downstream module (e.g. selection-highlight) still activates regardless of
the caller's language.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib.features.visual.intent_keywords import intent_mentions_tag  # noqa: E402


def test_english_tag_matches_verbatim():
    assert (
        intent_mentions_tag("verify the selection outline renders", "selection") is True
    )


def test_english_synonym_selected_matches():
    assert intent_mentions_tag("confirm the element is selected", "selection") is True


def test_russian_vybran_matches():
    assert intent_mentions_tag("проверь что элемент выбран", "selection") is True


def test_russian_vydelen_matches():
    assert (
        intent_mentions_tag("элемент должен быть выделен рамкой", "selection") is True
    )


def test_russian_podsvechen_matches():
    assert intent_mentions_tag("элемент подсвечен синей рамкой", "selection") is True


def test_russian_ramka_vydeleniya_matches():
    assert intent_mentions_tag("должна быть видна рамка выделения", "selection") is True


def test_unrelated_intent_does_not_match():
    assert (
        intent_mentions_tag("check the button is styled correctly", "selection")
        is False
    )


def test_negated_prefix_glued_does_not_falsely_match():
    """A left word-boundary match must NOT fire when the keyword is glued to a
    semantically-reversing prefix with no boundary: English "deselect"/"unselect" run
    straight into "select", and the Russian ATTRIBUTIVE adjective "невыбранный"
    ("not-selected [element]") is written as one solid word per Russian orthography for
    that word class — "не" runs straight into "выбран"."""
    assert intent_mentions_tag("deselect the item first", "selection") is False
    assert intent_mentions_tag("unselect everything", "selection") is False
    assert (
        intent_mentions_tag("невыбранный элемент остаётся серым", "selection") is False
    )


def test_negated_prefix_separate_word_does_not_falsely_match():
    """The Russian PREDICATIVE negation is written as a SEPARATE word — "элемент **не
    выбран**" — which is actually the more common QA-caption shape, and the English
    equivalent "is **not selected**". A bare left-word-boundary match does NOT exclude
    these (the space is itself a boundary), so this needs its own negative lookbehind —
    without it, a caption explicitly denying a selection would falsely hard-veto a
    CORRECT render that has no outline (codex review finding on the tg#6188 fix PR)."""
    assert intent_mentions_tag("элемент не выбран", "selection") is False
    assert intent_mentions_tag("строка не выделена", "selection") is False
    assert intent_mentions_tag("the element is not selected", "selection") is False


def test_accepted_limitation_non_adjacent_negation_still_matches():
    """ACCEPTED (see module docstring "NEGATION IS BEST-EFFORT, NOT EXHAUSTIVE"): only
    the single most common adjacent negation ("не "/"not " directly before the stem) is
    special-cased. An intervening word ("не БЫЛ выбран") or a different negator ("no
    selection") is NOT recognized and DOES still activate — a real, known gap, not an
    oversight, locked here so it reads as deliberate scope, not an untested miss."""
    assert intent_mentions_tag("элемент ещё не был выбран", "selection") is True
    assert intent_mentions_tag("there is no selection here", "selection") is True


def test_stem_after_word_ending_in_ne_still_matches():
    """The negation lookbehind must require "не"/"not" to be its OWN word — not just the
    bare substring "не "/"not ". Without that `\\b`, the lookbehind falsely fires on the
    TAIL of an unrelated word ending in "-не" (окне, экране, фоне — the Russian locative
    case), silently suppressing a real match on exactly the most common opening of a
    screenshot caption ("на экране …", "в окне …") — reproducing the tg#6188 silent-miss
    bug on its own fix (codex review finding on the tg#6188 fix PR)."""
    assert intent_mentions_tag("на экране выбран элемент", "selection") is True
    assert intent_mentions_tag("в окне выделен пункт", "selection") is True
    assert intent_mentions_tag("на фоне подсвечен блок", "selection") is True


def test_different_root_does_not_match():
    """ "подсветка" (a noun, e.g. syntax highlighting) is a DIFFERENT root from
    "подсвечен" (the adjective this module matches on) — must not collide."""
    assert intent_mentions_tag("подсветка синтаксиса включена", "selection") is False


def test_accepted_tradeoff_shared_root_in_unrelated_context_does_match():
    """ACCEPTED (tg#6188, see module docstring): the Russian roots Alex explicitly asked
    for ("выбран"/"выделен"/"подсвечен") are common enough in unrelated software prose
    that a caption using them for something else (e.g. a highlighted LOG line, not a
    HyperCanvas selection) still activates the module. This is intentional — a missed
    real "selected" claim is worse than an occasional spurious activation — and is
    locked here explicitly so it reads as a deliberate choice, not an oversight."""
    assert (
        intent_mentions_tag("выделенная строка лога содержит ошибку", "selection")
        is True
    )


def test_empty_intent_or_tag_does_not_match():
    assert intent_mentions_tag("", "selection") is False
    assert intent_mentions_tag("выбран", "") is False


def test_unknown_tag_falls_back_to_verbatim_only():
    # A tag with no registered synonyms still matches its own literal substring, and
    # does NOT pick up an unrelated tag's synonyms.
    assert intent_mentions_tag("style check please", "style") is True
    assert intent_mentions_tag("элемент выбран", "style") is False


def test_word_boundary_narrowing_applies_to_every_tag_not_just_selection():
    """The switch from a bare substring check to a left-word-boundary match narrows
    activation for EVERY tag, not only "selection" — e.g. "restyle" no longer trips the
    "style" tag the way a naive `"style" in intent` would have. Locked explicitly so
    this narrowing reads as an intentional, covered behavior change (codex review
    finding on the tg#6188 fix PR), not an untested side effect."""
    assert intent_mentions_tag("please restyle this component", "style") is False
    assert intent_mentions_tag("please style this component", "style") is True


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
