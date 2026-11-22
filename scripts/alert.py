#!/usr/bin/env python3
"""Run by cybershorts-alert@<fail|ok>:<unit>.service. Use --test to send a test alert."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("instance", nargs="?", help="fail:<unit> or ok:<unit>")
    ap.add_argument("--test", action="store_true", help="send a test alert to Telegram")
    args = ap.parse_args()

    from cybershorts import alerts
    from cybershorts.config import load

    cfg = load()
    if args.test:
        ok = alerts.send_test(cfg)
        print("Test alert sent." if ok else "Could not send the test alert (see the error above).")
        return 0 if ok else 1
    if not args.instance:
        ap.error("instance is required (or use --test)")
    mode, unit = alerts.parse_instance(args.instance)
    return 0 if alerts.handle(cfg, mode, unit) else 1


if __name__ == "__main__":
    sys.exit(main())
