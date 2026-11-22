"""Recap v2 "newsroom" visuals: the second LLM call and its (pure) validation.

Everything validating here never raises: a bad item is dropped and the reason is recorded in `dropped`.
The LLM may only use facts from the story material, and terminal scenes must stay harmless.
"""
from __future__ import annotations

import logging
import re
from collections import Counter

from . import captions, cvedata, tags as tagmod, writer

log = logging.getLogger("cybershorts")

SCENE_TYPES = ("terminal", "flow", "stat", "timeline", "scam")
DEFAULT_SCENES = list(SCENE_TYPES)

# Capitalised words that are ordinary vocabulary, not names: they never need to appear in the source.
GENERIC_WORDS = frozenset(w.lower() for w in """
Attacker Attackers Hacker Hackers Actor Actors Threat Server Servers Client Clients User Users Admin Admins
Victim Victims Target Targets Network Networks System Systems Host Hosts Device Devices Endpoint Endpoints
Disclosed Disclosure Patched Patch Patches Update Updates Updated Fixed Fix Fixes Released Release Exploited
Exploit Exploits Exploitation Vulnerable Vulnerability Vulnerabilities Flaw Flaws Bug Bugs Malware Ransomware
Phishing Scam Scams Message Messages Email Emails Text Link Links Click Account Accounts Password Passwords
Login Logins Logon Access Remote Local Code Data File Files Folder Folders Service Services Software Version
Versions Security Alert Alerts Warning Critical High Medium Low Severity Risk Fixed Attack Attacks Breach
Breaches Leak Leaked Stolen Stole Steal Credentials Credential Token Tokens Session Sessions Request Requests
Response Responses Traffic Connection Connections Connect Connected Connecting Scan Scans Scanning Scanned
Check Checks Checking Checked Run Running Runs Start Started Starting Stop Stopped Open Opened Close Closed
Read Reading Write Writing Send Sends Sent Sending Receive Received Receives Download Downloads Downloaded
Upload Uploads Uploaded Install Installs Installed Installing Enable Enabled Disable Disabled Review Reviewed
Reviewing Apply Applied Applying Restart Restarted Reboot Rebooted Reset Verify Verified Verifying Confirm
Confirmed Block Blocked Blocking Allow Allowed Deny Denied Denying Detect Detected Detecting Found Finding
Search Searching Log Logs Logging Logged Monitor Monitoring Monitored Report Reported Reports Contain
Contained Isolate Isolated Isolating Remove Removed Removing Revoke Revoked Rotate Rotated Rotating
Change Changed Changing Limit Limited Limiting Use Used Using Make Made Never Always Do Does Dont Not Now
Then Next First Second Third Last Step Steps Here There This That These Those What When Where Why Who How
The A An And Or But If So As At By For From In Into Of On To With Without Is Are Was Were Be Been It Its
Your You Our We They Them Their He She His Her My Me Us All Any Some No Yes Ok Okay New Old Latest Recent
Urgent Important Notice Support Team Service Help Desk Bank Customer Customers Employee Employees Company
Companies Vendor Vendors Provider Providers Agency Agencies Government Police Staff Manager Owner Owners
Hello Hi Dear Thanks Thank Please Sorry Welcome Free Win Prize Gift Refund Payment Payments Invoice Order
Package Delivery Parcel Shipping Tracking Failed Failure Success Successful Error Errors Denied Unknown
Suspicious Unusual Unauthorized Unauthorised Malicious Hidden Fake Real Secure Safe Unsafe Public Private
Internal External Online Offline Cloud Web Website Browser Browsers App Apps Application Applications
Mobile Phone Phones Windows Linux Admin Root Shell Command Commands Script Scripts Process Processes Memory
Disk Drive Database Databases Table Tables Config Configuration Settings Setting Policy Policies Rule Rules
Firewall Gateway Router Routers Port Ports Packet Packets Payload Backdoor Beacon Implant Dropper Loader
Exfiltration Lateral Movement Privilege Escalation Persistence Foothold Compromise Compromised Incident
Response Contained Breached Hacked Locked Encrypted Decrypted Decrypt Encrypt Ransom Demand Note Notes
Week Day Days
Asks Ask Asking Asked Pressure Pressures Pressuring Threatens Threatening Mismatched Misspelled Generic Odd
Strange Unexpected Vague Poor Spelling Grammar Urgency Deadline Pretends Claims Impersonates Unsolicited
Sender Senders Domain Domains Money Cards Card Personal Information Info Sensitive Demands Requests Offers
Console Terminal Prompt Output Input Dashboard Panel Timeline Flow Attack Chain Overview Summary
CVE CVSS SMS URL IP DNS OK ID PIN OTP MFA VPN API CPU RAM
""".split())

_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
           "oct": 10, "nov": 11, "dec": 12}
_MONTH_FULL = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
               "august": 8, "september": 9, "october": 10, "november": 11, "december": 12}
