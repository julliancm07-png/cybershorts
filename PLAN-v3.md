# cybershorts v3 plan

Baseline: 200 tests pass, pyflakes clean, `scripts/test_render.py` works offline (~60 s). No DEFAULTS↔example parity test exists yet (T1 adds it; they match today).

## Findings that shape the plan
- `tts.clean_for_speech` strips `[`/`]` → `[sighs]` would be spoken. Tags must be protected around cleaning.
- ElevenLabs: request stitching (previous_text/next_text) is NOT available for `eleven_v3`. `eleven_v4` is Text-to-Dialogue only. → stitching off for any tag model.
- google-auth: `Credentials.from_authorized_user_file(path, SCOPES)` overrides the file's scopes → refresh requests ungranted scopes → invalid_scope → uploads break. Load WITHOUT scopes.
- google_auth_oauthlib `to_json` writes requested scopes, not granted (`granted_scopes`). Save granted.
- `commentThreads.insert` needs `youtube.force-ssl`; no pin API. youtubeAnalytics v2 discovery ships with googleapiclient (no new dep).

## Decisions
- Default model stays `eleven_multilingual_v2` (v3 has no stitching → beats generated cold; opt-in one line).
- `data["script"]` always tag-free; `data["voice_script"]` (tagged) only for TTS.
- flock lock `data/draft.lock` at entry points only (make_draft.py, breaking.py, bot handlers); pipeline functions never lock.
- Data-card values confirmed in code against source/NVD/KEV; never trust LLM alone.

## New files
`cybershorts/{tags,cvedata,breaking,analytics}.py`; `scripts/breaking.py` (`--dry-run`), `scripts/analytics.py` (`--no-telegram`);
`systemd/cybershorts-breaking.{service,timer}` (OnCalendar=*:00/30, RandomizedDelaySec=120, Nice=10, TimeoutStartSec=30min);
`systemd/cybershorts-analytics.{service,timer}` (OnCalendar=Sun 18:05 America/New_York, Persistent=true); hardening copied from draft service.
Tests: test_config_parity, test_db_v3, test_tags, test_elevenlabs_v3, test_captions_v3, test_writer_v3, test_pipeline_v3, test_youtube_scopes, test_analytics, test_cvedata, test_breaking.

## Config additions (DEFAULTS == example YAML, one comment line each)
```
voice.elevenlabs: tag_models: [eleven_v3], audio_tags: true,
                  allowed_tags: [curious, excited, whispers, sighs, serious, laughs], max_tags: 3
llm: hooks: 3 (1 = off), hook_model: ""
series: enabled: true, items: [
  {name: "Patch This Now", hashtag: PatchThisNow, badge: "PATCH THIS NOW", per_week: null,
   match: "patch, update, fixed, actively exploited, KEV", guidance: "Name the product, the fix and any deadline."},
  {name: "Scam of the Week", hashtag: ScamOfTheWeek, badge: "SCAM OF THE WEEK", per_week: 1,
   match: "scam, phishing, fraud, smishing, impersonation", guidance: "How the scam works + the one tell."},
  {name: "CVE in 30s", hashtag: CVEin30s, badge: "CVE IN 30S", per_week: null,
   match: "a single named CVE", guidance: "What, who, how bad, what to do."}]
breaking: enabled: true, kev: true, rss_urgency: true, urgency_threshold: 8, rss_max_age_minutes: 120,
          max_score_items: 15, urgency_model: "", daily_cap: 2, quiet_hours: ["22:00","07:00"],
          timezone: America/New_York, kev_max_age_days: 2, badge: "BREAKING", badge_color: "#FF3B30",
          title_prefix: "🚨 "
data_card: enabled: true, nvd: true, seconds: 4.5
analytics: enabled: true, lookback_days: 90, min_age_hours: 48, min_videos: 6, top_n: 5,
           learnings_max_chars: 800, use_learnings: true, report: true, model: ""
youtube: first_comment: true
```
SECRET_KEYS += NVD_API_KEY (optional).

