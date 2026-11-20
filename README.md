# cybershorts

Automated IT and cybersecurity news Shorts. Each run picks a recent story from RSS feeds, writes a short script, narrates it with Piper, adds word-timed captions and b-roll, and sends the finished 1080x1920 video to Telegram for approval. Approved videos go to YouTube Shorts and to your TikTok inbox as drafts.

Flow: RSS -> OpenRouter script -> Piper TTS -> faster-whisper captions -> Pexels b-roll -> FFmpeg render -> Telegram approve -> YouTube + TikTok.

Runs on a small Debian server with Python 3.11+, systemd timers and no paid scheduler.

## Quick start

Debian LXC, as root. The LXC needs `features: nesting=1` (Proxmox: container Options > Features) or the systemd sandboxing options (`PrivateTmp`, `ProtectSystem`) fail.

```
apt install -y python3 python3-venv ffmpeg git
useradd --system --create-home --shell /usr/sbin/nologin cybershorts
# put the project in /opt/cybershorts, then:
chown -R cybershorts:cybershorts /opt/cybershorts
cd /opt/cybershorts
runuser -u cybershorts -- python3 -m venv .venv
runuser -u cybershorts -- .venv/bin/pip install -r requirements.txt
runuser -u cybershorts -- cp config.example.yaml config.yaml
runuser -u cybershorts -- cp .env.example .env
chmod 600 .env                      # then fill in the keys
cp systemd/cybershorts-*.service systemd/cybershorts-*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now cybershorts-bot.service cybershorts-draft.timer
```

Always run scripts as the service user, never as root. Root-owned 0600 token or db files make the service fail with permission errors:

```
runuser -u cybershorts -- /opt/cybershorts/.venv/bin/python scripts/doctor.py
runuser -u cybershorts -- /opt/cybershorts/.venv/bin/python scripts/auth_youtube.py
```

Google notes:
- The Google OAuth consent screen must be set to "In production". In "Testing" status the refresh token expires after 7 days.
- Videos uploaded through an unverified API project are forced to private until the project passes the YouTube API audit.

## Visual upgrades (v2)

- Karaoke captions: each word highlights as it is spoken (`captions.style`).
- Hook: a bold caption over the first seconds (`video.hook_seconds`).
- Branding: accent colors, a logo watermark, a progress bar and an end card (`branding`).
- Ken Burns motion on b-roll, capped at `broll.max_clips` clips (`broll.ken_burns`).
- Fact check: a second model pass checks the script against the article (`llm.fact_check`).

To add a watermark logo:

```
mkdir -p /opt/cybershorts/data/brand
cp logo.png /opt/cybershorts/data/brand/logo.png     # square PNG
chown -R cybershorts:cybershorts /opt/cybershorts/data/brand
```

Every feature can be switched off in `config.yaml`: `captions.style: plain`, `video.hook_seconds: 0`, `branding.progress_bar: false`, `branding.end_card: false`, `llm.fact_check: false`.

## Commands

```
python scripts/make_draft.py      # create one draft now (also run by the timer)
python scripts/bot.py             # Telegram review bot (long-running, run by systemd)
python scripts/doctor.py          # check config, FFmpeg, fonts, tokens and API keys
python scripts/test_render.py     # offline render smoke test (no network)
python -m unittest discover -s tests -v   # unit tests (stdlib only)
```

Secrets live in `.env`. Settings live in `config.yaml` (copy from `config.example.yaml`).
