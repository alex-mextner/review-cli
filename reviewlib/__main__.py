"""`python -m reviewlib` entry — same dispatch as the `review` console script."""
from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
