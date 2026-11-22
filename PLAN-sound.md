# cybershorts — ElevenLabs sound kit plan

## Findings
- `data/` is gitignored → kit never reaches the public repo (Music terms forbid libraries/resale). backup.py copies `data/brand/` but skips files > 20 MB → store kit as **mp3 192k** (~7 MB for 9 items).
- SFX timings already decided in `captions.build_ass`: hook card 0→`hook_seconds`; badge 0→`max(hook_s,1.6)`; data card `beats[1]`→`min(+data_card.seconds, beats[2], limit)` (only if rows and ≥1 s); end card at `beats[-1]` when `show_end_card`. Extract one pure function so mixer and ASS can't drift (golden ASS test keeps ASS byte-identical).
- Mix as an **audio-only pass on the voice WAV before `render.render`**: no extra video encode, no RAM spike; master carries SFX → YouTube, TikTok and music version all get them; `music.mix`/publish routing untouched. `sfx_on_tiktok: false` → `-c:v copy` remux of master with plain voice.
- `alimiter` defaults `level=true` (auto-gain) → SFX pass must use `level=disabled:latency=1` (voice stays unity gain).
- Music beds already loop/trim in `music.mix` (`-stream_loop -1`, `-t dur`) → only selection change + seamless loop points made once at generation.

## API facts
- SFX: `POST https://api.elevenlabs.io/v1/sound-generation`, header `xi-api-key`; body `text` (req), `loop` (bool), `duration_seconds` 0.5–30 (unset = model guesses), `prompt_influence` 0–1 (default 0.3), `model_id` `eleven_text_to_sound_v3` (v2 deprecated, removed 2027-03-01). Query `output_format` (default `mp3_44100_128`; 192 kbps needs Creator+). Errors 422 `detail[{loc,msg,type}]`. Cost: 40 credits/second when duration specified. https://elevenlabs.io/docs/api-reference/text-to-sound-effects/convert.md
- Music: `POST https://api.elevenlabs.io/v1/music`; body `prompt` (xor `composition_plan`), `music_length_ms` 3000–600000, `force_instrumental`, `model_id` `music_v2` (music_v1 removed 2027-01-31). `output_format` `mp3_44100_192`. No loop param. Paid subscribers only. https://elevenlabs.io/docs/api-reference/music/compose.md
- Music terms (Creator): commercial use OK except film/TV/radio/studio games; no attribution; streaming OK; no music libraries/resale; must follow platform AI-disclosure rules (user checks YouTube/TikTok labelling). https://elevenlabs.io/eleven-music-model-specific-terms
- Music credit cost NOT verifiable; estimate ~900 credits/min (config value). Measure real cost via `GET /v1/user/subscription` before/after each item; refuse if balance < 1.5× estimate; balance unreadable → warn, continue. 422 rejecting `force_instrumental` → retry once without it.
- Budget per variant: SFX ~5.6 s × 40 ≈ 224 credits; music (8 + 75 + 90 + 60 s) ≈ 3.5k credits (est.).

## Config (DEFAULTS == example YAML)
```
sound: enabled: true, folder: brand/sound, sfx_on_tiktok: true,
  sfx_model: eleven_text_to_sound_v3, music_model: music_v2, output_format: mp3_44100_192,
  prompt_influence: 0.5, whoosh_lead: 0.12, min_gap: 0.3, limit: 0.95,
  gain_db: {hook: -10, whoosh: -16, breaking: -12, card: -18, outro: -10},
  recap: {theme_db: -12, bed_db: -22, duck: true},
  credits: {sfx_per_second: 40, music_per_minute: 900},
  kit: [{name, kind, prompt, seconds, loop}] x9
       hook 1.0 sfx (short digital glitch impact), whoosh 0.6 sfx (fast airy transition),
       breaking 1.5 sfx (urgent alert tone), card 0.5 sfx (UI data blip), outro 2.0 sfx (signature sting),
       theme 8 music (dark synth/cyber signature intro), bed1 75 / bed2 90 / bed3 60 music loop:true
       (instrumental, dark minimal techno/synthwave under narration, no vocals)
music: + prefer_kit: true
```