_MON = "|".join(sorted(list(_MONTH_FULL) + list(_MONTHS), key=len, reverse=True))
_MD_RE = re.compile(r"\b(%s)\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?!\s*(?:%%|percent))" % _MON, re.I)
_DM_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(%s)\b\.?" % _MON, re.I)
_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_VERSION_RE = re.compile(r"(?<![\w.])v?\d+(?:\.\d+){1,3}[a-z]?(?![\w]|\.\d)", re.I)
_NUM_RE = re.compile(r"(?<![\w.])\$?\d[\d,]*(?:\.\d+)?(?:\s*(?:%|percent|million|billion|thousand|trillion|bn|mn|[kmb])\b)?",
                     re.I)
_NAME_RE = re.compile(r"\b[A-Z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)*")
_UNITS = {"million": 10 ** 6, "billion": 10 ** 9, "thousand": 10 ** 3, "trillion": 10 ** 12,
          "mn": 10 ** 6, "m": 10 ** 6, "bn": 10 ** 9, "b": 10 ** 9, "k": 10 ** 3}


def _month(tok: str) -> int:
    t = tok.lower().rstrip(".")
    return _MONTH_FULL.get(t) or _MONTHS.get(t) or 0


def _source_dates(source: str) -> set[tuple[int, int]]:
    out: set[tuple[int, int]] = set()
    for m in _MD_RE.finditer(source):
        out.add((_month(m.group(1)), int(m.group(2))))
    for m in _DM_RE.finditer(source):
        out.add((_month(m.group(2)), int(m.group(1))))
    for m in _ISO_RE.finditer(source):
        out.add((int(m.group(2)), int(m.group(3))))
    return out


def _num_in(num: str, source_nc: str) -> bool:
    return re.search(r"(?<![\d.])%s(?!\d|\.\d)" % re.escape(num), source_nc) is not None


_ORDINAL_RE = re.compile(r"(?:\b(?:step|stage|phase|part|tip|rule|item|point|no|number)\.?|#)\s*$", re.I)
_WEEKDAY_RE = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?\b", re.I)


def _ordinal_before(text: str, pos: int) -> bool:
    return _ORDINAL_RE.search(text[max(0, pos - 12):pos]) is not None


def facts_ok(text: str, source: str) -> tuple[bool, str]:
    """Every checkable fact in `text` must appear in `source`. Returns (ok, reason)."""
    try:
        return _facts_ok(str(text or ""), str(source or ""))
    except Exception as e:  # noqa: BLE001 - never raise
        return False, f"fact check error {type(e).__name__}"


def _facts_ok(text: str, source: str) -> tuple[bool, str]:
    rest = text
    # CVE ids
    for m in cvedata.CVE_RE.finditer(rest):
        if not re.search(cvedata._cve_pattern(m.group(0)), source, re.I):
            return False, f"cve {m.group(0)} not in source"
    rest = cvedata.CVE_RE.sub(" ", rest)
    # ISO dates, then month/day dates
    sdates = _source_dates(source)
    for m in _ISO_RE.finditer(rest):
        y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
        if (mo, d) not in sdates or not re.search(r"(?<!\d)%s(?!\d)" % y, source):
            return False, f"date {m.group(0)} not in source"
    rest = _ISO_RE.sub(" ", rest)
    for rx, mi, di in ((_MD_RE, 1, 2), (_DM_RE, 2, 1)):
        for m in rx.finditer(rest):
            md = (_month(m.group(mi)), int(m.group(di)))
            if md not in sdates:
                return False, f"date {m.group(0).strip()} not in source"
        rest = rx.sub(" ", rest)
    # versions (exact); a "2.3 million" / "70.5%" / "$2.3" is a number, handled below
    blank = []
    for m in _VERSION_RE.finditer(rest):
        v = m.group(0)
        tail = rest[m.end():m.end() + 12].lower()
        if rest[max(0, m.start() - 1):m.start()] == "$" or re.match(r"\s*(%|percent|million|billion|thousand)", tail):
            continue
        core = v[1:] if v[:1] in "vV" else v
        if not re.search(r"(?<![\d.])%s(?!\d|\.\d)" % re.escape(core), source, re.I):
            return False, f"version {v} not in source"
        blank.append(m.span())
    for a, b in reversed(blank):
        rest = rest[:a] + " " + rest[b:]
    # numbers: commas normalised, digit boundaries, units must match
    source_nc = re.sub(r"(?<=\d),(?=\d{3}\b)", "", source)
    for m in _NUM_RE.finditer(rest):
        raw = m.group(0)
        mm = re.fullmatch(r"\$?(\d[\d,]*(?:\.\d+)?)(?:\s*(%|percent|million|billion|thousand|trillion|bn|mn|[kmb]))?",
                          raw.strip(), re.I)
        if not mm:
            continue
        num = mm.group(1).replace(",", "")
        unit = (mm.group(2) or "").lower()
        nxt = rest[m.end():m.end() + 1]
        if not unit and nxt.isalpha() and "$" not in raw:
            continue  # "2FA", "5G", "3D": a token, not a quantity
        if re.fullmatch(r"\d", num) and not unit and "$" not in raw and _ordinal_before(rest, m.start()):
            continue  # list ordinals ("step 3") are not facts; any other bare number must be in the source
        if unit in ("%", "percent"):
            ok = re.search(r"(?<![\d.])%s\s?(%%|percent|per cent)" % re.escape(num), source_nc, re.I) is not None
        elif unit:
            ok = re.search(r"(?<![\d.])%s\s?(million|billion|thousand|trillion|bn|mn|[kmb])\b" % re.escape(num),
                           source_nc, re.I) is not None
            if not ok:
                try:
                    ok = _num_in(str(int(round(float(num) * _UNITS[unit]))), source_nc)
                except (ValueError, KeyError):
                    ok = False
        else:
            ok = _num_in(num, source_nc)
        if not ok:
            return False, f"number {raw.strip()} not in source"
    for m in _WEEKDAY_RE.finditer(rest):
        if not re.search(r"\b%s" % re.escape(m.group(1)), source, re.I):
            return False, f"day {m.group(0)} not in source"
    # names / acronyms
    for m in _NAME_RE.finditer(rest):
        tok = m.group(0)
        low = tok.lower()
        if low in GENERIC_WORDS:
            continue
        cands = {tok}
        if low.endswith("s") and len(tok) > 3:
            cands.add(tok[:-1])
        if any(cvedata._word_in(c, source) for c in cands):
            continue
        if low.rstrip("s") in GENERIC_WORDS:
            continue
        return False, f"name {tok} not in source"
    return True, ""


