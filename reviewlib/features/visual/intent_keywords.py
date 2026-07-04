"""Multilingual keyword expansion for free-text `--intent` tag matching (§4/§6).

A module's `activates_on` tags (e.g. `"selection"`) are machine-side identifiers used
for `--check <tag>` force-activation — single English words, matched verbatim. Matching
those SAME tags against the free-text, UNTRUSTED `--intent` prose is a different, looser
problem: an actor describing the same concept in another language (or a natural
inflection of it) must still trip the same activation, or the check silently never runs.

Bug this fixes (tg#6188): a HyperCanvas screenshot captioned in Russian ("элемент
выбран" — "element selected") sailed through `review visual` even though no selection
outline was drawn, because the intent-matching only ever looked for the English tag
substring ("selection") inside the intent text — a Russian caption never contains it, so
`selection-highlight`'s hard CV veto (contrib/selection_highlight.py) never activated and
the render fell through to the generic style/blank/error-overlay checks, none of which
ask about a selection frame at all.

Adding an ENTRY to `_INTENT_SYNONYMS` can only make a module MORE likely to activate,
never less — the same "intent may only tighten, never loosen" invariant `contract.py`
already applies to risk. (The matching PRIMITIVE itself — left word-boundary instead of
a bare substring — is a separate, deliberate narrowing from before this fix; see
`test_word_boundary_narrowing_applies_to_every_tag_not_just_selection`. The "never
less" claim is about the synonym table, not the primitive.) A caller still needs to
actually pass `--intent`/`--check` (this module only widens what counts as a match once
intent text exists — see the `pre-send-photo` hook, which is the OTHER half of this bug:
it must forward the caption as `--intent` for any of this to run at all on a tg-sent
screenshot).

ACCEPTED TRADE-OFF: `selection`'s synonyms below (Alex's explicit list, tg#6188) are
common Russian software-prose roots ("выбран", "выделен", "подсвечен") that can appear
in an unrelated caption (e.g. "подсветка синтаксиса" — syntax highlighting) and would
then trip `selection-highlight`'s hard CV veto on a render that never claimed a
HyperCanvas selection. That is accepted: `selection-highlight` MISSING a real "selected"
claim (the bug this fixes) is worse than it firing on an unrelated caption that happens
to share a root word — a spurious block is loud and recoverable (re-send with a
different caption or `--check`), a silent miss is not.

NEGATION IS BEST-EFFORT, NOT EXHAUSTIVE: `_word_start_pattern` special-cases only the
single most common adjacent negation ("не "/"not " directly before the stem — e.g.
"элемент не выбран", "is not selected"). It does NOT parse negation in general —
"не был выбран" (an intervening word), "no selection", "isn't selected", "no longer
selected" etc. are NOT recognized as negation and WILL still activate the module. This
is intentional, not a gap to keep closing: building a real negation parser for a
best-effort caption heuristic is out of proportion to the risk it mitigates (the SAME
accepted trade-off above — a spurious block is recoverable, a silent miss is the actual
bug), so the line is drawn at the one adjacent-negation shape that is both common and
cheap to special-case.
"""

from __future__ import annotations

import re

# tag (lowercase) -> extra lowercase substrings that count as "this tag was mentioned in
# intent text", in ANY language. The tag string itself always counts implicitly (see
# `intent_mentions_tag` below) — only additional synonyms belong here.
_INTENT_SYNONYMS: dict[str, tuple[str, ...]] = {
    "selection": (
        "selected",
        # Russian inflections of "selected" (выбран/выбрана/выбрано/выбранный).
        "выбран",
        # Russian inflections of "highlighted/marked" (выделен/выделена/выделено/
        # выделенный) — also covers the phrase "рамка выделения" (selection frame),
        # whose "выделен" stem starts a left word boundary the same way.
        "выделен",
        # Russian "highlighted/lit up" (подсвечен/подсвечена/подсвечено).
        "подсвечен",
    ),
}

# A LEFT word-boundary before the keyword, with the immediately-preceding word
# excluded when it is a negation ("не "/"not "): matches every inflection sharing that
# stem (выбран/выбрана/выбранный; select/selected) but NOT when negated.
#
# Two negation shapes must both be caught:
#   * GLUED (no boundary at all): English "deselect"/"unselect" ("de"/"un" run straight
#     into "select") and the Russian ATTRIBUTIVE adjective "невыбранный" ("не" runs
#     straight into "выбран", written solid per Russian orthography for that word
#     class) — `\b` alone already excludes both, since there is no \w/non-\w
#     transition at that position.
#   * SEPARATE (a real word boundary, then a negation word, then the stem): the
#     Russian PREDICATIVE form is written with a space — "элемент **не выбран**",
#     "строка **не выделена**" — which is in fact the more common QA-caption shape,
#     and English "is **not selected**". `\b` alone does NOT exclude this (the space
#     IS a boundary), so it needs an explicit negative lookbehind for a preceding
#     "не "/"not " (codex review finding, tg#6188 PR).
#
# The negation lookbehind itself needs a `\b` INSIDE it (`(?<!\bне )`, not `(?<!не )`):
# without it, the lookbehind matches "не " as a bare substring — including the TAIL of
# an unrelated word ending in "-не" (окне, экране, фоне, регионе, зоне — the locative
# case, extremely common in a screenshot caption's opening "на экране …" / "в окне …").
# That falsely suppresses a real match ("на экране выбран элемент" was silently NOT
# activating — reproducing the exact tg#6188 silent-miss bug this fix exists to close,
# caught in review before merge). `\b` is zero-width, so the lookbehind stays
# fixed-width (a Python `re` requirement) while still requiring "не"/"not" to be its
# own word.
_WORD_START_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _word_start_pattern(word: str) -> re.Pattern[str]:
    pattern = _WORD_START_RE_CACHE.get(word)
    if pattern is None:
        pattern = re.compile(r"(?<!\bне )(?<!\bnot )\b" + re.escape(word))
        _WORD_START_RE_CACHE[word] = pattern
    return pattern


def intent_mentions_tag(intent: str, tag: str) -> bool:
    """True if the free-text `intent` mentions `tag` at a left word boundary — verbatim,
    in English, or via a registered non-English synonym. Case-insensitive. `intent`/`tag`
    may be empty."""
    if not intent or not tag:
        return False
    low = intent.lower()
    tag_low = tag.lower()
    if _word_start_pattern(tag_low).search(low):
        return True
    return any(
        _word_start_pattern(syn).search(low)
        for syn in _INTENT_SYNONYMS.get(tag_low, ())
    )
