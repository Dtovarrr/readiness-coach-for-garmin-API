"""Repo hygiene: every place the app writes personal data is git-ignored, and the files that must
be committed (the .env template, the synthetic test fixture) are not.

Three layers: concrete example files at each known write location; the default folders the
modules resolve at runtime (store.DATA_DIR, applog.LOG_FILE, ...); and a scan of the source for
any folder joined onto the project root, so a new write location can't slip in unnoticed.

Run from the project folder:  .venv/bin/python -m unittest discover -s tests -v
Needs git; skipped when the folder isn't a git work tree.
"""
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Paths relative to the project root, one per kind of file each module writes.
WRITTEN = [
    ".env", ".env.local", ".env.production",
    ".data/coach.db", ".data/coach.db-wal", ".data/settings.json", ".data/goals.json",  # store.py
    ".data/photos/2026/01/0123abcd.jpg",                                                # photos.py
    ".data/dexa/0123abcd.pdf", ".data/dexa/staging/0123abcd.pdf", ".data/dexa/staging/0123abcd.json",  # dexa.py
    ".data/exports/workouts/tempo.fit",                                                 # server.py workout export
    ".cache/snapshot/x.json", ".cache/activity/1.json", ".cache/calendar/2026-10.json",  # server.py cache
    ".cache/dexa/x.json", ".cache/health/events.json",                                  # server.py / health.py
    ".cache/export/readiness-coach-export-20261030.zip",                                # server.py backup
    ".cache/export/plan.pdf",                                                           # server.py review export fallback
    "logs/readiness_coach.log", "logs/readiness_coach.log.1", "logs/app.lock",                # applog.py, launcher.c, app_main.py
    "samples/bodyspec-2026-01-01.pdf", "exports/anything.fit",
    ".venv/bin/python", "venv/bin/python", "__pycache__/x.pyc", "tests/__pycache__/x.cpython-314.pyc",
    ".pytest_cache/v/cache", ".DS_Store", "static/.DS_Store", "anything.sqlite3", "x.db", "x.log",
    "Readiness Coach.app/Contents/Info.plist", "dist/x", "build/x",
]
COMMITTED = [".env.example", ".gitignore", "README.md", "server.py", "static/index.html",
             "tests/fixtures/synthetic_bodyspec.pdf", "tests/fixtures/make_synthetic_dexa.py"]
SOURCES = [*ROOT.glob("*.py"), *ROOT.glob("macos/*")]
# Folders the source joins onto the project root that hold committed code, not user data.
CODE_DIRS = {"static", "macos", "tests"}


def ignored(rel: str) -> bool:
    r = subprocess.run(["git", "check-ignore", "-q", "--no-index", rel], cwd=ROOT)
    return r.returncode == 0


def in_git() -> bool:
    if not shutil.which("git"):
        return False
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=ROOT, capture_output=True, text=True)
    return r.returncode == 0 and Path(r.stdout.strip()).resolve() == ROOT


@unittest.skipUnless(in_git(), "not a git work tree (run `git init` first)")
class Ignored(unittest.TestCase):
    def test_every_write_location_is_ignored(self):
        for rel in WRITTEN:
            with self.subTest(path=rel):
                self.assertTrue(ignored(rel), f"{rel} would be committed")

    def test_templates_and_fixtures_are_not_ignored(self):
        for rel in COMMITTED:
            with self.subTest(path=rel):
                self.assertFalse(ignored(rel), f"{rel} is ignored but must be committed")

    def test_runtime_defaults_are_ignored(self):
        """The folders the modules actually resolve when no COACH_* overrides are set."""
        code = ("import store, applog; print(store.DATA_DIR); print(store.DB); print(applog.LOG_FILE)")
        env = {k: v for k, v in os.environ.items() if not k.startswith("COACH_")}
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, check=True)
        for p in out.stdout.split():
            with self.subTest(path=p):
                rel = Path(p).resolve().relative_to(ROOT).as_posix()
                # a folder may not exist yet, and "x/" patterns only match folders git can see: probe inside it too
                self.assertTrue(ignored(rel) or ignored(f"{rel}/probe"), f"{rel} would be committed")

    def test_every_folder_joined_onto_the_project_is_ignored(self):
        """`Path(__file__).parent / "x"`, `PROJECT / "x"` and launcher.c's `PROJECT_DIR "/x"`."""
        pat = re.compile(r'(?:Path\(__file__\)(?:\.resolve\(\))?\.parent(?:\.parent)?|PROJECT)\s*/\s*"([^"/]+)"'
                         r'|PROJECT_DIR\s+"/([^"/]+)')
        found = set()
        for src in SOURCES:
            for m in pat.finditer(src.read_text(errors="ignore")):
                found.add(m[1] or m[2])
        self.assertIn(".data", found)  # the scan itself works
        self.assertIn("logs", found)
        for name in sorted(found - CODE_DIRS):
            if name.endswith(".py"):  # code next to the caller, e.g. PROJECT / "run.py"
                continue
            with self.subTest(folder=name):
                self.assertTrue(ignored(name) or ignored(f"{name}/probe"), f"{name} is written to but not ignored")


class EnvExample(unittest.TestCase):
    """.env.example lists every variable the code reads, and holds no values."""

    def test_every_variable_is_documented_and_empty(self):
        read = set()
        for src in [*SOURCES, *ROOT.glob("tests/*.py")]:
            read |= set(re.findall(r'(?:getenv|environ\.get|environ\[)\(?\s*"([A-Z][A-Z0-9_]+)"', src.read_text(errors="ignore")))
        read |= {"COACH_SAMPLES_DIR"}  # read through os.getenv(...) with a fallback expression
        listed = {}
        for line in (ROOT / ".env.example").read_text().splitlines():
            if line and not line.startswith("#"):
                k, _, v = line.partition("=")
                listed[k.strip()] = v.strip()
        self.assertIn("GARMIN_API_KEY", read)  # the scan itself works
        self.assertEqual(sorted(read - set(listed)), [], "read by the code but missing from .env.example")
        self.assertEqual({k: v for k, v in listed.items() if v}, {}, ".env.example must not contain values")


if __name__ == "__main__":
    unittest.main()
