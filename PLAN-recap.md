# cybershorts — Weekly recap (long-form) plan

Baseline: 447 tests pass. No new Python deps (Pillow is NOT in requirements.txt → thumbnail drawn with FFmpeg+libass, which Shorts render already uses).

## Findings that shape the plan
- Drafts store `data_json` (script, description, titles, source_url, data_card, series, breaking) and `stories.summary`; article text is NOT stored → optional re-fetch (`recap.fetch_article`), capped.
- `pipeline._voice` already feeds `voice_script or script` to TTS; `tts._synthesize_with` strips tags for non-tag engines/fallbacks → reuse `_voice` = same audio-tag handling as Shorts.
- `tts` writes per-paragraph spans → chapter times from the span of each part's first paragraph (exact, no guessing).
- Cleanup (`drafts_to_cleanup` + `work/draft_{id}.*`/`draft_{id}_*` globs + `video_path`/`audio_path`) already covers recap files if named `draft_{id}_*` → no cleanup code change, only a test.
- Bot callbacks `pub:yt:id` / `rej:id` reusable; `pipeline.publish` must dispatch on `kind` (else Shorts URL / TikTok offered). `bot.offer_stuck` would offer TikTok for a recap.
- One big 1080p filter graph with ~12 decoders risks OOM on 2 GB → staged render: one bg per part, concat copy, one final overlay pass (2 encodes, low RAM).
- `captions._card_events` is hard-wired to centre-top at `w/1080` scale → kw-only overrides; defaults keep Shorts byte-identical (golden test).
- `analytics` learnings would mix long-form views into Shorts comparison → exclude recaps from learnings, keep in report.

