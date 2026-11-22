#!/usr/bin/env python3
"""Generate the brand sound kit with ElevenLabs. Run --dry-run first to see the credit estimate."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("make_sound_kit")


def parse_use(values) -> dict:
    out = {}
    for v in values or []:
        name, sep, k = v.partition("=")
        if not sep or not name or not k.isdigit() or int(k) < 1:
            raise argparse.ArgumentTypeError(f"--use expects NAME=K (K >= 1), got {v!r}")
        out[name] = int(k)
    return out


def print_table(results) -> None:
    print(f"{'name':<10} {'kind':<6} {'seconds':>8} {'est. credits':>13}  status")
    for r in results:
        print(f"{r['name']:<10} {r['kind']:<6} {r.get('seconds', 0):>8g} {r['estimate']:>13,}  {r['status']}")
    total = sum(r["estimate"] for r in results if r["status"] in ("dry-run", "pending", "ok", "error"))
    print(f"{'total':<10} {'':<6} {'':>8} {total:>13,}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", help="comma-separated kit item names to (re)generate")
    ap.add_argument("--variants", type=int, default=1, help="variants per item (name.mp3, name.v2.mp3, ...)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and credit estimate; no API calls")
    ap.add_argument("--use", action="append", metavar="NAME=K", help="swap variant K into NAME.mp3")
    args = ap.parse_args(argv)
    try:
        use = parse_use(args.use)
    except argparse.ArgumentTypeError as e:
        ap.error(str(e))
    only = [n.strip() for n in args.only.split(",") if n.strip()] if args.only else None

    from cybershorts import soundkit
    from cybershorts.config import load

    cfg = load()
    tg = None
    if not args.dry_run and cfg.telegram_bot_token and cfg.telegram_chat_id:
        from cybershorts.telegram import Telegram

        tg = Telegram(cfg.telegram_bot_token, cfg.telegram_chat_id)
    try:
        results = soundkit.generate(cfg, only=only, variants=args.variants, dry_run=args.dry_run,
                                    use=use or None, tg=tg)
    except soundkit.KitFatal as e:
        print(f"Stopped: {e}", file=sys.stderr)
        return 1
    print_table(results)
    if args.dry_run:
        print("Dry run: nothing was generated.")
        return 0
    ok = sum(1 for r in results if r["status"] in ("ok", "swapped"))
    bad = [r for r in results if r["status"] == "error"]
    used = sum(r["credits_used"] or 0 for r in results)
    print(f"\nDone: {ok} ok, {len(bad)} failed, {sum(r['status'] == 'skipped' for r in results)} skipped"
          f"; credits used (measured): {used:,}")
    for r in bad:
        print(f"  failed: {r['name']}: {r['error']}")
    print(soundkit.MUSIC_TERMS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
