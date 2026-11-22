import json
import unittest
from pathlib import Path
from unittest import mock

import pytest

from cybershorts import recap_visuals as rv

FX = json.loads((Path(__file__).parent / "fixtures" / "recap_visuals.json").read_text(encoding="utf-8"))
SRC = FX["source"]
NARR = FX["narration"]


class Cfg:
    openrouter_api_key = "k"

    def __init__(self, **vals):
        self.vals = vals

    def get(self, dotted, default=None):
        return self.vals.get(dotted, default)


CFG = Cfg()


@pytest.mark.parametrize("text", [
    "4000 servers", "4,000 servers", "70%", "70 percent", "$2.3 million", "2.3 million",
    "Version 7.4.2", "patched in 7.4.3", "CVE-2026-1111", "cve-2026-1111",
    "Nov 12", "November 12", "12 November", "12th of November", "2026-11-12", "In 2026",
    "Attacker sends Server Update", "Patched and Disclosed", "Acme Gateway", "rated 9.8",
    "step 3", "no facts at all here",
])
def test_facts_accept(text):
    ok, why = rv.facts_ok(text, SRC)
    assert ok, why


@pytest.mark.parametrize("text", [
    "5,000 servers", "400 servers", "40000 servers", "71%", "$2.4 million", "$3.3 million", "7.4.9", "7.4",
    "CVE-2026-9999", "CVE-2026-11112", "Nov 13", "November 30", "13 November", "2026-11-13", "In 2019",
    "Zeta Gateway", "Lazarus", "Contoso", "70 million", "9.9",
])
def test_facts_reject(text):
    ok, why = rv.facts_ok(text, SRC)
    assert not ok and why


def test_facts_card_values_via_source():
    ok, _ = rv.facts_ok("CVSS 9.1 Contoso", SRC + "\nCVE-2026-1111 9.1 CRITICAL Contoso")
    assert ok


def test_generic_words_constant():
    assert {"attacker", "server", "disclosed", "patched", "update"} <= rv.GENERIC_WORDS
    assert "acme" not in rv.GENERIC_WORDS
    assert set(rv.SCENE_TYPES) == {"terminal", "flow", "stat", "timeline", "scam"}


@pytest.mark.parametrize("line", [
    "nmap target", "masscan range", "msfconsole", "msfvenom out", "sqlmap run", "hydra attack", "mimikatz dump",
    "nc listener", "ncat listener", "netcat listener", "curl something", "wget something", "powershell go",
    "cmd.exe /c", "bash -i now", "python -c boom", "/dev/tcp/x", "cat /etc/shadow", "cat /etc/passwd",
    "rm -rf stuff", "certutil decode", "iex payload", "Invoke-Thing", "base64 decode",
    "connect 10.0.0.1", "host 192.168.1.20 up", "peer fe80::1ff:fe23:4567", "peer 2001:db8:85a3:0:0:8a2e:370:7334",
    "open http://x", "ftp://x", "go evil.com", "see site.ru", "mail bob@corp", "mail bob@corp.org",
    "a | b", "a ; b", "a && b", "a || b", "a > b", "a < b", "a `b`", "a $(b)", r"\x41\x42", "QUJDREVGR0hJSktMTU5PUFFSU1RVVldY",
])
def test_unsafe_terminal(line):
    assert rv.unsafe_terminal(line), line


@pytest.mark.parametrize("line", [
    "probe gateway", "access granted", "ALERT: Server compromised", "scanning ports 3 of 9", "12:30:45 login", "ok",
])
def test_safe_terminal(line):
    assert rv.unsafe_terminal(line) is None, line


def seg(**kw):
    base = json.loads(json.dumps(FX["raw_segment"]))
    base.update(kw)
    return base


def clean(raw, cfg=CFG, **kw):
    return rv.clean_visuals(raw, SRC, NARR, cfg, **kw)


def term(lines, **kw):
    sc = {"type": "terminal", "para": 2, "cue": "run code", "title": "Console", "lines": lines}
    sc.update(kw)
    return sc