## Decisions
- Storage: `drafts` row, `kind='recap'`, `story_url='recap:<YYYY-Www>'`. Reason: every needed column exists; reject/cleanup/`/status`/alerts work unchanged; no new table.
- "Published in last 7 days" proxy = `drafts.updated_at` (already used by `published_videos`); nothing updates a draft post-publish except comment id → no `published_at` column, no Shorts publish change.
- Same week twice: blocked if a `recap:<week>` row exists with status ≠ failed; `/recap force` overrides.
- Ranking: fresh `youtube.video_stats` if token has force-ssl, else stored `video_stats`; key = (views or 0, breaking, updated_at) desc. Segments in rank order.
- `/recap` runs `scripts/recap.py --on-demand` via `subprocess.Popen` so the bot keeps answering buttons during the 15–25 min job; the script takes the lock itself.
- `recap.enabled: false` disables timer run and `/recap` (bot replies it's off).

## New files
- `cybershorts/recap.py` (selection, script, chapters, `run`)
- `cybershorts/recap_captions.py` (landscape ASS + thumbnail ASS)
- `cybershorts/recap_render.py` (staged render, preview, still, thumbnail)
- `cybershorts/recap_publish.py` (review message + publish)
- `scripts/recap.py` (`--force`, `--on-demand`), `scripts/test_recap_render.py` (offline ffmpeg smoke)
- `systemd/cybershorts-recap.service` (oneshot, `TimeoutStartSec=90min`, `Nice=10`, OnFailure/OnSuccess alert lines + hardening copied from draft unit), `systemd/cybershorts-recap.timer` (`OnCalendar=Sun *-*-* 09:30:00 America/New_York`, `Persistent=true`, `RandomizedDelaySec=300`)
- Tests: `test_shorts_ass_golden` (+ `tests/fixtures/shorts_ass/*.ass`), `test_db_recap`, `test_visuals_orientation`, `test_recap_captions`, `test_recap_render`, `test_recap_script`, `test_recap_chapters`, `test_recap_publish`, `test_youtube_thumbnail`

## Config (DEFAULTS == example YAML; one comment line each)
```
recap: enabled: true, stories: 5, min_stories: 3, lookback_days: 7, timezone: America/New_York,
       model: "", target_words: [600, 800], fetch_article: true, max_article_chars: 2500,
       max_audio_tags: 5, title_prefix: "This Week in Cyber: ",
       subscribe_line: "Subscribe for a new recap every Sunday.", category_id: "28",
       width: 1920, height: 1080, fps: 30, crf: 22, preset: veryfast, render_timeout: 3600,
       broll_per_part: 2, max_clips: 12, caption_font_size: 52, words_per_chunk: 6,
       title_card_seconds: 3.5, lock_timeout: 1800,
       preview: {width: 854, height: 480, max_mb: 45, audio_kbps: 64, max_kbps: 1500}
```

## Interfaces (code against these)

**db.py** (no schema change)
- `recap_candidates(conn, since_iso) -> list[Row]`: `d.*` + `s.summary AS story_summary`, `s.title AS story_title` (LEFT JOIN stories); `COALESCE(kind,'regular')!='recap'`, `youtube_id NOT NULL`, `status IN ('published','partial')`, `updated_at >= ?`, newest first.
- `find_recap(conn, week) -> Row|None`: newest row `kind='recap' AND story_url='recap:'||week`.
- `views_for(conn, ids) -> dict[str,int]` from `video_stats`.

**visuals.py** (portrait default unchanged)
- `fetch_broll_set(cfg, n, query, out_prefix, *, orientation="portrait", max_clips=None)`; `max_clips` overrides `broll.max_clips`.
- thread `orientation` through `_fetch_remote(..., orientation=)` → `_pixabay_search` / `_pexels_search`.
- `_pixabay_pick(hits, require, exclude, orientation="portrait")`: landscape → renditions 1280 ≤ w ≤ 1920, score = w + (400 if w > h); fallback smallest ≥ 1280 wide.
- Pexels: `params["orientation"]=orientation`; `_pexels_pick(videos, orientation)`: landscape → w ≥ 1280, w > h, closest to 1920.

**captions.py** (only change)
- `_card_events(card, accent, w, h, start, end, title_bottom, *, scale=None, center=None)`: `sc = scale or w/1080`; `center=(cx, cy)` replaces the cx/cy formula. Defaults → identical output.

**recap_captions.py**
- `build_recap_ass(cfg, words, chunks, parts, duration, *, title, week_label, handle, overlays=None) -> str`; PlayRes = `recap.width` x `recap.height`.
  - `parts` = `[{kind: intro|story|outro, start, end, title, index, total}]`; `overlays` = `{part_idx: {badge, badge_color, card}}` from `pipeline._overlay_kwargs(cfg, story_data)`.
  - Lower-third captions: own `Caption` style (size `recap.caption_font_size`, outline 3, semi-opaque back); reuse `captions._karaoke_events(..., pos=\pos(w/2, 0.87h))`, fallback `captions._fit_events` plain.
  - Story title card (part start, `title_card_seconds`): centred `_rrect` box, "STORY i OF N" accent, headline wrapped ≤ 2 lines, `\fad(200,200)`, layers 4–5; badge pill (`_rrect`+`_est_width`) above headline.
  - Persistent top-left chapter label "i/N  HEADLINE" for the whole part.
  - Intro: title + week label (≤ 6 s). Outro: "SUBSCRIBE" + handle for outro part.
  - Data card: `captions._card_events(card, accent, w, h, s, e, 0, scale=0.75, center=(0.74w, 0.40h))`, `s = start + title_card_seconds`, `e = min(s + data_card.seconds, part.end)`; skip if < 1 s.
  - Progress bar with a tick at each part start.
- `write_recap_ass(..., out_path) -> Path`.
- `build_thumbnail_ass(cfg, text, *, w=1280, h=720, label="WEEKLY RECAP") -> str`: huge 1–2 line keyword-colour text, thick outline, accent label pill.

**recap_render.py** (every ffmpeg call `-threads 2`; temp files `{prefix}_seg_*` / `{prefix}_bg.mp4` deleted in `finally`)
- `render_recap(cfg, wav, ass, part_clips: list[list[Path]], part_durations: list[float], out, *, prefix) -> Path`; durations sum = audio + `render.PAD_SECONDS`.
  1. Per part: video only; clips split the part evenly, scale/crop cover, optional Ken Burns pan, `eq` darken from `video.darken`; no clips → colour bg. `-preset ultrafast -crf 18 -an`.
  2. concat demuxer `-c copy`.
  3. Final: bg + watermark (`render._watermark_path`/`_logo_ok`) + `ass=` (cwd = ass dir) + audio `apad`; `-preset recap.preset -crf recap.crf -maxrate 8M -bufsize 16M`, aac 192k, `+faststart`, timeout `recap.render_timeout`.
  - Raise RuntimeError with stderr tail.
- `preview_bitrate_kbps(duration, max_mb, audio_kbps, cap_kbps) -> int` (pure): `min(cap, floor(max_mb*8192*0.92/duration) - audio)`, floor 200.
- `render_preview(cfg, src, out, duration) -> Path`: scale to preview, mono 64k; if > `max_mb` MiB retry once at 0.7× bitrate.
- `extract_still(clip|None, out_png, *, w=1280, h=720, at=1.0) -> Path`.
- `render_thumbnail(cfg, still, ass, out_jpg) -> Path`: logo overlay + ass, `-frames:v 1 -q:v 3`; > 2 MB → retry q 6.

**recap.py**
- `week_key(cfg, now=None) -> "YYYY-Www"`: ISO week in `recap.timezone` (zoneinfo; missing tzdata → UTC + warning).
- `select_stories(cfg, conn, now=None) -> list[dict]`: keys draft_id, youtube_id, url, title, summary, script, description, breaking, series, data (full draft data), views. Dedup by url; top `recap.stories`.
- `source_text(cfg, s) -> str`: title, summary, Short script, description, + article if `fetch_article`.
- `write_recap_script(cfg, stories, week) -> dict`: one `writer.chat`; model = `recap.model` or `llm.model` (provider routing free via chat); 2 attempts on ValueError like `write_script`. Prompt reuses writer facts rules ("ONLY provided material, no new facts"), adds `writer._tag_instructions` when `tags_supported`; JSON: title, description, intro, segments `[{story_index, chapter, narration}]` (2–3 paragraphs, 90–125 words ≈ 35–50 s), outro, tags, thumbnail_text, first_comment.
- `validate_recap(raw, stories, cfg) -> dict`; ValueError if segments ≠ stories / wrong story_index set / empty narration / total words outside [0.8·lo, 1.25·hi]. Cleanup: strip URLs from narration; chapter plain text ≤ 60 chars; outro gets `subscribe_line` if no "subscribe"; thumbnail_text ≤ 4 words, uppercase, ≤ 28 chars, fallback "WEEK IN CYBER"; title = `title_prefix` + plain title ≤ 100, `#Shorts` removed; tags ≤ 15, ≤ 450 chars; comment via `writer._clean_first_comment`. Audio tags: `sanitize_tags` per part with a shared `max_audio_tags` budget → `voice_script`; `script = strip_tags(voice)`; keep `voice_script` only if it differs. Store `para_counts` per part (intro, segments, outro).
- `part_starts(spans, para_counts) -> list[float]|None`; fallback `part_starts_from_words(words, script, para_counts)` by word counts (`captions.beat_ranges`).
- `chapters(parts, duration, min_len=10) -> list[(int, str)]`: Intro @0, one per story, outro merged into last story; floor to whole seconds; a part < 10 s merges into the previous (short intro merges forward → story 1 at 0); [] if < 3 remain.
- `valid_chapters(ch, duration) -> bool`: first 0, ≥ 3, strictly increasing, every gap ≥ 10 s, last ≤ duration − 10.
- `format_chapters(ch)`: `MM:SS Title` (`H:MM:SS` from 1 h).
- `build_description(cfg, data, ch, stories) -> str`: body, "Chapters" (only if valid), per story "• title / Short: youtube.com/shorts/<id> / Source: <url>", hashtag line; cap 4900 chars trimming sources first, never chapters.
- `run(cfg, *, force=False, on_demand=False) -> int|None` (caller holds the lock):
  1. disabled → return.
  2. Week dedup (on_demand → Telegram "already made (#id); /recap force").
  3. < `min_stories` → `pipeline._notify("Weekly recap skipped: N stories … need M")`, return None.
  4. Script; if `llm.fact_check`: `writer.fact_check(cfg, {title, summary: joined sources}, {on_screen_title, script, description})`.
  5. `db.create_draft(..., "rendered", kind="recap")`.
  6. `pipeline._voice` → probe → words (`transcribe_by_spans` else `transcribe_words` + `align_to_script`).
  7. Part starts/ends → chapters, description.
  8. `fetch_broll_set(…, n=min(parts·broll_per_part, max_clips), orientation="landscape", max_clips=n)`, round-robin assign.
  9. Write ASS → `render_recap` (RuntimeError → retry once without b-roll) → still from first clip → `pipeline._cleanup_broll`.
  10. `music.add_music` → thumbnail (non-fatal) → preview (non-fatal).
  11. Save `data_json`, `video_path=videos/draft_{id}.mp4`.
  12. `recap_publish.send_review` → `tg_message_id`.
  - Any exception after the row exists → status failed, `error=_short_error`, `_notify`, re-raise.

**recap_publish.py**
- `recap_caption(data, draft_id) -> str`: HTML, all values escaped, ≤ 1024: "🎬 Weekly recap #id · week", title, duration + words, chapter lines (or "⚠️ No chapters: …"), "🖼 thumbnail_text", `pipeline._fact_check_lines`. Trim chapters first, then fact-check issues.
- `send_review(cfg, draft_id, data, preview, thumb, full) -> msg_id|None`: `send_photo(thumb)` best-effort; `send_video(preview, caption, recap_keyboard)`; no preview or > `pipeline.MAX_TG_VIDEO_BYTES` → message with caption + server path + keyboard.
- `publish(cfg, draft_id) -> str`: same published/publishing guards as `pipeline.publish`; never calls TikTok. Upload music version if present else `video_path`: `youtube.upload(…, title, description, tags, category_id=recap.category_id)`; save `youtube_id` immediately; `youtube.set_thumbnail` → "🖼 Thumbnail set" or "⚠️ Thumbnail not set: <reason>. Custom thumbnails need a phone-verified channel (youtube.com/verify)"; `pipeline._first_comment`. Returns "Recap {id}: published\nYouTube: https://youtu.be/<id>\n…".

**youtube.py**
- `upload(..., *, category_id=None)` (None → config value as today).
- `set_thumbnail(cfg, vid, path) -> (bool, reason)`: never raises; `thumbnails().set(videoId, media_body=MediaFileUpload(jpeg))`; one retry after 5 s on 5xx; 403 → "channel not allowed custom thumbnails (verify phone)". Never logs secrets/token.

**telegram.py**
- `recap_keyboard(id)`: `[[▶️ Publish to YouTube → pub:yt:id]], [[❌ Reject → rej:id]]`.
- `Telegram.send_photo(path, caption="")` (sendPhoto, same retry/files pattern).

**pipeline.py**
- `publish`: after `get_draft`, if `(row["kind"] if "kind" in row.keys() else None) == "recap"` → `return recap_publish.publish(cfg, id)`. Nothing else.

**bot.py**
- `/recap [force]`: reply "Making this week's recap (~20 min)…", `Popen([sys.executable, scripts/recap.py, "--on-demand", (+"--force")], cwd=app_dir, start_new_session=True)`. `/start` help lists `/recap`. `offer_stuck`: `tt_left=False` for `kind=='recap'`.

**scripts/recap.py**
- Timer: `draft_lock(blocking=True, timeout=recap.lock_timeout)`; Busy → log + exit 1 (alert). `--on-demand`: non-blocking; Busy → Telegram "Another draft job is running; try /recap later." exit 0. Exceptions → `log.exception`, exit 1.

**analytics.py**
- Learnings count/ranking use only non-recap videos; stats upsert + report keep all.

## Tasks (disjoint ownership per wave)
Wave 0
0. [haiku] Golden Shorts ASS. Owns `tests/test_shorts_ass_golden.py`, `tests/fixtures/shorts_ass/*.ass`. Capture from CURRENT `captions.build_ass`: plain, karaoke+keywords, badge+card, end-card+beats. Fixed cfg via `APP_DIR` temp dir. AC: passes on untouched tree; committed before T4.

Wave 1
1. [haiku] Config/infra. Owns `cybershorts/config.py`, `config.example.yaml`, `tests/test_config_parity.py` (add `RECAP_KEYS`), `systemd/cybershorts-recap.{service,timer}`, `README.md` (copy units, daemon-reload, enable timer, restart bot, `/recap` test, ElevenLabs credit note, phone-verified channel note, bot restart kills a running `/recap`). AC: parity passes; empty config loads every `recap.*`.
2. [haiku] DB helpers. Owns `cybershorts/db.py`, `tests/test_db_recap.py`. AC: candidates exclude recap/unpublished/no-youtube_id/older and join summary; `find_recap` newest; `views_for` round-trips.
3. [haiku] Visuals orientation. Owns `cybershorts/visuals.py`, `tests/test_visuals_orientation.py`. AC: default call → byte-identical picks/request params vs today on fixture hits; landscape prefers 1920x1080; Pexels `orientation` passed; `max_clips` override honoured.
4. [sonnet] Landscape overlays. Owns `cybershorts/captions.py` (`_card_events` kwargs only), `cybershorts/recap_captions.py`, `tests/test_recap_captions.py`. AC: golden passes; one title card per story at its start; card inside its part on the right half; captions y ≥ 0.8h; every Dialogue start < end ≤ duration; thumbnail ASS 1280x720 with text.
5. [sonnet] Staged render. Owns `cybershorts/recap_render.py`, `scripts/test_recap_render.py`, `tests/test_recap_render.py`. AC: mocked subprocess shows N part encodes + concat copy + one final, `-threads 2`, temp removed on failure; preview bitrate keeps 300 s ≤ 45 MB; smoke script → 1920x1080, 854x480, 1280x720 offline.
6a. [sonnet] Recap core. Owns `cybershorts/recap.py`, `tests/test_recap_script.py`, `tests/test_recap_chapters.py`, `tests/fixtures/recap_llm.json`. AC: ranking + fallback; week dedup / failed retry / force; skip note < min; tags only in `voice_script`; `run()` happy path (heavy deps mocked) creates `kind='recap'` row and calls `send_review`; chapters valid or omitted.

Wave 2
6b. [sonnet] Publish/Telegram flow. Owns `cybershorts/recap_publish.py`, `cybershorts/youtube.py`, `cybershorts/telegram.py`, `cybershorts/pipeline.py`, `cybershorts/analytics.py`, `scripts/bot.py`, `scripts/recap.py`, `tests/test_recap_publish.py`, `tests/test_youtube_thumbnail.py`. AC: `pub:yt` on recap → `recap_publish`, no `#Shorts`, category 28, no TikTok; thumbnail failure still `published` + warning line; caption ≤ 1024; cleanup deletes recap mp4/wav/preview/thumb/music after `keep_days`; Shorts `publish` tests unchanged.

Wave 3
7. [sonnet] Integration: full suite, pyflakes, `scripts/test_render.py`, `scripts/test_recap_render.py`; fix interface drift. Then Opus review.

## Test strategy
- `python3 -m pytest -q`: 447 old + new pass, offline (requests, googleapiclient, `writer.chat`, Telegram, subprocess/ffmpeg in unit tests, tts, faster-whisper mocked).
- `test_shorts_ass_golden`: `build_ass`/`write_ass` for 4 cases byte-equal to fixtures captured before T4 (+ existing `test_old_calls_identical`).
- `test_recap_chapters`: first `00:00`, ≥ 3, strictly increasing, all gaps ≥ 10 s incl. last; short intro merged; < 3 → no "Chapters" block; times from spans × `para_counts` with word-count fallback; `MM:SS`/`H:MM:SS`.
- `python3 -m pyflakes cybershorts scripts tests` clean.
- `python3 scripts/test_render.py`: Shorts unchanged (1080x1920, same duration).
- `python3 scripts/test_recap_render.py`: real ffmpeg, ~20 s synthetic; pass = dims right, duration ≈ audio + 0.6 s, preview ≤ 45 MB, JPG ≤ 2 MB; report wall time + peak RSS.
- Server: `/recap` e2e on LXC; RSS < 1.2 GB, total < 30 min, YouTube chapters show, thumbnail set or warning shown.

## User steps after deploy
1. `csgit pull`, `systemctl restart cybershorts-bot`.
2. Copy recap units, `systemctl daemon-reload`, `systemctl enable --now cybershorts-recap.timer`.
3. `/recap` in Telegram. Phone-verify the channel (youtube.com/verify) for custom thumbnails.
4. Optional: landscape clips in `data/broll/`; tune `recap.stories` / `target_words`.
