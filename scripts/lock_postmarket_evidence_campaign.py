#!/usr/bin/env python3
"""Lock a prospective postmarket evidence campaign before coverage begins."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tradebot.postmarket_evidence_campaign import main


if __name__ == "__main__":
    raise SystemExit(main())