def test_clean_happy_path():
    v = clean(FX["raw_segment"])
    assert v["keywords"] == ["Acme Gateway", "four thousand servers"]
    assert [k["text"] for k in v["key_points"]] == ["Acme Gateway flaw exploited", "About 4,000 servers exposed"]
    assert len(v["checklist"]) == 2
    assert [s["type"] for s in v["scenes"]] == ["terminal", "stat"]
    assert v["scenes"][1]["value"] == "4,000"
    assert any("not said anywhere" in d for d in v["dropped"])


def test_clean_never_raises():
    for bad in (None, 5, "x", [], {"scenes": "no", "key_points": [1, None], "keywords": 3}):
        v = rv.clean_visuals(bad, SRC, NARR, CFG)
        assert set(v) == {"keywords", "key_points", "checklist", "scenes", "dropped"}
        assert v["scenes"] == []


def test_scene_type_limited_by_cfg():
    v = clean(FX["raw_segment"], Cfg(**{"recap.newsroom.scenes": ["stat"]}))
    assert [s["type"] for s in v["scenes"]] == ["stat"]


def test_terminal_needs_three_safe_lines_with_cmd():
    ok = [{"kind": "cmd", "text": "probe gateway"}, {"kind": "out", "text": "access granted"},
          {"kind": "alert", "text": "Server compromised"}]
    assert len(clean(seg(scenes=[term(ok)]))["scenes"]) == 1
    unsafe = [ok[0], ok[1], {"kind": "alert", "text": "curl evil"}]
    v = clean(seg(scenes=[term(unsafe)]))
    assert v["scenes"] == [] and any("unsafe" in d for d in v["dropped"])
    no_cmd = [{"kind": "out", "text": "a b"}, {"kind": "out", "text": "c d"}, {"kind": "alert", "text": "e f"}]
    assert clean(seg(scenes=[term(no_cmd)]))["scenes"] == []
    assert clean(seg(scenes=[term(ok[:2])]))["scenes"] == []
    # an invented fact in a line drops the line (then <3 lines)
    fact = [ok[0], ok[1], {"kind": "out", "text": "5,000 hosts hit"}]
    assert clean(seg(scenes=[term(fact)]))["scenes"] == []


def scam(**kw):
    sc = {"type": "scam", "para": 1, "cue": "serious flaw", "channel": "sms", "sender": "Acme Support",
          "body": "Your Acme account is locked. Verify now: {link}", "flags": ["Urgent tone", "Odd link"]}
    sc.update(kw)
    return sc


def test_scam_rules():
    cfg = CFG
    v = clean(seg(scenes=[scam()]))
    assert v["scenes"][0]["body"].endswith("{link}")
    for bad in (scam(body="Call 5551234567 now"), scam(body="Call 555 123 4567 now"), scam(body="Call 555-123-4567"),
                scam(body="Mail help@acme.com"), scam(body="Go to acme.com now"), scam(body="See http://a"),
                scam(channel="fax"), scam(sender="x" * 40), scam(body="x" * 300)):
        assert clean(seg(scenes=[bad]), cfg)["scenes"] == [], bad["body"]


def test_length_caps():
    kp = lambda t: {"text": t, "para": 1, "cue": "serious flaw"}  # noqa: E731
    v = clean(seg(key_points=[kp("Acme " * 8 + "x"), kp("Acme Gateway flaw"), kp("Acme " * 14)]))
    texts = [k["text"] for k in v["key_points"]]
    assert "Acme Gateway flaw" in texts and len(texts) == 2   # 70 chars (>57) dropped, 41 trimmed
    assert all(len(t) <= 38 for t in texts)
    assert any("too long" in d for d in v["dropped"])


def test_checklist_needs_two():
    one = [{"text": "Patch Acme Gateway today", "para": 3, "cue": "patch it today"}]
    v = clean(seg(checklist=one))
    assert v["checklist"] == [] and any("checklist" in d for d in v["dropped"])
    assert len(clean(seg(checklist=one * 2))["checklist"]) == 2
    assert clean(seg(checklist=one * 5))["checklist"].__len__() == 3
    assert clean(seg(checklist=[]))["checklist"] == []


