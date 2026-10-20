"""Allow ``python -m apiparity``."""

from __future__ import annotations

from apiparity.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
