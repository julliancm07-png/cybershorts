"""Uptime Kuma heartbeat."""
import copy
import tempfile
import time
import unittest
from unittest import mock

import requests

from cybershorts import config, health


class CheckTests(unittest.TestCase):
    def _resp(self, ok=True, status=200, body=None):
        r = mock.Mock(ok=ok, status_code=status)
        r.json.return_value = body if body is not None else {"ok": True}
        return r

    def test_ok_and_url_shape(self):
        with mock.patch.object(health.requests, "get", return_value=self._resp()) as g:
            self.assertEqual(health.check("http://k:3001/api/push/abc?status=up&msg=OK&ping="), (True, "ok"))
        url = g.call_args[0][0]
        self.assertTrue(url.startswith("http://k:3001/api/push/abc?status=up&msg=ok"))
        self.assertTrue(g.call_args.kwargs["verify"])

    def test_tls_error_hint(self):
        with mock.patch.object(health.requests, "get", side_effect=requests.exceptions.SSLError("x")):
            ok, why = health.check("https://kuma.home.lan/api/push/abc")
        self.assertFalse(ok)
        self.assertIn("verify_tls", why)

    def test_kuma_not_ok(self):
        with mock.patch.object(health.requests, "get",
                               return_value=self._resp(body={"ok": False, "msg": "Monitor not found or not active."})):
            ok, why = health.check("http://k/api/push/bad")
        self.assertFalse(ok)
        self.assertIn("token", why)

    def test_unset(self):
        self.assertFalse(health.ping(""))

    def test_verify_false_passed(self):
        with mock.patch.object(health.requests, "get", return_value=self._resp()) as g:
            health.ping("https://k/api/push/a", verify=False)
        self.assertFalse(g.call_args.kwargs["verify"])


class HeartbeatTests(unittest.TestCase):
    def test_thread_pings_and_none_without_url(self):
        raw = copy.deepcopy(config.DEFAULTS)
        cfg = config.Config(tempfile.mkdtemp(), raw, {})
        self.assertIsNone(health.start_heartbeat(cfg))
        cfg = config.Config(tempfile.mkdtemp(), raw, {"HEALTHCHECK_URL": "http://k/api/push/a"})
        with mock.patch.object(health, "ping", return_value=True) as p:
            t = health.start_heartbeat(cfg)
            for _ in range(50):
                if p.called:
                    break
                time.sleep(0.02)
            t.stop.set()
        self.assertTrue(p.called)
        self.assertEqual(p.call_args[0][1], "up")


if __name__ == "__main__":
    unittest.main()
