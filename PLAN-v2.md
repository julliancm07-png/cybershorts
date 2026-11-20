# cybershorts v2: video step-up (plan)

Goal: more engaging Shorts. Five features, no new accounts or costs:
1. Word-by-word "karaoke" captions + emphasized keywords
2. Hook card: big title punch-in for the first ~1.6 s
3. A new b-roll clip per script beat, each with a slow pan
4. Branding: corner watermark (logo PNG), progress bar, end card on the sign-off beat
5. Automatic fact-check of the script against the article; result shown in the Telegram draft

Repo: /home/claude/cs-repo (read PLAN.md for v1 context). Python 3.13, ffmpeg 6/7, libass. Keep every existing
public function working (old names stay as thin wrappers where signatures change). All new behaviour is
config-switchable; defaults ON. Never log secrets. Tests: stdlib unittest, no network.

## Interfaces (exact; agents code against these)

### captions.py (Agent A)
- `beat_ranges(script: str) -> list[int]`: word counts per beat. Beats = paragraphs split on blank lines (or single
  newlines); count words the same way `align_to_script` does (`script.split()` keeping tokens whose `_norm` is non-empty).
- `beat_times(words: list[Word], script: str, duration: float) -> list[float]`: start time of each beat; first is 0.0.
  If `len(words) == sum(beat_ranges)` use the start of the first word of each beat; otherwise split proportionally by
  word count over `duration`. Always strictly increasing; length == number of beats (min 1).
- `build_ass(chunks, cfg, title, handle, duration, *, words=None, keywords=None, beats=None) -> str`
  (keyword-only extras so old calls still work; `write_ass` gets the same extras and passes them through).
  - `captions.style` = `karaoke` (default) | `plain`. Karaoke needs `words`; else falls back to plain.
  - Karaoke: for each chunk, one Dialogue per word in it, from that word's start to the next word's start (last word →
    chunk end); text = the whole chunk, the current word coloured `branding.accent` and scaled 112%, other words white.
    First event of each chunk gets a pop-in: `\fscx85\fscy85\t(0,90,\fscx100\fscy100)`.
  - Keywords: any word whose `_norm` matches a `_norm` of a keyword token is always yellow `branding.keyword_color`
    (when not the current word) and bold.
  - Hook card (`video.hook_seconds`, default 1.6, 0 = off): Dialogue style `Hook` centred at (w/2, h*0.36),
    size ≈ 1.15× caption, wraps (`\q0`), text = title uppercased, `\fad(0,180)` and punch-in `\fscx70\fscy70\t(0,220,\fscx100\fscy100)`.
    The normal top `Title` line starts at hook_seconds instead of 0.
  - Progress bar (`branding.progress_bar`, default true): one Dialogue on top layer, 0 → duration, ASS vector drawing
    `{\an7\pos(0,{h-14})\p1\fscx0\t(0,{dur_ms},\fscx100)\c&H{accent}&}m 0 0 l {w} 0 {w} 14 0 14{\p0}` (no outline/shadow;
    add a style `Bar` with Outline 0, Shadow 0).
  - End card (`branding.end_card`, default true, needs `beats` with ≥2 entries): from last beat start to duration,
    centred at (w/2, h*0.42): line 1 "FOLLOW" (accent), line 2 handle (white, large). While shown, the top Title is hidden
    (end the Title event at the last beat start).
  - Colours: config hex `#RRGGBB` → ASS `&H00BBGGRR` via helper `ass_color(hex)`.
- Existing `chunk_words`, `format_ass_time`, `align_to_script`, `transcribe_words` unchanged.

### render.py (Agent A)
- `render(cfg, audio_wav, ass_path, broll, out_mp4)` keeps working; `broll` may now be `None`, a path, **or a list of
  paths**. New kw-only `beats: list[float] | None = None`.
- Multi-clip: segments = beats (or equal splits of the total if `beats` is None/len mismatch). Clip i is used for
  segment i (cycle the list if fewer clips than segments). Each input: `-stream_loop -1 -t {seg+0.5} -i clip`.
  Per segment filter: `scale={W*1.12}:{H*1.12}:force_original_aspect_ratio=increase,crop={W}:{H}:x='(iw-ow)*t/{seg}'` (alternate
  direction each segment: `(iw-ow)*(1-t/{seg})`), `y=(ih-oh)/2`, `setsar=1,fps={fps},trim=duration={seg},setpts=PTS-STARTPTS,
  eq=brightness=...:saturation=0.7` (when `broll.ken_burns` false: plain crop centred). Then `concat=n=N:v=1:a=0`.
