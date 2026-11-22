"""Telegram alerts when a systemd job fails, and a note when it works again.

systemd starts cybershorts-alert@fail:<unit>.service (OnFailure=) or
cybershorts-alert@ok:<unit>.service (OnSuccess=). Repeat failures of the same job are
muted for alerts.repeat_minutes; a success after a failure sends one "working again" note.
A success with no earlier failure does nothing (no network call).
"""
from __future__ import annotations

import fcntl
import html
import json
import logging
import os
import re
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from .config import SECRET_KEYS, Config

log = logging.getLogger(__name__)

# (pattern, tip). First match wins. Patterns are matched case-insensitively on the log tail.
HINTS = [
    (r"No space left on device", "The disk is full. Check with: df -h"),
    (r"openrouter.{0,200}(402|insufficient|credits)|(402|insufficient credits).{0,200}openrouter",
     "OpenRouter is out of credits. Top up at openrouter.ai/settings/credits"),
    (r"invalid_grant|Token has been expired or revoked",
     "The YouTube login expired or was revoked. Re-run: cs scripts/auth_youtube.py"),
    (r"tiktok.{0,200}(refresh_token|access_token_invalid|token)",
     "The TikTok login may have expired. Re-run: cs scripts/auth_tiktok.py"),
    (r"quotaExceeded|quota.{0,40}exceeded", "An API quota ran out. It usually resets within a day."),
    (r"Temporary failure in name resolution|NameResolutionError|Failed to resolve|Network is unreachable",
     "The container could not reach the internet (DNS or network)."),
    (r"database is locked", "Another job held the database. It usually clears on the next run."),
]

RESULT_TIPS = {
    "timeout": "The job hit its time limit (TimeoutStartSec).",
    "oom-kill": "The job ran out of memory. The container has 2 GB.",
    "signal": "The job was killed by a signal.",
    "core-dump": "The job crashed (core dump).",
}

_TOKENISH = [
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),       # Telegram bot token
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),              # OpenRouter / similar
    re.compile(r"\bya29\.[A-Za-z0-9_.-]{20,}\b"),          # Google access token
    re.compile(r"\b1//[A-Za-z0-9_-]{20,}\b"),              # Google refresh token
    re.compile(r"(?i)(api[_-]?key|token|secret|passphrase)=([^\s&]+)"),
]


def scrub(cfg: Config, text: str) -> str:
    """Remove configured secret values and anything shaped like a token."""
    for key in SECRET_KEYS:
        val = getattr(cfg, key.lower(), "") or ""
        if isinstance(val, str) and len(val) >= 6 and key not in ("TELEGRAM_CHAT_ID", "TIKTOK_REDIRECT_URI"):
            text = text.replace(val, "***")
    for rx in _TOKENISH:
        if rx.groups == 2:
            text = rx.sub(lambda m: m.group(1) + "=***", text)
        else:
            text = rx.sub("***", text)
    return text


def pick_lines(lines: list[str], n: int) -> list[str]:
    """The most useful tail: from the last traceback on (capped), else the last n lines."""
    lines = [ln.rstrip() for ln in lines if ln.strip()]
    start = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith("Traceback (most recent call last)"):
            start = i
            break
    if start is not None:
        tb = lines[start:]
        if len(tb) > n:  # keep the head line and the end (where the error is)
            tb = [tb[0], "  ..."] + tb[-(n - 2):]
        # include the log line just before the traceback (usually "x failed")
        return ([lines[start - 1]] if start > 0 else []) + tb
    return lines[-n:]


def hint_for(text: str, result: str = "") -> str:
    if result in RESULT_TIPS:
        return RESULT_TIPS[result]
    for pattern, tip in HINTS:
        if re.search(pattern, text, re.IGNORECASE | re.DOTALL):
            return tip
    return ""


def _run(cmd: list[str], timeout: int = 20) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("%s failed: %s", cmd[0], e)
        return ""


def unit_info(unit: str) -> dict:
    out = _run(["systemctl", "show", unit, "-p", "Result", "-p", "ExecMainStatus", "-p", "InvocationID"])
    info = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k] = v.strip()
    return info


def recent_logs(unit: str, invocation: str = "") -> list[str]:
    if invocation:
        out = _run(["journalctl", f"_SYSTEMD_INVOCATION_ID={invocation}", "-o", "cat", "--no-pager", "-n", "400"])
        if out.strip():
            return out.splitlines()
    out = _run(["journalctl", "-u", unit, "-o", "cat", "--no-pager", "-n", "200", "--since", "-3h"])
    return out.splitlines()


