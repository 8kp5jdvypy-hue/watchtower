#!/usr/bin/env python3
"""Regenerate the public status page (data/status.html by default).

Reads only from the journal, tradebot.incidents, and tradebot.metrics —
every number on the page is reproducible on demand, nothing is cached
or hand-maintained. Run this on a schedule (cron) or by hand after an
incident resolves; it does not serve the file, only writes it — see
tradebot.status_page's module docstring for why.

Usage:
    python3 scripts/generate_status_page.py
    python3 scripts/generate_status_page.py --output /path/to/status.html
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tradebot.journal import connect
from tradebot.status_page import DEFAULT_OUTPUT_PATH, generate_status_page


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH), help="path to write the HTML file to")
    args = parser.parse_args()

    conn = connect()
    output_path = generate_status_page(conn, output_path=Path(args.output))
    print(f"wrote {output_path}")


if __name__ == "__main__":
    main()