def test_key_point_bad_para_and_fact():
    v = clean(seg(key_points=[{"text": "Acme flaw", "para": 9, "cue": "x"},
                              {"text": "Zeta flaw", "para": 1, "cue": "x"},
                              {"text": "Acme flaw", "para": "1", "cue": "a b c d e f g h i j"}]))
    assert len(v["key_points"]) == 1 and v["key_points"][0]["cue"].split() == list("abcdefgh")
    assert len([d for d in v["dropped"] if d.startswith("key_points")]) == 2


def test_flow_rules():
    base = {"type": "flow", "para": 2, "cue": "run code", "title": "How it works",
            "nodes": [{"label": "Attacker", "sub": "remote"}, {"label": "Gateway", "sub": "Acme 7.4.2"}],
            "arrows": ["sends request"]}
    v = clean(seg(scenes=[base]))
    assert v["scenes"][0]["arrows"] == ["sends request"] and "fixed_in" not in v["scenes"][0]
    one = dict(base, nodes=base["nodes"][:1])
    assert clean(seg(scenes=[one]))["scenes"] == []
    bad_nodes = dict(base, nodes=[{"label": "Zeta"}, {"label": "Gateway"}])
    assert clean(seg(scenes=[bad_nodes]))["scenes"] == []
    good = clean(seg(scenes=[dict(base, fixed_in="7.4.3")]))["scenes"][0]
    assert good["fixed_in"] == "7.4.3"
    badge = clean(seg(scenes=[dict(base, fixed_in="9.9.9")]))
    assert len(badge["scenes"]) == 1 and "fixed_in" not in badge["scenes"][0]
    assert any("fixed_in" in d for d in badge["dropped"])


def test_stat_and_timeline():
    st = lambda **k: {"type": "stat", "para": 2, "cue": "four thousand", "value": "4,000", "label": "servers", **k}  # noqa: E731
    assert clean(seg(scenes=[st()]))["scenes"][0]["value"] == "4,000"
    assert clean(seg(scenes=[st(value="5,000")]))["scenes"] == []
    assert clean(seg(scenes=[st(value="$2.3 million")]))["scenes"][0]["value"] == "$2.3 million"
    tl = {"type": "timeline", "para": 1, "cue": "serious flaw",
          "events": [{"date": "Nov 12", "label": "Disclosed"}, {"date": "7.4.3", "label": "Patched"}]}
    assert len(clean(seg(scenes=[tl]))["scenes"][0]["events"]) == 2
    tl["events"][0]["date"] = "Nov 20"
    assert clean(seg(scenes=[tl]))["scenes"] == []


def test_keywords_must_be_in_narration():
    v = clean(seg(keywords=["Gateway", "serious flaw", "logs", "Missing phrase", "gateway"]))
    assert v["keywords"] == ["Gateway", "serious flaw", "logs"]


def test_card_values_count_as_source():
    st = {"type": "stat", "para": 2, "cue": "x", "value": "9.1", "label": "CVSS score"}
    assert clean(seg(scenes=[st]))["scenes"] == []
    card = {"cve_id": "CVE-2026-1111", "cvss": 9.1, "severity": "CRITICAL", "products": ["Acme"]}
    assert len(clean(seg(scenes=[st]), card=card)["scenes"]) == 1


def test_keyword_set():
    assert {"acme", "gateway"} <= rv.keyword_set(["Acme Gateway"])
    assert rv.keyword_set([]) == set()


# --- timing -------------------------------------------------------------------
SCRIPT = "One two three four.\n\nFive six seven eight nine.\n\nTen eleven twelve."
WORDS = [(i * 0.5, i * 0.5 + 0.4, w) for i, w in enumerate(
    "One two three four. Five six seven eight nine. Ten eleven twelve.".split())]


def test_story_word_ranges():
    r = rv.story_word_ranges(WORDS, SCRIPT, [2, 1])
    assert r == [[(0, 4), (4, 9)], [(9, 12)]]
    assert rv.story_word_ranges(WORDS, SCRIPT, [2, 2]) is None
    assert rv.story_word_ranges(WORDS[:-1], SCRIPT, [2, 1]) is None
    assert rv.story_word_ranges([], SCRIPT, [2, 1]) is None


