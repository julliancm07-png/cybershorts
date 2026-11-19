#!/usr/bin/env python3
"""One-time YouTube OAuth. Writes data/youtube_token.json."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import logging

import os
import socket

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

PORT = 8765


def _ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "<container-ip>"


def main() -> int:
    from google_auth_oauthlib.flow import InstalledAppFlow

    from cybershorts.config import load
    from cybershorts.youtube import SCOPES, token_path

    cfg = load()
    secret = cfg.path("client_secret.json")
    if not secret.exists():
        print("Missing %s. Download the OAuth 'Desktop app' client JSON from Google Cloud Console." % secret)
        return 1
    msg = (
        "\n=== YouTube authorization ===\n"
        "This server has no browser. On your PC, open a terminal and run:\n\n"
        "    ssh -L %d:%s:%d root@<proxmox-host>\n\n"
        "Leave it open, then open the URL printed below in your PC's browser,\n"
        "sign in, and approve. The redirect to localhost:%d goes through the tunnel.\n\n"
        "Open this URL: {url}\n"
    ) % (PORT, _ip(), PORT, PORT)
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    creds = flow.run_local_server(
        host="localhost",
        bind_addr="0.0.0.0",
        port=PORT,
        open_browser=False,
        prompt="consent",
        access_type="offline",
        authorization_prompt_message=msg,
    )
    out = token_path(cfg)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(creds.to_json())
    os.chmod(out, 0o600)
    print("Saved %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
