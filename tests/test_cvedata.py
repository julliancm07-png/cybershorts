import json
import os
import tempfile
import time
import unittest
from unittest import mock

import requests

from cybershorts import cvedata

KEV = {"vulnerabilities": [
    {"cveID": "CVE-2026-1111", "vendorProject": "Acme", "product": "Gateway", "dateAdded": "2026-11-20",
     "knownRansomwareCampaignUse": "Known"},
    {"cveID": "CVE-2026-2222", "vendorProject": "Foo", "product": "Bar", "dateAdded": "2026-11-19",
     "knownRansomwareCampaignUse": "Unknown"},
    {"cveID": "CVE-2026-3333", "vendorProject": "Baz", "product": "Qux", "dateAdded": "2026-11-18",
     "knownRansomwareCampaignUse": "Unknown"}]}

NVD = {"vulnerabilities": [{"cve": {"metrics": {
    "cvssMetricV2": [{"type": "Primary", "baseSeverity": "HIGH", "cvssData": {"baseScore": 7.5}}],
    "cvssMetricV31": [
        {"type": "Secondary", "cvssData": {"baseScore": 5.0, "baseSeverity": "MEDIUM"}},
        {"type": "Primary", "cvssData": {"baseScore": 9.8, "baseSeverity": "CRITICAL"}}]}}}]}


def resp(status=200, body=None, headers=None):
    r = mock.Mock()
    r.status_code = status
    r.headers = headers or {}
    r.json.return_value = body
    if status >= 400:
        r.raise_for_status.side_effect = requests.HTTPError("bad")
    return r


class Cfg:
    nvd_api_key = ""

    def __init__(self, d, **vals):
        self.d = d
        self.vals = vals

    def path(self, *parts):
        return os.path.join(self.d, *parts)

    def get(self, k, default=None):
        return self.vals.get(k, default)


class KevTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Cfg(self._tmp.name)
        self.cache = self.cfg.path("kev.json")

    def tearDown(self):
        self._tmp.cleanup()

    def _seed_cache(self, age):
        with open(self.cache, "w") as f:
            json.dump({"fetched": time.time() - age, "etag": 'W/"abc"', "last_modified": "",
                       "vulnerabilities": KEV["vulnerabilities"]}, f)

    def test_fetch_writes_cache_with_etag(self):
        with mock.patch.object(cvedata.requests, "get", return_value=resp(200, KEV, {"ETag": 'W/"abc"'})):
            self.assertEqual(len(cvedata.fetch_kev(self.cfg)), 3)
        self.assertEqual(json.loads(open(self.cache).read())["etag"], 'W/"abc"')

    def test_fresh_cache_no_network(self):
        self._seed_cache(60)
        with mock.patch.object(cvedata.requests, "get") as g:
            self.assertEqual(len(cvedata.fetch_kev(self.cfg)), 3)
        g.assert_not_called()

    def test_stale_cache_sends_etag_and_304_keeps_cache(self):
        self._seed_cache(7 * 3600)
        with mock.patch.object(cvedata.requests, "get", return_value=resp(304)) as g:
            self.assertEqual(len(cvedata.fetch_kev(self.cfg)), 3)
        self.assertEqual(g.call_args.kwargs["headers"]["If-None-Match"], 'W/"abc"')
        self.assertLess(time.time() - json.loads(open(self.cache).read())["fetched"], 5)

    def test_error_falls_back_to_cache_then_empty(self):
        self._seed_cache(7 * 3600)
        with mock.patch.object(cvedata.requests, "get", side_effect=requests.ConnectionError("x")):
            self.assertEqual(len(cvedata.fetch_kev(self.cfg)), 3)
        os.remove(self.cache)
        with mock.patch.object(cvedata.requests, "get", side_effect=requests.ConnectionError("x")):
            self.assertEqual(cvedata.fetch_kev(self.cfg), [])

    def test_kev_entry(self):
        self._seed_cache(60)
        self.assertEqual(cvedata.kev_entry(self.cfg, "cve-2026-2222")["product"], "Bar")
        self.assertIsNone(cvedata.kev_entry(self.cfg, "CVE-2026-9999"))