- Watermark (`branding.watermark`, path relative to data/ unless absolute; skip silently if missing): extra input
  `-loop 1 -i logo.png`, `scale={branding.watermark_size}:-1,format=rgba,colorchannelmixer=aa={branding.watermark_opacity}`,
  overlay at top-right with 36 px margin, `shortest=1`.
- Then `format=yuv420p,ass=<file>`; audio as today (`apad`), `-t total`. Same encoder args. Fallback colour bg path unchanged
  (watermark + ass still applied).
- Must render 1080x1920 for 1, 2 and 5 clips, with/without watermark, with clips shorter than their segment.

### visuals.py (Agent B)
- `fetch_broll_set(cfg, n: int, query: str, out_prefix: Path) -> list[Path]`: up to n distinct clips. Use the same
  provider/match_story/queries/tag-filter logic as `fetch_broll` (refactor shared bits; keep `fetch_broll` working =
  `fetch_broll_set(..., n=1)`[0] or None). Pick different queries per clip when `match_story` is false (random sample of
  the pool, no repeats), never the same download URL twice. Files: `{out_prefix}_{i}.mp4`. Fill shortfalls from the local
  folder (distinct files). Return [] for provider none / nothing found. `n` capped by `broll.max_clips` (default 5).

### writer.py (Agent B)
- `write_script` JSON gains `"keywords"`: 3–6 short words/phrases from the script to emphasise on screen (product,
  threat, action). `validate_script` coerces to list[str] (≤6, each ≤24 chars), missing → [].
- Default `llm.target_words` becomes [65, 95] (shorter Shorts).
- `fact_check(cfg, story: dict, data: dict) -> dict`: `{"ok": bool, "issues": [str, ...]}`. One `chat` call, temperature 0,
  model `llm.fact_check_model` or `llm.model`. Source = story["article"] or summary + title. Prompt: list each claim in the
  script/on_screen_title/description NOT supported by the source (numbers, names, versions, dates, attributions,
  exploitation status); ignore the sign-off and style; return JSON `{"issues": [..]}` (≤5, each ≤140 chars). Any error →
  `{"ok": True, "issues": [], "skipped": "<reason>"}` (never blocks a draft).

### pipeline.py (Agent B)
- make_draft: after write_script, if `llm.fact_check` (default true) → `data["fact_check"] = writer.fact_check(...)`.
- render_draft: compute `beats = captions.beat_times(words, script, duration)`; `clips = visuals.fetch_broll_set(cfg,
  len(beats), query, cfg.path("work")/f"draft_{id}_broll")` (provider ≠ none); `captions.write_ass(..., words=words,
  keywords=data.get("keywords"), beats=beats)`; `render.render(cfg, wav, ass, clips or None, out, beats=beats)`;
  retry without b-roll on RuntimeError as today.
- `_telegram_caption`: add a line: `✅ Fact-check: nothing unsupported found` | `⚠️ Fact-check:` + bullet list of issues |
  `ℹ️ Fact-check skipped` (HTML-escaped, keep ≤1024 total by trimming the script preview first).
- cleanup: also remove `draft_{id}_broll_*.mp4` (current glob `draft_{id}_*` already covers it; verify).

### config (Agent C, Haiku)
DEFAULTS and config.example.yaml must stay identical (a test checks). Add:
```
video:    hook_seconds: 1.6
captions: style: karaoke
branding: accent: "#3DDCFF", keyword_color: "#FFD60A", watermark: "brand/logo.png", watermark_size: 150,
          watermark_opacity: 0.85, progress_bar: true, end_card: true
broll:    max_clips: 5, ken_burns: true
llm:      fact_check: true, fact_check_model: ""   (target_words → [65, 95])
```
Each with a one-line comment in the YAML. README: short "v2 visuals" section (how to add the logo:
`mkdir -p /opt/cybershorts/data/brand` and copy a square PNG as `logo.png`).

## Tasks
1. [sonnet] Agent A: captions.py + render.py + scripts/test_render.py (multi-clip: make 3 lavfi testsrc clips of 2 s,
   a generated 300x300 PNG logo, fake words/beats, karaoke + hook + bar + end card; assert 1080x1920 and duration) +
   tests/test_captions_v2.py (beat_ranges/beat_times/ass_color/karaoke event count/hook/bar/end-card presence).
2. [sonnet] Agent B: visuals.py, writer.py, pipeline.py + tests/test_visuals_set.py, tests/test_fact_check.py,
   updates to tests/test_pipeline.py for new calls (mock captions/render/visuals as needed).
3. [haiku] Agent C: cybershorts/config.py DEFAULTS, config.example.yaml, README.md section. Touch nothing else.

## Test strategy
`python3 -m unittest discover -s tests` all OK; `python3 -m pyflakes cybershorts scripts tests` clean;
`python3 scripts/test_render.py` OK 1080x1920; DEFAULTS == config.example.yaml.
