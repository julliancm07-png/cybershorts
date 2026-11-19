#!/usr/bin/env python3
"""One-time TikTok OAuth: open URL, paste the redirected URL (or bare code)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import logging

import secrets
import urllib.parse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def parse_code(text: str) -> str:
    text = text.strip()
    if "code=" in text:
        query = urllib.parse.urlparse(text).query if "://" in text or "?" in text else text
        vals = urllib.parse.parse_qs(query).get("code")
        if vals:
            return urllib.parse.unquote(vals[0])
    return urllib.parse.unquote(text)


def main() -> int:
    from cybershorts import tiktok
    from cybershorts.config import load

    cfg = load()
    if not (cfg.tiktok_client_key and cfg.tiktok_client_secret and cfg.tiktok_redirect_uri):
        print("Set TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET and TIKTOK_REDIRECT_URI in .env first.")
        return 1
    state = secrets.token_urlsafe(16)
    print("\nOpen this URL in a browser and approve:\n\n%s\n" % tiktok.auth_url(cfg, state))
    pasted = input("Paste the full redirected URL (or just the code): ")
    code = parse_code(pasted)
    if not code:
        print("No code found.")
        return 1
    returned = urllib.parse.parse_qs(urllib.parse.urlparse(pasted.strip()).query).get("state")
    if returned and returned[0] != state:
        print("State mismatch; aborting.")
        return 1
    tok = tiktok.exchange_code(cfg, code)
    print("Saved %s (open_id %s)" % (tiktok.token_path(cfg), tok.get("open_id", "?")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
