# cybershorts — Recap v2 "newsroom" visuals plan

Baseline 669 tests. Design reference: design/recap-v2/ (PNGs + mock.py/mock2.py: reuse geometry, colours, drawing approach).

## Findings
- Current recap: per-part bg ffmpeg passes → concat → final pass (`recap_render.render_recap`). With b-roll off the per-part stage only paints colour → newsroom = one pass over a looped background image.
- Static chrome (panel fills/borders, show title, Week pill, handle, caption box, progress track + ticks) is baked once into the bg PNG (one ffmpeg `ass=` on a single frame); only moving things are libass events.
- Background: one `gradients` frame (ffmpeg 5.1+/6.1) + light `noise` once (anti-banding) + chrome; fallback `color` if `gradients` missing. Final pass reads it with `-framerate fps -i bg.png` + `format=yuv420p,loop=loop=-1:size=1:start=0` (decode once; `-loop 1` would decode every frame, ~2 min extra on 8 min).
- Watermark clash: classic watermark top-right (`W-w-36:36`) would cover the handle → newsroom logo in a 64x64 box at `x=W-60-w, y=40`; chrome shifts the handle left when a logo exists.
- Captions: 1100-px box, 6 words at 46 px can overflow → newsroom re-chunks by chars (in new module; Shorts untouched). Recap currently passes `set()` keywords → newsroom supplies per-story keywords. Captions are suppressed during transition cards by passing only chunks outside those windows (with their words) to `captions._karaoke_events`.
- Timing exists: words aligned 1:1 to script words, `para_counts` per part, `captions.beat_ranges` → each story paragraph = a word-index range; cue phrases match inside it.
- Whoosh: `sound.recap_events` puts whoosh at story start − `whoosh_lead`; transition card starts exactly at story start → whoosh leads by ~0.12 s by design (test pins it). No sound change.
- Source name: `stories.source` exists but `db.recap_candidates` doesn't select it → add `s.source AS story_source`; fallback URL domain.
- Visuals get a SECOND LLM call after the final script (bad visuals never force a script retry; cues copied from final narration).
- Facts: reuse `cvedata` patterns (`CVE_RE`/`_cve_pattern`, `_number_in`, `_word_in`). `writer.fact_check` sees only `script` → append on-screen text to the script it receives.

