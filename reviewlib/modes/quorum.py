"""quorum: experts answer + a moderator finds consensus/disagreement.

`review quorum "<question>"` — a two-phase structured panel (experts cite evidence,
a moderator finds quorum/disagreement). Originally the `--quorum` flag (Stage 0
decomposition); now a first-class SUBCOMMAND backed by the `MODE` descriptor at the
bottom of this file (see `modes/contract.py`).
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import TYPE_CHECKING

from ..backends import ReviewResult, cap_diff_for_dispatch
from ..panel import (
    PanelJob,
    format_result,
    recount_round_by_usability,
    result_is_usable,
    run_moderator,
    run_panel,
)
from . import PERSONAS, _diff_context_block, _run_effort, _visual_images
from .contract import ModeContext, ModeSpec

if TYPE_CHECKING:
    from ..config import EffortOverride


def _expert_prompt(
    persona_name: str, persona_bg: str, question: str, diff_block: str
) -> str:
    """One seat's prompt: brainstorm's persona framing grafted onto quorum's existing
    evidence-citing / INSUFFICIENT-EVIDENCE contract (Alex, 2026-08-18 — quorum reuses
    brainstorm's role set so a padded/reused seat gets a genuinely distinct lens, not
    just a disclosure label)."""
    return (
        f"You are a '{persona_name}' ({persona_bg}) serving as one expert on a panel. "
        "Give a clear RECOMMENDATION on the question below, reasoned from YOUR "
        "perspective as this role. Cite concrete evidence for every claim (file path, "
        "line number, command output, or a verifiable fact). If you do not have an "
        "evidence base to answer, say exactly 'INSUFFICIENT EVIDENCE' and explain what "
        "you would need — do NOT guess. Do not edit files or run commands.\n\n"
        f"QUESTION:\n{question}" + diff_block
    )


# --- Adversarial refutation pass (audit finding: "no refutation / adversarial
# cross-check step", opt-in via --adversarial-check) -----------------------------
#
# The moderator (`mod_prompt` below) only AGGREGATES the experts' own opinions — no
# pass ever tries to independently attack a clean "no blocking disagreement" verdict.
# `--adversarial-check` is an opt-in extra pass for the SHIP-GATE-CRITICAL path (a
# merge/ship decision), not forced on every ad-hoc `review quorum` call: it costs one
# more model call, which routine dev-loop questions shouldn't always pay for.
# `_DISAGREEMENT_HEADING` / `_ABSTAINED_HEADING` are the SINGLE source of truth for
# the section-2/section-3 heading text — used to BUILD `mod_prompt` below (the actual
# instruction sent to the moderator) AND to compile the regex that parses its answer
# (Opus review finding, round 3): before this, the regex hard-coded its own copy of
# the literal heading wording with no link to `mod_prompt`'s own text, so every test
# validated the regex only against a hand-rolled fixture built to satisfy it, never
# against the real prompt — a future reword of `mod_prompt`'s headings would silently
# make `quorum_verdict_is_clean` return False on every real run (the whole
# --adversarial-check pass going permanently dark) with the entire test suite still
# green. Deriving both from one constant makes that class of drift impossible instead
# of merely tested-against.
_DISAGREEMENT_HEADING = "DISAGREEMENT / NO QUORUM"
_ABSTAINED_HEADING = "ABSTAINED"
# `_LINE_SEP` eats what separates the heading from its content ON THE SAME LINE ONLY —
# NOT `\s` (Opus/k3 review finding, round 1): a class that includes `\n` greedily
# swallows the newline the section-3 lookahead below depends on, so a moderator that
# leaves section 2 GENUINELY EMPTY ("...NO QUORUM\n3. ABSTAINED...", no "None." text)
# gets misread as "not clean" — the regex engine, having already found an overall
# match once `.*?` expands past that eaten newline to the next `\Z`/next heading,
# never backtracks `_LINE_SEP` back to zero-width. Restricting the class to inline
# whitespace/punctuation (space, tab, colon, dash, em dash, and light markdown
# emphasis a moderator may wrap the heading in) leaves the newline for the lookahead,
# so a genuinely empty section is detected via the ZERO-WIDTH capture path instead.
_LINE_SEP = r"[ \t:\-—*_`#>]*"
_DISAGREEMENT_SECTION_RE = re.compile(
    rf"(?is)2\.\s*{re.escape(_DISAGREEMENT_HEADING)}\b{_LINE_SEP}(.*?)"
    rf"(?=\n{_LINE_SEP}3\.\s*{re.escape(_ABSTAINED_HEADING)}\b|\Z)"
)
# Tolerant of a leading bullet/dash and trailing markdown emphasis around the
# trivial phrase itself, and the plural "disagreements" (Opus review finding,
# round 4): a moderator writing an ordinary bulleted or bolded clean section —
# "- None." or "**None.**" or "— No disagreements." — are all normal synthesis
# output, not edge cases, and the ORIGINAL pattern (bare `\s*`, singular only)
# read every one of them as "not clean," silently skipping --adversarial-check on
# exactly the clean verdict it exists to double-check — the same class of bug as
# round 1's F1, just in the trivial-content check rather than the section regex.
#
# The leading class is missing '—' itself (k3 review finding, round 7): the comment
# right above has claimed "— No disagreements." was tolerated since round 4, but the
# character class never actually included the em dash, only the hyphen `-` — so a
# moderator putting the trivial phrase on its OWN line under the heading (content
# after `_LINE_SEP` strips to exactly "— None.") failed this match, read as "not
# clean," and silently skipped `--adversarial-check` on a genuinely clean verdict —
# with the skip message then wrongly telling the user "the moderator synthesis
# reported real disagreement" (a parse gap misreported as a real disagreement).
# Adding '—' here closes the gap the comment already assumed was closed.
_TRIVIAL_DISAGREEMENT_RE = re.compile(
    r"(?i)^[\s*_`#>—-]*(none|no disagreements?|n/a|nothing)[.\s*_`#]*$"
)
# Single source of truth for the three marker WORDS (Opus review finding, round 9 —
# mirrors `_DISAGREEMENT_HEADING`/`_ABSTAINED_HEADING`'s own anti-drift discipline
# above, which this section's marker text had NOT been following): before this, the
# word "REFUTATION FOUND" existed as three independent literals — the exported
# `REFUTATION_NOT_FOUND_MARKER` constant, a bare string hardcoded inline in
# `_adversarial_refutation_prompt`'s own text, and the regex patterns below (which
# spell "NO"/"REFUTATION"/"FOUND" out again themselves) — so rewording ANY one of
# them (a typo fix, a prompt tweak) would silently desync the actual prompt sent to
# the model from what the regexes look for, with every test in this file still
# green because the test fixtures ALSO hardcode the same literals independently.
# Deriving the prompt text AND the regex word-tokens from these three constants
# makes that drift impossible to introduce silently, instead of merely untested.
_NO_WORD = "NO"
_REFUTATION_WORD = "REFUTATION"
_FOUND_WORD = "FOUND"
REFUTATION_NOT_FOUND_MARKER = f"{_NO_WORD} {_REFUTATION_WORD} {_FOUND_WORD}"
AFFIRMATIVE_REFUTATION_MARKER = f"{_REFUTATION_WORD} {_FOUND_WORD}:"
# Bounded decoration run (Opus review finding, round 6): an UNBOUNDED `*`-quantified
# class backtracks O(L^2) over a long run of decoration-only characters with no real
# match inside it — round 5 reacted to that by adding a whole-body length cutoff
# (`_MARKER_SCAN_MAX_LEN`, since removed) ahead of the scan. But that cutoff had its
# OWN false-negative, and a more dangerous one: a genuine, verbose refutation (prose +
# a code snippet + the per-scenario "checked" list the prompt explicitly asks for)
# routinely exceeds a few thousand characters with NO pathological decoration run
# anywhere in it, and got silently downgraded to `'inconclusive'` before the scan
# even started — discarding a real finding, exactly the "more dangerous direction"
# this file swears off repeatedly. Bounding each decoration run to a small fixed max
# (no real marker needs more than a couple dozen decoration characters between two
# words) keeps every match attempt's backtracking cost O(1)-ish regardless of the
# body's total length, so the scan no longer needs a length gate to stay safe —
# replacing a blunt "skip the whole body" defense with one that targets the actual
# pathological shape instead.
# One character class shared by every decoration-tolerant gap in this section (Opus
# review finding, round 6): includes ':' — round 4's `NO\s+` join, and an earlier
# version of this round's own fix, both omitted it, so "NO: REFUTATION FOUND"
# (colon directly after "NO", one of Opus's own two reported trigger strings) still
# fell through to a false "found". A single shared class means widening it once
# (as this line just did) fixes every gap that reuses it, instead of each gap's own
# copy silently drifting out of sync with the others.
_DECO_CHARS = r"\s*_`#>—:-"
_DECORATION = rf"[{_DECO_CHARS}]{{0,24}}"
# `_NO_GAP` separates "NO" from "REFUTATION" in the null-marker reading. Round 4 used
# a literal `\s+` here (one-or-more whitespace, no markdown) — tolerant of a double
# space or a newline, but NOT of a model wrapping just "NO" in emphasis ("**NO**
# REFUTATION FOUND") or separating it with a colon ("NO: REFUTATION FOUND"): the
# immediate next character isn't whitespace, `\s+` fails to match at that position,
# and the leftmost successful match then starts at "REFUTATION" itself with no "NO"
# consumed — misreading a genuine null answer as an affirmative "found" and flipping
# a clean run to a false failure (Opus review finding, round 6). Reusing the same
# `_DECO_CHARS` class here (a MINIMUM of one character, so "NOREFUTATION" glued with
# no separator at all is never misread as "NO" + "REFUTATION") closes this the same
# way decoration is already tolerated everywhere else in this file.
_NO_GAP = rf"[{_DECO_CHARS}]{{1,24}}"
# A hand-rolled boundary instead of `\b` (Opus review finding, round 6): Python's
# `\w` treats `_` as a word character, so `\bNO\b` would NOT see a boundary right
# after "NO" when the very next character is markdown-italic `_` (`_NO_ REFUTATION
# FOUND`) — `_` is in `_DECORATION`/`_NO_GAP`'s own class, so this case is real, not
# theoretical. Blocking only adjacent LETTERS (not digits/underscore/punctuation)
# still keeps "KNOWN"/"NOTE" from matching as "NO" while letting markdown-wrapped
# "NO" through.
_NO_WORD_RE = rf"(?<![A-Za-z]){re.escape(_NO_WORD)}(?![A-Za-z])"
# `(?![A-Za-z])` right after "FOUND" (k3 review finding, round 8): without it, any
# word merely BEGINNING with "found" — "FOUNDED", "FOUNDATION", "founders" — matched
# too, so ordinary prose like "the strongest candidate refutation founded on the
# checkpoint/stamp race fails" false-fired the affirmative marker with no real "NO"
# anywhere nearby, flipping a genuinely clean run to `'found'` and exiting the ship
# gate at 1 — the exact false-failure class round 1 set out to kill, and NOT one of
# the two documented residuals (those both require a preceding "no" of some form).
# Symmetric with `_NO_WORD_RE`'s own letter-only boundary above.
_REFUTATION_PHRASE = (
    rf"{re.escape(_REFUTATION_WORD)}{_DECORATION}{re.escape(_FOUND_WORD)}"
    rf"(?![A-Za-z]){_DECORATION}:?"
)
# Two SEPARATE regexes instead of one shared pattern with an optional "NO" capture
# group (Opus/k3 review finding, round 6 — replacing round 4's "definitive fix",
# which wasn't): a single pattern where "NO" is merely OPTIONAL makes "NO REFUTATION
# FOUND" and "REFUTATION FOUND" fight over the SAME text — "NO REFUTATION FOUND"
# literally CONTAINS "REFUTATION FOUND" as a substring (the exact round-2 bug), and
# every attempt to resolve that fight with lookbehinds/anchoring/ordering (rounds
# 2-4) reopened a new decoration/whitespace combination the fix's own asymmetry
# couldn't see. Matching each reading with its OWN pattern and then explicitly
# checking for OVERLAP (`refutation_verdict` below) sidesteps the fight entirely:
# an affirmative match is only counted if it is NOT contained inside a null match's
# own span, so "NO REFUTATION FOUND"'s internal "REFUTATION FOUND" substring is
# recognized as PART OF the null marker, never as a competing standalone finding.
_NULL_MARKER_RE = re.compile(
    rf"(?i){_DECORATION}{_NO_WORD_RE}{_NO_GAP}{_REFUTATION_PHRASE}"
)
_AFFIRMATIVE_MARKER_RE = re.compile(rf"(?i){_DECORATION}{_REFUTATION_PHRASE}")


def quorum_verdict_is_clean(summary: str) -> bool:
    """Whether the moderator's synthesis (`mod_prompt`'s output) reports NO real
    disagreement — every expert essentially agreed there's nothing blocking.

    Looks specifically at the "2. DISAGREEMENT / NO QUORUM" section the moderator is
    asked to always emit: clean iff that section is empty, or reduces to a trivial
    "none"/"no disagreement" phrase once the heading itself is stripped. If the section
    can't be found at all (a moderator that didn't follow the structure), this returns
    False — the adversarial pass is a COST, so a synthesis we can't confidently read as
    clean should not silently trigger it. False positives here (missing a genuinely
    clean verdict) only mean --adversarial-check does slightly less than it could; false
    negatives (running the pass when disagreement DID exist) would be the wrong-cost
    direction, so this is deliberately conservative."""
    match = _DISAGREEMENT_SECTION_RE.search(summary)
    if not match:
        return False
    content = match.group(1).strip()
    if not content:
        return True
    return bool(_TRIVIAL_DISAGREEMENT_RE.match(content))


def _adversarial_refutation_prompt(question: str, transcript: str, summary: str) -> str:
    """One extra pass whose ONLY job is to attack the panel's clean verdict — not
    another vote, an explicit attempt to find what the panel missed. Mirrors the
    evidence-or-it-didn't-happen contract DEFAULT_PROMPT now asks every board reviewer
    for (audit finding: no evidence requirement for a clean pass): a null result must
    name what was specifically checked, not just assert agreement with the panel.

    Deliberately does NOT receive `diff_block`/`dispatch_diff` (k3 review finding,
    round 6): this pass can only re-examine the EXPERTS' OWN ANSWERS and the
    moderator's synthesis, not the raw diff independently — if every expert overlooks
    a buggy hunk and so never quotes it, this pass cannot discover the omission
    either. This mirrors a PRE-EXISTING, already-tracked limitation: the moderator
    call just above this function has NEVER received the diff either, in any version
    of this file (see the `run_moderator(moderators, mod_prompt, ...)` call site's own
    comment) — whether the moderator (and, by the same reasoning, this adversarial
    pass) SHOULD see the diff directly is tracked as review-cli#189, not fixed here.

    Uses `AFFIRMATIVE_REFUTATION_MARKER` / `REFUTATION_NOT_FOUND_MARKER` (Opus review
    finding, round 9) rather than a bare inline literal — see those constants' own
    comment for why a hardcoded copy here would silently desync from the regexes
    that parse this prompt's answer."""
    return (
        "You are an ADVERSARIAL reviewer whose ONLY job is to try to REFUTE the "
        "panel's conclusion below — do not rubber-stamp it. The panel reached a CLEAN "
        "verdict (no blocking disagreement). Actively hunt for something it missed: a "
        "failure scenario, an edge case, a risk, or evidence that contradicts the "
        "synthesis. Try specific concrete scenarios before agreeing the panel was "
        "right — a skim that just restates the synthesis is not an acceptable answer "
        "either way.\n\n"
        f"If you find a genuine problem the panel missed, start your answer with "
        f"'{AFFIRMATIVE_REFUTATION_MARKER}' followed by a concise description, "
        "concrete evidence (file/line or reasoning), and why it matters.\n"
        f"If you tried and found nothing that overturns the panel's conclusion, answer "
        f"with exactly '{REFUTATION_NOT_FOUND_MARKER}' followed by a short list of the "
        "specific scenarios you checked and why each doesn't hold up.\n\n"
        f"QUESTION:\n{question}\n\n=== EXPERT ANSWERS ===\n{transcript}\n\n"
        f"=== MODERATOR SYNTHESIS ===\n{summary}"
    )


