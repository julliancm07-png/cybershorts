"""CVE facts: CISA KEV cache, NVD scores and the data-card confirmation logic.

Everything here is best effort: functions never raise, and API keys are never logged.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

import requests

log = logging.getLogger("cybershorts")

KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
NVD_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"
KEV_TTL_SECONDS = 6 * 3600
UA = "cybershorts/1.0"
CVE_RE = re.compile(r"CVE-\d{4}-\d{4,7}", re.I)

_EXPLOITED_RE = re.compile(
    r"actively exploited|exploited in the wild|under active exploitation|exploited as (a )?zero[- ]?day|"
    r"zero[- ]?day (attack|exploit)s?|under (active )?attack|exploitation (has been )?(observed|detected)|"
    r"being exploited", re.I)
_HEDGE_BEFORE_RE = re.compile(r"\b(could|can|may|might|would)\s+(also\s+)?(easily\s+)?(actively\s+)?be\s*$", re.I)
_NEGATION_RE = re.compile(r"\b(no|not|never|without|isn't|aren't|hasn't|haven't|yet to|unaware)\b|n't", re.I)
_PATCH_RE = re.compile(
    r"\b(patch(es|ed)?|fix(es|ed)?|hotfix(es)?|upgrade[sd]?|security updates?|software updates?|"
    r"updates? (is|are|was|were) (now )?(available|released)|fixed in|patch(es)? (was |were |is |are )?(released|available))\b", re.I)
_NO_PATCH_RE = re.compile(
    r"\bno (official |security |available )?(patch|fix|update|hotfix)|unpatched|not (yet )?(been )?(patched|fixed)|"
    r"(patch|fix|update)(es)? (is|are|was|were)? ?(not|n't) (yet )?(available|released)|awaiting (a |an )?(patch|fix)|"
    r"mitigations? (is|are) unavailable|discontinue use|"
    r"yet to (release|issue|publish) (a |an )?(patch|fix|update)|remains? unpatched|no fix", re.I)
_SEV_WORDS = ("critical", "high", "medium", "moderate", "low")
_SEV_RES = [
    re.compile(r"\b(critical|high|medium|moderate|low)[- ]severity", re.I),
    re.compile(r"severity (?:rating |level |score )?(?:of |is |as )?(critical|high|medium|moderate|low)\b", re.I),
    re.compile(r"\brated (?:as )?(critical|high|medium|moderate|low)\b", re.I),
    re.compile(r"\b(critical)\b (?:vulnerability|flaw|bug|security flaw|remote code)", re.I),
]


# -- KEV ---------------------------------------------------------------------
def _kev_cache_path(cfg) -> Path:
    return Path(cfg.path("kev.json"))


def _read_cache(p: Path) -> dict:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_cache(p: Path, d: dict) -> None:
    try:
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        tmp.replace(p)
    except OSError as e:
        log.warning("KEV cache write failed: %s", type(e).__name__)


def fetch_kev(cfg) -> list[dict]:
    """KEV vulnerabilities, cached 6 h at data/kev.json with ETag/If-Modified-Since. [] on failure."""
    try:
        p = _kev_cache_path(cfg)
        cache = _read_cache(p)
        vulns = cache.get("vulnerabilities") if isinstance(cache.get("vulnerabilities"), list) else []
        if vulns and time.time() - float(cache.get("fetched", 0) or 0) < KEV_TTL_SECONDS:
            return vulns
        headers = {"User-Agent": UA}
        if vulns and cache.get("etag"):
            headers["If-None-Match"] = cache["etag"]
        if vulns and cache.get("last_modified"):
            headers["If-Modified-Since"] = cache["last_modified"]
        try:
            r = requests.get(KEV_URL, headers=headers, timeout=30)
            if r.status_code == 304 and vulns:
                cache["fetched"] = time.time()
                _write_cache(p, cache)
                return vulns
            r.raise_for_status()
            data = r.json()
            fresh = data.get("vulnerabilities")
            if not isinstance(fresh, list):
                raise ValueError("no vulnerabilities list")
        except Exception as e:  # noqa: BLE001
            log.warning("KEV fetch failed (%s); using %s", type(e).__name__, "cache" if vulns else "nothing")
            return vulns
        _write_cache(p, {"fetched": time.time(), "etag": r.headers.get("ETag", ""),
                         "last_modified": r.headers.get("Last-Modified", ""), "vulnerabilities": fresh})
        return fresh
    except Exception as e:  # noqa: BLE001
        log.warning("KEV unavailable: %s", type(e).__name__)
        return []


def kev_entry(cfg, cve: str) -> dict | None:
    try:
        want = (cve or "").upper()
        for v in fetch_kev(cfg):
            if str(v.get("cveID", "")).upper() == want:
                return v
    except Exception as e:  # noqa: BLE001
        log.warning("KEV lookup failed: %s", type(e).__name__)
    return None


# -- NVD ---------------------------------------------------------------------
def _nvd_key(cfg) -> str:
    return getattr(cfg, "nvd_api_key", "") or os.environ.get("NVD_API_KEY", "") or ""


def _pick_metric(metrics: dict) -> dict:
    for key in ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        items = [m for m in (metrics.get(key) or []) if isinstance(m, dict) and isinstance(m.get("cvssData"), dict)]
        if not items:
            continue
        items.sort(key=lambda m: 0 if m.get("type") == "Primary" else 1)
        m = items[0]
        try:
            score = float(m["cvssData"]["baseScore"])
        except (KeyError, TypeError, ValueError):
            continue
        sev = m["cvssData"].get("baseSeverity") or m.get("baseSeverity") or ""
        return {"cvss": score, "severity": str(sev).upper()}
    return {}


def nvd_lookup(cfg, cve: str) -> dict:
    """{'cvss': float, 'severity': 'CRITICAL'} or {} when unavailable."""
    try:
        if not CVE_RE.fullmatch(cve or ""):
            return {}
        headers = {"User-Agent": UA}
        key = _nvd_key(cfg)
        if key:
            headers["apiKey"] = key
        r = requests.get(NVD_URL, params={"cveId": cve.upper()}, headers=headers, timeout=15)
        r.raise_for_status()
        vulns = r.json().get("vulnerabilities") or []
        if not vulns:
            return {}
        return _pick_metric(vulns[0].get("cve", {}).get("metrics", {}) or {})
    except Exception as e:  # noqa: BLE001
        log.warning("NVD lookup failed: %s", type(e).__name__)
        return {}


# -- confirmation (pure) -----------------------------------------------------
def _band(score: float) -> str:
    if score >= 9.0:
        return "CRITICAL"
    if score >= 7.0:
        return "HIGH"
    if score >= 4.0:
        return "MEDIUM"
    if score > 0:
        return "LOW"
    return ""


def _to_float(v):
    try:
        if isinstance(v, bool):
            return None
        f = float(v)
        return f if 0.0 <= f <= 10.0 else None
    except (TypeError, ValueError):
        return None


def _number_in(score: float, text: str) -> bool:
    return re.search(r"(?<![\d.])%s(?![\d])" % re.escape("%.1f" % score), text) is not None


def _source_severity(text: str) -> list[str]:
    found = []
    for rx in _SEV_RES:
        for m in rx.finditer(text):
            w = m.group(1).lower()
            w = "medium" if w == "moderate" else w
            if w not in found:
                found.append(w)
    return [w.upper() for w in found]


def _says_exploited(text: str) -> bool:
    for m in _EXPLOITED_RE.finditer(text):
        window = text[max(0, m.start() - 40): m.start()]
        if _NEGATION_RE.search(window) or _HEDGE_BEFORE_RE.search(window):
            continue
        if re.match(r"zero[- ]?day", m.group(0), re.I) and re.match(r"\W*initiative", text[m.end():m.end() + 20], re.I):
            continue
        return True
    return False


CONTEXT_CHARS = 200


def _cve_scope(source: str, cve: str) -> str:
    """Text whose severity/exploitation wording may be attributed to `cve`: everything when the source
    names only this CVE, otherwise just the windows around its mentions."""
    named = {m.upper() for m in CVE_RE.findall(source)}
    if named <= {cve}:
        return source
    parts = []
    for m in re.finditer(_cve_pattern(cve), source, re.I):
        parts.append(source[max(0, m.start() - CONTEXT_CHARS): m.end() + CONTEXT_CHARS])
    return "\n".join(parts)


def _cve_pattern(cve: str) -> str:
    return r"(?<![\w-])%s(?!\d)" % re.escape(cve)


def _word_in(name: str, text: str) -> bool:
    return re.search(r"(?<!\w)%s(?!\w)" % re.escape(name), text, re.I) is not None


def confirm_card(raw, source_text: str, kev: dict | None, nvd: dict | None) -> dict | None:
    """Keep only data-card values that source/KEV/NVD confirm. Pure; None if the CVE can't be confirmed."""
    if not isinstance(raw, dict):
        return None
    source = source_text or ""
    kev = kev if isinstance(kev, dict) else None
    nvd = nvd if isinstance(nvd, dict) else {}
    cve = str(raw.get("cve_id") or "").strip().upper()
    if not re.fullmatch(CVE_RE.pattern, cve) or not re.search(_cve_pattern(cve), source, re.I):
        return None
    scope = _cve_scope(source, cve)

    cvss = _to_float(nvd.get("cvss"))
    if cvss is None:
        guess = _to_float(raw.get("cvss"))
        if guess is not None and _number_in(guess, scope):
            cvss = guess
    severity = str(nvd.get("severity") or "").upper()
    if severity not in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
        severity = ""
    if not severity and cvss is not None:
        severity = _band(cvss)
    if not severity:
        want = str(raw.get("severity") or "").upper().replace("MODERATE", "MEDIUM")
        severity = want if want in _source_severity(scope) else ""

    kev_text = ""
    if kev:
        kev_text = " ".join(str(kev.get(k, "")) for k in ("vendorProject", "product")).lower()
    products = []
    raw_products = raw.get("products") if isinstance(raw.get("products"), list) else []
    for p in raw_products:
        name = str(p or "").strip()
        if name and (_word_in(name, scope) or (kev_text and _word_in(name, kev_text))) and name not in products:
            products.append(name)
    if not products and kev:
        name = str(kev.get("product") or "").strip()
        if name:
            products.append(name)
    products = products[:2]

    exploited = bool(kev) or _says_exploited(scope)
    patch = bool(raw.get("patch_available")) and bool(_PATCH_RE.search(scope)) and not _NO_PATCH_RE.search(scope)

    return {"cve_id": cve, "cvss": cvss, "severity": severity or None, "products": products,
            "exploited": exploited, "patch_available": patch}


def build_card(cfg, story: dict, raw) -> dict | None:
    """Look up KEV/NVD and confirm the LLM's card. None when disabled, unconfirmable or on any error."""
    try:
        if not cfg.get("data_card.enabled", True) or not isinstance(raw, dict):
            return None
        cve = str(raw.get("cve_id") or "").strip().upper()
        if not re.fullmatch(CVE_RE.pattern, cve):
            return None
        story = story or {}
        source = "\n".join(str(story.get(k) or "") for k in ("title", "summary", "article"))
        kev = kev_entry(cfg, cve)
        nvd = nvd_lookup(cfg, cve) if cfg.get("data_card.nvd", True) else {}
        return confirm_card(raw, source, kev, nvd)
    except Exception as e:  # noqa: BLE001
        log.warning("build_card failed: %s", type(e).__name__)
        return None