## Architecture
**captions.py** (refactor only): `overlay_times(cfg, duration, *, title, beats, badge, card) -> {hook_end, badge_end|None, card:(s,e)|None, end_card:float|None}`; `build_ass` uses it; golden ASS identical.

**sound.py** (runtime; every failure non-fatal: log via `alerts.scrub`, return None)
- `kit_dir(cfg) -> Path`; `kit_file(cfg, name) -> Path|None` (`name.mp3` or other audio ext, size > 0).
- `short_events(cfg, beats, duration, *, title, badge, card) -> list[(float, str)]`: hook @0 (breaking @0 instead if badge); whoosh at `beat − whoosh_lead` for `beats[1:]`; card at `card[0]`; outro at `end_card`. Priority outro > breaking > hook > card > whoosh; lower event within `min_gap` of a higher one dropped; events with missing kit file dropped.
- `build_sfx_cmd(cfg, voice, events: list[(float, Path, float)], out) -> list[str]` (pure): each kit file input once + `asplit`; per event `aformat=44100:mono,volume=GdB,adelay=MS:all=1`; `[0:a]aformat…[v]` + events → `amix=inputs=N:duration=first:normalize=0:dropout_transition=0,alimiter=limit=L:level=disabled:latency=1`; out `-ac 1 -ar 44100 pcm_s16le`.
- `short_sfx(cfg, draft_id, wav, *, beats, duration, title, overlay) -> Path|None` → `work/draft_{id}_sfx.wav`; None if disabled/no events.
- `tiktok_version(cfg, id) -> Path` (`work/draft_{id}_tiktok.mp4`); `make_clean_tiktok(cfg, master, voice, out) -> Path|None` (copy-video remux, `apad=pad_dur=PAD_SECONDS`).
- `recap_events(cfg, parts, duration)`; `recap_audio(cfg, voice_wav, parts, out_wav) -> (Path|None, has_bed: bool)`; parts per PLAN-recap `[{kind,start,end,...}]`: theme @0 fading 1 s at intro end; kit bed from intro end to end (`-stream_loop -1`, ducked with same `sidechaincompress` as `music.mix`, fade in/out); whoosh at each story start − lead; outro sting at `max(outro.start, dur − sting_len)`; stereo 44.1k WAV exactly voice duration.

**soundkit.py** (generator)
- `SoundClient(cfg)`: `sfx(text, seconds, *, loop, influence, model) -> bytes`, `music(prompt, ms, *, instrumental, model) -> bytes`, `balance() -> int|None`; copy `tts._ElevenLabs` retry loop (3 tries, backoff 429/5xx, `detail` parsing). 401/403/402 or "quota" → `KitFatal` (stop); 422/unknown → `KitItemError` (continue); 200 non-audio (JSON or < 1 kB) → item error; timeouts 120 s sfx / 300 s music; key never in messages.
- `estimate(item, variants) -> int`.
- `postprocess(raw, out, item)`: sfx `silenceremove` leading edge + peak-normalize −1 dBFS; music `loudnorm I=-16:TP=-1.5`; `loop: true` music: tail-into-head `acrossfade d=1.5` seamless loop; all mp3 192k.
- `generate(cfg, *, only=None, variants=1, dry_run=False, use=None, tg=None) -> list[dict]`: skip existing `name.mp3` unless `only` names it; temp file + `replace()`; variants `name.mp3`, `name.v2.mp3`…; `use name=2` swaps; `manifest.json` `{name, kind, prompt, seconds, duration, created, model, credits_used, variants}`; each file → `tg.send_audio` (failure only warns).

**scripts/make_sound_kit.py**: `--only`, `--variants N`, `--dry-run` (table + total, no HTTP/files), `--use NAME=K`.

**telegram.py**: `Telegram.send_audio(path, caption="", title=None, performer=None)` (sendAudio, `files=lambda` retry pattern, `audio/mpeg`, timeout 300).

**music.py**: `kit_beds(cfg) -> list[Path]` (`bed*` in kit_dir); `pick_track` prefers kit beds only when `sound.enabled` and `music.prefer_kit` and beds exist; else unchanged.