def test_cue_time():
    r = rv.story_word_ranges(WORDS, SCRIPT, [2, 1])[0]
    assert rv.cue_time("six seven eight", 2, WORDS, r) == pytest.approx(2.5)        # exact in paragraph
    assert rv.cue_time("Six, seven eight!", 2, WORDS, r) == pytest.approx(2.5)      # normalised
    assert rv.cue_time("two three", 2, WORDS, r) == pytest.approx(0.5)              # exact elsewhere in part
    assert rv.cue_time("six sevn eight nine", 2, WORDS, r) == pytest.approx(2.5)     # fuzzy 3/4 tokens
    assert rv.cue_time("banana kiwi", 2, WORDS, r) == pytest.approx(2.0 + 0.4)      # paragraph start + 0.4
    assert rv.cue_time("", 1, WORDS, r) == pytest.approx(0.4)
    assert rv.cue_time("six", 3, WORDS, r) is None and rv.cue_time("six", 0, WORDS, r) is None
    assert rv.cue_time("six", 1, [], r) is None and rv.cue_time("six", 1, WORDS, None) is None


# --- prompt / parse / call -------------------------------------------------------
def test_prompt_contents():
    cfg = Cfg(**{"recap.newsroom.scenes": ["terminal", "stat"]})
    msgs = rv.prompt(cfg, [FX["story_stub"]], FX["data"])
    text = "\n".join(m["content"] for m in msgs)
    assert msgs[0]["role"] == "system" and msgs[1]["role"] == "user"
    assert "[P1] Acme Gateway had" in text and "[P3] Patch it today" in text
    assert "CVE-2026-1111" in text and "{link}" in text and "ONLY facts" in text
    assert "NEVER contain real commands" in text and "terminal, stat" in text
    assert "timeline: " in text  # schema lists all fields; allowed types line restricts


def test_parse():
    raw = "```json\n" + json.dumps({"stories": [{"story_index": 2, "keywords": ["b"]},
                                                  {"story_index": 1, "keywords": ["a"]}]}) + "\n```"
    out = rv.parse(raw, 3)
    assert out[0]["keywords"] == ["a"] and out[1]["keywords"] == ["b"] and out[2] == {}
    for bad in ("nope", '{"x": 1}', '{"stories": [{"story_index": 9}]}'):
        with pytest.raises(ValueError):
            rv.parse(bad, 2)


GOOD = json.dumps({"stories": [FX["raw_segment"]]})


class WriteTests(unittest.TestCase):
    def run_write(self, replies, cfg=CFG):
        with mock.patch("cybershorts.writer.chat", side_effect=replies) as chat:
            out = rv.write_recap_visuals(cfg, [dict(FX["story_stub"])], FX["data"])
        return out, chat

    def test_success(self):
        out, chat = self.run_write([GOOD])
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["story_index"], 1)
        self.assertEqual(len(out[0]["scenes"]), 2)

    def test_model_override(self):
        _, chat = self.run_write([GOOD], Cfg(**{"recap.model": "x/y"}))
        self.assertEqual(chat.call_args.kwargs["model"], "x/y")

    def test_retry_once_then_ok(self):
        out, chat = self.run_write(["garbage", GOOD])
        self.assertEqual(chat.call_count, 2)
        self.assertEqual(len(out), 1)

    def test_retry_then_empty(self):
        out, chat = self.run_write(["garbage", "still garbage", GOOD])
        self.assertEqual(chat.call_count, 2)
        self.assertEqual(out, [])

    def test_chat_error_gives_empty(self):
        out, _ = self.run_write([RuntimeError("down")])
        self.assertEqual(out, [])

    def test_no_stories(self):
        with mock.patch("cybershorts.writer.chat") as chat:
            self.assertEqual(rv.write_recap_visuals(CFG, [], {}), [])
        chat.assert_not_called()


