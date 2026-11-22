"""Breaking-news drafts: new CISA KEV entries and urgent RSS items. One draft per run."""
from __future__ import annotations

import datetime as dt
import difflib
import json
import logging
import re

from . import cvedata, db, news, pipeline, writer

log = logging.getLogger("cybershorts")

TITLE_SIMILARITY = 0.75
SEEDED_KEY = "kev_seeded"
_CVE_RE = re.compile(r"CVE-\d{4}-\d{4,7}", re.I)


# -- time helpers ------------------------------------------------------------
def _tz(cfg):
    name = cfg.get("breaking.timezone", "America/New_York") or "UTC"
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001  (missing tzdata or bad name)
        log.warning("Timezone %r unavailable; using UTC", name)
        return dt.timezone.utc


def _local_now(cfg, now: dt.datetime | None = None) -> dt.datetime:
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.astimezone(_tz(cfg))


def _hm(s) -> int | None:
    try:
        h, m = str(s).split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def in_quiet_hours(cfg, now: dt.datetime | None = None) -> bool:
    qh = cfg.get("breaking.quiet_hours") or []
    if not isinstance(qh, (list, tuple)) or len(qh) != 2:
        return False
    start, end = _hm(qh[0]), _hm(qh[1])
    if start is None or end is None or start == end:
        return False
    local = _local_now(cfg, now)
    cur = local.hour * 60 + local.minute
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end  # window crosses midnight


def local_midnight_utc_iso(cfg, now: dt.datetime | None = None) -> str:
    local = _local_now(cfg, now)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# -- candidates ---------------------------------------------------------------
def _seed_kev(cfg, conn, dry_run: bool) -> bool:
    """True if this run only seeded (or would have)."""
    if db.kv_get(conn, SEEDED_KEY):
        return False
    vulns = cvedata.fetch_kev(cfg)
    if not vulns:
        log.warning("Breaking: KEV unavailable, cannot seed yet")
        return True
    if dry_run:
        log.info("Breaking (dry run): first run would seed %d KEV entries and stop", len(vulns))
        return True
    db.mark_kev_seen(conn, [(v.get("cveID"), v.get("dateAdded")) for v in vulns if v.get("cveID")])
    db.kv_set(conn, SEEDED_KEY, "1")
    log.info("Breaking: seeded %d KEV entries; no draft on the first run", len(vulns))
    return True


def kev_candidates(cfg, conn) -> list[dict]:
    max_age = int(cfg.get("breaking.kev_max_age_days", 2))
    today = _local_now(cfg).date()
    seen = set(db.kev_seen_ids(conn))
    out = []
    for v in cvedata.fetch_kev(cfg):
        cve = str(v.get("cveID") or "").upper()
        if not cve or cve in seen:
            continue
        try:
            added = dt.date.fromisoformat(str(v.get("dateAdded")))
        except ValueError:
            continue
        if (today - added).days > max_age:
            continue
        out.append(v)
    out.sort(key=lambda v: (0 if str(v.get("knownRansomwareCampaignUse", "")).lower() == "known" else 1,
                            [-ord(c) for c in str(v.get("dateAdded"))], str(v.get("cveID"))))
    return out


def story_for_kev(v: dict, rss_stories: list[dict]) -> dict:
    cve = str(v.get("cveID")).upper()
    for s in rss_stories:
        if cve in (s.get("title", "") + " " + s.get("summary", "")).upper():
            return dict(s)
    title = "%s: %s %s vulnerability added to CISA's exploited list" % (
        cve, v.get("vendorProject", ""), v.get("product", ""))
    summary = " ".join(str(x) for x in (v.get("vulnerabilityName"), v.get("shortDescription"),
                                        v.get("requiredAction")) if x)
    return {"url": "https://nvd.nist.gov/vuln/detail/" + cve, "title": " ".join(title.split()),
            "source": "CISA KEV", "published": "%sT00:00:00Z" % v.get("dateAdded"), "summary": summary[:600],
            "article": ""}  # KEV text is complete; no fetch of the NVD page


