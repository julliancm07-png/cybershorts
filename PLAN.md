# cybershorts — PLAN

Automated IT/cyber news Shorts: RSS → OpenRouter script → Piper TTS → faster-whisper captions → Pexels b-roll → FFmpeg 1080x1920 → Telegram approve → YouTube (Data API) + TikTok (Content Posting API, **inbox/draft upload**). Runs in a Debian 13 LXC on Proxmox, Python 3.11+, systemd timers. Self-hosted, no paid scheduler.

## Constraints
- Python 3.11+ (target 3.13). Deps only: requests, feedparser, pyyaml, python-dotenv, piper-tts, faster-whisper, google-api-python-client, google-auth-oauthlib, google-auth-httplib2. No telegram libraries — raw Bot API via requests.
- All paths relative to project root `APP_DIR` (env, default = dir of package parent). Data in `data/` (db, tokens, videos, voices, models). Secrets only in `.env`; tokens written chmod 600.
- SQLite single file `data/cybershorts.db` (WAL off, tiny). Videos deleted `cleanup.keep_days` after posted/rejected.
- Every external call: timeout, clear exception message. Never log secrets.
- Telegram: only act on updates whose chat id == TELEGRAM_CHAT_ID (security).
- Voice mode `tts` (Piper) or `record` (user sends Telegram voice message; future use). Must be config switch.
- Platforms individually enable/disable in config.
- Facts come only from RSS title+summary; LLM told not to invent. Description always includes source URL.

## Layout
```
cybershorts/
  PLAN.md  README.md  requirements.txt  config.example.yaml  .env.example  .gitignore
  cybershorts/__init__.py
  cybershorts/config.py        # load .env + config.yaml -> Settings dataclass/dict; paths
  cybershorts/db.py            # sqlite schema + helpers
  cybershorts/news.py          # fetch feeds -> candidate stories (unseen, recent)
  cybershorts/writer.py        # OpenRouter: pick story, write script JSON
  cybershorts/tts.py           # Piper synth -> wav ; ensure voice downloaded
  cybershorts/captions.py      # faster-whisper word timestamps -> .ass file
  cybershorts/visuals.py       # Pexels portrait clip download, fallback None
  cybershorts/render.py        # ffmpeg compose -> mp4
  cybershorts/telegram.py      # thin Bot API client (send_message, send_video w/ keyboard, edit, answer_callback, get_updates, get_file/download)
  cybershorts/youtube.py       # upload via google client, token refresh
  cybershorts/tiktok.py        # token refresh + inbox video upload FILE_UPLOAD + status poll
  cybershorts/pipeline.py      # make_draft(), render_draft(draft_id), publish(draft_id, targets), cleanup()
  cybershorts/health.py        # ping(url) helper for Uptime Kuma push (optional)
  scripts/make_draft.py        # entry for timer
  scripts/bot.py               # long-running Telegram loop
  scripts/auth_youtube.py      # one-time OAuth, writes data/youtube_token.json
  scripts/auth_tiktok.py       # one-time OAuth (print URL, paste code), writes data/tiktok_token.json
  scripts/test_render.py       # offline smoke test: fake audio+words -> mp4 (no network)
  scripts/doctor.py            # checks config, ffmpeg, fonts, tokens, API keys reachable
  systemd/cybershorts-bot.service  systemd/cybershorts-draft.service  systemd/cybershorts-draft.timer
  site/index.html site/terms.html site/privacy.html site/callback.html   # GitHub Pages for TikTok app review + OAuth redirect
  tests/test_db.py tests/test_captions.py tests/test_writer_parse.py tests/test_news.py
```