# --- review round: host/IOC, terminal denylist, scam sender, keywords, escapes -------------------
@pytest.mark.parametrize("line", [
    "rm -f -r /", "vssadmin delete shadows /all /quiet", "dd if=/dev/zero of=/dev/sda", "cat ~/.ssh/id_rsa",
    "net user admin Passw0rd /add", "sudo chmod 777 /etc/sudoers", "perl -e 'exec /bin/sh'", "ssh admin",
    "ssh root@host", "connect evil.online", "echo ${IFS}", "wmic process list", "schtasks /create", "reg add HKLM",
    "bcdedit /set", "mkfs.ext4 disk", "socat TCP", "psexec go", "sh -i", "chown root file", "scp file out",
    "telnet host", "ftp host", "ruby -e boom", "php -r boom", "node -e boom", "bash -c boom", "python3 -c boom",
    "rm -r stuff", "rm -f file", "cat /etc/hosts", "ls /bin/", "open /dev/null", "attrib /s", "del /f x",
    "set password=hunter2", "login Passw0rd", "visit hxxp://x", "go evil[.]com", "ping 10[.]0[.]0[.]1",
    "run C:\\Windows\\x",
])
def test_unsafe_terminal_review_list(line):
    assert rv.unsafe_terminal(line), line


@pytest.mark.parametrize("line", [
    "probe gateway --version", "send crafted request", "Server compromised", "Acme Gateway 7.4.2",
    "attacker has admin access", "scanning ports 3 of 9", "CVE-2026-1111 confirmed", "payload delivered",
])
def test_safe_terminal_review_list(line):
    assert rv.unsafe_terminal(line) is None, line


def test_terminal_title_is_checked_and_denylist_applies_end_to_end():
    ok = [{"kind": "cmd", "text": "probe gateway --version"}, {"kind": "out", "text": "Acme Gateway 7.4.2"},
          {"kind": "alert", "text": "Server compromised"}]
    assert len(clean(seg(scenes=[term(ok)]))["scenes"]) == 1
    v = clean(seg(scenes=[term(ok, title="sudo chmod now")]))
    assert v["scenes"] == [] and any("title" in d and "unsafe" in d for d in v["dropped"])
    v = clean(seg(scenes=[term(ok, title="Console evil.online")]))
    assert v["scenes"] == []
    bad = ok[:2] + [{"kind": "alert", "text": "rm -f -r /"}]
    assert clean(seg(scenes=[term(bad)]))["scenes"] == []


@pytest.mark.parametrize("text", [
    "Texts link to acme-login.online", "Attackers used hxxp://evil[.]com", "visit acme.shop now", "go to a.b.c.club",
    "hosted on bad-site.ru", "site evil(.)com", "mail ops[at]acme[.]com", "ip 10.0.0.1", "ip 10[.]0[.]0[.]1",
    "mail bob@acme", "see www.acme", "login.acme-pay.xyz", "files.vip", "x.icu", "x.cfd", "x.sbs", "x.top",
    "acme.zip", "acme.mov", "acme.su", "acme.pw", "acme.ai", "acme.tk", "acme.cn", "https://acme",
])
def test_host_ioc_detected(text):
    assert rv.host_ioc(text), text


@pytest.mark.parametrize("text", [
    "Acme Gateway flaw exploited", "About 4,000 servers exposed", "Patched in 7.4.3", "Node.js and Next.js",
    "e.g. a flaw", "Update now", "rated 9.8",
])
def test_host_ioc_clean(text):
    assert rv.host_ioc(text) is None, text