## Interfaces (code against these)
**db.py**
- `init()` = SCHEMA + idempotent `migrate(conn)` (PRAGMA table_info before each ALTER).
- drafts += `kind TEXT DEFAULT 'regular'`, `series TEXT`, `yt_comment_id TEXT`; DRAFT_FIELDS += these.
- New tables IF NOT EXISTS: `kv(key PK, value)`, `kev_seen(cve_id PK, date_added, seen_at)`, `urgency(url PK, score INT, scored_at)`, `cve_used(cve_id PK, draft_id, used_at)`, `video_stats(youtube_id PK, draft_id, views, likes, comments, avg_view_pct REAL, avg_view_sec REAL, fetched_at)`.
- `create_draft(..., *, kind="regular", series=None)`; `count_drafts_since(conn, since_iso, *, kind=None, series=None)` excludes rejected/failed.
- Helpers: `kv_get/kv_set`, `kev_seen_ids`, `mark_kev_seen(conn, [(cve, date)])`, `get_urgency/set_urgency`, `cve_used/mark_cve_used`, `published_videos(conn, since_iso)`, `upsert_video_stats(...)`, `all_video_stats`.

**tags.py**
- `TAG_RE = r"\[([A-Za-z][A-Za-z \-]{1,24})\]"`
- `strip_tags(text)`: keeps newlines, collapses spaces, drops paragraphs left empty.
- `sanitize_tags(text, allowed, max_tags)`: lowercase, drop unknown, keep first N; tag-only paragraph merges into the next; `strip_tags` result must have the same paragraph count as the clean script.
- `tags_supported(cfg, engine=None, model=None)`: engine elevenlabs AND audio_tags AND model in tag_models (case-insensitive).

**tts.py** (signatures unchanged)
- `_synthesize_with` strips tags unless `tags_supported(cfg, engine_name)` (covers Kokoro/Piper fallback + non-tag models).
- When kept: placeholder-protect tags around `clean_for_speech` + `apply_pronunciations`, then restore.
- `_ElevenLabs.context = not tags_supported(...)` (no previous/next text for tag models).
- Tag models: snap stability to nearest of {0.0, 0.5, 1.0}.

