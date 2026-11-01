"""Every script and stylesheet static/index.html loads from a CDN is version-pinned and carries a
Subresource Integrity hash, so a changed or compromised CDN file is refused by the browser.

Run from the project folder:  .venv/bin/python -m unittest discover -s tests -v
The online check (hash matches the live file) runs only with COACH_CHECK_SRI_ONLINE=1.
"""
import base64
import hashlib
import os
import re
import unittest
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text()
TAGS = re.findall(r"<(?:script|link)\b[^>]*(?:src|href)=\"https?://[^\"]+\"[^>]*>", HTML)


def attr(tag: str, name: str) -> str | None:
    m = re.search(rf'\b{name}="([^"]*)"', tag)
    return m[1] if m else None


class Sri(unittest.TestCase):
    def test_found_the_cdn_tags(self):
        self.assertEqual(len(TAGS), 3)  # Chart.js, Leaflet CSS, Leaflet JS

    def test_every_cdn_tag_is_pinned_and_hashed(self):
        for tag in TAGS:
            with self.subTest(tag=tag[:80]):
                url = attr(tag, "src") or attr(tag, "href")
                self.assertRegex(url, r"@\d+\.\d+\.\d+/")  # exact version
                self.assertRegex(attr(tag, "integrity") or "", r"^sha(384|512)-[A-Za-z0-9+/]+={0,2}$")
                self.assertEqual(attr(tag, "crossorigin"), "anonymous")

    @unittest.skipUnless(os.getenv("COACH_CHECK_SRI_ONLINE") == "1", "set COACH_CHECK_SRI_ONLINE=1 to fetch and compare")
    def test_hashes_match_the_live_files(self):
        for tag in TAGS:
            url = attr(tag, "src") or attr(tag, "href")
            with self.subTest(url=url):
                import httpx  # certifi's roots; python.org builds may have none for urllib
                body = httpx.get(url, timeout=20, follow_redirects=True).raise_for_status().content
                algo, want = attr(tag, "integrity").split("-", 1)
                self.assertEqual(base64.b64encode(hashlib.new(algo, body).digest()).decode(), want)


if __name__ == "__main__":
    unittest.main()
