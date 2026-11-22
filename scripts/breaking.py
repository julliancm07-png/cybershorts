#!/usr/bin/env python3
"""Timer entry point: make at most one breaking-news draft (new CISA KEV entry or urgent RSS item)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("breaking")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="show what would be drafted; no draft, no Telegram")
    args = ap.parse_args()

    from cybershorts import breaking
    from cybershorts.config import load

    cfg = load()
    try:
        breaking.run(cfg, dry_run=args.dry_run)
    except Exception:  # noqa: BLE001
        log.exception("breaking failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
