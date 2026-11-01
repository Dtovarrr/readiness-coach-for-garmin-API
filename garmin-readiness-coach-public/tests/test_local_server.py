"""Local server hardening: Host header validation (DNS rebinding), Origin/Referer checks on every
request that isn't GET/HEAD (cross-site requests from other pages in a browser), no CORS headers,
and the /api/raw debug helper (off unless COACH_DEBUG_RAW=1; Garmin service prefixes only).

Run from the project folder:  .venv/bin/python -m unittest discover -s tests -v
Uses a throwaway COACH_DATA_DIR and cache folder; never calls Garmin or Claude.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("COACH_DATA_DIR", tempfile.mkdtemp(prefix="coach-test-"))
os.environ.setdefault("COACH_CACHE_DIR", tempfile.mkdtemp(prefix="coach-cache-"))
sys.path.insert(0, str(ROOT))

ENV = {"GARMIN_DEMO": "1", "ANTHROPIC_API_KEY": "", "COACH_DEBUG_RAW": ""}


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = mock.patch.dict(os.environ, ENV)
        cls.env.start()
        import server
        from fastapi.testclient import TestClient
        cls.server = server
        cls.port = server.PORT
        cls.c = TestClient(server.app, base_url=server.LOCAL_URL)  # no default Origin: each test sets its own

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()

    def post(self, path="/api/coach/notices/snooze", **headers):
        return self.c.post(path, json=dict(minutes=1), headers=headers)


class HostHeader(Base):
    def test_local_names_on_our_port_are_served(self):
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"LOCALHOST:{self.port}", f"[::1]:{self.port}"):
            with self.subTest(host=host):
                self.assertEqual(self.c.get("/api/settings", headers={"Host": host}).status_code, 200)

    def test_other_hosts_are_refused(self):
        for host in ("evil.example", f"evil.example:{self.port}", "127.0.0.1", "localhost", f"127.0.0.1:{self.port + 1}",
                     f"localhost.:{self.port}", f"127.0.0.1.nip.io:{self.port}", f"0.0.0.0:{self.port}", f"192.168.1.5:{self.port}", ""):
            with self.subTest(host=host):
                r = self.c.get("/api/settings", headers={"Host": host})
                self.assertEqual(r.status_code, 403)
                self.assertIn("127.0.0.1 or localhost", r.json()["detail"])

    def test_static_page_is_guarded_too(self):
        self.assertEqual(self.c.get("/", headers={"Host": "rebind.example"}).status_code, 403)
        self.assertEqual(self.c.get("/").status_code, 200)

    def test_bad_host_refused_even_with_good_origin(self):
        r = self.post(Host="evil.example", Origin=self.server.LOCAL_URL)
        self.assertEqual(r.status_code, 403)


class OriginAndReferer(Base):
    def test_same_origin_writes_are_accepted(self):
        for origin in (f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"):
            with self.subTest(origin=origin):
                self.assertNotEqual(self.post(Origin=origin).status_code, 403)

    def test_referer_is_the_fallback_when_origin_is_missing(self):
        self.assertNotEqual(self.post(Referer=f"http://127.0.0.1:{self.port}/#today").status_code, 403)
        self.assertEqual(self.post(Referer="https://evil.example/page").status_code, 403)
        self.assertEqual(self.post(Referer=f"http://127.0.0.1:{self.port}.evil.example/").status_code, 403)

    def test_cross_site_and_missing_origins_are_refused(self):
        bad = ["https://evil.example", "null", f"http://127.0.0.1:{self.port + 1}", f"https://127.0.0.1:{self.port}",
               f"http://evil.example:{self.port}", f"http://127.0.0.1:{self.port}.evil.example", ""]
        for origin in bad:
            with self.subTest(origin=origin):
                r = self.post(Origin=origin)
                self.assertEqual(r.status_code, 403)
                self.assertIn("app's own page", r.json()["detail"])
        r = self.post()  # neither header
        self.assertEqual(r.status_code, 403)
        self.assertIn("no Origin or Referer", r.json()["detail"])

    def test_bad_origin_beats_good_referer(self):
        r = self.post(Origin="https://evil.example", Referer=f"http://127.0.0.1:{self.port}/")
        self.assertEqual(r.status_code, 403)

    def test_every_unsafe_method_is_checked(self):
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            with self.subTest(method=method):
                r = self.c.request(method, "/api/goals/x", headers={"Origin": "https://evil.example"})
                self.assertEqual(r.status_code, 403)

    def test_refusal_happens_before_the_handler(self):
        with mock.patch.object(self.server.goals, "delete", side_effect=AssertionError("handler ran")) as h:
            self.assertEqual(self.c.delete("/api/goals/abc", headers={"Origin": "https://evil.example"}).status_code, 403)
            h.assert_not_called()

    def test_reads_need_no_origin(self):
        self.assertEqual(self.c.get("/api/settings").status_code, 200)

    def test_uploads_are_checked(self):
        files = [("files", ("x.pdf", b"%PDF-1.4", "application/pdf"))]
        self.assertEqual(self.c.post("/api/dexa/parse", files=files, headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.c.post("/api/dexa/parse", files=files, headers={"Origin": self.server.LOCAL_URL}).status_code, 200)


class NoCors(Base):
    def test_no_cors_headers_for_any_origin(self):
        for origin in ("https://evil.example", self.server.LOCAL_URL):
            r = self.c.get("/api/settings", headers={"Origin": origin})
            self.assertFalse([k for k in r.headers if k.lower().startswith("access-control-")], r.headers)

    def test_preflight_is_refused(self):
        r = self.c.options("/api/settings", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "PUT"})
        self.assertEqual(r.status_code, 403)
        self.assertNotIn("access-control-allow-origin", {k.lower() for k in r.headers})

    def test_no_cors_middleware_installed(self):
        from starlette.middleware.cors import CORSMiddleware
        self.assertFalse([m for m in self.server.app.user_middleware if m.cls is CORSMiddleware])


class RawDebug(Base):
    def test_off_by_default(self):
        for v in ("", "0", "true", "yes"):
            with self.subTest(COACH_DEBUG_RAW=v), mock.patch.dict(os.environ, {"COACH_DEBUG_RAW": v}):
                self.assertEqual(self.c.get("/api/raw", params=dict(path="/hrv-service/hrv/2025-01-01")).status_code, 404)

    def test_known_prefixes_only_when_enabled(self):
        ok = ["/hrv-service/hrv/2025-01-01", "/usersummary-service/stats/steps/daily/2025-01-01/2025-01-07",
              "/activitylist-service/activities/search/activities?start=0&limit=5", "/wellness-service/wellness/dailyHeartRate"]
        bad = ["/personalapitoken-service/personal-api/personal-api-key/token", "/hrv-service/../personalapitoken-service/x",
               "/hrv-service/%2e%2e/x", "//evil.example/hrv-service/x", "https://evil.example/hrv-service/x", "hrv-service/x",
               "/hrv-servicex/x", "/HRV-SERVICE/x", "/hrv-service//x", "/hrv-service/x#frag", "/hrv-service/x\\y",
               "/hrv-service/x?a=<script>", "/unknown-service/x", "/", ""]
        with mock.patch.dict(os.environ, {"COACH_DEBUG_RAW": "1"}):
            for p in ok:
                with self.subTest(ok=p):
                    r = self.c.get("/api/raw", params=dict(path=p))
                    self.assertEqual((r.status_code, r.json()), (200, {"demo": True}))
            for p in bad:
                with self.subTest(bad=p):
                    self.assertEqual(self.c.get("/api/raw", params=dict(path=p)).status_code, 400)

    def test_prefixes_cover_what_the_client_uses(self):
        """Every service garmin_client.py calls is debuggable, and the token service never is."""
        import re
        used = set(re.findall(r'["/](\w[\w-]*-service)/', (ROOT / "garmin_client.py").read_text()))
        used.discard("personalapitoken-service")
        self.assertTrue(used, "scan found no services")
        self.assertLessEqual(used, set(self.server.RAW_PREFIXES))
        self.assertNotIn("personalapitoken-service", self.server.RAW_PREFIXES)

    def test_enabled_raw_still_needs_a_local_host(self):
        with mock.patch.dict(os.environ, {"COACH_DEBUG_RAW": "1"}):
            r = self.c.get("/api/raw", params=dict(path="/hrv-service/x"), headers={"Host": "evil.example"})
            self.assertEqual(r.status_code, 403)


class Port(unittest.TestCase):
    def test_run_and_server_agree_on_the_port(self):
        import subprocess
        code = "import run, server; print(run.PORT, server.PORT)"
        env = {**os.environ, "COACH_PORT": "8799", "GARMIN_DEMO": "1"}
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.split()[-2:], ["8799", "8799"])


if __name__ == "__main__":
    unittest.main()