# --- host / IOC + terminal safety ---------------------------------------------------
_TLDS = ("com|net|org|io|co|ru|cn|xyz|info|biz|gov|edu|dev|app|top|tk|ly|me|sh|cc|us|uk|de|fr|onion|local|"
         "internal|lan|exe|ps1|bat|dll|py|php|online|shop|site|live|club|icu|vip|ai|su|pw|zip|mov|cfd|sbs|"
         "store|tech|website|space|click|fun|world|cloud|"
         "digital|cyou|rest|buzz|monster|ml|ga|cf|gq|jp|kr|br|ir|kp|ua|pl|nl|es|ch|se|fi|"
         "au|ca|mx|za|ng|ke|ro|bg|tr|vn|ph|sg|hk|tw|eu|asia|pro|mobi|tel|bz|ws|gg|tv|fm|"
         "gl|xxx|bid|download|stream|loan|racing|science|gdn")
_DEFANG = [
    (re.compile(r"\s*[\[({]\s*(?:\.|dot)\s*[\])}]\s*", re.I), "."),
    (re.compile(r"\s*[\[({]\s*(?:@|at)\s*[\])}]\s*", re.I), "@"),
    (re.compile(r"\[\s*:\s*\]|\[\s*://\s*\]|\(\s*:\s*\)", re.I), ":"),
    (re.compile(r"\bh(?:xx|tt)p(s?)\b", re.I), r"http\1"),
    (re.compile(r"\bf(?:x|t)p\b", re.I), "ftp"),
]
_HOST_RX: list[tuple[str, re.Pattern]] = [
    ("url", re.compile(r"://|\bwww\.|\bhttps?\b|\bftp\b", re.I)),
    ("ipv4", re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")),
    ("ipv6", re.compile(r"(?i)(?:\b[0-9a-f]{0,4}::[0-9a-f:]*|\b(?:[0-9a-f]{1,4}:){4,7}[0-9a-f]{1,4}\b)")),
    ("email", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("at-sign", re.compile(r"\S@\S")),
    ("domain", re.compile(r"(?<![\w-])[a-z0-9][\w-]*(?:\.[\w-]+)*\.(?:%s)(?![\w-])" % _TLDS, re.I)),
]


def host_ioc(text: str) -> str | None:
    """Reason when `text` holds a host/IOC (URL, domain, IP, email, also defanged), else None."""
    s = str(text or "")
    for rx, rep in _DEFANG:
        s = rx.sub(rep, s)
    for why, rx in _HOST_RX:
        if rx.search(s):
            return why
    return None


_SHELLWORDS = (r"nmap|masscan|msf\w*|metasploit|meterpreter|sqlmap|hydra|hashcat|mimikatz|nc|ncat|netcat|curl|wget|"
               r"powershell|pwsh|certutil|iex|base64|sudo|su|doas|chmod|chown|chgrp|dd|mkfs\w*|mount|umount|ssh|scp|"
               r"sftp|ftp|tftp|telnet|rsync|socat|psexec|wmic|vssadmin|schtasks|bcdedit|netsh|wevtutil|diskpart|"
               r"icacls|takeown|runas|taskkill|cipher|useradd|usermod|userdel|passwd|visudo|crontab|systemctl|"
               r"iptables|ufw|xp_cmdshell|reg\.exe|rundll32|regsvr32|mshta|cscript|wscript|bitsadmin|"
               r"tcpdump|tshark|aircrack\w*|nikto|gobuster|ffuf|dirb|responder|bloodhound|chisel|ngrok")
_UNSAFE: list[tuple[str, re.Pattern]] = [
    ("ipv4", dict(_HOST_RX)["ipv4"]),
    ("ipv6", dict(_HOST_RX)["ipv6"]),
    ("url", re.compile(r"://|\bwww\.|\bhxxp|\bhttps?\b", re.I)),
    ("domain", dict(_HOST_RX)["domain"]),
    ("email", dict(_HOST_RX)["email"]),
    ("at-sign", dict(_HOST_RX)["at-sign"]),
    ("defanged", re.compile(r"[\[({]\s*(?:\.|dot|@|at)\s*[\])}]", re.I)),
    ("metachar", re.compile(r"\||;|&&|`|\$\(|[<>]")),
    ("variable", re.compile(r"\$\{|\$[A-Za-z_]|%[A-Za-z_]+%")),
    ("hex escape", re.compile(r"\\x[0-9a-fA-F]{2}")),
    ("backslash", re.compile(r"\\")),
    ("base64", re.compile(r"[A-Za-z0-9+/=]{24,}")),
    ("tool", re.compile(r"(?<![\w-])(?:%s)(?![\w-])" % _SHELLWORDS, re.I)),
    ("tool", re.compile(r"cmd\.exe|/dev/tcp|invoke-[\w-]+|(?<![\w-])(?:reg|net|sc|netsh|wmic)\s+"
                        r"(?:add|delete|query|import|export|save|user|localgroup|use|share|create|config|start|stop"
                        r"|advfirewall)\b", re.I)),
    ("path", re.compile(r"~[/\\]|(?<![\w])(?:/bin/|/dev/|/etc/|/usr/|/var/|/tmp/|/proc/|/sys/|/root\b|/home/|/opt/)|"
                        r"(?:^|\s)/[A-Za-z?]|\b[A-Za-z]:[/\\]|\.\./", re.I)),
    ("rm", re.compile(r"(?<![\w-])rm\s+(?:-|[/~.*])|(?<![\w-])(?:rmdir|del|erase|shred)\s+(?:-|/|[~.*])", re.I)),
    ("interpreter", re.compile(r"(?<![\w-])(?:perl|ruby|python\d?|php|node|nodejs|sh|bash|zsh|ksh|dash|lua|awk|"
                               r"tclsh|jjs|cscript|wscript|osascript)\s+-[A-Za-z]", re.I)),
    ("exec", re.compile(r"(?<![\w-])(?:exec|eval|system|popen|os\.system|subprocess)\s*[(\s'\"]", re.I)),
    ("password", re.compile(r"(?<![\w-])(?:pass(?:word|wd)?|pwd|secret|token|api[_-]?key)\s*[=:]|--?pass(?:word)?\b",
                            re.I)),
    ("password", re.compile(r"p[a@4]s{1,2}w[o0]r?d|\b(?:admin|root|user)\s*[:/]\s*\S+", re.I)),
]


def unsafe_terminal(line: str) -> str | None:
    """Reason a dramatized terminal line is unsafe, or None when it is fine."""
    s = str(line or "")
    for why, rx in _UNSAFE:
        if rx.search(s):
            return why
    return host_ioc(s)


# --- helpers --------------------------------------------------------------------
_PHONE_RE = re.compile(r"\d(?:[\s().-]?\d){6,}")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
# bidi embeddings/overrides/isolates, zero-width and invisible format characters: removed, not spaced
_BIDI_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\u00ad\u061c\u180e]")


def _clean_str(v) -> str:
    if not isinstance(v, str):
        return ""
    return re.sub(r"\s+", " ", _CTRL_RE.sub(" ", _BIDI_RE.sub("", v))).strip()


def _fit(v, cap: int):
    """(text, '') trimmed to `cap`, or (None, reason). Longer than 1.5x cap drops, empty drops."""
    s = _clean_str(v)
    if not s:
        return None, "empty"
    if len(s) > int(cap * 1.5):
        return None, f"too long ({len(s)}>{int(cap * 1.5)})"
    if len(s) > cap:
        cut = s[:cap - 1]
        if " " in cut and not s[cap - 1:cap].isspace():
            cut = cut[:cut.rfind(" ")]
        s = cut.rstrip(" ,;:-") + "…"
    return s, ""


def _cap(cfg, key: str, default):
    try:
        v = cfg.get(f"recap.newsroom.{key}", default)
    except Exception:  # noqa: BLE001
        return default
    return default if v is None else v


def _as_int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _paragraphs(narration: str) -> list[str]:
    return tagmod.split_paragraphs(tagmod.strip_tags(narration or ""))


def _toks(text: str) -> list[str]:
    return [n for t in str(text or "").split() if (n := captions._norm(t))]


_STOPWORDS = frozenset("""
a about above after again all also am an and any are as at be because been before being below between both but by
can could did do does doing down during each few for from further had has have having he her here hers him his how
i if in into is it its just me more most my no nor not now of off on once only or other our out over own same she
should so some such than that the their them then there these they this those through to too under until up very
was we were what when where which while who whom why will with would you your
""".split())


def keyword_set(keywords) -> set[str]:
    """Normalised caption-highlight tokens for `captions._karaoke_events`. Stopwords and tokens of 2 chars or
    fewer are dropped so "the patch" highlights "patch" but not every "the"."""
    out = set()
    for k in keywords or []:
        for t in str(k).split():
            n = captions._norm(t)
            if len(n) > 2 and n not in _STOPWORDS:
                out.add(n)
    return out


# --- cue timing -----------------------------------------------------------------
def story_word_ranges(words, script: str, para_counts):
    """Per part (para_counts order), the (start, end_exclusive) word-index range of each paragraph.
    None when words don't line up 1:1 with the script's words."""
    try:
        counts = captions.beat_ranges(script)
        if not words or not para_counts or sum(para_counts) != len(counts) or len(words) != sum(counts):
            return None
        out, idx, pos = [], 0, 0
        for c in para_counts:
            part = []
            for n in counts[idx:idx + c]:
                part.append((pos, pos + n))
                pos += n
            out.append(part)
            idx += c
        return out
    except Exception:  # noqa: BLE001
        return None


def _find_exact(toks, wt, a, b):
    L = len(toks)
    for i in range(a, b - L + 1):
        if wt[i:i + L] == toks:
            return i
    return None


def _find_fuzzy(toks, wt, a, b):
    L = len(toks)
    if L == 0 or b - a < 1:
        return None
    want = Counter(toks)
    best, best_i = 0.0, None
    for i in range(a, max(a + 1, b - L + 1)):
        win = wt[i:i + L]
        score = sum((Counter(win) & want).values()) / L
        if score > best:
            best, best_i = score, i
    return best_i if best >= 0.6 else None


def cue_time(cue: str, para: int, words, ranges):
    """Seconds at which `cue` is spoken. `ranges` is one part's paragraph ranges (see story_word_ranges),
    `para` is 1-based. Exact in paragraph, exact in the part, fuzzy (>=60% tokens), paragraph start + 0.4 s;
    None when the paragraph doesn't exist."""
    try:
        if not words or not ranges or not isinstance(para, int) or not 1 <= para <= len(ranges):
            return None
        a, b = ranges[para - 1]
        if not 0 <= a < len(words):
            return None
        wt = [captions._norm(w[2]) for w in words]
        toks = _toks(cue)
        if toks:
            lo, hi = ranges[0][0], min(ranges[-1][1], len(words))
            for x, y in ((a, b), (lo, hi)):
                i = _find_exact(toks, wt, x, min(y, len(words)))
                if i is not None:
                    return float(words[i][0])
            for x, y in ((a, b), (lo, hi)):
                i = _find_fuzzy(toks, wt, x, min(y, len(words)))
                if i is not None:
                    return float(words[i][0])
        return float(words[a][0]) + 0.4
    except Exception:  # noqa: BLE001
        return None


# --- prompt / parse ---------------------------------------------------------------
def _enabled_scenes(cfg) -> list[str]:
    raw = _cap(cfg, "scenes", DEFAULT_SCENES)
    if not isinstance(raw, (list, tuple)):
        raw = DEFAULT_SCENES
    out = [s for s in (str(x).strip().lower() for x in raw) if s in SCENE_TYPES]
    return list(dict.fromkeys(out))


def _numbered(narration: str) -> str:
    return "\n".join(f"[P{i}] {p}" for i, p in enumerate(_paragraphs(narration), 1))


def prompt(cfg, stories: list[dict], data: dict) -> list[dict]:
    """Chat messages for the visuals call: each story's source material and numbered final narration."""
    from . import recap

    scenes = _enabled_scenes(cfg)
    segs = (data or {}).get("segments") or []
    n = len(stories)
    blocks = []
    for i, s in enumerate(stories):
        narr = str(segs[i].get("narration", "")) if i < len(segs) and isinstance(segs[i], dict) else ""
        blocks.append(f"[Story {i + 1}]\nSOURCE MATERIAL:\n{recap.source_text(cfg, s)}\n\n"
                      f"NARRATION (final, numbered paragraphs):\n{_numbered(narr)}")
    system = (
        "You design the on-screen graphics for a weekly IT and security news recap video. The narration is "
        "already written. You pick short keywords, key points and a few illustrative scenes that match it.\n"
        "Strict rules:\n"
        "- Use ONLY facts from the story material: no invented numbers, names, CVE ids, versions or dates.\n"
        "- Terminal lines are clearly dramatized, generic and harmless. They must NEVER contain real commands "
        "or tools, IP addresses, domains, URLs, email addresses, payloads or code. No pipes, semicolons, "
        "redirects, backticks, or encoded strings.\n"
        "- A scam message body uses {link} wherever a link would be, and contains no real phone numbers or "
        "email addresses.\n"
        "- Every cue is 3 to 8 consecutive words copied exactly from the numbered paragraph given in 'para'.\n"
        "- Keep every text short; respect the character limits.\n"
        "Reply with JSON only, no prose."
    )
    maxkp = _as_int(_cap(cfg, "max_key_points", 4), 4)
    schema = (
        '{"stories": [ {"story_index": <1-%d>,\n'
        '  "keywords": [2-5 short phrases copied from the narration],\n'
        '  "key_points": [{"text": "<=38 chars", "para": 1, "cue": "3-8 consecutive narration words"}]  (2-%d),\n'
        '  "checklist": [{"text": "<=40 chars, imperative", "para": 1, "cue": "..."}]  (0-3; [] if not actionable),\n'
        '  "scenes": [ {"type": T, "para": 1, "cue": "...", ...fields} ]  (2-3 scenes)} ] }\n'
        "Scene types allowed: %s.\n"
        "Scene fields by type:\n"
        '- terminal: "title" (<=30), "lines": [{"kind": "cmd"|"out"|"alert", "text": <=44 chars}] (3-7, at least '
        'one cmd; the renderer adds "$ ")\n'
        '- flow: "title" (<=28), "nodes": [{"label": <=14, "sub": <=22}] (2-4), "arrows": [labels <=14] '
        '(nodes minus 1), optional "fixed_in" (a version from the material)\n'
        '- stat: "value" (exactly as written in the material, e.g. "4,000", "70%%", "$2.3 million"), "label" '
        '(<=40), optional "context" (<=40)\n'
        '- timeline: "events": [{"date": as written in the material, "label": <=18}] (2-4)\n'
        '- scam: "channel": "sms"|"email", "sender": <=24, optional "subject", "body": <=140 with {link} for '
        'links, "flags": up to 3 red flags <=28 chars each\n'
    ) % (n, max(2, maxkp), ", ".join(scenes))
    user = "\n\n".join(blocks) + "\n\nReturn JSON in this shape:\n" + schema
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse(raw: str, n: int) -> list[dict]:
    """Per-story raw dicts, length n (index = story - 1); missing stories become {}. ValueError on bad JSON."""
    obj = writer.extract_json(raw)
    items = obj.get("stories", obj.get("segments"))
    if not isinstance(items, list):
        raise ValueError("visuals JSON has no 'stories' list")
    out: list[dict] = [{} for _ in range(max(0, n))]
    seen = False
    for pos, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        try:
            idx = int(it.get("story_index"))
        except (TypeError, ValueError):
            idx = pos + 1
        if 1 <= idx <= n and not out[idx - 1]:
            out[idx - 1] = it
            seen = True
    if not seen and n:
        raise ValueError("visuals JSON has no usable story entries")
    return out


# --- cleaning ---------------------------------------------------------------------
def _card_text(card) -> str:
    if not isinstance(card, dict):
        return ""
    bits = [str(card.get("cve_id") or "")]
    if card.get("cvss") is not None:
        try:
            bits.append("%.1f" % float(card["cvss"]))
        except (TypeError, ValueError):
            pass
    bits.append(str(card.get("severity") or ""))
    bits.extend(str(p) for p in (card.get("products") or []))
    return " ".join(b for b in bits if b)


class _Ctx:
    def __init__(self, source: str, narration: str):
        self.source = source
        self.narration = narration
        self.nparas = max(1, len(_paragraphs(narration)))
        self.dropped: list[str] = []

    def drop(self, what: str, why: str) -> None:
        self.dropped.append(f"{what}: {why}")

    def text(self, v, cap: int, what: str, *, facts: bool = True):
        s, why = _fit(v, cap)
        if s is None:
            self.drop(what, why)
            return None
        # the host check also sees the untrimmed text: a cap must not cut a domain off
        bad = host_ioc(_clean_str(v).replace("{link}", " ")) or host_ioc(s.replace("{link}", " "))
        if bad:
            self.drop(what, f"host/ioc ({bad})")
            return None
        if facts:
            ok, why = facts_ok(s, self.source)
            if not ok:
                self.drop(what, why)
                return None
        return s

    def anchor(self, it: dict, what: str):
        """(para, cue) for an item, or None."""
        try:
            para = int(it.get("para"))
        except (TypeError, ValueError):
            self.drop(what, "bad para")
            return None
        if not 1 <= para <= self.nparas:
            self.drop(what, f"para {para} out of range")
            return None
        cue = " ".join(_clean_str(it.get("cue")).split()[:8])
        return para, cue


def _items(raw, key: str) -> list:
    v = raw.get(key) if isinstance(raw, dict) else None
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _text_items(ctx: _Ctx, raw, key: str, cap: int, limit: int) -> list[dict]:
    out = []
    for i, it in enumerate(_items(raw, key), 1):
        what = f"{key} {i}"
        t = ctx.text(it.get("text"), cap, what)
        a = ctx.anchor(it, what) if t else None
        if t and a:
            out.append({"text": t, "para": a[0], "cue": a[1]})
    return out[:limit]


def _terminal(ctx: _Ctx, sc: dict, what: str):
    title = ctx.text(sc.get("title"), 30, what + " title")
    if not title:
        return None
    bad = unsafe_terminal(title)
    if bad:
        ctx.drop(what + " title", f"unsafe ({bad})")
        return None
    lines = []
    raw_lines = sc.get("lines") if isinstance(sc.get("lines"), list) else []
    for j, ln in enumerate(raw_lines, 1):
        if not isinstance(ln, dict):
            continue
        lw = f"{what} line {j}"
        kind = str(ln.get("kind") or "").lower()
        if kind not in ("cmd", "out", "alert"):
            ctx.drop(lw, "bad kind")
            continue
        s, why = _fit(ln.get("text"), 44)
        if s is None:
            ctx.drop(lw, why)
            continue
        bad = unsafe_terminal(_clean_str(ln.get("text"))) or unsafe_terminal(s)
        if bad:
            ctx.drop(lw, f"unsafe ({bad})")
            continue
        bad = host_ioc(_clean_str(ln.get("text"))) or host_ioc(s)
        if bad:
            ctx.drop(lw, f"host/ioc ({bad})")
            continue
        ok, why = facts_ok(s, ctx.source)
        if not ok:
            ctx.drop(lw, why)
            continue
        lines.append({"kind": kind, "text": s})
    lines = lines[:7]
    if len(lines) < 3 or not any(x["kind"] == "cmd" for x in lines):
        ctx.drop(what, "terminal needs >=3 safe lines incl. a cmd")
        return None
    return {"title": title, "lines": lines}


def _flow(ctx: _Ctx, sc: dict, what: str):
    title = ctx.text(sc.get("title"), 28, what + " title")
    if not title:
        return None
    nodes = []
    for j, nd in enumerate(sc.get("nodes") if isinstance(sc.get("nodes"), list) else [], 1):
        if not isinstance(nd, dict):
            continue
        label = ctx.text(nd.get("label"), 14, f"{what} node {j}")
        if not label:
            continue
        sub = ""
        if _clean_str(nd.get("sub")):
            sub = ctx.text(nd.get("sub"), 22, f"{what} node {j} sub") or ""
        nodes.append({"label": label, "sub": sub})
    nodes = nodes[:4]
    if len(nodes) < 2:
        ctx.drop(what, "flow needs >=2 valid nodes")
        return None
    raw_arrows = sc.get("arrows") if isinstance(sc.get("arrows"), list) else []
    arrows = []
    for j in range(len(nodes) - 1):
        a = ""
        if j < len(raw_arrows) and _clean_str(raw_arrows[j]):
            a = ctx.text(raw_arrows[j], 14, f"{what} arrow {j + 1}") or ""
        arrows.append(a)
    out = {"title": title, "nodes": nodes, "arrows": arrows}
    if _clean_str(sc.get("fixed_in")):
        fx = ctx.text(sc.get("fixed_in"), 16, what + " fixed_in")
        if fx and re.search(r"\d", fx):
            out["fixed_in"] = fx
        elif fx:
            ctx.drop(what + " fixed_in", "no version")
    return out


def _stat(ctx: _Ctx, sc: dict, what: str):
    value = _clean_str(sc.get("value"))
    if not value or len(value) > 18 or not re.search(r"\d", value):
        ctx.drop(what, "bad stat value")
        return None
    bad = host_ioc(value)
    ok, why = facts_ok(value, ctx.source)
    if bad or not ok:
        ctx.drop(what, f"host/ioc ({bad})" if bad else why)
        return None
    if not re.search(r"\d{2}|\d[.,]\d|%|\$|million|billion", value):
        ctx.drop(what, "stat value too trivial")
        return None
    label = ctx.text(sc.get("label"), 40, what + " label")
    if not label:
        return None
    out = {"value": value, "label": label}
    if _clean_str(sc.get("context")):
        c = ctx.text(sc.get("context"), 40, what + " context")
        if c:
            out["context"] = c
    return out


def _timeline(ctx: _Ctx, sc: dict, what: str):
    events = []
    for j, ev in enumerate(sc.get("events") if isinstance(sc.get("events"), list) else [], 1):
        if not isinstance(ev, dict):
            continue
        date = ctx.text(ev.get("date"), 14, f"{what} event {j} date")
        label = ctx.text(ev.get("label"), 18, f"{what} event {j}") if date else None
        if date and label:
            events.append({"date": date, "label": label})
    events = events[:4]
    if len(events) < 2:
        ctx.drop(what, "timeline needs >=2 events")
        return None
    return {"events": events}


def _scam(ctx: _Ctx, sc: dict, what: str):
    channel = str(sc.get("channel") or "").lower()
    if channel not in ("sms", "email"):
        ctx.drop(what, "bad channel")
        return None

    def risky(s: str) -> str | None:
        t = s.replace("{link}", " ")
        if _PHONE_RE.search(t):
            return "phone-like number"
        for why in ("email", "at-sign", "url", "domain"):
            rx = dict(_UNSAFE)[why]
            if rx.search(t):
                return why
        return None

    parts: dict[str, str] = {}
    for key, cap, facts in (("sender", 24, True), ("subject", 40, True), ("body", 140, True)):
        if key == "subject" and not _clean_str(sc.get(key)):
            continue
        s, why = _fit(sc.get(key), cap)
        if s is None:
            ctx.drop(f"{what} {key}", why)
            return None
        r = risky(_clean_str(sc.get(key))) or risky(s)
        if r:
            ctx.drop(what, f"scam {key} has {r}")
            return None
        bad = host_ioc(_clean_str(sc.get(key)).replace("{link}", " ")) or host_ioc(s.replace("{link}", " "))
        if bad:
            ctx.drop(what, f"scam {key} has host/ioc ({bad})")
            return None
        if facts:
            ok, why = facts_ok(s.replace("{link}", " "), ctx.source)
            if not ok:
                ctx.drop(what, why)
                return None
        parts[key] = s
    flags = []
    for j, f in enumerate(sc.get("flags") if isinstance(sc.get("flags"), list) else [], 1):
        if risky(_clean_str(f)):
            ctx.drop(f"{what} flag {j}", "unsafe")
            continue
        s = ctx.text(f, 28, f"{what} flag {j}")
        if s is None:
            continue
        flags.append(s)
    out = {"channel": channel, "sender": parts["sender"], "body": parts["body"], "flags": flags[:3]}
    if "subject" in parts:
        out["subject"] = parts["subject"]
    return out


_SCENE_FNS = {"terminal": _terminal, "flow": _flow, "stat": _stat, "timeline": _timeline, "scam": _scam}


def clean_visuals(raw_seg, source: str, narration: str, cfg, *, card=None) -> dict:
    """Validate one story's raw visuals. Never raises. `source` is the story material; the confirmed
    card's values count as source too."""
    empty = {"keywords": [], "key_points": [], "checklist": [], "scenes": [], "dropped": []}
    try:
        return _clean(raw_seg, source, narration, cfg, card)
    except Exception as e:  # noqa: BLE001
        log.warning("clean_visuals failed: %s", type(e).__name__)
        empty["dropped"] = [f"all: {type(e).__name__}"]
        return empty


def _clean(raw, source, narration, cfg, card) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    ctx = _Ctx(f"{source or ''}\n{_card_text(card)}", str(narration or ""))
    ntoks = " " + " ".join(_toks(ctx.narration)) + " "

    keywords: list[str] = []
    kws = raw.get("keywords") if isinstance(raw.get("keywords"), list) else []
    for k in kws:
        s = _clean_str(k)
        kt = _toks(s)
        if not s or len(s) > 30 or not kt or f" {' '.join(kt)} " not in ntoks:
            ctx.drop(f"keyword {s[:20]!r}", "not in narration")
            continue
        if s.lower() not in [x.lower() for x in keywords]:
            keywords.append(s)
    keywords = keywords[:5]

    key_points: list[dict] = []
    if _cap(cfg, "key_points", True):
        limit = max(1, _as_int(_cap(cfg, "max_key_points", 4), 4))
        key_points = _text_items(ctx, raw, "key_points", 38, limit)
        if not key_points and _items(raw, "key_points"):
            ctx.drop("key_points", "none valid")

    checklist: list[dict] = []
    if _cap(cfg, "checklist", True):
        checklist = _text_items(ctx, raw, "checklist", 40, 3)
        if 0 < len(checklist) < 2:
            ctx.drop("checklist", "fewer than 2 valid items")
            checklist = []

    allowed = _enabled_scenes(cfg)
    scenes: list[dict] = []
    for i, sc in enumerate(_items(raw, "scenes"), 1):
        what = f"scene {i}"
        typ = str(sc.get("type") or "").lower()
        if typ not in allowed:
            ctx.drop(what, f"type {typ!r} not allowed")
            continue
        a = ctx.anchor(sc, what)
        if not a:
            continue
        body = _SCENE_FNS[typ](ctx, sc, f"{what} {typ}")
        if body is None:
            continue
        scenes.append({"type": typ, "para": a[0], "cue": a[1], **body})
    scenes = scenes[:3]

    return {"keywords": keywords, "key_points": key_points, "checklist": checklist, "scenes": scenes,
            "dropped": ctx.dropped}


# --- the call ---------------------------------------------------------------------
def write_recap_visuals(cfg, stories: list[dict], data: dict) -> list[dict]:
    """Second LLM call. One cleaned dict per story (with 1-based `story_index`); [] on any failure."""
    try:
        from . import recap

        n = len(stories)
        if not n:
            return []
        messages = prompt(cfg, stories, data)
        model = str(cfg.get("recap.model", "") or "").strip() or None
        raws = None
        for attempt in range(2):
            text = writer.chat(cfg, messages, model=model)
            try:
                raws = parse(text, n)
                break
            except ValueError as e:
                log.warning("Recap visuals parse failed (attempt %d): %s", attempt + 1, e)
        if raws is None:
            return []
        segs = (data or {}).get("segments") or []
        cards = (data or {}).get("cards") or []
        out = []
        for i, s in enumerate(stories):
            narr = str(segs[i].get("narration", "")) if i < len(segs) and isinstance(segs[i], dict) else ""
            card = cards[i] if i < len(cards) and isinstance(cards[i], dict) else None
            v = clean_visuals(raws[i], recap.source_text(cfg, s), tagmod.strip_tags(narr), cfg, card=card)
            v["story_index"] = i + 1
            out.append(v)
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("Recap visuals failed: %s", type(e).__name__)
        return []
