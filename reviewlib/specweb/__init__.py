"""`review spec-web <spec.md>` — an interactive web server for reviewing a spec.

A reusable subcommand that renders ANY markdown spec server-side and lets a reviewer
SELECT text -> ASK a question / leave a comment anchored to the selection, accumulate
those into a GitHub-review-style PENDING batch, then "Submit review" to finalize. Inline
answers thread under each comment. Comments persist to a JSON store and survive restarts.

Modules:
  * ``render`` — markdown -> HTML server-side, GitHub-slug heading ids (so the spec's own
                 ``[§9.4](#94-…)`` internal links resolve), assets served as real HTTP
                 resources (``/asset/<name>``) instead of inlined.
  * ``store``  — comment / reply / review-batch persistence (per-spec JSON, 0600).
  * ``server`` — stdlib ``ThreadingHTTPServer`` + routes; reads open, writes origin-guarded
                 (loopback + the configured Tailscale host).

Wired into the CLI as ``review spec-web <path>`` (see ``reviewlib.cli``).
"""
from __future__ import annotations

__all__ = ["render", "store", "server"]