**pipeline.py**: `render_draft`: after `write_ass`, `audio = sound.short_sfx(...) or wav` → both `render.render` calls; if SFX made and `sfx_on_tiktok` false → `make_clean_tiktok`, else unlink stale TikTok version; unlink `_sfx.wav`; "🔊 SFX: …" caption note only if fits 1024. `publish`: `video_tt = mv if (mv.exists() and on_tiktok) else (tv if tv.exists() else video)`. `swap_hook`: add `tiktok_version` to `originals`.

**recap.py** (steps 9–10): `mixed, has_bed = sound.recap_audio(cfg, wav, parts, work/draft_{id}_recap_mix.wav)`; `render_recap(cfg, mixed or wav, …)`; `music.add_music` only if `not has_bed`.

## Tasks
Wave 0 (before any edit below)
0. [haiku] Golden ffmpeg argv. Owns `tests/test_sound_regression.py`, `tests/fixtures/ffmpeg_golden/*.json`. Capture argv from real `render_draft` with subprocess/probe/transcription/visuals/Telegram mocked: regular+beats, breaking+card, with/without music track; paths normalized. AC: passes on untouched tree.

Wave 1
1. [haiku] Config/docs (after recap merge). Owns config.py, config.example.yaml, tests/test_config_parity.py (`SOUND_KEYS`), README.md ("Sound kit": dry-run first, listen in Telegram, --only/--variants/--use, credits, Music terms note, kit in backups + gitignored, how to disable), scripts/doctor.py ("Sound kit: n/9"). AC: parity passes; empty config loads every `sound.*`.
2. [sonnet] Generator. Owns cybershorts/soundkit.py, scripts/make_sound_kit.py, cybershorts/telegram.py (send_audio), tests/test_sound_kit.py. AC: mocked HTTP URL/body/params right; auth error stops, 422 continues, 429 retries then succeeds; key absent from exceptions/logs; items not in --only keep bytes+mtime; failed --only keeps old file; dry-run zero HTTP, SFX shows 40/s; manifest + send_audio once per file.
3. [sonnet] Mixing. Owns cybershorts/sound.py, cybershorts/captions.py (`overlay_times` only), tests/test_sound_mix.py, scripts/test_sound_mix.py. AC: golden ASS identical; parametrized event rules (breaking replaces hook, lead, priority/min_gap, missing files); cmd has `normalize=0`, `level=disabled`, adelay ms; disabled/empty kit → None, no subprocess.
4. [haiku] Kit beds. Owns cybershorts/music.py, tests/test_music.py (new cases). AC: prefers bed*, never same twice in a row, falls back to folder tracks; sound off → pick_track as today.

Wave 2
5. [sonnet] Shorts integration. Owns cybershorts/pipeline.py, tests/test_sound_pipeline.py, + disabled/empty-kit cases in test_sound_regression.py. AC: both byte-equal golden; with kit render.render gets `_sfx.wav`; sfx_on_tiktok false → TikTok version made and routed; SFX failure still renders; caption ≤ 1024.
6. [sonnet] Recap wiring. Owns cybershorts/recap.py (steps 9–10), tests/test_recap_sound.py. Needs recap.py + recap_render.py. AC: no kit → mock calls identical; with kit mixed WAV → render_recap, add_music skipped only when bed included.

Wave 3
7. [sonnet] Integration: full suite, pyflakes, scripts/test_render.py, scripts/test_recap_render.py, scripts/test_sound_mix.py; fix drift. Then Opus review.

## Test strategy
- All offline (requests, Telegram, subprocess/ffmpeg in unit tests, TTS, Whisper mocked); ffmpeg cases `skipUnless(HAVE_FFMPEG)`.
- Byte-identical regression: golden argv under `sound.enabled: false` and empty kit, Shorts (T5) and recap (T6).
- `scripts/test_sound_mix.py` (real ffmpeg, temp APP_DIR): synthetic 7 s voice + SFX bursts → `short_sfx` + `render.render`; assert sfx WAV duration within 10 ms of voice, mp4 ±0.35 s of voice+0.6, peak ≤ −0.3 dBFS, band-pass RMS at events > 6 dB above gaps, voice-only RMS within 0.3 dB (unity); 60 s `recap_audio` with fake parts: duration equal, peak OK, bed ducked; wall time < 5 s for a Short.