def _has_unclaimed_affirmative(body: str, null_spans: list[tuple[int, int]]) -> bool:
    """True iff some `_AFFIRMATIVE_MARKER_RE` match in `body` is NOT contained inside
    one of `null_spans` — i.e. a genuine standalone "REFUTATION FOUND", not just the
    tail substring of a "NO ... REFUTATION FOUND" null marker (round 6's overlap-
    exclusion fix; see `refutation_verdict`'s docstring)."""
    for m in _AFFIRMATIVE_MARKER_RE.finditer(body):
        start, end = m.span()
        if not any(ns <= start and end <= ne for ns, ne in null_spans):
            return True
    return False


def refutation_verdict(result: ReviewResult) -> str:
    """One of `'found'` / `'not_found'` / `'inconclusive'` — a three-way read of the
    adversarial pass, never a two-way guess (k3/Opus review finding, round 1): the
    ORIGINAL two-way `not startswith(NULL_MARKER)` treated ANY rewording of the null
    marker as a found refutation ("I could not find anything... Checked: X" doesn't
    start with 'NO REFUTATION FOUND', so it read as a real finding and flipped a
    passed ship-gate check to a false failure). This instead requires the EXPLICIT
    affirmative `REFUTATION FOUND` phrase for `'found'`, the (decoration- and
    whitespace-tolerant) null marker for `'not_found'`, and falls back to
    `'inconclusive'` for anything else — an unusable result (backend error, empty
    output, unavailable sentinel) is always inconclusive, never a found refutation.

    ANY qualifying occurrence wins "found" — this does NOT stop at the first match
    (k3/Opus review finding, round 5): the prompt asks for "a short list of the
    specific scenarios you checked," which invites a model to narrate per-scenario
    verdicts ("checked retry path: no refutation found there... REFUTATION FOUND:
    the checkpoint races the stamp"). A single leftmost `search` (round 4's design)
    let an EARLIER null mention hide a LATER genuine finding, silently discarding
    it — the exact false-pass this whole feature exists to prevent, and repeatedly
    documented in this file as "the more dangerous direction" than the reverse. So
    every occurrence is checked, and a single genuine affirmative anywhere in the
    body outweighs any number of null mentions elsewhere.

    Overlap-exclusion, not a shared optional-group pattern (Opus/k3 review finding,
    round 6 — see `_NULL_MARKER_RE`'s comment for why round 4's "definitive fix"
    wasn't): `_NULL_MARKER_RE` and `_AFFIRMATIVE_MARKER_RE` are matched SEPARATELY,
    then any affirmative match whose span falls entirely INSIDE a null match's span
    is treated as part of that null marker, not a competing standalone finding. This
    is what correctly reads "NO REFUTATION FOUND" as pure null (the affirmative
    sub-match is claimed by the null span) while still reading "**NO** REFUTATION
    FOUND" and "NO: REFUTATION FOUND" as null too (round 6's actual fix — see
    `_NO_GAP`'s comment for why round 4's bare `\\s+` missed these).

    ACCEPTED RESIDUAL (not fixed, documented — k3/Opus review finding, rounds 5-6):
    two distinct classes of free-text this regex-based approach cannot correctly
    parse, in OPPOSITE directions of risk:
      * A model that merely QUOTES the affirmative instruction back without
        asserting a finding, OR writes an ordinary sentence where a real word (not
        decoration) separates "no" from "refutation found" — e.g. "No actual
        refutation found." — can false-match as `'found'` on a genuinely clean run.
        This is the SAFE-direction residual: it costs an unnecessary extra human
        look at a clean diff, never hides a real problem.
      * A model that uses the null marker's own text inside a NEGATED or QUOTED
        context to describe a real problem — e.g. "I cannot honestly answer 'NO
        REFUTATION FOUND': quorum.py:469 never sends the diff to the adversarial
        pass" — reads as `'not_found'` even though the prose asserts a genuine
        refutation. This IS the dangerous direction (a real problem silently
        passing the ship gate), and is NOT closed by this fix. Both residuals need
        a fundamentally different parsing discipline (reasoning about where the
        model states its actual conclusion, not just marker text presence) that a
        regex cannot express; the prompt's own wording ("start your answer with
        REFUTATION FOUND:" / "answer with exactly NO REFUTATION FOUND") is written
        to steer a well-behaved model away from either shape, which is why this
        residual is accepted rather than chased further here."""
    if not result_is_usable(result):
        return "inconclusive"
    body = result.stdout.strip()
    null_spans = [m.span() for m in _NULL_MARKER_RE.finditer(body)]
    if _has_unclaimed_affirmative(body, null_spans):
        return "found"
    if null_spans:
        return "not_found"
    return "inconclusive"


