#!/usr/bin/env python3
"""Adversarial review-rigor audit fixes (Alex, 2026-08-21 — "Поправляй плиз все находки").

Four independent fixes, each pinned by its own tests below:

  1. NEUTRAL -> ADVERSARIAL base prompt (config.DEFAULT_PROMPT): every reviewer is now
     told to actively try to break the change before concluding it's fine, not just
     skim it. Shared by every board seat (review diff), so all 8 REVIEW_ROLES inherit it.
  2. Evidence requirement for a CLEAN verdict: DEFAULT_PROMPT now requires a "checked:
     ..." statement to back a "no issues" claim, and `panel.clean_verdict_missing_
     evidence` / `format_result` surface a body that skips it (a heuristic WARNING, not
     an enforcement gate — the full content-verdict effort is tracked separately as
     review-cli#137).
  3. Opt-in `--adversarial-check` on `review quorum`: after a CLEAN moderator synthesis,
     one more pass tries to REFUTE "no issues found"; a successful refutation is
     surfaced as a new finding and flips the run to non-zero. Gated on BOTH the flag
     AND a clean verdict, so it costs nothing on a routine question or a panel that
     already disagreed.
  4. REVIEW_ROLES["security"] / REVIEW_ROLES["tests"] now carry the "security-paranoid
     reviewer" / "skeptical SRE" adversarial framing from the quorum/brainstorm
     persona pool (reviewlib.modes.PERSONAS), while keeping the 8-role board structure
     and each role's own narrow focus intact.

Same harness style as tests/test_diff_cap.py: `q_mod.run_panel` / `q_mod.run_moderator`
are monkeypatched module globals, no real backend/network involved.
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib.backends import ReviewResult  # noqa: E402
from reviewlib.cli import _with_visual  # noqa: E402
from reviewlib.config import DEFAULT_PROMPT, REVIEW_ROLES, BoardReviewer  # noqa: E402
from reviewlib.modes import PERSONAS  # noqa: E402
from reviewlib.modes import quorum as q_mod  # noqa: E402
from reviewlib.modes import review as review_mod  # noqa: E402
from reviewlib.panel import (  # noqa: E402
    FailoverOutcome,
    clean_verdict_missing_evidence,
    format_result,
)


# --- Finding 1: adversarial framing in the shared base prompt -------------------------


def test_default_prompt_instructs_actively_trying_to_break_the_change():
    low = DEFAULT_PROMPT.lower()
    assert "actively try to find a way it is wrong" in low, DEFAULT_PROMPT
    assert "only report 'no issues'" in low or "only report ‘no issues’" in low.replace(
        "‘", "'"
    ), DEFAULT_PROMPT


def test_default_prompt_flows_into_every_board_role_lens():
    # REVIEW_ROLES entries are APPENDED to DEFAULT_PROMPT (panel.build_board_job); every
    # one of the 8 roles inherits the adversarial framing by construction, not by
    # duplicating it per-role. Pin the role count so this stays true if the table grows.
    assert len(REVIEW_ROLES) == 8, sorted(REVIEW_ROLES)
    for role, lens in REVIEW_ROLES.items():
        assert lens.strip(), role  # every role still has its own narrow-focus lens


# --- Finding 2: evidence requirement for a CLEAN verdict ------------------------------


def test_default_prompt_requires_evidence_for_a_clean_verdict():
    low = DEFAULT_PROMPT.lower()
    assert "checked:" in low, DEFAULT_PROMPT
    assert "not an acceptable answer" in low, DEFAULT_PROMPT
    assert "file:line" in low, DEFAULT_PROMPT


def test_clean_verdict_missing_evidence_flags_a_bare_rubber_stamp():
    assert clean_verdict_missing_evidence("Looks good, no issues found.") is True
    assert clean_verdict_missing_evidence("LGTM") is True
    assert clean_verdict_missing_evidence("No problems found in this diff.") is True


def test_clean_verdict_missing_evidence_accepts_a_checked_statement():
    body = (
        "No issues found. Checked: race condition on concurrent writes to the "
        "manifest — none found, guarded by a lock at manifest.py:42."
    )
    assert clean_verdict_missing_evidence(body) is False


def test_clean_verdict_missing_evidence_accepts_a_real_finding():
    body = (
        "reviewlib/foo.py:42 — off-by-one in the retry loop, retries one time too many."
    )
    assert clean_verdict_missing_evidence(body) is False


def test_clean_verdict_missing_evidence_ignores_empty_body():
    assert clean_verdict_missing_evidence("") is False
    assert clean_verdict_missing_evidence("   ") is False


def test_clean_verdict_missing_evidence_never_warns_on_a_rejecting_verdict():
    # k3 review finding, round 8: the original `_CLEAN_VERDICT_RE` included a bare
    # `\bapproved\b` alternative, which also matched inside a REJECTING verdict —
    # "Not approved — the checkpoint races the stamp write." — attaching the
    # missing-evidence WARNING to a verdict that already blocked the change, actively
    # misdescribing it as a suspicious "clean" pass rather than a real finding.
    # DEFAULT_PROMPT never asks a reviewer to say "approved" in the first place, so
    # the word was dropped from the alternation entirely rather than chasing every
    # negation ("not approved", "never approved") with more lookbehinds.
    # Deliberately no file:line / "checked:" text here — this must be excluded
    # because "approved" is gone from the word list, not because the earlier
    # evidence checks already short-circuited it (which would prove nothing about
    # this specific fix).
    rejecting = (
        "Not approved. The checkpoint races the stamp write, so I'm blocking this."
    )
    assert clean_verdict_missing_evidence(rejecting) is False


def test_format_result_surfaces_the_missing_evidence_warning_when_opted_in():
    result = ReviewResult(
        model="m1",
        command="cmd",
        returncode=0,
        stdout="Looks good, no issues found.",
        stderr="",
    )
    text = format_result(result, check_evidence=True)
    assert "WARNING" in text and "no evidence of what was checked" in text, text


def test_format_result_stays_quiet_on_a_backed_clean_verdict():
    result = ReviewResult(
        model="m1",
        command="cmd",
        returncode=0,
        stdout="No issues. Checked: injection via user input — sanitized at input.py:10.",
        stderr="",
    )
    assert "WARNING" not in format_result(result, check_evidence=True)


def test_format_result_default_never_warns_even_on_a_bare_rubber_stamp():
    # glm-cc review finding, round 1: DEFAULT_PROMPT's "checked: ..." contract is
    # diff-review-only — quorum/just-ask/brainstorm never ask a model for that
    # phrasing, so their format_result calls must NOT opt in, and a synthesis like
    # "nothing blocking, all experts agree" must never get flagged as a suspicious
    # skim it was never asked to avoid. check_evidence defaults to False.
    result = ReviewResult(
        model="m1",
        command="cmd",
        returncode=0,
        stdout="Nothing blocking — all experts agree.",
        stderr="",
    )
    assert "WARNING" not in format_result(result), format_result(result)


def test_clean_verdict_missing_evidence_skips_a_very_long_body():
    # glm-cc review finding, round 1 (F3): the finding-evidence regex is quadratic on
    # a long unbroken [\w./-] run with no match inside it — skip the scan outright
    # above a length no genuine rubber-stamp verdict would ever reach.
    long_body = "no issues found " + ("a" * 5000)
    assert clean_verdict_missing_evidence(long_body) is False


# --- Finding 4: security/tests roles blend in adversarial persona framing ------------


def test_security_role_carries_the_security_paranoid_persona_framing():
    persona_bg = dict(PERSONAS)["Security-paranoid reviewer"]
    assert "worst actor" in REVIEW_ROLES["security"], REVIEW_ROLES["security"]
    assert "worst actor" in persona_bg, (
        persona_bg
    )  # the persona's own wording, for parity
    assert "SECURITY" in REVIEW_ROLES["security"]  # narrow focus intact


def test_tests_role_carries_the_skeptical_sre_persona_framing():
    persona_bg = dict(PERSONAS)["Skeptical SRE"]
    assert "failure plan" in REVIEW_ROLES["tests"], REVIEW_ROLES["tests"]
    assert "failure plan" in persona_bg, persona_bg
    assert "TESTS" in REVIEW_ROLES["tests"]  # narrow focus intact


def test_other_roles_are_unchanged_in_focus_and_not_persona_ized():
    # The board stays 8 roles, each with its OWN narrow lens — this fix must not turn
    # the whole board into personas, only blend adversarial tone into security/tests.
    assert "ARCHITECTURE" in REVIEW_ROLES["architect"]
    assert "PERFORMANCE" in REVIEW_ROLES["performance"]
    assert "worst actor" not in REVIEW_ROLES["architect"]
    assert "failure plan" not in REVIEW_ROLES["performance"]


# --- Finding 3: opt-in adversarial refutation pass on `review quorum` ----------------


def _clean_summary(disagreement_body: str = "") -> str:
    return (
        "1. QUORUM — everyone agrees the change is fine.\n"
        f"2. DISAGREEMENT / NO QUORUM — {disagreement_body or 'None.'}\n"
        "3. ABSTAINED — none."
    )


def test_quorum_verdict_is_clean_true_on_empty_disagreement_section():
    assert q_mod.quorum_verdict_is_clean(_clean_summary()) is True
    assert q_mod.quorum_verdict_is_clean(_clean_summary("No disagreement.")) is True


def test_quorum_verdict_is_clean_true_on_a_genuinely_blank_section_with_no_none_text():
    # Opus review finding, round 1 (F1/blocker): the moderator leaves section 2
    # COMPLETELY blank (no "None." text at all) — the heading is immediately
    # followed by the next heading. The ORIGINAL separator class (`[\s:\-—]*`,
    # including `\n`) greedily ate the newline the lookahead needed, so this
    # genuinely clean case was misread as "not clean" and --adversarial-check
    # silently never ran on it. Plain string (not `_clean_summary`, which always
    # substitutes literal "None." text and so never exercised this path).
    summary = "1. QUORUM — fine.\n2. DISAGREEMENT / NO QUORUM\n3. ABSTAINED — none."
    assert q_mod.quorum_verdict_is_clean(summary) is True


def test_quorum_verdict_is_clean_true_on_markdown_decorated_headings():
    # k3 review finding, round 1: a moderator that bolds its section headings
    # (`**2. DISAGREEMENT / NO QUORUM**`) must not be misread as unclean just
    # because `**` sits between the heading and its content/terminator.
    summary = (
        "1. QUORUM — fine.\n"
        "**2. DISAGREEMENT / NO QUORUM** — None.\n"
        "**3. ABSTAINED** — none."
    )
    assert q_mod.quorum_verdict_is_clean(summary) is True


def test_quorum_verdict_is_clean_false_on_real_disagreement():
    summary = _clean_summary(
        "Expert A thinks the retry logic is broken; Expert B disagrees."
    )
    assert q_mod.quorum_verdict_is_clean(summary) is False


def test_quorum_verdict_is_clean_false_when_section_not_found():
    assert q_mod.quorum_verdict_is_clean("some unstructured moderator text") is False


def test_refutation_verdict_found_requires_the_explicit_affirmative_marker():
    exact = ReviewResult(
        model="m",
        command="c",
        returncode=0,
        stdout="REFUTATION FOUND: X breaks under Y.",
        stderr="",
    )
    with_preamble = ReviewResult(
        model="m",
        command="c",
        returncode=0,
        stdout="After careful review, REFUTATION FOUND: X breaks under Y.",
        stderr="",
    )
    assert q_mod.refutation_verdict(exact) == "found"
    assert q_mod.refutation_verdict(with_preamble) == "found"
    assert q_mod.refutation_succeeded(exact) is True


def test_refutation_verdict_not_found_tolerates_markdown_decoration():
    # k3/Opus review finding, round 1: a model routinely bolds the marker.
    decorated = ReviewResult(
        model="m",
        command="c",
        returncode=0,
        stdout="**NO REFUTATION FOUND** — checked X, Y, Z.",
        stderr="",
    )
    assert q_mod.refutation_verdict(decorated) == "not_found"
    assert q_mod.refutation_succeeded(decorated) is False


def test_refutation_verdict_not_found_with_colon_the_most_natural_null_shape():
    # glm-cc/Opus review finding, round 2 (the concrete regression): "NO REFUTATION
    # FOUND" literally CONTAINS "REFUTATION FOUND" as a substring, so a model that
    # naturally introduces its null answer's scenario list with a colon — exactly
    # what the prompt asks for — used to match the AFFIRMATIVE pattern too (checked
    # first in the original code) and flip a clean pass to a false failure. This is
    # the single most natural null shape a model would actually produce, and the
    # two earlier null tests (period, markdown+em-dash) both happened to dodge it.
    colon_form = ReviewResult(
        model="m",
        command="c",
        returncode=0,
        stdout=(
            "NO REFUTATION FOUND: retry-on-5xx (no retry path), manifest race "
            "(lock held)."
        ),
        stderr="",
    )
    assert q_mod.refutation_verdict(colon_form) == "not_found"
    assert q_mod.refutation_succeeded(colon_form) is False


def test_refutation_verdict_not_found_tolerates_whitespace_variant_marker():
    # k3 review finding, round 3: a literal-escaped multi-word marker only matched a
    # single ASCII space between "NO"/"REFUTATION"/"FOUND" — a double space (or a
    # tab/non-breaking space, both things real model/chat output introduces) between
    # words missed the null match entirely and fell through to a FOUND
    # misclassification, flipping a genuine null result to a false ship-gate failure.
    double_space = ReviewResult(
        model="m",
        command="c",
        returncode=0,
        stdout="NO  REFUTATION FOUND: checked retry-on-5xx (no retry path).",
        stderr="",
    )
    assert q_mod.refutation_verdict(double_space) == "not_found"
    assert q_mod.refutation_succeeded(double_space) is False


def test_refutation_verdict_found_tolerates_markdown_between_found_and_colon():
    # Opus review finding, round 3: the null marker's decoration tolerance was not
    # mirrored on the affirmative marker — a model bolding just the phrase
    # ("**REFUTATION FOUND**: ...") puts a `*` between "FOUND" and the colon, which
    # a bare `\s*` did not match, silently downgrading a REAL refutation to
    # "inconclusive" — the ship gate then PASSED despite a genuine finding, the more
    # dangerous direction than the null-marker false positives fixed earlier.
    bolded = ReviewResult(
        model="m",
        command="c",
        returncode=0,
        stdout="**REFUTATION FOUND**: the retry loop double-charges on a 5xx retry.",
        stderr="",
    )
    assert q_mod.refutation_verdict(bolded) == "found"
    assert q_mod.refutation_succeeded(bolded) is True


def test_refutation_verdict_found_tolerates_double_space_between_found_words():
    # k3 review finding, round 4: the round-3 `\s+` widening was applied only to
    # the NULL marker's words, never to the affirmative marker's own two words
    # ("REFUTATION" / "FOUND") — a double space between THEM was still silently
    # downgraded to "inconclusive", the dangerous direction (a real finding not
    # blocking the ship gate).
    double_spaced = ReviewResult(
        model="m",
        command="c",
        returncode=0,
        stdout="REFUTATION  FOUND: the checkpoint races the stamp write.",
        stderr="",
    )
    assert q_mod.refutation_verdict(double_spaced) == "found"
    assert q_mod.refutation_succeeded(double_spaced) is True


def test_refutation_verdict_found_tolerates_dash_separator_with_no_colon():
    # Opus review finding, round 4: requiring a colon on the affirmative side only
    # (the null form never required one) silently downgraded a genuine refutation
    # introduced with an em dash instead of a colon.
    dash_form = ReviewResult(
        model="m",
        command="c",
        returncode=0,
        stdout="REFUTATION FOUND — the retry loop double-charges on a 5xx retry.",
        stderr="",
    )
    assert q_mod.refutation_verdict(dash_form) == "found"
    assert q_mod.refutation_succeeded(dash_form) is True


def test_refutation_verdict_found_tolerates_header_form_with_no_colon():
    # Opus review finding, round 4: a model answering in a markdown-header style
    # ("# REFUTATION FOUND" on its own line, detail below, no colon anywhere) must
    # still read as "found" — the colon was never a hard requirement on the null
    # side either.
    header_form = ReviewResult(
        model="m",
        command="c",
        returncode=0,
        stdout="# REFUTATION FOUND\n\nThe retry breaks at reviewlib/foo.py:1.",
        stderr="",
    )
    assert q_mod.refutation_verdict(header_form) == "found"
    assert q_mod.refutation_succeeded(header_form) is True


def test_refutation_verdict_not_found_survives_preamble_plus_double_space():
    # Opus review finding, round 4 (the primary blocker): a short preamble
    # ("Verdict: ...", legitimate — the affirmative marker already allows one)
    # COMBINED with a double space between "NO" and "REFUTATION" defeated BOTH
    # the round-2 fixed-width lookbehind (its 3-char window no longer matched
    # "NO "/"O  ") and the round-3 null-first ordering (the round-3 `\s+` widening
    # lived only on the ANCHORED null path, and the preamble broke that anchor) —
    # together flipping a genuinely null answer to a false "found" and failing the
    # ship gate on a clean run. This is the concrete case the unified
    # capture-group rewrite (round 4) exists to close.
    preamble_and_double_space = ReviewResult(
        model="m",
        command="c",
        returncode=0,
        stdout=("Verdict: NO  REFUTATION FOUND: checked retry-on-5xx, manifest race."),
        stderr="",
    )
    assert q_mod.refutation_verdict(preamble_and_double_space) == "not_found"
    assert q_mod.refutation_succeeded(preamble_and_double_space) is False


def test_refutation_verdict_not_found_survives_a_newline_between_no_and_refutation():
    # Opus review finding, round 4: a line-wrapped marker ("NO\nREFUTATION FOUND")
    # defeats the same fixed 3-char lookbehind window ("NO\n" != "NO ") the
    # capture-group rewrite no longer depends on.
    line_wrapped = ReviewResult(
        model="m",
        command="c",
        returncode=0,
        stdout="NO\nREFUTATION FOUND: checked X, Y, Z.",
        stderr="",
    )
    assert q_mod.refutation_verdict(line_wrapped) == "not_found"
    assert q_mod.refutation_succeeded(line_wrapped) is False


def test_quorum_verdict_is_clean_true_on_bulleted_bolded_or_plural_disagreement():
    # Opus review finding, round 4: an ordinary bulleted ("- None.") or bolded
    # ("**None.**") clean section, or the plural "No disagreements.", are all
    # normal moderator phrasing, not edge cases — the original trivial-content
    # pattern (bare `\s*`, singular only) misread every one of them as "not
    # clean," silently skipping --adversarial-check on exactly the verdict it
    # exists to double-check.
    for disagreement_body in ("- None.", "**None.**", "No disagreements."):
        summary = _clean_summary(disagreement_body)
        assert q_mod.quorum_verdict_is_clean(summary) is True, summary


def test_quorum_verdict_is_clean_true_on_em_dash_prefixed_content_on_its_own_line():
    # k3 review finding, round 7: the comment above `_TRIVIAL_DISAGREEMENT_RE` has
    # claimed since round 4 that "— No disagreements." (em dash prefix) is tolerated,
    # but the character class only ever contained the hyphen '-', never the em dash
    # '—' — so a moderator that puts the heading on its own line and the trivial
    # phrase on the NEXT line (content, once `_LINE_SEP` strips the same-line
    # decoration and `.strip()` trims the surrounding newline, becomes exactly
    # "— None.") failed this match, read as "not clean," and silently skipped
    # --adversarial-check on a genuinely clean verdict.
    summary = (
        "1. QUORUM — everyone agrees the change is fine.\n"
        "2. DISAGREEMENT / NO QUORUM\n"
        "— None.\n"
        "3. ABSTAINED — none."
    )
    assert q_mod.quorum_verdict_is_clean(summary) is True, summary


def test_refutation_verdict_inconclusive_on_a_reworded_null_never_flips_the_gate():
    # k3/Opus review finding, round 1 (F2): the ORIGINAL check was
    # `not stdout.startswith(NULL_MARKER)` — ANY rewording of a genuine null result
    # (no exact marker) read as a FOUND refutation and flipped a passed ship-gate
    # check to a false failure. A reworded null must be INCONCLUSIVE, never "found".
    reworded = ReviewResult(
        model="m",
        command="c",
        returncode=0,
        stdout=(
            "I could not find anything that overturns the panel's conclusion. "
            "Checked: retry double-charge (no retry on 5xx), race on manifest "
            "(lock held)."
        ),
        stderr="",
    )
    assert q_mod.refutation_verdict(reworded) == "inconclusive"
    assert q_mod.refutation_succeeded(reworded) is False


def test_refutation_verdict_inconclusive_on_a_failed_or_empty_result():
    failed = ReviewResult(
        model="m", command="c", returncode=1, stdout="", stderr="boom"
    )
    empty = ReviewResult(model="m", command="c", returncode=0, stdout="", stderr="")
    assert q_mod.refutation_verdict(failed) == "inconclusive"
    assert q_mod.refutation_verdict(empty) == "inconclusive"
    assert q_mod.refutation_succeeded(failed) is False
    assert q_mod.refutation_succeeded(empty) is False


def test_refutation_verdict_found_wins_over_an_earlier_null_mention_in_the_same_body():
    # k3/Opus review finding, round 5: the prompt asks for "a short list of the
    # specific scenarios you checked," which invites a model to narrate per-scenario
    # verdicts rather than one final answer. A single leftmost `search` (round 4's
    # design) let an EARLIER null mention for one scenario hide a LATER genuine
    # affirmative for a different scenario, silently discarding a real finding — the
    # exact false-pass this whole feature exists to prevent. Every occurrence must be
    # checked, and one genuine affirmative anywhere in the body must win regardless
    # of how many null mentions precede it.
    mixed = ReviewResult(
        model="m",
        command="c",
        returncode=0,
        stdout=(
            "Checked the retry path: NO REFUTATION FOUND there, it's idempotent. "
            "But REFUTATION FOUND: the checkpoint races the stamp write on the "
            "second call path."
        ),
        stderr="",
    )
    assert q_mod.refutation_verdict(mixed) == "found"
    assert q_mod.refutation_succeeded(mixed) is True


def test_refutation_verdict_found_on_a_genuinely_long_verbose_refutation():
    # Opus review finding, round 6: round 5's `_MARKER_SCAN_MAX_LEN` whole-body length
    # cutoff (since removed) fired BEFORE the scan even started, so a genuine, verbose
    # refutation — prose + a code snippet + the per-scenario "checked" list the prompt
    # explicitly asks for — routinely exceeded a few thousand characters with NO
    # pathological decoration run in it, and got silently downgraded to
    # 'inconclusive' — discarding a real finding, exactly the dangerous direction this
    # file swears off repeatedly. Bounding each decoration run instead (`_DECORATION`,
    # `_NO_GAP`) removes the need for any whole-body length gate, so a long genuine
    # body is scanned in full regardless of its length.
    verbose = ReviewResult(
        model="m",
        command="c",
        returncode=0,
        stdout=(
            "REFUTATION FOUND: the checkpoint races the stamp write. "
            + ("Detailed evidence and a full trace of the call path. " * 200)
        ),
        stderr="",
    )
    assert len(verbose.stdout) > 4000
    assert q_mod.refutation_verdict(verbose) == "found"
    assert q_mod.refutation_succeeded(verbose) is True


def test_refutation_verdict_inconclusive_on_a_long_decoration_only_body():
    # The ReDoS-shaped input round 5's length cap was actually trying to guard
    # against: a long run of decoration-only characters with no real marker inside
    # it. Bounded decoration classes (`_DECORATION`/`_NO_GAP`, each capped at 24
    # repeats) make this scan cheap regardless of the run's length, so no length
    # gate is needed for safety either.
    decoration_only = ReviewResult(
        model="m", command="c", returncode=0, stdout=" -*_`#>—" * 2000, stderr=""
    )
    assert len(decoration_only.stdout) > 4000
    assert q_mod.refutation_verdict(decoration_only) == "inconclusive"
    assert q_mod.refutation_succeeded(decoration_only) is False


def test_refutation_verdict_not_found_on_markdown_wrapped_no():
    # Opus review finding, round 6: round 4's `NO\s+` join between "NO" and
    # "REFUTATION" required LITERAL whitespace immediately after "NO" — a model
    # bolding just the "NO" ("**NO** REFUTATION FOUND: ...") or separating it with a
    # colon ("NO: REFUTATION FOUND: ...") put a non-whitespace character right after
    # "NO", so `\s+` failed at that position; the leftmost successful match then
    # started at "REFUTATION" itself with no "NO" consumed, misreading a genuine null
    # answer as an affirmative "found" and flipping a clean run to a false failure.
    for body in (
        "**NO** REFUTATION FOUND: checked the retry path, nothing broke.",
        "NO: REFUTATION FOUND: checked the retry path, nothing broke.",
        "_NO_ REFUTATION FOUND: checked the retry path, nothing broke.",
    ):
        result = ReviewResult(
            model="m", command="c", returncode=0, stdout=body, stderr=""
        )
        assert q_mod.refutation_verdict(result) == "not_found", body
        assert q_mod.refutation_succeeded(result) is False, body


def test_refutation_verdict_accepted_residual_real_word_between_no_and_refutation():
    # k3 review finding, round 6 (documented ACCEPTED RESIDUAL, safe direction): an
    # ordinary sentence where a real WORD — not decoration — separates "No" from
    # "refutation found" cannot be told apart from a standalone affirmative by this
    # regex-based approach; it reads as 'found' on a genuinely clean run. Costs an
    # unnecessary extra human look, never hides a real problem — the accepted
    # trade-off documented in `refutation_verdict`'s own docstring.
    result = ReviewResult(
        model="m",
        command="c",
        returncode=0,
        stdout="No actual refutation found after checking the retry and race paths.",
        stderr="",
    )
    assert q_mod.refutation_verdict(result) == "found"


def test_refutation_verdict_accepted_residual_merely_quoting_the_instruction():
    # Opus review finding, round 11 (documented ACCEPTED RESIDUAL, SAFE direction —
    # third and last of the three residuals `refutation_verdict`'s docstring names,
    # previously unpinned): a model that merely QUOTES the affirmative instruction
    # back while explaining it does NOT apply, without ever asserting a real finding,
    # still contains the literal "REFUTATION FOUND" phrase with no preceding "NO"
    # anywhere near it — so it false-matches as `'found'` on a genuinely clean run.
    # Same safe-direction trade-off as the "real word between no/refutation found"
    # residual above: costs an unnecessary extra human look, never hides a problem.
    result = ReviewResult(
        model="m",
        command="c",
        returncode=0,
        stdout=(
            "I considered whether a 'REFUTATION FOUND' response would be warranted "
            "here — it isn't; the panel's synthesis holds up against every scenario "
            "I tried."
        ),
        stderr="",
    )
    assert q_mod.refutation_verdict(result) == "found"


def test_refutation_verdict_accepted_residual_quoted_null_marker_hides_a_real_finding():
    # k3 review finding, round 6 (documented ACCEPTED RESIDUAL, DANGEROUS direction):
    # a model that uses the null marker's own literal text inside a negated/quoted
    # context to describe a REAL problem reads as 'not_found' even though the prose
    # asserts a genuine refutation — this is NOT closed by the round-6 fix, and is
    # the one residual that costs the dangerous direction (a real problem silently
    # passing the ship gate) rather than the safe one. Pinned here so a future
    # attempt to close it has a concrete regression to verify against, and so this
    # limitation stays visible rather than silently forgotten.
    result = ReviewResult(
        model="m",
        command="c",
        returncode=0,
        stdout=(
            'I cannot honestly answer "NO REFUTATION FOUND": quorum.py:469 never '
            "sends the original diff to the adversarial pass."
        ),
        stderr="",
    )
    assert q_mod.refutation_verdict(result) == "not_found"


def test_refutation_verdict_inconclusive_on_prose_that_merely_starts_with_found():
    # k3 review finding, round 8: without a right-side word boundary after "FOUND",
    # ANY word merely BEGINNING with "found" ("FOUNDED", "FOUNDATION", "founders")
    # satisfied `_REFUTATION_PHRASE` too — so ordinary prose describing a clean
    # verdict in different words, with no real "NO" nearby, false-fired the
    # affirmative marker and flipped a genuinely clean run to `'found'`, exiting the
    # ship gate at 1. This is NOT one of the two documented residuals (those both
    # require some form of a preceding "no"); it is the same false-failure class
    # round 1 explicitly set out to kill, just via a different word than "REFUTATION"
    # itself.
    for body in (
        "The strongest candidate refutation founded on the checkpoint/stamp race "
        "fails: review.py:540 already holds the lock. I could not break the "
        "verdict.",
        "This is not a REFUTATION FOUNDATIONALLY different from the panel's own "
        "conclusion, so I did not overturn it.",
    ):
        result = ReviewResult(
            model="m", command="c", returncode=0, stdout=body, stderr=""
        )
        assert q_mod.refutation_verdict(result) == "inconclusive", body
        assert q_mod.refutation_succeeded(result) is False, body


def test_refutation_verdict_found_still_works_with_a_real_boundary_after_found():
    # Companion to the boundary fix above: a GENUINE affirmative marker, immediately
    # followed by ordinary punctuation/whitespace/EOL (never a letter), must still
    # match — the fix narrows false letter-glued matches, not real ones.
    for body in (
        "REFUTATION FOUND: the checkpoint races the stamp write.",
        "REFUTATION FOUND",
        "REFUTATION FOUND\nthe checkpoint races the stamp write.",
    ):
        result = ReviewResult(
            model="m", command="c", returncode=0, stdout=body, stderr=""
        )
        assert q_mod.refutation_verdict(result) == "found", body


def _run_quorum(
    *, adversarial_check: bool, mod_stdout: str, refutation_stdout: str | None
):
    """Drive mode_quorum with run_panel/run_moderator stubbed, mirroring
    tests/test_diff_cap.py's established harness for this mode. `refutation_stdout`
    (when not None) is returned by the SECOND run_moderator call (the adversarial
    pass); a test that expects the pass to be SKIPPED passes None and asserts the
    moderator was only invoked once."""
    calls: list[str] = []

    def _fake_run_panel(jobs, cwd, timeout):
        return [
            ReviewResult(
                model=j.model,
                command="fake",
                returncode=0,
                stdout="ok, agrees",
                stderr="",
            )
            for j in jobs
        ]

    def _fake_run_moderator(candidates, prompt, cwd, timeout, diff="", round_no=0):
        calls.append(prompt)
        if len(calls) == 1:
            return ReviewResult(
                model=candidates[0],
                command="fake",
                returncode=0,
                stdout=mod_stdout,
                stderr="",
            )
        return ReviewResult(
            model=candidates[0],
            command="fake",
            returncode=0,
            stdout=refutation_stdout or "",
            stderr="",
        )

    saved_panel = q_mod.run_panel
    saved_mod = q_mod.run_moderator
    q_mod.run_panel = _fake_run_panel
    q_mod.run_moderator = _fake_run_moderator
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = q_mod.mode_quorum(
                "should we ship this?",
                ["m1"],
                "",
                REPO_ROOT,
                5,
                ["mod"],
                adversarial_check=adversarial_check,
            )
    finally:
        q_mod.run_panel = saved_panel
        q_mod.run_moderator = saved_mod
    return rc, buf.getvalue(), calls


def test_mod_prompt_uses_the_same_heading_constants_the_parser_relies_on():
    # Opus review finding, round 3: `_DISAGREEMENT_SECTION_RE` used to hard-code its
    # own copy of the heading wording with no link to the ACTUAL text sent to the
    # moderator (`mod_prompt`) — every other test here validates the regex only
    # against a hand-rolled fixture (`_clean_summary`) built to satisfy it, never
    # against the real prompt, so a future reword of `mod_prompt`'s headings could
    # silently make `quorum_verdict_is_clean` return False on every real run (the
    # whole --adversarial-check pass going permanently dark) with every test still
    # green. Capturing the REAL prompt `mode_quorum` sends (not a fixture) and
    # asserting the shared constants are IN it proves the coupling is now
    # structural (one constant used by both), not a coincidence two hard-coded
    # copies happened to agree on.
    _, _, calls = _run_quorum(
        adversarial_check=False, mod_stdout=_clean_summary(), refutation_stdout=None
    )
    assert len(calls) == 1, calls
    sent_prompt = calls[0]
    assert q_mod._DISAGREEMENT_HEADING in sent_prompt, sent_prompt
    assert q_mod._ABSTAINED_HEADING in sent_prompt, sent_prompt


def test_adversarial_prompt_uses_the_same_marker_constants_the_parser_relies_on():
    # Opus review finding, round 9: the same anti-drift gap the test above closes for
    # the DISAGREEMENT/ABSTAINED headings existed for the refutation MARKER text too
    # — `REFUTATION_NOT_FOUND_MARKER` built the null-marker instruction, but the
    # AFFIRMATIVE instruction was a bare inline literal `'REFUTATION FOUND:'` with no
    # link to the regexes that parse it, and every test here validated the regexes
    # only against hand-rolled fixtures using the SAME hardcoded words — a reword of
    # either literal in `_adversarial_refutation_prompt` would silently make
    # `refutation_verdict` misclassify every real answer, with the whole suite still
    # green. Capturing the REAL prompt sent to the adversarial pass (not a fixture)
    # and asserting the shared constants are IN it proves the coupling is structural.
    rc, out, calls = _run_quorum(
        adversarial_check=True,
        mod_stdout=_clean_summary(),
        refutation_stdout=f"{q_mod.REFUTATION_NOT_FOUND_MARKER}. Checked: X.",
    )
    assert rc == 0, out
    assert len(calls) == 2, calls
    adversarial_prompt = calls[1]
    assert q_mod.REFUTATION_NOT_FOUND_MARKER in adversarial_prompt, adversarial_prompt
    assert q_mod.AFFIRMATIVE_REFUTATION_MARKER in adversarial_prompt, adversarial_prompt


def test_adversarial_check_off_never_spawns_the_refutation_pass():
    rc, out, calls = _run_quorum(
        adversarial_check=False, mod_stdout=_clean_summary(), refutation_stdout=None
    )
    assert rc == 0, out
    assert len(calls) == 1, calls  # only the ordinary moderator call
    assert "Adversarial check" not in out, out


def test_adversarial_check_skipped_when_verdict_is_not_clean():
    dirty_summary = _clean_summary("Expert A and Expert B disagree on the retry logic.")
    rc, out, calls = _run_quorum(
        adversarial_check=True, mod_stdout=dirty_summary, refutation_stdout=None
    )
    assert rc == 0, (
        out
    )  # experts + moderator both usable -> ok, no refutation attempted
    assert len(calls) == 1, calls  # the refutation pass must NOT have run
    # k3 review finding, round 1: a SKIP must never be silent — the ship-gate-critical
    # caller who asked for --adversarial-check needs to see WHY it didn't run.
    assert "SKIPPED" in out, out
    assert "Adversarial check" in out, out  # the SKIPPED note itself names the section
    # k3 review finding, round 8: the message is HEDGED, not asserted — a non-empty
    # section 2 could equally be ordinary clean phrasing `_TRIVIAL_DISAGREEMENT_RE`
    # doesn't yet recognize, not necessarily real disagreement.
    assert "did not reduce to a recognized trivial" in out, out
    assert "could not be located" not in out, out  # distinguishes it from unparseable


def test_adversarial_check_skip_reason_distinguishes_unparseable_synthesis():
    # Opus review finding, round 6: `quorum_verdict_is_clean` deliberately collapses
    # "real disagreement" and "could not parse the section at all" into the same
    # False (see its own docstring), but a human reading the transcript to judge
    # whether the skip itself is a problem needs to tell those two cases apart — an
    # unparseable synthesis means the panel's own verdict was never legible to begin
    # with, a different (and arguably more concerning) situation than "the panel
    # looked at it and disagreed."
    unparseable_summary = "1. QUORUM\nEverything agrees.\n\n(no further sections)"
    rc, out, calls = _run_quorum(
        adversarial_check=True, mod_stdout=unparseable_summary, refutation_stdout=None
    )
    assert rc == 0, out
    assert len(calls) == 1, calls  # the refutation pass must NOT have run
    assert "SKIPPED" in out, out
    assert "could not be located" in out or "unparseable" in out, out
    assert "did not reduce to a recognized trivial" not in out, out


def test_adversarial_check_on_clean_verdict_with_no_refutation_stays_ok():
    rc, out, calls = _run_quorum(
        adversarial_check=True,
        mod_stdout=_clean_summary(),
        refutation_stdout=f"{q_mod.REFUTATION_NOT_FOUND_MARKER}. Checked: X, Y, Z.",
    )
    assert rc == 0, out
    assert len(calls) == 2, calls  # moderator + the adversarial pass both ran
    assert "Adversarial check" in out, out
    assert "INCONCLUSIVE" not in out, out
    assert q_mod.REFUTATION_NOT_FOUND_MARKER in out, out


def test_quorum_render_never_applies_the_evidence_warning_even_with_bait_text():
    # Opus review finding, round 11 (missing-test gap): every OTHER test proves
    # `check_evidence` defaults to False for quorum's `format_result` calls by
    # inspecting the DEFAULT in isolation — none of them drive the real `mode_quorum`
    # render path with a moderator summary that would ACTUALLY trip
    # `_CLEAN_VERDICT_RE` if `check_evidence=True` were ever accidentally added to
    # quorum's own `format_result(mod_result)` call (mirroring the end-to-end gap the
    # round-2 `review.py` tests closed for the diff-review render paths). Bait the
    # moderator's summary with a literal "Looks good overall." — a real
    # `--adversarial-check` run that appended `check_evidence=True` here would warn;
    # this must not.
    baited_summary = _clean_summary() + " Looks good overall."
    rc, out, calls = _run_quorum(
        adversarial_check=False, mod_stdout=baited_summary, refutation_stdout=None
    )
    assert rc == 0, out
    assert "Looks good overall." in out, out  # confirms the bait text really rendered
    assert "WARNING" not in out, out


def test_adversarial_check_on_clean_verdict_with_inconclusive_refutation_surfaces_it():
    # k3/Opus review finding, round 1: an answer that matches NEITHER marker must be
    # surfaced distinctly (not silently folded into "must be a clean pass"), and must
    # NOT flip the exit code either (an inconclusive extra check is not evidence).
    rc, out, calls = _run_quorum(
        adversarial_check=True,
        mod_stdout=_clean_summary(),
        refutation_stdout="The panel's reasoning seems sound to me.",
    )
    assert rc == 0, out
    assert len(calls) == 2, calls
    assert "INCONCLUSIVE" in out, out
    assert "Adversarial check" in out, out


def test_adversarial_check_inconclusive_header_distinguishes_a_failed_extra_call():
    # Opus review finding, round 9: `refutation_verdict` returns `'inconclusive'` for
    # BOTH an unusable result (backend error / empty output) and a usable answer that
    # simply matches neither marker — the original header wording always assumed the
    # second case ("the answer matched neither... marker"), which is misleading when
    # the extra call itself never produced a real answer to weigh at all.
    rc, out, calls = _run_quorum(
        adversarial_check=True,
        mod_stdout=_clean_summary(),
        refutation_stdout="",  # empty stdout -> result_is_usable is False
    )
    assert rc == 0, out  # a failed EXTRA check must never fail an already-clean run
    assert len(calls) == 2, calls
    assert "INCONCLUSIVE" in out, out
    assert "the extra call itself was not usable" in out, out
    assert "matched neither the affirmative" not in out, out


def test_adversarial_check_surfaces_a_successful_refutation_and_flips_exit_code():
    rc, out, calls = _run_quorum(
        adversarial_check=True,
        mod_stdout=_clean_summary(),
        refutation_stdout="REFUTATION FOUND: the retry loop double-charges on a 5xx retry.",
    )
    assert rc == 1, out  # the run must NOT silently discard a found refutation
    assert len(calls) == 2, calls
    assert "REFUTATION FOUND" in out, out
    assert "Adversarial check" in out, out


# --- End-to-end: `check_evidence=(prompt == DEFAULT_PROMPT)` actually reaches
# `mode_review`'s two render call sites (Opus review finding, round 2; conditioned
# on the real prompt in round 6, k3 finding) ------------------------------------------
#
# Every OTHER test above exercises `format_result`/`clean_verdict_missing_evidence` in
# ISOLATION and pins that the default is `check_evidence=False`. None of them drove
# `mode_review` itself and inspected its captured stdout — so deleting `check_evidence
# =True` from either of review.py's two call sites (the fix for audit finding #2)
# would silently revert that fix while every existing test stayed green. These tests
# close that gap by running the real flat and board dispatch paths (with only the
# backend/board-runner call stubbed): two assert the WARNING shows up when the real
# DEFAULT_PROMPT is in effect, one asserts it does NOT show up under a caller-supplied
# `--prompt` override that never asked for the "checked: ..." phrasing.


def test_flat_diff_review_render_opts_into_the_evidence_warning():
    def _fake_seat(model, dispatch):
        return ReviewResult(
            model=model,
            command="fake",
            returncode=0,
            stdout="Looks good, no issues found.",
            stderr="",
        )

    saved = review_mod._flat_seat_with_provider_failover
    review_mod._flat_seat_with_provider_failover = _fake_seat
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = review_mod.mode_review(
                ["m1"], DEFAULT_PROMPT, "+x", REPO_ROOT, 5, False, board=None
            )
    finally:
        review_mod._flat_seat_with_provider_failover = saved
    assert rc == 0, buf.getvalue()
    assert "WARNING" in buf.getvalue(), buf.getvalue()


def test_flat_diff_review_render_skips_the_warning_under_a_custom_prompt():
    # k3 review finding, round 6: `review diff` documents `--prompt` as an override
    # of the whole base instruction, but both render paths used to pass
    # `check_evidence=True` UNCONDITIONALLY — so a caller who replaced DEFAULT_PROMPT
    # entirely still got the "checked: ..." evidence warning enforced against a
    # prompt that never asked for that phrasing.
    #
    # Bait text deliberately still IN `_CLEAN_VERDICT_RE`'s word list (Opus review
    # finding, round 9): the original bait was "APPROVED", but round 8 dropped
    # "approved" from the word list entirely (a DIFFERENT, unrelated fix) — after
    # that, "APPROVED" can no longer trip the WARNING under ANY setting, so this
    # test's `"WARNING" not in out` assertion passed regardless of whether
    # `check_evidence` was actually gated correctly. A regression that reverted to
    # unconditional `check_evidence=True` for custom prompts would still pass this
    # test. "Looks good, no issues found." stays in the word list, so it actually
    # exercises the `prompt.startswith(DEFAULT_PROMPT)` gate this test is about.
    def _fake_seat(model, dispatch):
        return ReviewResult(
            model=model,
            command="fake",
            returncode=0,
            stdout="Looks good, no issues found.",
            stderr="",
        )

    saved = review_mod._flat_seat_with_provider_failover
    review_mod._flat_seat_with_provider_failover = _fake_seat
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = review_mod.mode_review(
                ["m1"],
                "Answer APPROVED if safe; otherwise list blockers.",
                "+x",
                REPO_ROOT,
                5,
                False,
                board=None,
            )
    finally:
        review_mod._flat_seat_with_provider_failover = saved
    assert rc == 0, buf.getvalue()
    assert "WARNING" not in buf.getvalue(), buf.getvalue()


def test_flat_diff_review_render_still_warns_when_visual_appends_to_default_prompt():
    # k3 review finding, round 7: `--visual` composes with whatever prompt is in
    # effect by APPENDING the image's context note (`cli.py`'s `_with_visual`:
    # `text + visual_ctx.context_note`) — it never replaces it. Round 6's first cut
    # gated `check_evidence` on an EXACT `prompt == DEFAULT_PROMPT` check, which broke
    # on this composable path: `review diff --visual shot.png` with no `--prompt`
    # dispatches `DEFAULT_PROMPT + context_note`, a DIFFERENT string from
    # `DEFAULT_PROMPT`, even though the model still received DEFAULT_PROMPT's full
    # "checked: ..." contract verbatim — so the exact check silently disabled the
    # WARNING on every visual diff review. `.startswith(DEFAULT_PROMPT)` recognizes
    # this composed prompt as still carrying the unmodified base contract.
    #
    # Drives the REAL `cli._with_visual` (Opus review finding, round 10), not a
    # hand-rolled string that merely LOOKS like what it produces: an earlier version
    # of this test built `composed_prompt` by string concatenation, which proves
    # nothing about the ACTUAL composition function — if `_with_visual` ever changed
    # to prepend or wrap the note instead of appending it, this test would keep
    # passing (its fixture appends by construction) while every real `review diff
    # --visual` run silently lost the WARNING. Only `visual_ctx.context_note` is
    # read by `_with_visual`, so a minimal stand-in with just that one attribute is
    # enough to call the real function — the point is exercising the function's
    # OWN logic, not constructing a full `VisualContext`.
    def _fake_seat(model, dispatch):
        return ReviewResult(
            model=model,
            command="fake",
            returncode=0,
            stdout="Looks good, no issues found.",
            stderr="",
        )

    saved = review_mod._flat_seat_with_provider_failover
    review_mod._flat_seat_with_provider_failover = _fake_seat
    visual_ctx = SimpleNamespace(
        context_note="\n\n[visual context: a screenshot of the UI]"
    )
    composed_prompt = _with_visual(DEFAULT_PROMPT, visual_ctx)
    assert composed_prompt != DEFAULT_PROMPT  # confirms the note really composed in
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = review_mod.mode_review(
                ["m1"], composed_prompt, "+x", REPO_ROOT, 5, False, board=None
            )
    finally:
        review_mod._flat_seat_with_provider_failover = saved
    assert rc == 0, buf.getvalue()
    assert "WARNING" in buf.getvalue(), buf.getvalue()


def test_board_diff_review_render_opts_into_the_evidence_warning():
    result = ReviewResult(
        model="m1",
        command="fake",
        returncode=0,
        stdout="Looks good, no issues found.",
        stderr="",
    )
    outcome = FailoverOutcome(
        results=[result],
        usable=[result],
        target=1,
        degraded=False,
        usable_models=["m1"],
    )

    def _fake_run_board_with_failover(
        pool, reserve, prompt, diff, cwd, timeout, images
    ):
        return outcome

    saved = review_mod.run_board_with_failover
    review_mod.run_board_with_failover = _fake_run_board_with_failover
    board = [BoardReviewer("m1", "correctness", "M1")]
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = review_mod.mode_review(
                [],
                DEFAULT_PROMPT,
                "+x",
                REPO_ROOT,
                5,
                False,
                board=board,
                pool_size=1,
                exact_board=True,
            )
    finally:
        review_mod.run_board_with_failover = saved
    assert rc == 0, buf.getvalue()
    assert "WARNING" in buf.getvalue(), buf.getvalue()


def test_board_diff_review_render_skips_the_warning_under_a_custom_prompt():
    # Opus review finding, round 9 (missing-test gap): the flat path has a
    # negative-prompt test proving `check_evidence` is correctly gated OFF under a
    # custom `--prompt`, but the board path's IDENTICAL gating (the same
    # `prompt.startswith(DEFAULT_PROMPT)` line, just at the board's own render call
    # site) had no equivalent test — a regression reverting the board path alone to
    # unconditional `check_evidence=True` would have gone unnoticed.
    result = ReviewResult(
        model="m1",
        command="fake",
        returncode=0,
        stdout="Looks good, no issues found.",
        stderr="",
    )
    outcome = FailoverOutcome(
        results=[result],
        usable=[result],
        target=1,
        degraded=False,
        usable_models=["m1"],
    )

    def _fake_run_board_with_failover(
        pool, reserve, prompt, diff, cwd, timeout, images
    ):
        return outcome

    saved = review_mod.run_board_with_failover
    review_mod.run_board_with_failover = _fake_run_board_with_failover
    board = [BoardReviewer("m1", "correctness", "M1")]
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = review_mod.mode_review(
                [],
                "Answer APPROVED if safe; otherwise list blockers.",
                "+x",
                REPO_ROOT,
                5,
                False,
                board=board,
                pool_size=1,
                exact_board=True,
            )
    finally:
        review_mod.run_board_with_failover = saved
    assert rc == 0, buf.getvalue()
    assert "WARNING" not in buf.getvalue(), buf.getvalue()


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
