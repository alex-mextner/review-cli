"""Load the resolved prose suite files into the single text block the tester prompt
embeds, and cap the run by ``--max-cases``.

WHY HERE. ``modes/qa.py`` owns DISCOVERY (which ``*.md`` files resolve, how many
``## Case:`` blocks each holds — the no-suites gate). This module owns READING those
already-resolved files into prompt text, headed by their filename so the agent (and a
human reading the report) can attribute a finding to its source suite. It also enforces
the cost cap: ``--max-cases`` trims the concatenated text to the first N cases so a run
never silently exercises more than the cap (the spec's mandatory cost control, §1/§9).

The case-boundary regex MUST stay identical to ``modes/qa.py``'s ``_CASE_HEADING_RE`` —
they count the same thing. It is imported from there rather than re-declared so the two
can never drift.
"""
from __future__ import annotations

from pathlib import Path

from ..modes.qa import _CASE_HEADING_RE


def load_suites_text(suite_files: list[Path], *, max_cases: int | None = None) -> str:
    """Concatenate the resolved suite files into one prompt block, each headed by its
    filename, optionally TRIMMED to the first ``max_cases`` cases across all files.

    ``max_cases=None`` means "no cap" (run every case). ``max_cases=1`` (the qa default)
    keeps only the first case of the first suite that has one — the cost-capped smoke
    shape. The trim is by CASE, not by file: it walks the concatenated text and stops
    after the Nth ``## Case:`` heading, so the cap is exact regardless of how cases are
    spread across files. An unreadable file contributes a visible ``(could not read …)``
    note rather than vanishing silently.
    """
    blocks = [_one_suite_block(path) for path in suite_files]
    full = "\n\n".join(blocks)
    if max_cases is None:
        return full
    return _truncate_to_cases(full, max_cases)


def _one_suite_block(path: Path) -> str:
    """One suite file rendered for the prompt: a ``=== <name> ===`` header + its body.

    A read failure is DISCLOSED in-band (not dropped) so the agent never silently runs a
    short set; discovery already counted this file as having >=1 case, so a read failure
    here is a surprising state worth surfacing in the transcript."""
    try:
        body = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        body = f"(could not read this suite: {exc})"
    return f"=== suite: {path.name} ===\n{body}"


def _truncate_to_cases(text: str, max_cases: int) -> str:
    """Keep only the prefix of ``text`` up to and including the first ``max_cases``
    ``## Case:`` blocks. A ``max_cases`` >= the total case count returns ``text``
    unchanged; ``max_cases <= 0`` is treated as "no usable cap" and returns ``text`` (the
    no-suites gate already guarantees >=1 case, so an empty result is never desirable)."""
    if max_cases <= 0:
        return text
    starts = [m.start() for m in _CASE_HEADING_RE.finditer(text)]
    if len(starts) <= max_cases:
        return text
    cut = starts[max_cases]
    trimmed = text[:cut].rstrip()
    return (
        f"{trimmed}\n\n"
        f"(NOTE: capped to the first {max_cases} case(s) by --max-cases; "
        f"{len(starts) - max_cases} further case(s) in the suites are NOT in scope this run.)"
    )