def refutation_succeeded(result: ReviewResult) -> bool:
    """A refutation pass 'succeeds' (surfaces a real, new finding) only when
    `refutation_verdict` reads `'found'` — an inconclusive or backend-failed extra
    pass must never silently fail a run just because IT couldn't produce a clean
    answer; see `refutation_verdict`'s docstring for the false-failure bug this
    three-way read replaces."""
    return refutation_verdict(result) == "found"


def _seat_assignments(
    models: list[str],
) -> tuple[list[str], list[tuple[str, str]]]:
    """One (label, persona) pair per seat, computed ONCE and shared by both the
    transcript label and the seat's own prompt (Fable/k3 review finding: computing
    `PERSONAS[i % len(PERSONAS)]` independently in two places can silently desync a
    seat's disclosed lens from the lens it actually reasoned from).

    Persona is chosen by PER-MODEL occurrence, offset by that model's OWN first seat
    index — not by the raw global seat index — so two seats sharing one model always
    land on DISTINCT personas up to `len(PERSONAS)` repeats of that one model. Raw
    `PERSONAS[i % len(PERSONAS)]` collides once a model's repeats are spaced exactly
    `len(PERSONAS)` seats apart (e.g. seats 0 and 6 of a 7-seat `[A,B,A,B,A,B,A]`
    panel, which `expand_flat_models_with_reuse`'s cycling padding reaches for real
    once a pool is down to 2 reachable models) — Fable AND k3 independently found
    this exact collision, reintroducing the zero-diversity duplicate-seat failure
    this feature exists to prevent. Distinct models MAY still land on the same
    persona by position (harmless — the invariant that matters is same-model seats
    never repeating a lens, not global uniqueness). The one case this can't fully
    avoid is a SINGLE model occupying more than `len(PERSONAS)` seats by itself (a
    >6x reuse of one model in one panel) — k3 review finding: this IS reachable
    today, not just theoretical, via a config.yaml `models:` list of 7+ entries
    (no length cap, cli.py's `expand_flat_models_with_reuse(reachable, ...)`) where
    usage-limit exclusion (or its all-near-limit least-depleted fallback) leaves
    only one survivor — the panel becomes `[A]*7+` and seat #1/#7 of A repeat a
    lens. Deliberately NOT guarded against here (a large `models:` list collapsing
    to one survivor is itself an unusual config), but a caller building much
    larger or more usage-constrained panels should re-check this — tracked as a
    follow-up (review-cli#206) rather than adding warn/fail-loud machinery now.

    Labels are `<model> [<persona>]`, or `<model>#N [<persona>]` when a model
    occupies more than one seat (reuse-aware panel padding, `expand_flat_models_
    with_reuse` in cli.py) — the `#N` keeps repeated seats grep-able by model
    identity even though their persona differs."""
    first_index: dict[str, int] = {}
    seat_counts: dict[str, int] = {}
    for i, model in enumerate(models):
        first_index.setdefault(model, i)
        seat_counts[model] = seat_counts.get(model, 0) + 1

    occurrence: dict[str, int] = {}
    labels: list[str] = []
    personas: list[tuple[str, str]] = []
    for model in models:
        # 0-indexed occurrence of THIS model so far -> occ+1 is its 1-indexed seat
        # number, reused directly for both the persona offset and the `#N` label
        # (Fable review finding, round 3: a separate `seen` counter that always
        # equalled `occ + 1` was redundant bookkeeping in a function whose whole
        # point is eliminating exactly this class of desync risk).
        occ = occurrence.get(model, 0)
        occurrence[model] = occ + 1
        persona = PERSONAS[(first_index[model] + occ) % len(PERSONAS)]
        personas.append(persona)
        if seat_counts[model] > 1:
            labels.append(f"{model}#{occ + 1} [{persona[0]}]")
        else:
            labels.append(f"{model} [{persona[0]}]")
    return labels, personas


