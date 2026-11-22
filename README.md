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

## v3 features

Deploy steps after pulling v3 (no new Python dependencies):

1. `csgit pull`, then `systemctl restart cybershorts-bot`.
2. Google Cloud: enable the YouTube Analytics API. Add the `youtube.force-ssl` and `yt-analytics.readonly` scopes to the OAuth consent screen.
3. Re-run `scripts/auth_youtube.py` (over an SSH tunnel) and tick every box. Then run `scripts/doctor.py`.
4. Install the timers: copy the `cybershorts-breaking` and `cybershorts-analytics` units to `/etc/systemd/system/`, run `systemctl daemon-reload`, then `systemctl enable --now cybershorts-breaking.timer cybershorts-analytics.timer`. The first breaking run only seeds the KEV list and creates no draft.
5. Optional: set `voice.engine: elevenlabs` and `voice.elevenlabs.model: eleven_v3` for audio tags (both are needed); add `NVD_API_KEY` to `.env`; tune `breaking.quiet_hours`, `breaking.daily_cap` and `series` in `config.yaml`.
6. Pin each auto-posted first comment in the YouTube app. The API cannot pin comments.
7. Learnings start once at least 6 videos are 48 hours old. Analytics lags 2-3 days.

Note: with an old `config.yaml` (one that lacks the v3 keys), every v3 feature defaults to on: breaking drafts (once the timer is enabled), first comments, NVD lookups, and the hook-pick LLM call. To turn them off, set `breaking.enabled: false`, `youtube.first_comment: false`, `data_card.nvd: false` and `llm.hooks: 1` (1 also disables alternate hooks and the hook-pick call).

## Commands

```
python scripts/make_draft.py      # create one draft now (also run by the timer)
python scripts/bot.py             # Telegram review bot (long-running, run by systemd)
python scripts/breaking.py --dry-run      # show what a breaking run would do; no draft, no Telegram, no seen/used/urgency marks; still fetches feeds and calls the LLM
python scripts/analytics.py --no-telegram # pull stats and update learnings without the Telegram report
python scripts/doctor.py          # check config, FFmpeg, fonts, tokens and API keys
python scripts/test_render.py     # offline render smoke test (no network)
python -m unittest discover -s tests -v   # unit tests (stdlib only)
```

Secrets live in `.env`. Settings live in `config.yaml` (copy from `config.example.yaml`).

## Background music

Drop licensed instrumental tracks (mp3/m4a/wav) into `/opt/cybershorts/data/music/`
(e.g. YouTube Studio → Audio library → filter "Attribution not required").
Each draft picks one at random (never the same twice in a row), mixes it quietly under
the narration and ducks it while the voice talks. The music version goes to YouTube and the
Telegram preview; TikTok gets the clean version so you can add a TikTok sound in the app
(`music.on_tiktok: true` to send the music version there too). No tracks = no music.