class NvdTests(unittest.TestCase):
    def test_prefers_newest_primary(self):
        cfg = Cfg("/tmp")
        with mock.patch.object(cvedata.requests, "get", return_value=resp(200, NVD)):
            self.assertEqual(cvedata.nvd_lookup(cfg, "CVE-2026-1111"), {"cvss": 9.8, "severity": "CRITICAL"})

    def test_v4_beats_v31(self):
        body = json.loads(json.dumps(NVD))
        body["vulnerabilities"][0]["cve"]["metrics"]["cvssMetricV40"] = [
            {"type": "Primary", "cvssData": {"baseScore": 8.7, "baseSeverity": "HIGH"}}]
        with mock.patch.object(cvedata.requests, "get", return_value=resp(200, body)):
            self.assertEqual(cvedata.nvd_lookup(Cfg("/tmp"), "CVE-2026-1111")["cvss"], 8.7)

    def test_key_sent_as_header_and_never_logged(self):
        cfg = Cfg("/tmp")
        cfg.nvd_api_key = "SECRETKEY"
        err = requests.ConnectionError("failed apiKey=SECRETKEY")
        with mock.patch.object(cvedata.requests, "get", side_effect=err) as g, \
             self.assertLogs("cybershorts", "WARNING") as lg:
            self.assertEqual(cvedata.nvd_lookup(cfg, "CVE-2026-1111"), {})
        self.assertEqual(g.call_args.kwargs["headers"]["apiKey"], "SECRETKEY")
        self.assertNotIn("SECRETKEY", "\n".join(lg.output))

    def test_bad_input_and_errors_never_raise(self):
        self.assertEqual(cvedata.nvd_lookup(Cfg("/tmp"), "not a cve"), {})
        with mock.patch.object(cvedata.requests, "get", return_value=resp(500)):
            self.assertEqual(cvedata.nvd_lookup(Cfg("/tmp"), "CVE-2026-1111"), {})


SRC = ("Acme Gateway flaw CVE-2026-1111 is rated critical with a CVSS score of 9.8. Attackers are actively "
       "exploited in the wild. Acme released a security update to fix it.")