## Decisions
- `recap.layout: newsroom|classic`; `recap.broll: auto|true|false` (auto = on for classic, off for newsroom). Newsroom v1 ignores b-roll (one warning). Classic byte-identical to today (golden).
- Transition card: full screen from story start, `min(transition_seconds, 0.4 × part len)`; near-opaque dark rect (libass can't blur; dimming approximates).
- Intro: full-screen card for the whole intro: show title, Week pill, lineup of story chapter titles fading in one by one. Outro: "SUBSCRIBE", handle, "Full sources in the description". Captions stay on top of both with their own caption box.
- Scene gaps > `scene_max_seconds` filled by auto scenes, fact-safe: "headline" (kinetic title + source) and "cve" (count-up of a confirmed card's CVSS). No valid LLM scenes → only these.
- Illustrative labels: terminal title bar reads "DRAMATIZATION · …"; scam phone shows "EXAMPLE SCAM MESSAGE".

## Second LLM call: `write_recap_visuals(cfg, stories, data) -> list[dict]`
Input: each story's `source_text` + its final narration with numbered paragraphs [P1]…; ValueError → retry once → empty visuals. Model = `recap.model` or `llm.model`.
Per story:
```
{"story_index": i,
 "keywords": [2-5 short phrases copied from the narration],
 "key_points": [{"text": "<=38 chars", "para": 1, "cue": "3-8 consecutive narration words"}] (2-4),
 "checklist":  [{"text": "<=40 chars imperative", "para": n, "cue": "..."}] (0-3, [] if not actionable),
 "scenes": [ {"type": T, "para": n, "cue": "...", ...fields} ] (2-3, types from recap.newsroom.scenes)}
```
Scene fields:
- `terminal`: `title` (≤30), `lines` [{`kind`: cmd|out|alert, `text` ≤44}] (3–7); renderer adds `$ `.
- `flow`: `title` (≤28), `nodes` [{`label` ≤14, `sub` ≤22}] (2–4), `arrows` [labels ≤14] (nodes − 1), optional `fixed_in`.
- `stat`: `value` (as written in source, e.g. "4,000", "70%", "$2.3 million"), `label` (≤40), optional `context` (≤40).
- `timeline`: `events` [{`date`, `label` ≤18}] (2–4).
- `scam`: `channel` (sms|email), `sender` (≤24), optional `subject`, `body` (≤140, links as `{link}`), `flags` (≤3, ≤28 chars).

Validation (pure, `recap_visuals.py`, never raises; drop reasons → `dropped`). Source = `recap.source_text(cfg, s)` + confirmed card values.
- CVE ids must match `cvedata._cve_pattern` in source. Versions (`\d+(\.\d+){1,3}[a-z]?`) must appear exactly. Numbers (`,`, `%`, `$`, million/billion) normalised + digit-boundary match like `_number_in`. Dates: month+day in "Nov 12" / "November 12" / "12 November" / ISO; years must be in source. Names: capitalised tokens/acronyms not in `GENERIC_WORDS` (Attacker, Server, Disclosed, Patched, Update…) must pass `_word_in`.
- Terminal safety, drop the line on: IPv4/IPv6, `://`, domain TLDs, emails; shell metachars ``| ; && || > < ` $(``, `\x..`, base64-ish runs ≥24; denylisted tools/paths: nmap, masscan, msf*, sqlmap, hydra, mimikatz, nc/ncat/netcat, curl, wget, powershell, cmd.exe, `bash -i`, `python -c`, `/dev/tcp`, `/etc/shadow`, `/etc/passwd`, `rm -rf`, certutil, `iex`/`invoke-`, base64. Keep scene only if ≥3 lines survive incl. ≥1 cmd.
- Scam: any run of ≥7 digits or an email address drops the scene; `{link}` drawn as a redacted bar.
- Other: text > 1.5× cap drops the item; < 2 checklist items or 0 key points drops that section; flow with < 2 valid nodes dropped; invalid `fixed_in` drops only the badge; keywords must appear in narration.

## Files and signatures
**New `cybershorts/recap_visuals.py`** (pure): `SCENE_TYPES`, `GENERIC_WORDS`; `prompt(cfg, stories, data) -> list[dict]`; `parse(raw, n) -> list[dict]`; `clean_visuals(raw_seg, source, narration, cfg, *, card=None) -> {keywords, key_points, checklist, scenes, dropped}`; `facts_ok(text, source) -> (bool, str)`; `unsafe_terminal(line) -> str|None`; `story_word_ranges(words, script, para_counts) -> list[list[(int,int)]]|None`; `cue_time(cue, para, words, ranges) -> float|None` (exact in paragraph → whole part → fuzzy ≥60% token overlap → paragraph start + 0.4 s → None); `keyword_set(list[str]) -> set[str]`.

**New `cybershorts/recap_scenes.py`** (pure event builders; mock geometry): `Theme`, `theme(cfg)`; `Box(x, y, w, h)` (left panel `Box(60, 150, 1160, 653)`); primitives `ev(layer, s, e, text)` (clamps, drops empty), `rr`, `tx`; `fit_text(text, max_w, size, *, min_size, max_lines, factor=0.60) -> (lines, size)` (0.602 for mono; ellipsis on last line). Scene builders `(sc, box, s, e, th) -> list[str]`: `terminal` (one event per line with `\t(\clip)` growth; blinking cursor), `flow` (nodes pop with `\fscx\t`; arrows grow via `\clip` step by step; badge last), `stat` (count-up ≤14 stepped events, keep source formatting), `timeline` (line grows; dots + labels per event), `scam` (phone mock, bubbles, red-flag callouts), `headline`, `cve`; `BUILDERS`. Panel builders: `cve_card(card, x, y, w, s, e, th) -> (lines, h)` (CVSS bar `\clip` growth + badges); `key_point(text, x, y, w, s, e, th) -> (lines, h)` (`\move` slide-in + `\fad`); `checklist(items, times, x, y, w, s, e, th)`. Full-screen: `transition(n, total, headline, tagline, s, e, W, H, th)` (glitch offsets jitter 400 ms then settle); `intro_card(...)`; `outro_card(...)`.

**New `cybershorts/recap_newsroom.py`**: `schedule(cfg, part, vis, times) -> {transition: (s,e), scenes: [(s,e,scene)], points: [(t,text)], checklist: (start,[t])|None}` (pure; all times within [start,end]; scenes ≥ `scene_min_seconds`; gaps > `scene_max_seconds` → auto scene; points monotonic ≥1.2 s apart; checklist ≥3 s after last point and needs ≥4 s else omitted); `fit_chunks(words, max_words, max_chars)`; `build_chrome_ass(cfg, parts, duration, *, week_label, handle, has_logo) -> str`; `build_newsroom_ass(cfg, words, chunks, parts, duration, *, stories, visuals, cards, script, para_counts, week_label, handle) -> str` (captions layer 30; per-story chapter text, chip, right panel, scenes, transition card; white ticks + progress fill); `write_newsroom_ass(..., out_path) -> Path`.

**`recap_render.py`** (additions only): `render_background(cfg, out_png, *, w, h, chrome_ass=None) -> Path`; `render_recap_single(cfg, wav, ass, bg_png, total, out) -> Path` (one pass: bg loop, watermark 64-px box, `ass=`, `apad`; `-threads 2`; same encode flags). Newsroom thumbnail still = `render_background(w=1280, h=720)`.

**`recap.py`**: `select_stories` adds `source`; `run`: after script, `data["visuals"] = [clean_visuals(...)]` (newsroom only); visuals text appended to fact-check input; `_produce` branches on layout (newsroom: no `fetch_broll_set`; chrome → bg → `write_newsroom_ass` → `render_recap_single`; bg PNGs cleaned in `finally`; classic untouched). `recap_audio`/`add_music` unchanged.

**`db.py`**: `story_source` column in `recap_candidates`.

**Config** (DEFAULTS == example YAML):
```
recap: + layout: newsroom, broll: auto, show_title: "THIS WEEK IN CYBER",
  newsroom: {transition_seconds: 3.0, scene_seconds: 12, scene_min_seconds: 6, scene_max_seconds: 18,
             scenes: [terminal, flow, stat, timeline, scam], key_points: true, max_key_points: 4,
             checklist: true, caption_font_size: 44, caption_max_chars: 42, scanline: true,
             bg_colors: ["#070B14", "#101A33"], panel_color: "#0F1626"}
```
`max_key_points` effectively 3 when a CVE card is shown; `scanline` = faint 2 px sweep ~every 8 s.

## Tasks
Wave 0
- T0 [haiku] Classic golden. Owns `tests/test_recap_classic_golden.py`, `tests/fixtures/recap_classic/*`. Capture today's `build_recap_ass` output + `render_recap` argv (subprocess mocked). AC: passes on untouched tree.

Wave 1
- T1 [haiku] Config/DB/README. Owns config.py, config.example.yaml, tests/test_config_parity.py (`NEWSROOM_KEYS`), db.py, tests/test_db_recap.py (`story_source` case), README.md (layout, b-roll, switching back to classic). AC: parity passes; empty config loads every new key.
- T2 [sonnet] Visuals validation. Owns recap_visuals.py, tests/test_recap_visuals.py, tests/fixtures/recap_visuals.json. AC: fact/safety matrix; `cue_time` exact/fuzzy/paragraph/None.
- T3 [sonnet] Scene builders. Owns recap_scenes.py, tests/test_recap_scenes.py. AC: every builder's events inside [s,e] and its box (parse `\pos`/`\move`/`\clip`, estimate widths); overlong text wraps/shrinks/ellipsis; `{` `}` `\` escaped.
- T4 [sonnet] Render. Owns recap_render.py, tests/test_recap_render.py. AC: newsroom argv = 1 bg cmd + 1 final pass, no `_seg_`, `loop` filter, `-threads 2`, temp removed on failure; classic golden passes.

Wave 2
- T5 [sonnet] Layout assembly. Owns recap_newsroom.py, tests/test_recap_newsroom.py. AC: boundary/whoosh/blackout tests; 8-min fixture ≤3000 events, ≤80 on screen at any second.
- T6 [sonnet] Recap wiring. Owns recap.py, tests/test_recap_script.py, tests/fixtures/recap_llm_visuals.json. AC: visuals LLM failure → recap with auto scenes; newsroom never calls b-roll; classic mock calls unchanged.
- T7 [haiku] Smoke + review caption. Owns scripts/test_recap_newsroom.py, recap_publish.py + its test ("🎞 scenes S · points P · dropped D", caption ≤1024). AC: script runs offline, writes PNGs.

Wave 3
- T8 [sonnet] Integration: full suite, pyflakes, scripts/test_render.py, test_recap_render.py, test_sound_mix.py, new smoke; fix drift. Then Opus reviews code + PNG frames vs design/recap-v2/.

## Test strategy
- Offline units (LLM/ffmpeg/network mocked): fact accept/reject matrix (comma numbers, %, $, versions, CVEs, date variants, unknown names, generic words); terminal denylist parametrised; text-fitting extremes for every scene/point/checklist; timing: every event of story i inside [start,end]; transition starts at part start = `recap_events` whoosh + lead (fake kit); no caption starts inside a transition; points monotonic and after transition; checklist after points and before part end or omitted; fallback story → auto scenes cover window.
- Unchanged: Shorts goldens (`test_shorts_ass_golden`, `test_sound_regression`) and the classic golden.
- Real-ffmpeg smoke `scripts/test_recap_newsroom.py`: ~30 s (intro; story 1 terminal+flow+CVE card+points+checklist; story 2 stat+timeline+scam; outro), tone audio; checks 1920x1080, duration ±0.35 s, frames = fps×dur ±1; saves PNGs at intro, mid-transition glitch, terminal mid-typing, flow mid-draw, checklist tick, stat count-up, timeline, scam, outro; prints wall time, fps, peak RSS (getrusage children), extrapolated 8-min time.

## Performance estimate (2-core LXC)
Final pass ~30–40 fps x264 veryfast on mostly static frames; libass ~2–6 ms/frame with 40–60 events → 8 min (14,400 frames) ≈ 7–10 min + 480p preview 2–3 min. Faster than today (no per-part encodes). RAM < ~500 MB. ~1,800–2,000 events total.
