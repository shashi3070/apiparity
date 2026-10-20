from __future__ import annotations

import sys
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures"

if str(FIXTURES_DIR) not in sys.path:
    sys.path.insert(0, str(FIXTURES_DIR))
