#!/usr/bin/env python3
"""One-time YouTube OAuth. Writes data/youtube_token.json."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
import logging
import os
import socket

# Google returns only the scopes the user ticked; without this oauthlib raises on a partial grant.
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

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
    from cybershorts.youtube import (SCOPE_ANALYTICS, SCOPE_FORCE_SSL, SCOPE_UPLOAD, SCOPES,
                                     token_path)

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
    granted = sorted(getattr(creds, "granted_scopes", None) or creds.scopes or [])
    data = json.loads(creds.to_json())
    data["scopes"] = granted  # to_json writes the requested scopes; record what was granted
    out = token_path(cfg)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data))
    os.chmod(out, 0o600)
    print("Saved %s" % out)
    ssl = SCOPE_FORCE_SSL in granted or "https://www.googleapis.com/auth/youtube" in granted
    print("Uploads:          %s" % ("enabled" if SCOPE_UPLOAD in granted else "MISSING (tick the upload box and re-run)"))
    print("First comments:   %s" % ("enabled" if ssl else "disabled (youtube.force-ssl not granted)"))
    print("Analytics:        %s" % ("enabled" if ssl and SCOPE_ANALYTICS in granted else
                                    "limited/disabled (needs force-ssl and yt-analytics.readonly)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