def _age_minutes(published: str | None) -> float | None:
    try:
        ts = dt.datetime.strptime(published, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return None
    return (dt.datetime.now(dt.timezone.utc) - ts).total_seconds() / 60


def _parse_scores(text: str, n: int) -> list[int | None]:
    scores: list[int | None] = [None] * n
    m = re.search(r"\[.*\]", text or "", re.S)
    if not m:
        return scores
    try:
        arr = json.loads(m.group(0))
    except ValueError:
        return scores
    for i, x in enumerate(arr[:n]):
        try:
            scores[i] = max(0, min(10, int(round(float(x)))))
        except (TypeError, ValueError):
            pass
    return scores


def score_items(cfg, conn, items: list[dict], dry_run: bool) -> None:
    """One batched LLM call; stores each URL's urgency (1-10) so it is scored once."""
    if not items:
        return
    lines = "\n".join("%d. %s | %s" % (i + 1, s["title"], (s.get("summary") or "")[:200]) for i, s in enumerate(items))
    system = ("You rate cybersecurity headlines for urgency from 0 to 10. 9-10: actively exploited critical "
              "flaw, major outage or breach affecting many people right now. 7-8: serious, time-sensitive. "
              "0-4: routine, opinion, research or old news. Reply with only a JSON array of integers, one per "
              "headline, in order.")
    try:
        text = writer.chat(cfg, [{"role": "system", "content": system}, {"role": "user", "content": lines}],
                           temperature=0, model=cfg.get("breaking.urgency_model") or None)
    except Exception as e:  # noqa: BLE001
        log.warning("Urgency scoring failed: %s", type(e).__name__)
        return
    for s, sc in zip(items, _parse_scores(text, len(items))):
        s["_urgency"] = sc if sc is not None else 0
        if not dry_run:
            db.set_urgency(conn, s["url"], s["_urgency"])


def rss_candidates(cfg, conn, stories: list[dict], dry_run: bool) -> list[dict]:
    max_age = float(cfg.get("breaking.rss_max_age_minutes", 120))
    threshold = int(cfg.get("breaking.urgency_threshold", 8))
    fresh = []
    for s in stories:
        age = _age_minutes(s.get("published"))
        if age is None or age > max_age or db.story_used(conn, s["url"]):
            continue
        fresh.append(s)
    fresh = fresh[: int(cfg.get("breaking.max_score_items", 15))]
    todo = []
    for s in fresh:
        sc = db.get_urgency(conn, s["url"])
        if sc is None:
            todo.append(s)
        else:
            s["_urgency"] = sc
    score_items(cfg, conn, todo, dry_run)
    hot = [s for s in fresh if s.get("_urgency", 0) >= threshold]
    hot.sort(key=lambda s: s["_urgency"], reverse=True)
    return hot


CVE_USED_DAYS = 30
_BOILERPLATE_RE = re.compile(r"\bvulnerability added to cisa'?s exploited list\b|\badded to cisa'?s exploited list\b", re.I)


def _norm_title(title: str) -> str:
    t = _CVE_RE.sub(" ", title or "")
    t = _BOILERPLATE_RE.sub(" ", t)
    return " ".join(t.lower().replace(":", " ").split())


def _is_dup(conn, story: dict, cves: list[str], recent_titles: list[str], kev: dict | None = None) -> str | None:
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=CVE_USED_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    for c in cves:
        if db.cve_used(conn, c, since=since):
            return "CVE already used: " + c
    if db.story_used(conn, story["url"]):
        return "story already used"
    if kev:  # KEV titles share boilerplate; cve_used is the dedup there
        return None
    t = _norm_title(story.get("title") or "")
    if not t:
        return None
    for old in recent_titles:
        if difflib.SequenceMatcher(None, t, _norm_title(old or "")).ratio() >= TITLE_SIMILARITY:
            return "similar to recent title: " + old[:60]
    return None


# -- entry point ---------------------------------------------------------------
def run(cfg, dry_run: bool = False) -> int | None:
    """Make at most one breaking draft. Returns the draft id or None."""
    if not cfg.get("breaking.enabled", True):
        log.info("Breaking disabled")
        return None
    if cfg.get("voice.mode", "tts") == "record":
        log.info("Breaking: skipped (voice.mode is record; breaking drafts need TTS)")
        return None
    conn = db.connect(cfg)
    try:
        db.init(conn)
        use_kev = bool(cfg.get("breaking.kev", True))
        if use_kev and _seed_kev(cfg, conn, dry_run):
            return None  # seeding runs even in quiet hours
        if in_quiet_hours(cfg):
            log.info("Breaking: quiet hours")
            return None
        cap = int(cfg.get("breaking.daily_cap", 2))
        # count every breaking row (failed/rejected too) so persistent failures still trip the cap
        done = db.count_drafts_since(conn, local_midnight_utc_iso(cfg), kind="breaking", include_all=True)
        if done >= cap:
            log.info("Breaking: daily cap reached (%d/%d)", done, cap)
            return None
        if dry_run:
            return _pick_and_draft(cfg, conn, True)
        try:
            with pipeline.draft_lock(cfg, blocking=False):
                return _pick_and_draft(cfg, conn, False)
        except pipeline.Busy:
            log.info("Breaking: another draft is in progress; skipping")
            return None
    finally:
        conn.close()


def _pick_and_draft(cfg, conn, dry_run: bool) -> int | None:
    use_kev = bool(cfg.get("breaking.kev", True))
    use_rss = bool(cfg.get("breaking.rss_urgency", True))
    try:
        stories = news.fetch_candidates(cfg, conn)
    except Exception as e:  # noqa: BLE001
        log.warning("Breaking: feeds failed: %s", type(e).__name__)
        stories = []

    cands: list[tuple[dict, dict | None]] = []  # (story, kev entry)
    if use_kev:
        for v in kev_candidates(cfg, conn):
            cands.append((story_for_kev(v, stories), v))
    if use_rss:
        for s in rss_candidates(cfg, conn, stories, dry_run):
            cands.append((s, None))
    if not cands:
        log.info("Breaking: nothing new")
        return None

    recent = db.recent_used_titles(conn, 20)
    for story, kev in cands:
        cves = [str(kev.get("cveID")).upper()] if kev else []
        cves += [c.upper() for c in _CVE_RE.findall(story.get("title", "")) if c.upper() not in cves]
        reason = _is_dup(conn, story, cves, recent, kev)
        if reason:
            log.info("Breaking: skip %s (%s)", story["title"][:70], reason)
            if kev and not dry_run:
                db.mark_kev_seen(conn, [(kev["cveID"], kev.get("dateAdded"))])
            continue
        if dry_run:
            log.info("Breaking (dry run): would draft %s [%s]", story["title"][:90],
                     kev["cveID"] if kev else "rss urgency %s" % story.get("_urgency"))
            return None
        if kev:  # mark before drafting so a failing draft cannot loop every 30 minutes
            db.mark_kev_seen(conn, [(kev["cveID"], kev.get("dateAdded"))])
        else:  # same for RSS stories
            db.upsert_story(conn, story["url"], story["title"], story.get("source", ""),
                            story.get("published"), story.get("summary", ""))
            db.mark_used(conn, story["url"])
        if "article" not in story and cfg.get("news.fetch_full_text", True):
            # fetch once here; make_draft_for_story skips the fetch when "article" is present
            try:
                from . import article

                text = article.fetch_text(story["url"], int(cfg.get("news.max_article_chars", 6000)))
            except Exception as e:  # noqa: BLE001
                log.warning("Breaking: article fetch failed: %s", type(e).__name__)
                text = ""
            story = dict(story, article=text or "")
        story = {k: v for k, v in story.items() if not k.startswith("_")}
        draft_id = pipeline.make_draft_for_story(cfg, story, kind="breaking", kev=kev)
        log.info("Breaking draft #%s created: %s", draft_id, story["title"][:80])
        return draft_id
    return None
