"""visual: standalone screenshot verification, optionally grounded by a diff.

`review visual <image>` is the canonical standalone visual surface. With `--diff` or
`--staged`, the CLI threads the screenshot into the normal diff-review companion path.
The heavy routing lives in `reviewlib.cli`; this descriptor exists so the parser/help and
mode registry have the same self-describing shape as the other modes.
"""
from __future__ import annotations

import argparse

from ..config import DEFAULT_PROMPT
from .contract import ModeContext, ModeSpec
from .review import _handler as _review_handler


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("visual", nargs="?", metavar="IMAGE", help="image to verify")
    parser.set_defaults(prompt=DEFAULT_PROMPT)


def _handler(ctx: ModeContext) -> int:
    return _review_handler(ctx)


MODE = ModeSpec(
    name="visual",
    subcommand="visual",
    diff_policy="optional",
    stats_mode="visual",
    summary="Visual verification for a screenshot; add --diff to include git diff context.",
    handler=_handler,
    add_arguments=_add_arguments,
)