class ConfirmTests(unittest.TestCase):
    def raw(self, **kw):
        base = {"cve_id": "CVE-2026-1111", "cvss": 9.8, "severity": "critical", "products": ["Acme Gateway"],
                "exploited": True, "patch_available": True}
        base.update(kw)
        return base

    def test_full_confirmation(self):
        c = cvedata.confirm_card(self.raw(), SRC, None, None)
        self.assertEqual(c["cve_id"], "CVE-2026-1111")
        self.assertEqual(c["cvss"], 9.8)
        self.assertEqual(c["severity"], "CRITICAL")
        self.assertEqual(c["products"], ["Acme Gateway"])
        self.assertTrue(c["exploited"] and c["patch_available"])

    def test_unconfirmable_cve_returns_none(self):
        self.assertIsNone(cvedata.confirm_card(self.raw(cve_id="CVE-2026-9999"), SRC, None, None))
        self.assertIsNone(cvedata.confirm_card(self.raw(cve_id="CVE-26-1"), "CVE-26-1", None, None))
        self.assertIsNone(cvedata.confirm_card(None, SRC, None, None))

    def test_invented_cvss_dropped_and_nvd_wins(self):
        c = cvedata.confirm_card(self.raw(cvss=9.1), SRC, None, None)
        self.assertIsNone(c["cvss"])
        c = cvedata.confirm_card(self.raw(cvss=9.1), SRC, None, {"cvss": 7.2, "severity": "HIGH"})
        self.assertEqual((c["cvss"], c["severity"]), (7.2, "HIGH"))

    def test_severity_from_band_and_wording(self):
        c = cvedata.confirm_card(self.raw(severity="low"), SRC, None, {"cvss": 5.5})
        self.assertEqual(c["severity"], "MEDIUM")
        c = cvedata.confirm_card(self.raw(cvss=None, severity="high"), "CVE-2026-1111 is a high-severity bug.", None, None)
        self.assertEqual(c["severity"], "HIGH")
        c = cvedata.confirm_card(self.raw(cvss=None, severity="low"), "CVE-2026-1111 is a high-severity bug.", None, None)
        self.assertIsNone(c["severity"])
        c = cvedata.confirm_card(self.raw(cvss=None, severity="critical"), "CVE-2026-1111 was disclosed.", None, None)
        self.assertIsNone(c["severity"])

    def test_products_filtered_and_capped(self):
        src = "CVE-2026-1111 hits Alpha, Beta and Gamma products."
        c = cvedata.confirm_card(self.raw(products=["Alpha", "Beta", "Gamma", "Invented"]), src, None, None)
        self.assertEqual(c["products"], ["Alpha", "Beta"])
        c = cvedata.confirm_card(self.raw(products=["Invented"]), src, None, None)
        self.assertEqual(c["products"], [])
        c = cvedata.confirm_card(self.raw(products=["Invented"]), src, {"vendorProject": "Acme", "product": "Gateway"}, None)
        self.assertEqual(c["products"], ["Gateway"])

    def test_exploited_from_kev_source_and_negation(self):
        quiet = "CVE-2026-1111 is serious. There is no evidence it is actively exploited."
        self.assertFalse(cvedata.confirm_card(self.raw(), quiet, None, None)["exploited"])
        self.assertTrue(cvedata.confirm_card(self.raw(), quiet, {"cveID": "CVE-2026-1111"}, None)["exploited"])
        self.assertTrue(cvedata.confirm_card(self.raw(), "CVE-2026-1111 is used in zero-day attacks.", None, None)["exploited"])

    def test_exploited_false_positives(self):
        for txt in ("CVE-2026-1111 could be exploited by remote attackers.",
                    "CVE-2026-1111 was reported via Trend Micro's Zero Day Initiative.",
                    "CVE-2026-1111 may be exploited by a local user.",
                    "CVE-2026-1111 might be actively exploited later."):
            self.assertFalse(cvedata.confirm_card(self.raw(), txt, None, None)["exploited"], txt)
        for txt in ("CVE-2026-1111 is actively exploited.", "CVE-2026-1111 is under active exploitation.",
                    "CVE-2026-1111 was exploited in the wild.", "CVE-2026-1111 was used in zero-day attacks.",
                    "CVE-2026-1111 was exploited as a zero-day."):
            self.assertTrue(cvedata.confirm_card(self.raw(), txt, None, None)["exploited"], txt)

    def test_updated_alone_is_not_a_patch(self):
        c = cvedata.confirm_card(self.raw(), "CVE-2026-1111 is bad. Microsoft updated its advisory.", None, None)
        self.assertFalse(c["patch_available"])
        c = cvedata.confirm_card(self.raw(), "CVE-2026-1111 is bad. A software update is available.", None, None)
        self.assertTrue(c["patch_available"])

    def test_checks_use_cve_local_scope(self):
        src = ("CVE-2026-1111 affects Alpha and is bad. " + "x " * 300 +
               "Other CVE-2026-2222 in Beta scored 7.5; a patch was released and it is unpatched elsewhere. "
               "Beta is fixed in 2.0.")
        # raw claims a number, product and patch that belong only to the other CVE
        c = cvedata.confirm_card(self.raw(cvss=7.5, products=["Beta"], severity=None), src, None, None)
        self.assertIsNone(c["cvss"])
        self.assertEqual(c["products"], [])
        self.assertFalse(c["patch_available"])

    def test_patch_needs_wording_and_no_negation(self):
        self.assertFalse(cvedata.confirm_card(self.raw(), "CVE-2026-1111 is bad.", None, None)["patch_available"])
        self.assertFalse(cvedata.confirm_card(self.raw(), "CVE-2026-1111: there is no patch yet.", None, None)["patch_available"])
        self.assertFalse(cvedata.confirm_card(self.raw(), "CVE-2026-1111 remains unpatched; update later.", None, None)["patch_available"])
        self.assertFalse(cvedata.confirm_card(self.raw(patch_available=False), SRC, None, None)["patch_available"])

    def test_pure(self):
        raw = self.raw()
        before = json.dumps(raw)
        with mock.patch.object(cvedata.requests, "get") as g:
            cvedata.confirm_card(raw, SRC, None, None)
        g.assert_not_called()
        self.assertEqual(json.dumps(raw), before)


