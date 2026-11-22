#!/usr/bin/env python3
"""Back up secrets, logins, settings and the database (run weekly by cybershorts-backup.timer)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("backup")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-telegram", action="store_true", help="keep the backup local only")
    args = ap.parse_args()

    from cybershorts import backup
    from cybershorts.config import load

    cfg = load()
    try:
        path = backup.run(cfg, telegram=not args.no_telegram)
    except Exception:  # noqa: BLE001
        log.exception("backup failed")
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
