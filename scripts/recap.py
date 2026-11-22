#!/usr/bin/env python3
"""Weekly recap entry point (timer, or /recap via the bot with --on-demand)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import logging
import signal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("recap")


def _on_sigterm(signum, frame):  # noqa: ARG001
    """Turn SIGTERM (bot restart, systemd stop) into SystemExit so recap.run can mark its row failed."""
    raise SystemExit(128 + signum)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="make a recap even if one exists for this week")
    ap.add_argument("--on-demand", action="store_true", help="started from Telegram: don't wait for the lock")
    ap.add_argument("--week", type=int, default=None, help="week number to show (overrides the automatic count)")
    args = ap.parse_args(argv)

    from cybershorts import pipeline, recap
    from cybershorts.config import load

    signal.signal(signal.SIGTERM, _on_sigterm)

    cfg = load()
    try:
        if args.on_demand:
            lock = pipeline.draft_lock(cfg, blocking=False)
        else:
            lock = pipeline.draft_lock(cfg, blocking=True, timeout=float(cfg.get("recap.lock_timeout", 1800)))
        with lock:
            recap.run(cfg, force=args.force, on_demand=args.on_demand, week_number=args.week)
    except pipeline.Busy:
        if args.on_demand:
            pipeline._notify(cfg, "Another draft job is running; try /recap later.")
            return 0
        log.error("Weekly recap: another draft job held the lock too long; giving up")
        return 1
    except Exception:  # noqa: BLE001
        log.exception("recap failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