class BuildCardTests(unittest.TestCase):
    def test_build_card_uses_lookups(self):
        cfg = Cfg("/tmp")
        story = {"title": "Acme Gateway CVE-2026-1111", "summary": "Patch released.", "article": "update available"}
        with mock.patch.object(cvedata, "kev_entry", return_value=None), \
             mock.patch.object(cvedata, "nvd_lookup", return_value={"cvss": 9.8, "severity": "CRITICAL"}):
            c = cvedata.build_card(cfg, story, {"cve_id": "CVE-2026-1111", "patch_available": True})
        self.assertEqual(c["cvss"], 9.8)
        self.assertTrue(c["patch_available"])

    def test_disabled_and_errors(self):
        self.assertIsNone(cvedata.build_card(Cfg("/tmp", **{"data_card.enabled": False}), {}, {"cve_id": "CVE-2026-1111"}))
        with mock.patch.object(cvedata, "kev_entry", side_effect=RuntimeError("x")):
            self.assertIsNone(cvedata.build_card(Cfg("/tmp"), {"title": "CVE-2026-1111"}, {"cve_id": "CVE-2026-1111"}))

    def test_nvd_skipped_when_off(self):
        with mock.patch.object(cvedata, "kev_entry", return_value=None), \
             mock.patch.object(cvedata, "nvd_lookup") as n:
            cvedata.build_card(Cfg("/tmp", **{"data_card.nvd": False}), {"title": "CVE-2026-1111"},
                               {"cve_id": "CVE-2026-1111"})
        n.assert_not_called()


class ConfirmRegressionTests(unittest.TestCase):
    def raw(self, **kw):
        d = {"cve_id": "CVE-2026-1111", "cvss": None, "severity": "", "products": [], "patch_available": True}
        d.update(kw)
        return d

    def card(self, source, **kw):
        return cvedata.confirm_card(self.raw(**kw), source, None, None)

    def test_cve_id_needs_boundary(self):
        self.assertIsNone(self.card("CVE-2026-11112 was disclosed."))
        self.assertIsNotNone(self.card("cve-2026-1111, a flaw."))
        self.assertIsNotNone(self.card("Fixes (CVE-2026-1111)."))

    def test_mitigations_alone_is_not_a_patch(self):
        src = "CVE-2026-1111 added to KEV. Apply mitigations per vendor instructions."
        self.assertFalse(self.card(src)["patch_available"])
        self.assertTrue(self.card(src + " A security update is out.")["patch_available"])

    def test_no_mitigations_or_discontinue_use_means_no_patch(self):
        self.assertFalse(self.card("CVE-2026-1111: update when possible; mitigations are unavailable.")["patch_available"])
        self.assertFalse(self.card("CVE-2026-1111: apply the update or discontinue use of the product.")["patch_available"])

    def test_severity_only_when_it_matches_llm(self):
        src = "CVE-2026-1111 is a high-impact bug rated high-severity elsewhere."
        self.assertIsNone(self.card("CVE-2026-1111 is a high-impact bug.", severity="high")["severity"])
        self.assertEqual(self.card(src, severity="high")["severity"], "HIGH")
        self.assertIsNone(self.card(src, severity="critical")["severity"])

    def test_severity_and_exploited_scoped_to_cve_mention(self):
        filler = " ".join(["filler"] * 100)
        src = ("CVE-2026-1111 is a bug. " + filler + " CVE-2026-2222 is a critical-severity flaw "
               "exploited in the wild as a zero-day.")
        c = self.card(src, severity="critical")
        self.assertIsNone(c["severity"])
        self.assertFalse(c["exploited"])
        near = "CVE-2026-1111 is a critical-severity flaw, exploited in the wild. " + filler + " CVE-2026-2222 too."
        c = self.card(near, severity="critical")
        self.assertEqual(c["severity"], "CRITICAL")
        self.assertTrue(c["exploited"])

    def test_single_cve_source_uses_whole_text(self):
        src = "CVE-2026-1111 affects Acme. " + " ".join(["filler"] * 100) + " It was exploited as a zero-day."
        self.assertTrue(self.card(src)["exploited"])

    def test_products_match_on_word_boundaries(self):
        src = "CVE-2026-1111 hits Gateways and Acme Corp."
        self.assertEqual(self.card(src, products=["Gate", "Acme"])["products"], ["Acme"])


if __name__ == "__main__":
    unittest.main()