@contextmanager
def _state(cfg: Config):
    """Locked read-modify-write of data/alerts.json."""
    path = cfg.path("alerts.json")
    lock = open(cfg.path("alerts.lock"), "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            if not isinstance(state, dict):
                state = {}
        except (OSError, ValueError):
            state = {}
        yield state
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _short(unit: str) -> str:
    return unit[:-len(".service")] if unit.endswith(".service") else unit


def _clock(ts: float) -> str:
    return time.strftime("%a %H:%M", time.localtime(ts))


def failure_message(cfg: Config, unit: str, info: dict, lines: list[str], count: int,
                    first_ts: float, repeat_minutes: int) -> str:
    n = int(cfg.get("alerts.log_lines", 25))
    tail = scrub(cfg, "\n".join(pick_lines(lines, n)))
    if len(tail) > 2800:
        tail = "..." + tail[-2800:]
    result = info.get("Result", "")
    status = info.get("ExecMainStatus", "")
    how = ", ".join(x for x in (result, f"exit {status}" if status not in ("", "0") else "") if x)
    name = _short(unit)
    parts = [f"\U0001f534 <b>{html.escape(name)} failed</b>" + (f" ({html.escape(how)})" if how else "")]
    if count > 1:
        parts.append(f"Failure #{count} since {_clock(first_ts)}.")
    tip = hint_for(tail, result)
    if tip:
        parts.append("\U0001f4a1 " + html.escape(tip))
    if tail:
        parts.append("<pre>" + html.escape(tail) + "</pre>")
    else:
        parts.append("(No log lines could be read.)")
    if name == "cybershorts-bot":
        parts.append("systemd restarts the bot automatically.")
    parts.append(f"More: <code>journalctl -u {html.escape(name)} -n 100</code>")
    parts.append(f"Repeat alerts for this job are muted for {repeat_minutes} min.")
    return "\n\n".join(parts)


def _send(cfg: Config, text: str) -> bool:
    if not (cfg.telegram_bot_token and cfg.telegram_chat_id):
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; cannot send alert")
        return False
    from .telegram import Telegram

    try:
        Telegram(cfg.telegram_bot_token, cfg.telegram_chat_id).send_message(text)
        return True
    except Exception as e:  # noqa: BLE001
        log.error("Could not send alert: %s", scrub(cfg, str(e)))
        return False


def handle(cfg: Config, mode: str, unit: str, *, now: float | None = None) -> bool:
    """mode is 'fail' or 'ok'. Returns False only if a message was due and could not be sent."""
    if not cfg.get("alerts.enabled", True):
        return True
    now = time.time() if now is None else now
    if not unit.endswith(".service"):
        unit += ".service"
    repeat = int(cfg.get("alerts.repeat_minutes", 180))

    if mode == "ok":
        with _state(cfg) as state:
            entry = state.pop(unit, None)
        if not entry:
            return True
        n = int(entry.get("count", 1))
        text = (f"✅ <b>{html.escape(_short(unit))} is working again</b>\n"
                f"It failed {n} time{'s' if n != 1 else ''} since {_clock(entry.get('first', now))}.")
        return _send(cfg, text)

    if mode != "fail":
        raise ValueError(f"unknown mode {mode!r}")
    with _state(cfg) as state:
        entry = state.get(unit) or {"first": now, "count": 0, "last_sent": None}
        entry["count"] = int(entry.get("count", 0)) + 1
        last = entry.get("last_sent")
        due = not last or now - float(last) >= repeat * 60
        if due:
            entry["last_sent"] = now  # claim it now so parallel alerts don't double-send
        state[unit] = entry
    if not due:
        log.info("%s failed again (#%d); alert muted", unit, entry["count"])
        return True
    info = unit_info(unit)
    lines = recent_logs(unit, info.get("InvocationID", ""))
    text = failure_message(cfg, unit, info, lines, entry["count"], float(entry["first"]), repeat)
    ok = _send(cfg, text)
    if not ok:
        with _state(cfg) as state:  # let the next failure try again
            if unit in state:
                state[unit]["last_sent"] = None
    return ok


def parse_instance(instance: str) -> tuple[str, str]:
    """'fail:cybershorts-draft' -> ('fail', 'cybershorts-draft.service')."""
    mode, sep, unit = instance.partition(":")
    if not sep or mode not in ("fail", "ok") or not unit:
        raise ValueError(f"expected fail:<unit> or ok:<unit>, got {instance!r}")
    if not unit.endswith(".service"):
        unit += ".service"
    return mode, unit


def send_test(cfg: Config) -> bool:
    return _send(cfg, "\U0001f9ea <b>Test alert</b>\nFailure alerts are set up. "
                      "If a cybershorts job fails you will get a message like this with its last log lines.")