## Module specs
- **config.py**: `load()` returns `Config` with `.raw` (yaml dict merged over defaults) + attrs for secrets from env: OPENROUTER_API_KEY, PEXELS_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET, TIKTOK_REDIRECT_URI, HEALTHCHECK_URL (optional). `path(*parts)` under APP_DIR/data, mkdir data subdirs (videos, voices, models, work). Defaults dict lives in config.py and mirrors config.example.yaml.
- **config.example.yaml keys**: `channel: {name, handle, sign_off}`, `feeds: [ {name,url} ]` (BleepingComputer https://www.bleepingcomputer.com/feed/, The Hacker News https://feeds.feedburner.com/TheHackersNews, Krebs https://krebsonsecurity.com/feed/, SecurityWeek https://www.securityweek.com/feed/, Dark Reading https://www.darkreading.com/rss.xml, CISA https://www.cisa.gov/cybersecurity-advisories/all.xml), `news: {max_age_hours: 36, max_candidates: 25}`, `llm: {model: "anthropic/claude-haiku-4.5", temperature: 0.5, target_words: [80,110]}`, `voice: {mode: tts, piper_voice: en_US-ryan-high, length_scale: 0.92}`, `captions: {whisper_model: base.en, words_per_chunk: 3, font: "DejaVu Sans", font_size: 92}`, `video: {width:1080,height:1920,fps:30,crf:23,preset:veryfast, darken:0.35}`, `pexels: {enabled: true}`, `youtube: {enabled: true, privacy: public, category_id: "28", contains_synthetic_media: false, made_for_kids: false}`, `tiktok: {enabled: true}`, `cleanup: {keep_days: 7}`.
- **db.py**: tables `stories(url PK, title, source, published TEXT, summary, seen_at, used INT DEFAULT 0)`, `drafts(id INTEGER PK, story_url, status TEXT, data_json TEXT, audio_path, video_path, tg_message_id INT, youtube_id, tiktok_publish_id, error, created_at, updated_at)`. Status values: `awaiting_voice, rendered, approved, publishing, published, partial, rejected, failed`. Helpers: `connect()`, `init()`, `upsert_story`, `story_seen(url)`, `mark_used(url)`, `recent_used_titles(n)`, `create_draft`, `update_draft(id, **fields)`, `get_draft(id)`, `drafts_to_cleanup(days)`, `latest_awaiting_voice()`.
- **news.py**: `fetch_candidates(cfg, conn) -> list[dict]` parse all feeds (feedparser, requests w/ UA + 20s timeout, skip failing feed with warning), strip HTML from summary (≤600 chars), drop entries older than max_age_hours (no date → keep), drop URLs already in stories table with used=1, upsert seen, sort newest first, cap max_candidates.
- **writer.py**: `chat(cfg, messages) -> str` POST https://openrouter.ai/api/v1/chat/completions (headers Authorization Bearer, X-Title "cybershorts"), timeout 90, retry 2 on 429/5xx. `extract_json(text) -> dict` tolerant (strip ``` fences, first {...} block). `pick_story(cfg, candidates, recent_titles) -> int` returns index (LLM picks most significant/actionable for IT pros, avoids repeats of recent_titles; validate range, fallback 0). `write_script(cfg, story) -> dict` keys required: `on_screen_title` (≤42 chars), `script` (spoken, target_words, hook first sentence, plain language, spell out CVE as "C V E" not needed—keep ids, ends with sign_off), `youtube_title` (≤95 chars, ends " #Shorts"), `description` (2–3 sentences + "Source: <url>" + hashtags line), `hashtags` (list 3–6, no '#'), `pexels_query` (2–4 words generic visual, e.g. "server room"). Validate + coerce (truncate, ensure source url in description). Prompt forbids inventing facts not in title/summary.
- **tts.py**: `ensure_voice(cfg) -> Path` (if `data/voices/<voice>.onnx` missing run `python -m piper.download_voices <voice> --data-dir data/voices`), `synthesize(cfg, text, out_wav)` using `piper.PiperVoice.load(model)` + `SynthesisConfig(length_scale=...)` + `voice.synthesize_wav(text, wave_file, syn_config=...)`. Lazy imports.
- **captions.py**: `transcribe_words(cfg, wav, prompt) -> list[(start,end,word)]` faster-whisper WhisperModel(model, device="cpu", compute_type="int8", download_root=data/models), `word_timestamps=True`, `initial_prompt=prompt[:800]`. Pure functions (unit-tested): `chunk_words(words, n) -> list[(start,end,text)]` (break also on sentence punctuation, max n words), `build_ass(chunks, cfg, title, handle, duration) -> str` (PlayRes 1080x1920; styles Caption (bold, white, black outline 6, centered ~y 1150, uppercase text), Title (top band y~260, bold, yellow-ish, wraps), Handle (bottom small); escape `{}`\\ in text; times `H:MM:SS.cc`). `write_ass(...)`.
- **visuals.py**: `fetch_broll(cfg, query, out_path) -> Path|None` GET https://api.pexels.com/videos/search?query=&orientation=portrait&per_page=15, header Authorization: key; choose random among top videos having file with height>=1280 and height>width, prefer closest to 1920 height and ≤ 1080x1920-ish; stream download; on any error return None. Second try with query "technology" before None.
- **render.py**: `render(cfg, audio_wav, ass_path, broll|None, out_mp4) -> Path`. Duration via ffprobe. Input0 = broll with `-stream_loop -1` OR lavfi `color=c=0x0b1020:s=WxH:r=fps`. filter: scale increase+crop WxH, setsar=1, fps, `eq=brightness=-darken*0.6:saturation=0.7` (or drawbox overlay black@darken), then `ass=<file>` (run ffmpeg with cwd = ass dir and pass bare filename to avoid escaping). Audio: input1, `apad=pad_dur=0.6`. `-t duration+0.6`, libx264 preset/crf, yuv420p, aac 160k, `-movflags +faststart`. Raise RuntimeError with ffmpeg stderr tail on failure.
- **telegram.py**: class `Telegram(token, chat_id)`: `send_message(text, reply_markup=None)`, `send_video(path, caption, reply_markup)` (multipart, caption ≤1024 chars, HTML parse_mode with escaping helper), `edit_caption(message_id, caption, reply_markup=None)`, `answer_callback(id, text)`, `get_updates(offset, timeout=50)`, `download_file(file_id, dest)`. `keyboard(draft_id)` → inline buttons: "✅ Post both" `pub:all:<id>`, "▶️ YouTube only" `pub:yt:<id>`, "🎵 TikTok only" `pub:tt:<id>`, "❌ Reject" `rej:<id>`. Only include buttons for enabled platforms.
- **youtube.py**: `get_service(cfg)` loads `data/youtube_token.json` via google.oauth2.credentials.Credentials, refresh if expired, re-save. `upload(cfg, video, title, description, tags) -> video_id` resumable MediaFileUpload chunks 8MB, status: privacyStatus, selfDeclaredMadeForKids, containsSyntheticMedia; snippet categoryId. SCOPES = ["https://www.googleapis.com/auth/youtube.upload"].
- **tiktok.py**: token file `data/tiktok_token.json` {access_token, refresh_token, expires_at, refresh_expires_at, open_id}. `exchange_code(cfg, code)`, `refresh(cfg)` POST https://open.tiktokapis.com/v2/oauth/token/ form-encoded (client_key, client_secret, grant_type, code|refresh_token, redirect_uri). `get_token(cfg)` refresh if <5 min left. `upload_draft(cfg, video) -> publish_id`: POST /v2/post/publish/inbox/video/init/ JSON `{"source_info":{"source":"FILE_UPLOAD","video_size":N,"chunk_size":N,"total_chunk_count":1}}` (file must be < 64MB; else chunk 10MB chunks, last chunk absorbs remainder, count = floor(size/chunk)); PUT each chunk to upload_url with headers Content-Type video/mp4, Content-Length, `Content-Range: bytes a-b/N`. Then poll POST /v2/post/publish/status/fetch/ {publish_id} every 5s up to 3 min until status in SEND_TO_USER_INBOX / PUBLISH_COMPLETE / FAILED. Check `error.code != "ok"` → raise with message. `auth_url(cfg, state)` → https://www.tiktok.com/v2/auth/authorize/?client_key=..&scope=user.info.basic,video.upload&response_type=code&redirect_uri=..&state=..
- **pipeline.py**: 
  - `make_draft(cfg)`: init db; cleanup(); candidates; if none → log + return None. pick → write_script → create_draft(status) ; mark_used. If voice.mode==record: status awaiting_voice, Telegram message with script + "Reply with a voice message to record this one." return. Else tts → `render_draft(cfg, id)`.
  - `render_draft(cfg, id)`: captions from draft audio_path (prompt = script), broll (if pexels enabled & key), render to `data/videos/draft_<id>.mp4`, status rendered, send_video with caption (on_screen_title, script preview ≤300 chars, source url, TikTok caption text = description first sentence + hashtags) and keyboard; save tg_message_id. On exception: status failed + error, send Telegram text "Draft <id> failed: <err>" and re-raise.
  - `attach_voice(cfg, draft_id, ogg_path)`: ffmpeg → wav 22050 mono, set audio_path, render_draft.
  - `publish(cfg, id, targets: set{"yt","tt"})`: status publishing; try each target independently, store ids/errors; status published if all chosen ok, partial if some, failed if none. Return summary text (YouTube URL https://youtube.com/shorts/<id>; TikTok "Draft sent to your TikTok inbox — open TikTok to post (expires ~24h)").
  - `reject(cfg, id)`, `cleanup(cfg)`: delete video/audio files for drafts published/rejected/failed older than keep_days; also delete work files; never delete db rows.
- **scripts/bot.py**: loop get_updates (offset persisted in `data/bot_offset`), ignore other chats; callback `pub:*` → answer_callback("Publishing…"), edit caption remove keyboard, run publish, send result message; `rej` → reject + edit caption "Rejected"; voice/audio message → if latest awaiting_voice draft exists: download, attach_voice. Commands: `/draft` runs make_draft now; `/status` last 5 drafts. Catch all exceptions per update; sleep 5s on network errors; never crash loop.
- **scripts/make_draft.py**: load cfg, run make_draft, health ping on success (`HEALTHCHECK_URL` + "?status=up&msg=ok"), on failure ping status=down & exit 1.
- **scripts/auth_youtube.py**: needs `data/client_secret.json`; InstalledAppFlow.run_local_server(host="localhost", bind_addr="0.0.0.0", port=8765, open_browser=False, prompt="consent", access_type="offline"); print instructions for SSH tunnel; save token chmod 600.
- **scripts/auth_tiktok.py**: print auth_url with random state; input() pasted full redirected URL or bare code; parse `code` (urllib.parse, unquote); exchange; save.
- **scripts/doctor.py**: report ✓/✗ for: config loads, each env key set, ffmpeg+ffprobe present, ffmpeg has `ass` filter (`ffmpeg -filters`), font present via fc-list, piper voice present, youtube token present/refreshes, tiktok token present, OpenRouter key valid (GET https://openrouter.ai/api/v1/key), Telegram getMe, Pexels test query. Network checks skippable `--offline`.
- **scripts/test_render.py**: generate 6s sine wav via ffmpeg lavfi, fake words list, build ass, render with broll None → `data/videos/test_render.mp4`; assert ffprobe width/height 1080x1920 and duration≥6. No network.
- **systemd**: bot service (User=cybershorts, WorkingDirectory=/opt/cybershorts, ExecStart=/opt/cybershorts/.venv/bin/python scripts/bot.py, Restart=always, RestartSec=10, EnvironmentFile none — app loads .env itself). draft service Type=oneshot same user; timer `OnCalendar=*-*-* 08:15,12:45,17:30:00` America/New_York via `OnCalendar=... America/New_York`, Persistent=true, RandomizedDelaySec=300.
- **site/**: minimal clean HTML (no external assets). index: what the app is (personal tool that uploads videos the owner made to the owner's own TikTok account as drafts). terms + privacy: short, honest, personal-use, data stored only on owner's server, contact = placeholder `YOUR_EMAIL`. callback.html: JS reads `code` and `state` from query and shows them in a copy box with instructions; never sends anywhere.
- **README.md**: short: what it is, quick start pointing to guide, commands.
- **.gitignore**: .env, data/, .venv/, __pycache__, *.mp4, client_secret.json.

## Tasks
1. [sonnet] Core + pipeline: config.py, db.py, news.py, writer.py, captions.py, render.py, pipeline.py, health.py, __init__.py. Accept: unit tests below pass; test_render produces valid mp4.
2. [sonnet] Integrations + entry points: telegram.py, youtube.py, tiktok.py, tts.py, visuals.py, scripts/*.py. Accept: `python -m py_compile` all; doctor --offline runs; auth scripts' parsing logic correct per spec; bot loop resilient.
3. [haiku] Boilerplate: requirements.txt, config.example.yaml (exactly the keys above, with comments), .env.example (all env keys with comments), .gitignore, systemd/*, site/*, README.md, tests/test_db.py, tests/test_captions.py, tests/test_writer_parse.py, tests/test_news.py (use stdlib unittest; news test parses a local RSS string via feedparser monkeypatching requests; captions tests chunk_words + ass time format; writer tests extract_json on fenced/noisy text; db tests in tmp dir). Tests must import from `cybershorts.*` per API names in this plan.

Task 1 and 2 share function names in this plan — Task 2 imports Task 1 modules by the names given; don't edit other agents' files.

## Test strategy
- `python -m venv .venv && .venv/bin/pip install -r requirements.txt` (faster-whisper/piper may install; models are NOT downloaded in tests).
- `.venv/bin/python -m unittest discover -s tests -v` → all pass.
- `.venv/bin/python -m py_compile` every .py → ok.
- `.venv/bin/python scripts/test_render.py` → prints OK, mp4 1080x1920.
- `.venv/bin/python scripts/doctor.py --offline` → runs without traceback (✗ for missing secrets is fine).
- Mock test: pipeline.publish with youtube/tiktok upload functions monkeypatched → status transitions correct (add tests/test_pipeline.py in Test stage if missing).
