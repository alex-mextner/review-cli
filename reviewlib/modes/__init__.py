"""Review modes: plain diff review, --just-ask, --quorum, --brainstorm.

Originally extracted verbatim from the single-file `bin/review` (Stage 0
decomposition — zero behaviour change at the time); has since grown shared
cross-mode helpers, e.g. `PERSONAS` (the persona/lens pool `brainstorm` and
`quorum` both assign to seats).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import EffortOverride


def _diff_context_block(diff: str) -> str:
    if not diff.strip():
        return ""
    return f"\n\nAdditional context — a git diff:\n\n```diff\n{diff}\n```"


# Distinct expert personas shared by every mode that assigns a per-seat role/lens
# (brainstorm's rotating panel, quorum's expert board). Moved here from brainstorm.py
# — NOT a pure move (Fable review finding, round 3): the third entry was also
# RENAMED from "DX / ergonomics designer" to "Developer-experience designer" in the
# same change (see the path-legibility note below), so any brainstorm log/transcript
# that pinned the old name changes too — so quorum can reuse the SAME role set
# rather than inventing a parallel one (Alex, 2026-08-18: "под разными полями" —
# panels reusing a model across seats must give each seat a genuinely distinct
# field/role, not just a numeric disclosure label).
#
# CROSS-MODE CONTRACT (Fable/k3 review finding): two callers with different
# constraints share this ONE tuple, so editing it edits both:
#   * brainstorm (`mode_brainstorm`): needs a pool >= 5; each round fills
#     `max(3, len(panel))` slots via `panel[slot % len(panel)]`, rotating a
#     `persona_index` across rounds. Trimming this tuple below ~5 entries starves
#     that rotation.
#   * quorum (`_seat_assignments`): keys persona choice on PER-MODEL occurrence,
#     offset by that model's own first seat index, and relies on `len(PERSONAS)`
#     being large enough that no realistic panel repeats one model more than
#     `len(PERSONAS)` times (see that function's docstring for the exact
#     collision this guards against). Reordering the tuple reshuffles WHICH
#     lens every quorum seat gets (its own regression tests pin exact values),
#     though it does not break the no-repeat-per-model guarantee.
# Both invariants (pool size, name shape below) are asserted in
# tests/test_reuse_warnings.py, not just documented here (Fable review finding,
# round 3 — a prose-only contract goes stale the moment someone edits the tuple).
#
# Persona names avoid `/` for LEGIBILITY, not because it's a real path hazard
# (k3 review finding, round 3, correcting an earlier — WRONG — comment here that
# claimed a `/` would "become a bogus path component"; traced and confirmed
# false: per-call log filenames key off the bare `job.model` via `_safe_backend`,
# never the label, and even where the label DOES reach `write_retry_log` via
# `run_seat_with_retry`, `_safe_backend` maps `/` to `_` safely — brainstorm
# shipped "DX / ergonomics designer" through these exact paths for months with
# no breakage). The real reason: quorum's label format is `<model> [<persona>]`
# and tests/`mode_quorum` extract the persona via `label.rsplit("[", 1)` — a
# `/` inside the persona name is harmless there too, but keeping names free of
# punctuation that LOOKS path-like keeps transcripts and labels easy to read.
PERSONAS = (
    (
        "Pragmatic staff engineer",
        "20 years shipping production systems; values simplicity, "
        "incremental delivery, and proven tech over novelty.",
    ),
    (
        "Security-paranoid reviewer",
        "thinks adversarially about every input, trust boundary, secret, "
        "and failure mode; assumes the worst actor and the worst case.",
    ),
    (
        "Developer-experience designer",
        "obsessed with developer and user experience: clear APIs, good "
        "defaults, discoverability, error messages, and minimal friction.",
    ),
    (
        "Skeptical SRE",
        "cares about operability, observability, blast radius, rollback, "
        "and what breaks at 3am; distrusts anything without a failure plan.",
    ),
    (
        "Product-minded architect",
        "connects technical choices to user value and roadmap; weighs "
        "long-term flexibility against time-to-market.",
    ),
    (
        "Cost-conscious performance engineer",
        "watches latency, throughput, token/compute spend, and resource "
        "footprint; allergic to waste and premature scale.",
    ),
)


def _run_effort(effort_override: "EffortOverride | None", model: str) -> str | None:
    """The run-scoped `--effort` level for a flat-panel seat (quorum / just-ask /
    brainstorm), or None. These modes build PanelJobs from a bare model list with no
    per-seat config effort, so the run-scoped override (a duck-typed
    `config.EffortOverride`) is the only source; `effort_for` yields None when nothing is
    overridden, leaving the job at its default."""
    return effort_override.effort_for(model) if effort_override is not None else None


def _visual_images(ctx) -> tuple[Path, ...]:
    # Suppress raw image attachments when --no-ai is set: vision fan-out is disabled
    # and panels must not receive image bytes even if a --visual path was given. (P1 fix.)
    if getattr(getattr(ctx, "args", None), "no_ai", False):
        return ()
    image_path = getattr(getattr(ctx, "visual_ctx", None), "image_path", None)
    return (Path(image_path),) if image_path else ()
