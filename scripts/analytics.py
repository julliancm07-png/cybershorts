#!/usr/bin/env python3
"""Weekly timer entry point: pull YouTube stats, refresh learnings, send the report."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("analytics")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-telegram", action="store_true", help="do not send Telegram messages")
    args = ap.parse_args()

    from cybershorts import analytics
    from cybershorts.config import load

    cfg = load()
    try:
        result = analytics.run(cfg, telegram=not args.no_telegram)
    except Exception:  # noqa: BLE001
        log.exception("analytics failed")
        return 1
    log.info("analytics result: %s", result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