def mode_quorum(
    question: str,
    models: list[str],
    diff: str,
    cwd: Path,
    timeout: int,
    moderators: list[str],
    visual_images: tuple[Path, ...] = (),
    effort_override: "EffortOverride | None" = None,
    diff_from_stdin: bool = False,
    diff_already_capped: bool = False,
    adversarial_check: bool = False,
) -> int:
    # codex review finding: the dispatch-time diff cap used to be applied ONLY by
    # cli.py's `_dispatch`, so a caller reaching this function directly (a library
    # consumer, an MCP seam, a test) could still send an uncapped diff to every expert
    # — capped here too, at the mode's own dispatch boundary, mirroring mode_review /
    # mode_brainstorm / mode_just_ask. Same stdin exemption: a piped diff was already an
    # explicit, deliberate scope choice by the caller.
    #
    # codex review finding, round 3 — corrects an earlier, INACCURATE version of this
    # comment that claimed capping applies "to every expert AND THE MODERATOR": the
    # moderator call below (`run_moderator(moderators, mod_prompt, cwd, timeout)`) has
    # NEVER received `diff` at all, in any version of this function — it synthesizes
    # QUORUM/DISAGREEMENT from the experts' own transcript, not from the raw diff again.
    # That is PRE-EXISTING behavior this PR did not introduce or change, so this cap
    # genuinely has nothing to do for the moderator today. Whether the moderator SHOULD
    # see the diff too is a separate, real question — tracked as review-cli#189, not
    # fixed here (out of scope for the diff-cap feature this comment is actually about).
    #
    # codex review finding (round 2, applied here to match just_ask.py's identical fix):
    # capping AGAIN when the CLI layer already capped it (`diff_already_capped`) is
    # silently harmless at the default cap (the first call's output is already <= cap,
    # so a second call is a true no-op) but NOT idempotent when
    # `$REVIEW_DIFF_MAX_BYTES` is set below the truncation marker's own length — a
    # second application would re-truncate the FIRST call's marker text and report ITS
    # byte count as "the full diff". `diff_already_capped` (default False, so a direct
    # library caller bypassing the CLI is still protected) skips the redundant second
    # call entirely rather than relying on it happening to be a no-op.
    dispatch_diff = (
        diff if diff_from_stdin or diff_already_capped else cap_diff_for_dispatch(diff)
    )
    diff_block = _diff_context_block(dispatch_diff)
    # Every seat gets a persona/role from the SAME rotation brainstorm uses (Alex,
    # 2026-08-18: quorum panels should run "под разными полями" — distinct fields/roles
    # — not just distinct models). `models` can also contain the SAME model more than
    # once (reviewlib.config's reuse-aware panel padding — see cli.py's
    # `expand_flat_models_with_reuse` wiring): when a distinct-model pool is scarce or
    # some models are near their usage limit, one model fills several seats instead of
    # shrinking the panel — a model covering multiple roles in parallel is a perfectly
    # valid panel shape, not a lesser one (Alex, 2026-08-21). `_seat_assignments` picks
    # ONE (label, persona) pair per seat and both the label and the prompt below consume
    # that SAME value — see its docstring for why persona is keyed on per-model
    # occurrence rather than raw seat index. Repeated occurrences stay grep-able via a
    # "<model>#N" prefix (via PanelJob.label, which `run_panel` uses to relabel the
    # result), so the transcript and stats can always tell which seat produced which
    # answer even when one model fills several of them.
    labels, personas = _seat_assignments(models)

    jobs = [
        PanelJob(
            model=model,
            prompt=_expert_prompt(*personas[i], question, diff_block),
            diff="",
            images=visual_images,
            effort=_run_effort(effort_override, model),
            label=labels[i],
        )
        for i, model in enumerate(models)
    ]
    expert_results = run_panel(jobs, cwd, timeout)
    # glm-5.2 review finding (2026-08 seat-cooldown feature): `run_panel`'s own
    # auto-tally counts a cached-cooldown sentinel (rc=0, non-empty "unavailable"
    # body) as `ok` in run-stats. `run_moderator` (called below) already fixes this
    # for the moderator's own tally; this recount does the same for the expert panel
    # so the two halves of one quorum run agree.
    recount_round_by_usability(expert_results)

    transcript = "\n\n".join(
        f"### Expert: {r.model} [{'ok' if r.returncode == 0 else f'exit {r.returncode}'}]\n"
        f"{(r.stdout.strip() or r.stderr.strip() or '(no output)')}"
        for r in expert_results
    )
    # Unconditional (Fable/k3 review finding, round 1): every seat now carries a
    # `[<lens>]` suffix, not just duplicates, so the moderator needs the notation
    # explained regardless of whether any model repeats. NOT worded as "distinct"
    # (Fable review finding, round 2): `_seat_assignments` only guarantees a
    # repeated MODEL never repeats its own lens — two DIFFERENT models can land on
    # the same lens by position, which is fine (two independent security reviews
    # from two different vendors), but claiming blanket distinctness to the
    # moderator would be false and could bias its de-dup judgement. Bounded to
    # "up to its first len(PERSONAS) seats" (Fable/k3 review finding, round 3):
    # beyond that, a single model CAN repeat a lens — see `_seat_assignments`'
    # own docstring for the exact (reachable, not just theoretical) condition.
    # The count is INTERPOLATED, not hardcoded (Fable/k3 review finding, round 4
    # — both independently caught that a hardcoded "SIX" would silently go stale
    # the moment someone resizes the PERSONAS tuple, since the only guard on the
    # tuple's size was `>= 5`, not `== 6`; interpolating means the note can never
    # be wrong regardless of pool size, closing the gap for good instead of just
    # tightening one assertion). The worked example is ALSO interpolated, not a
    # hardcoded persona name (Fable review finding, round 5): this very diff
    # renamed one persona, and a future rename of whichever one happened to be
    # hardcoded here would go stale the same way — pulling from `PERSONAS`
    # directly means any future rename updates the example automatically.
    lens_note = (
        "\n\nNOTE: each expert below is assigned a role/lens (shown in brackets "
        f"after the model, e.g. `glm [{PERSONAS[1][0]}]`; not necessarily unique "
        "across different models, and a single model is only guaranteed a "
        f"different lens across its first {len(PERSONAS)} seats) — each reasons "
        "from that role, but the lens does not change how their answer should be "
        "weighted."
    )
    mod_prompt = (
        "You are the MODERATOR of an expert panel. Below are independent expert answers to "
        "one question. Produce a structured summary with exactly these sections:\n"
        "1. QUORUM — points where a majority of experts agree AND cite evidence "
        "(state the point, who agrees, and the evidence).\n"
        f"2. {_DISAGREEMENT_HEADING} — points where experts conflict or no majority exists.\n"
        f"3. {_ABSTAINED_HEADING} — experts who said INSUFFICIENT EVIDENCE, and on what.\n"
        "Do not invent agreement. Do not edit files."
        f"{lens_note}\n\n"
        f"QUESTION:\n{question}\n\n=== EXPERT ANSWERS ===\n{transcript}"
    )
    # No `diff=` here — pre-existing (not something this diff-cap feature changed): the
    # moderator synthesizes from the experts' own transcript above, never the raw diff
    # again. Whether it SHOULD also see the diff directly is tracked separately as
    # review-cli#189 (codex review finding, round 3), out of scope for this cap.
    mod_result = run_moderator(moderators, mod_prompt, cwd, timeout)

    # Opt-in adversarial refutation pass (audit finding: "no refutation / adversarial
    # cross-check step"). Only spent when the caller asked for it (--adversarial-check
    # — the ship-gate-critical path, e.g. a merge/ship decision) AND the synthesis
    # actually reached a clean verdict: if the panel already disagreed, there is
    # already something to look at and no clean "no issues found" claim to refute.
    # A SKIP is never silent (k3 review finding, round 1): a caller who explicitly
    # asked for the ship-gate check must see WHY it didn't run, not just its absence
    # from the output — a bare missing section reads as "forgot to print it", not
    # "deliberately skipped because X".
    adversarial_result: ReviewResult | None = None
    adversarial_skip_reason: str | None = None
    if adversarial_check:
        if not result_is_usable(mod_result):
            adversarial_skip_reason = (
                "the moderator's own synthesis was not usable (backend error, empty "
                "output, or an unavailable sentinel) — nothing to refute"
            )
        elif not quorum_verdict_is_clean(mod_result.stdout):
            # Distinguish "real disagreement" from "could not parse the section at
            # all" in the skip message (Opus review finding, round 6):
            # `quorum_verdict_is_clean` deliberately collapses both to False (see
            # its own docstring — a parse miss should not RUN the extra pass), but a
            # human reading the transcript to decide whether the skip itself is a
            # problem needs to know WHICH case they're looking at — a moderator
            # that genuinely disagreed already gives them something to read in
            # section 2 above, while an unparseable synthesis (a heading reword, a
            # renumbered section) means the panel's own real verdict was never
            # legible to begin with, independent of --adversarial-check.
            if _DISAGREEMENT_SECTION_RE.search(mod_result.stdout) is None:
                adversarial_skip_reason = (
                    "the moderator synthesis did not follow the expected structure "
                    "— its 'DISAGREEMENT / NO QUORUM' section could not be located "
                    "at all, so the verdict could not be confidently read as clean "
                    "— the refutation pass was skipped rather than risk running it "
                    "against an unparseable synthesis"
                )
            else:
                # Hedged, not asserted (k3 review finding, round 8): a non-empty
                # section 2 is NOT necessarily real disagreement — it could equally
                # be ordinary clean phrasing `_TRIVIAL_DISAGREEMENT_RE`'s fixed
                # alternation doesn't yet recognize ("None to report.", "No
                # conflicts.", "All experts agree."), which would make this message
                # just as wrong as the "unparseable" case it was split out to fix.
                # Either way there is real text in section 2 above worth reading
                # yourself, so the wording only claims that much.
                adversarial_skip_reason = (
                    "the moderator synthesis's 'DISAGREEMENT / NO QUORUM' section "
                    "above is non-empty and did not reduce to a recognized "
                    "trivial/clean phrase (this may be real disagreement, or may be "
                    "clean phrasing this heuristic doesn't recognize yet) — read "
                    "that section yourself; the refutation pass was skipped rather "
                    "than assume it's clean"
                )
        else:
            # round_no=1 (Opus review finding, round 9): distinct from the ORIGINAL
            # moderator call's implicit round_no=0 above. Every per-call log
            # filename is ALSO timestamped to microsecond precision (see
            # `_open_log`/`write_sidecar_log`), and this call only starts once the
            # first `run_moderator` above has already returned, so the two calls'
            # logs never actually collide even with round_no=0 for both — verified,
            # not assumed. This is purely a diagnostic improvement: a human tailing
            # `~/Library/Logs/review-cli/` can tell the adversarial pass's log apart
            # from the original moderator's at a glance, rather than by timestamp
            # order alone.
            adversarial_result = run_moderator(
                moderators,
                _adversarial_refutation_prompt(question, transcript, mod_result.stdout),
                cwd,
                timeout,
                round_no=1,
            )

    out = [
        "# Expert answers",
        "\n\n---\n\n".join(format_result(r) for r in expert_results),
    ]
    out += ["\n# Moderator summary", format_result(mod_result)]
    if adversarial_result is not None:
        # Surface an INCONCLUSIVE refutation pass distinctly (k3/Opus review finding,
        # round 1): an answer that is neither the affirmative marker nor the (tolerant)
        # null marker is NOT silently folded into "must be a clean pass" — the header
        # itself says the extra call didn't resolve one way or the other, so a human
        # reading the transcript knows to weigh the raw text themselves.
        header = "\n# Adversarial check (--adversarial-check)"
        if refutation_verdict(adversarial_result) == "inconclusive":
            # Distinguish "the call itself failed" from "it ran but matched neither
            # marker" (Opus review finding, round 9): `refutation_verdict` returns
            # `'inconclusive'` for BOTH an unusable result (backend error, empty
            # output, unavailable sentinel) and a usable answer that just doesn't
            # match either marker — the ORIGINAL wording here always assumed the
            # second case, so a genuinely failed extra call got told "the answer
            # matched neither marker" when there was no real answer to read at all.
            if not result_is_usable(adversarial_result):
                header += (
                    " — INCONCLUSIVE: the extra call itself was not usable (backend "
                    "error, empty output, or an unavailable sentinel) — there is no "
                    "real answer to weigh, not just an unmatched one"
                )
            else:
                header += (
                    " — INCONCLUSIVE: the answer matched neither the affirmative "
                    f"'{AFFIRMATIVE_REFUTATION_MARKER}' marker nor the null marker; "
                    "read the raw answer below yourself rather than trusting this "
                    "as a pass"
                )
        out += [header, format_result(adversarial_result)]
    elif adversarial_skip_reason is not None:
        out += [
            f"\n# Adversarial check (--adversarial-check) — SKIPPED: "
            f"{adversarial_skip_reason}"
        ]
    print("\n\n".join(out))
    # codex review finding (2026-08 seat-cooldown feature): a cached-cooldown-skip
    # result deliberately mirrors a live "is currently unavailable" sentinel — rc=0,
    # non-empty body — so a plain `returncode == 0` check would count a cooling-down
    # expert (or moderator) as a real answer instead of a cache hit. `result_is_usable`
    # is the same predicate `mode_review`'s flat/board paths already use for this.
    ok = all(result_is_usable(r) for r in expert_results) and result_is_usable(
        mod_result
    )
    # A SUCCESSFUL refutation (the adversarial pass surfaced a real finding, not just
    # an inconclusive/failed call) overturns the clean verdict — the whole point of
    # this pass is that it must not be silently discarded (audit finding). An
    # unusable refutation attempt (backend error) does NOT flip `ok`: the original
    # panel already reached a clean, usable verdict, and a failed EXTRA check is not
    # evidence against it.
    if adversarial_result is not None and refutation_succeeded(adversarial_result):
        ok = False
    return 0 if ok else 1


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("question", help="the question to put to the expert quorum")
    parser.add_argument(
        "--adversarial-check",
        action="store_true",
        help=(
            "after a CLEAN verdict (no blocking disagreement), spawn one more pass "
            "explicitly told to try to REFUTE 'no issues found'; a successful "
            "refutation is surfaced as a new finding and flips the run to non-zero. "
            "For the SHIP-GATE-CRITICAL path (consulted before a merge/ship decision) "
            "— an extra model call, so it's opt-in rather than run on every ad-hoc "
            "`review quorum` question."
        ),
    )