**captions.build_ass / write_ass** += kw-only `badge=None, badge_color=None, card=None`.
- Badge: pill vector shape, black bold text, above hook text, 0 → max(hook_s, 1.6) s, `\fad`.
- Card keys: cve_id, cvss, severity, products, exploited, patch_available.
  - Start `beats[1]` (fallback 2.0 s); end = min(start + data_card.seconds, beats[2], end-card start). Skip if < 1 s.
  - Centred y ≈ 0.21h, dark box, `\move` + `\fad` slide-in.
  - Rows stagger (`\alpha` + `\t`, 120 ms): ID → CVSS pill (≥9 #FF3B30, ≥7 #FF9500, ≥4 #FFD60A, else #34C759) → "Affects: …" (≤40 chars) → "ACTIVELY EXPLOITED" → "PATCH AVAILABLE ✓" (DejaVu has ✓).
  - Falsy/None → no row. New `Card` style; layers 4–5.
- Old calls (no new kwargs) must produce identical ASS.

**cvedata.py** (never raises; never logs secrets)
- `fetch_kev(cfg)`: cache `data/kev.json` 6 h, ETag/If-Modified-Since, cache fallback on error.
- `kev_entry(cfg, cve)`.
- `nvd_lookup(cfg, cve) -> {cvss, severity}`: prefer v4.0 > 3.1 > 3.0 > 2, Primary first; 15 s timeout; `apiKey` header if NVD_API_KEY set.
- `confirm_card(raw, source_text, kev, nvd) -> dict|None` (pure):
  - CVE ID must match regex AND appear in source, else None.
  - CVSS: NVD wins; else LLM number only if that exact number is in source.
  - Severity: NVD or source wording; if score confirmed, map by standard bands.
  - Products: only names present in source or KEV entry, max 2.
  - Exploited: in KEV, or source says exploited in the wild / actively exploited / zero-day etc.
  - Patch: LLM true AND source has patch/update/fix wording AND no negation (no patch, unpatched…).
- `build_card(cfg, story, raw)`: lookups + confirm_card.

**writer.py**
- `write_script(cfg, story, *, series_options=None, breaking=False)`.
- New JSON keys: `alt_hooks` (2, one paragraph each, ≤20 words, same facts), `series` (name or ""), `first_comment` (≤150 chars, ends "?", no URLs), `data_card` (only when enabled and story has a CVE; null for unknowns).
- Tag instructions in prompt only when `tags_supported(cfg)`.
- `validate_script`: `voice_script = sanitize_tags(raw)`; `script = strip_tags(voice_script)`; keep voice_script only if different. Strip tags from title/yt title/description/comment/hooks. `hooks = [first paragraph] + alt_hooks`, dedup, cap llm.hooks. Series must be an offered option; its hashtag first (cap 6) and added to description.
- Learnings: read `data/learnings.md` (capped), append as "style guidance, not facts".
- `pick_hook(cfg, story, hooks) -> int`: hook_model → fact_check_model → model, temp 0; 0 on any error.
- `fact_check` also checks alt hooks.

**pipeline.py**
- `class Busy`; `draft_lock(cfg, blocking=True, timeout=None)` flock `data/draft.lock`.
- `make_draft(cfg)` picks story → `make_draft_for_story`.
- `make_draft_for_story(cfg, story, *, kind="regular", kev=None) -> int`:
  - upsert story; series_options filtered by per_week via `count_drafts_since`; write_script.
  - pick_hook; if ≠0 swap first paragraph in voice_script/script; store `hook_index`.
  - fact_check; `data_card = cvedata.build_card`.
  - breaking → prefix title_prefix to YouTube title.
  - save with kind + series; mark_cve_used for card CVE and KEV CVE.
  - TTS gets `voice_script or script`.
- `render_draft`: badge = breaking badge first, else series badge; card only when data_card.enabled.
- `swap_hook(cfg, id, n)`: only status `rendered` in tts mode; replace first paragraph, delete old wav + spans, re-TTS, `render_draft` (sends new Telegram video).
- `publish`: after YouTube success, if youtube.first_comment and text → `youtube.post_comment` (never raises), save yt_comment_id. Line: "💬 First comment posted. Pin it in the YouTube app (the API can't pin comments)." else "Comment skipped: <reason>". Publish status unaffected.
- `_telegram_caption`: add "🚨 BREAKING", "📺 <series>", "🪝 Alt hooks: 2) … 3) …" (each ≤90 chars). Over 1024: trim preview → alt hooks → TikTok caption → fact-check issues.
- `_short_error` also scrubs ElevenLabs + NVD keys.

**telegram.keyboard(..., *, alt_hooks=())**: row of "🪝 Use hook N" (`hook:N:id`) above Reject.

**bot.py**: `hook:` callback → lock, close old buttons, swap_hook. `/draft` takes the lock (blocking).

**youtube.py**
- `SCOPE_UPLOAD`, `SCOPE_FORCE_SSL`, `SCOPE_ANALYTICS`; `SCOPES` = all three.
- Load creds WITHOUT scopes arg.
- `granted_scopes(cfg)` reads token file (no network). `has_scope(cfg, s)`: broad `auth/youtube` also satisfies force-ssl.
- `get_service(cfg, api="youtube", version="v3")`.
- `post_comment(cfg, vid, text) -> (id|None, reason)`: one retry after 5 s on 5xx/"processing".
- `video_stats(cfg, ids)`: videos.list statistics, batches of 50.
- `analytics_stats(cfg, ids, start)`: youtubeAnalytics v2 reports.query, ids=channel==MINE, dimensions=video, filters=video==a,b, metrics views,averageViewDuration,averageViewPercentage.

**auth_youtube.py**: `OAUTHLIB_RELAX_TOKEN_SCOPE=1`; save with `scopes = granted_scopes`; print which features are enabled.

**analytics.run(cfg)**
- No force-ssl → log line + Telegram hint, return. No analytics scope → views/likes/comments only.
- Upsert stats. If ≥ min_videos: LLM compares top vs bottom N (title, hook, series, kind, word count) → ≤6 bullets ≤120 chars to learnings.md (capped).
- Weekly HTML-escaped report: totals, top 3, learnings.

**breaking.run(cfg, dry_run=False) -> id|None**
1. Return if disabled; db.init.
2. Seed KEV (no `kev_seeded` kv flag → mark all seen, return). Runs even in quiet hours.
3. Quiet hours (zoneinfo; missing tzdata → UTC + warning).
4. Daily cap counted from local midnight.
5. `draft_lock(blocking=False)`; Busy → return.
6. Candidates: unseen KEV within kev_max_age_days, ransomware "Known" first then newest; use RSS story mentioning the CVE if any, else build story from KEV fields (URL nvd.nist.gov/vuln/detail/<id>). Plus fresh RSS items scored in one batched LLM call (each URL scored once), ≥ threshold.
7. Dedup: cve_used, story used, title similarity ≥ 0.75 vs last 20 used titles.
8. One draft per run. Mark chosen + deduped as seen; others stay pending.

## Tasks (disjoint ownership; T1–T7 parallel)
1. [haiku] Config/infra: config.py, config.example.yaml, .env.example, 4 systemd units, README "v3" section, tests/test_config_parity.py. AC: parity passes; empty config.yaml loads all new keys.
2. [haiku] DB: db.py, tests/test_db_v3.py. AC: init twice on v2 DB with rows → columns/tables added, no errors, no data loss; helpers round-trip.
3. [sonnet] Voice: tags.py, tts.py, tests/test_tags.py, tests/test_elevenlabs_v3.py (may edit test_elevenlabs.py). AC: v3 request has tags and no previous/next text; fallback/non-tag never get tags or speak "sighs"; span count == clean paragraph count.
4. [sonnet] Overlay: captions.py, scripts/test_render.py (badge + card, `--keep-frames DIR`), tests/test_captions_v3.py. AC: rows only for truthy values; card inside beat-2 window; badge timing; old calls identical; render passes.
5. [sonnet] Draft flow: writer.py, pipeline.py, telegram.py, scripts/bot.py, scripts/make_draft.py, tests/test_writer_v3.py, tests/test_pipeline_v3.py (may edit test_pipeline.py, test_writer_parse.py, test_fact_check.py). AC: tags only in voice_script; hook pick + swap + buttons; Scam of the Week not offered twice a week; series hashtag; comment failure leaves `published` + pin hint; caption ≤1024.
6. [sonnet] YouTube/analytics: youtube.py, analytics.py, scripts/auth_youtube.py, scripts/analytics.py, scripts/doctor.py, tests/test_youtube_scopes.py, tests/test_analytics.py. AC: upload-only token still uploads (no scopes arg); comment/analytics skip with log; doctor warns on missing scopes; learnings capped.
7. [sonnet] Breaking/CVE: cvedata.py, breaking.py, scripts/breaking.py, tests/test_cvedata.py, tests/test_breaking.py. AC: first run seeds only; quiet hours / cap / held lock / dedup each block; new KEV → make_draft_for_story(kind="breaking"); confirm_card drops unconfirmed.
8. [sonnet] Integration after T1–T7 (any file). AC: suite + pyflakes + test_render pass.

## Test strategy
- `python3 -m unittest discover -s tests` — all pass, all network mocked (requests, googleapiclient, writer.chat, Telegram).
- `python3 -m pyflakes cybershorts scripts tests` — clean.
- `python3 scripts/test_render.py` — 1080x1920, expected duration; extra badge + card render; `--keep-frames /tmp/v3` to eyeball hook/card/end-card frames.
- Fixtures: v2-schema SQLite; 3-entry KEV JSON; NVD JSON with v3.1 metrics; upload-only token file; fake chat returning tags/alt_hooks/series/data_card.
- Server: doctor.py, breaking.py --dry-run, analytics.py --no-telegram.

## User steps after deploy
1. `csgit pull` (no new deps); `systemctl restart cybershorts-bot`.
2. Google Cloud: enable YouTube Analytics API; add `youtube.force-ssl` + `yt-analytics.readonly` to consent screen.
3. Re-run auth_youtube.py (ssh tunnel), tick every box; run doctor.py.
4. Install timers: copy breaking + analytics units, daemon-reload, enable --now. First breaking run only seeds KEV.
5. Optional: `voice.elevenlabs.model: eleven_v3`; NVD_API_KEY; quiet_hours/daily_cap/series.
6. Pin each auto comment in the YouTube app.
7. Learnings start at ≥6 videos ≥48 h old; analytics lags 2–3 days.
