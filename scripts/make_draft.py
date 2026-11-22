#!/usr/bin/env python3
"""Timer entry point: build one draft and send it to Telegram for approval."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("make_draft")


def main() -> int:
    from cybershorts import pipeline
    from cybershorts.config import load

    cfg = load()
    try:
        with pipeline.draft_lock(cfg, blocking=True):  # one draft job at a time (shared with breaking/bot)
            pipeline.make_draft(cfg)
    except Exception:  # noqa: BLE001
        log.exception("make_draft failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
