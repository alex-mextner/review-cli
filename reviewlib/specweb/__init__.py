"""`review spec-web <spec.md>` — an interactive web server for reviewing a spec.

A reusable subcommand that renders ANY markdown spec server-side and lets a reviewer
SELECT text -> leave a QUESTION (expects an answer from the spec author) or a REMARK
(feedback that doesn't), anchored to the selection, accumulate those into a GitHub-review-
style PENDING batch, then "Submit review" to finalize. Inline answers thread under each
note; a note can be edited in place. Single implicit reviewer (no author field in the UI).
Notes persist to a JSON store and survive restarts.

Modules:
  * ``render`` — markdown -> HTML server-side, GitHub-slug heading ids (so the spec's own
                 ``[§9.4](#94-…)`` internal links resolve), assets served as real HTTP
                 resources (``/asset/<name>``) instead of inlined.
  * ``store``  — note / reply / review-batch persistence (per-spec JSON, 0600). Each note
                 carries a ``kind`` (``question`` | ``remark``, default remark) and can be
                 edited via ``edit_comment``.
  * ``server`` — stdlib ``ThreadingHTTPServer`` + routes (incl. ``/api/comments/<id>/edit``);
                 reads open, writes origin-guarded (loopback + the configured Tailscale host).

Wired into the CLI as ``review spec-web <path>`` (see ``reviewlib.cli``).
"""
from __future__ import annotations

__all__ = ["render", "store", "server"]