def _handler(ctx: ModeContext) -> int:
    images = _visual_images(ctx)
    diff_from_stdin = bool(ctx.extra.get("diff_from_stdin", False))
    diff_already_capped = bool(ctx.extra.get("diff_already_capped", False))
    adversarial_check = bool(getattr(ctx.args, "adversarial_check", False))
    if images:
        return mode_quorum(
            ctx.with_visual(ctx.args.question),
            ctx.models,
            ctx.diff,
            ctx.cwd,
            ctx.timeout,
            ctx.moderators,
            images,
            effort_override=ctx.effort_override,
            diff_from_stdin=diff_from_stdin,
            diff_already_capped=diff_already_capped,
            adversarial_check=adversarial_check,
        )
    return mode_quorum(
        ctx.with_visual(ctx.args.question),
        ctx.models,
        ctx.diff,
        ctx.cwd,
        ctx.timeout,
        ctx.moderators,
        effort_override=ctx.effort_override,
        diff_from_stdin=diff_from_stdin,
        diff_already_capped=diff_already_capped,
        adversarial_check=adversarial_check,
    )


MODE = ModeSpec(
    name="quorum",
    subcommand="quorum",
    diff_policy="none",
    stats_mode="quorum",
    summary="experts cite evidence + a moderator finds quorum/disagreement",
    handler=_handler,
    add_arguments=_add_arguments,
    announce_logs=True,
)
