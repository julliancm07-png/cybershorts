import unittest
from unittest import mock

from cybershorts import article

HTML = """<html><head><title>Big bug</title></head><body>
<nav>Home | News | Deals</nav>
<article><h1>Critical flaw in ExampleServer</h1>
<p>Researchers at Example Labs found a critical flaw in ExampleServer versions 2.1 through 2.4 that lets
unauthenticated attackers run code remotely. The vendor released version 2.5 on Tuesday to fix the issue.</p>
<p>Administrators who cannot update right away should block access to the management port from the internet.
The researchers said they have seen scanning activity targeting the bug since Monday morning.</p>
<p>The flaw is tracked as CVE-2026-12345 and carries a severity score of 9.8 out of 10.</p>
</article><footer>Subscribe to our newsletter</footer></body></html>"""


class ArticleTests(unittest.TestCase):
    def test_trim_at_boundary(self):
        text = "A" * 30 + ".\n\n" + "B" * 30
        self.assertEqual(article.trim(text, 45), "A" * 30 + ".")
        self.assertEqual(article.trim("short", 100), "short")

    def test_extracts_main_text(self):
        resp = mock.Mock(text=HTML)
        resp.raise_for_status = mock.Mock()
        with mock.patch.object(article.requests, "get", return_value=resp):
            text = article.fetch_text("https://example.com/a")
        self.assertIn("version 2.5", text)
        self.assertIn("CVE-2026-12345", text)
        self.assertNotIn("Subscribe", text)

    def test_failure_returns_empty(self):
        with mock.patch.object(article.requests, "get", side_effect=OSError("boom")):
            self.assertEqual(article.fetch_text("https://example.com/a"), "")