def test_every_on_screen_item_drops_hosts():
    bad = "Texts link to acme-login.online"
    defanged = "Attackers used hxxp://evil[.]com"
    for b in (bad, defanged):
        v = clean(seg(key_points=[{"text": b, "para": 1, "cue": "x"}],
                      checklist=[{"text": b, "para": 1, "cue": "x"}, {"text": b, "para": 2, "cue": "y"}],
                      scenes=[
                          {"type": "flow", "para": 1, "cue": "x", "title": b,
                           "nodes": [{"label": "Acme", "sub": "x"}, {"label": "Server", "sub": ""}], "arrows": [""]},
                          {"type": "flow", "para": 1, "cue": "x", "title": "Acme flow",
                           "nodes": [{"label": "Acme", "sub": b}, {"label": "evil.online", "sub": ""}],
                           "arrows": [b]},
                          {"type": "stat", "para": 1, "cue": "x", "value": "4,000", "label": b},
                          {"type": "stat", "para": 1, "cue": "x", "value": "4,000", "label": "servers", "context": b},
                          {"type": "timeline", "para": 1, "cue": "x",
                           "events": [{"date": "Nov 12", "label": b}, {"date": "Nov 14", "label": "Patch"}]},
                      ]))
        assert v["key_points"] == [] and v["checklist"] == [], b
        titles = [s for s in v["scenes"] if s["type"] == "flow"]
        assert titles == [], b
        stats = [s for s in v["scenes"] if s["type"] == "stat"]
        assert all("context" not in s for s in stats) and len(stats) <= 1
        assert not [s for s in v["scenes"] if s["type"] == "timeline"]
        blob = json.dumps(v["scenes"])
        assert "evil" not in blob and "acme-login" not in blob


def test_scam_sender_checked_against_source_and_flags_checked():
    v = clean(seg(scenes=[scam(sender="PayPal")]))
    assert v["scenes"] == [] and any("PayPal" in d for d in v["dropped"])
    v = clean(seg(scenes=[scam(sender="Acme Support")]))
    assert v["scenes"][0]["sender"] == "Acme Support"
    for generic in ("Support", "Bank", "Delivery", "Team"):
        assert clean(seg(scenes=[scam(sender=generic)]))["scenes"], generic
    v = clean(seg(scenes=[scam(flags=["Urgent tone", "Pay via acme-login.online", "Asks to verify", "PayPal link"])]))
    assert v["scenes"][0]["flags"] == ["Urgent tone", "Asks to verify"]
    v = clean(seg(scenes=[scam(body="Verify at acme-login.online {link}")]))
    assert v["scenes"] == []


def test_keyword_set_drops_stopwords_and_short_tokens():
    ks = rv.keyword_set(["the patch", "a flaw in it", "is up to us", "Acme Gateway"])
    assert ks == {"patch", "flaw", "acme", "gateway"}
    assert rv.keyword_set(["the", "of it", "ok"]) == set()


def test_clean_str_strips_bidi_and_controls():
    s = "A\u202eB\u2066C\u2069D\u200bE\x00F\x1bG\u0085H\u2028I\nJ\tK"
    out = rv._clean_str(s)
    assert out == "ABCDE F G H I J K"
    for ch in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069":
        assert rv._clean_str("x" + ch + "y") == "xy"


@pytest.mark.parametrize("text,ok", [
    ("17 flaws patched", False), ("3 servers hit", False), ("5 companies affected", False),
    ("step 3", True), ("Step 3: update", True), ("Enable 2FA", True), ("About 4,000 servers", True),
    ("hit on Monday", False), ("Update 4 servers", False), ("Patch 7.4.3", True),
])
def test_bare_numbers_and_weekdays_need_a_source(text, ok):
    assert rv.facts_ok(text, SRC)[0] is ok, text
    assert rv.facts_ok("Monday", SRC + " Monday")[0]


def test_weekdays_and_today_are_not_generic():
    for w in ("monday", "friday", "today", "yesterday", "tomorrow", "tonight", "sunday"):
        assert w not in rv.GENERIC_WORDS
    assert not rv.facts_ok("Yesterday it broke", SRC)[0]
    assert rv.facts_ok("Yesterday it broke", SRC + " yesterday")[0]


class CveNicknameTerminalTests(unittest.TestCase):
    def test_nicknames_allowed_credentials_blocked(self):
        from cybershorts import recap_visuals as V
        self.assertIsNone(V.unsafe_terminal("exploit Log4Shell"))
        self.assertIsNone(V.unsafe_terminal("scan for Log4j"))
        self.assertIsNotNone(V.unsafe_terminal("set Passw0rd for admin"))
        self.assertIsNotNone(V.unsafe_terminal("login admin:hunter2"))
