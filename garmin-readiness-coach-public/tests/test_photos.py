"""Upload validation, path safety, EXIF/GPS stripping and the goals migration.

Run from the project folder:  .venv/bin/python -m unittest discover -s tests -v
Everything runs in a throwaway COACH_DATA_DIR, never in the real .data/ folder.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("COACH_DATA_DIR", tempfile.mkdtemp(prefix="coach-test-"))
sys.path.insert(0, str(ROOT))

from PIL import Image, PngImagePlugin  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import photos  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402

DAY = "2026-10-27"


def jpeg(size=(64, 48), exif=None, color=(200, 80, 40)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG", **({"exif": exif} if exif is not None else {}))
    return buf.getvalue()


def png(size=(32, 32), mode="RGB", text=None) -> bytes:
    buf = io.BytesIO()
    info = None
    if text:
        info = PngImagePlugin.PngInfo()
        for k, v in text.items():
            info.add_text(k, v)
    Image.new(mode, size).save(buf, "PNG", pnginfo=info)
    return buf.getvalue()


def gps_exif(original="2026:10:20 07:59:00", orientation=None) -> Image.Exif:
    exif = Image.Exif()
    exif[0x0132] = "2026:10:21 09:00:00"  # DateTime (fallback)
    exif[0x010F] = "Apple"  # Make
    if orientation:
        exif[0x0112] = orientation
    exif.get_ifd(0x8769)[0x9003] = original  # DateTimeOriginal
    gps = exif.get_ifd(0x8825)
    gps.update({1: "N", 2: (34.0, 10.0, 30.0), 3: "W", 4: (118.0, 50.0, 12.0)})
    return exif


def stored_bytes(row, which="full") -> bytes:
    return photos.resolve(row, which).read_bytes()


class Validation(unittest.TestCase):
    def test_accepts_jpeg_png_heic(self):
        self.assertEqual(photos.validate(jpeg(), "a.jpg").format, "JPEG")
        self.assertEqual(photos.validate(png(), "a.PNG").format, "PNG")
        try:
            buf = io.BytesIO()
            Image.new("RGB", (40, 30), (10, 120, 200)).save(buf, "HEIF")
        except Exception as ex:  # encoder missing in this pillow-heif build
            self.skipTest(f"cannot create HEIC here: {ex}")
        self.assertEqual(photos.validate(buf.getvalue(), "IMG_0001.HEIC").format, "HEIF")

    def test_rejects_other_formats_and_disguises(self):
        buf = io.BytesIO()
        Image.new("RGB", (8, 8)).save(buf, "GIF")
        cases = {
            "a GIF": (buf.getvalue(), "a.gif"),
            "a GIF renamed .jpg": (buf.getvalue(), "a.jpg"),
            "text renamed .jpg": (b"hello, not an image" * 10, "notes.jpg"),
            "an SVG": (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "x.svg"),
            "SVG renamed .png": (b'<svg xmlns="http://www.w3.org/2000/svg"/>', "x.png"),
            "a PNG with a .exe name": (png(), "photo.exe"),
            "no extension": (jpeg(), "photo"),
        }
        for label, (data, name) in cases.items():
            with self.subTest(label), self.assertRaises(photos.PhotoError):
                photos.validate(data, name)

    def test_rejects_empty_and_oversize(self):
        with self.assertRaises(photos.PhotoError):
            photos.validate(b"", "a.jpg")
        with self.assertRaisesRegex(photos.PhotoError, "Too large"):
            photos.validate(b"\xff\xd8" + b"\0" * photos.MAX_BYTES, "a.jpg")

    def test_rejects_too_many_pixels(self):
        buf = io.BytesIO()
        Image.new("1", (9000, 9000)).save(buf, "PNG")  # 81 MP but tiny on disk
        with self.assertRaisesRegex(photos.PhotoError, "megapixels"):
            photos.validate(buf.getvalue(), "huge.png")


class Stripping(unittest.TestCase):
    def test_jpeg_gps_and_exif_removed_capture_date_kept(self):
        src = jpeg(exif=gps_exif())
        self.assertIn(b"Exif", src)  # sanity: the source really carries EXIF/GPS
        self.assertTrue(Image.open(io.BytesIO(src)).getexif().get_ifd(0x8825))
        row = photos.add(src, "IMG_1234.JPG", DAY)
        self.assertEqual(row["capture_date"], "2026-10-20")  # DateTimeOriginal, not DateTime
        for which in ("full", "thumb"):
            data = stored_bytes(row, which)
            img = Image.open(io.BytesIO(data))
            self.assertEqual(dict(img.getexif()), {}, which)
            self.assertNotIn(b"Exif", data, which)
            self.assertNotIn(b"Apple", data, which)
            self.assertIsNone(img.info.get("exif"), which)

    def test_orientation_applied_before_stripping(self):
        row = photos.add(jpeg(size=(60, 20), exif=gps_exif(orientation=6)), "rotated.jpg", DAY)
        self.assertEqual((row["width"], row["height"]), (20, 60))

    def test_heic_converted_to_jpeg_without_exif(self):
        try:
            buf = io.BytesIO()
            Image.new("RGB", (48, 32), (30, 160, 90)).save(buf, "HEIF", exif=gps_exif().tobytes())
        except Exception as ex:
            self.skipTest(f"cannot create HEIC here: {ex}")
        row = photos.add(buf.getvalue(), "IMG_0002.HEIC", DAY)
        self.assertTrue(row["path"].endswith(".jpg"))
        self.assertEqual(row["capture_date"], "2026-10-20")
        data = stored_bytes(row)
        self.assertEqual(Image.open(io.BytesIO(data)).format, "JPEG")
        self.assertNotIn(b"Exif", data)

    def test_png_text_chunks_dropped(self):
        row = photos.add(png(mode="RGBA", text={"Location": "34.17,-118.83", "Author": "me"}), "shot.png", DAY)
        data = stored_bytes(row)
        self.assertTrue(row["path"].endswith(".png"))  # transparency kept as PNG
        self.assertNotIn(b"34.17", data)
        self.assertNotIn(b"tEXt", data)
        self.assertEqual(Image.open(io.BytesIO(data)).text, {})

    def test_no_capture_date_is_fine(self):
        self.assertIsNone(photos.add(png(), "plain.png", DAY)["capture_date"])


class PathsAndApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.app, base_url=server.LOCAL_URL, headers={"Origin": server.LOCAL_URL})

    def upload(self, *files, day=DAY):
        return self.client.post("/api/photos", data={"day": day}, files=[("files", f) for f in files])

    def test_uploaded_name_never_used(self):
        r = self.upload(("../../evil.jpg", jpeg(), "image/jpeg")).json()["results"][0]
        self.assertTrue(r["ok"])
        row = store.photo_get(r["photo"]["id"])
        self.assertRegex(Path(row["path"]).name, r"^[0-9a-f]{32}\.jpg$")
        self.assertTrue(photos.resolve(row, "full").is_relative_to(photos.photos_dir().resolve()))
        self.assertEqual(list(store.DATA_DIR.rglob("evil*")), [])
        self.assertEqual(self.client.get(r["photo"]["full_url"]).headers["x-content-type-options"], "nosniff")

    def test_batch_with_a_bad_file_keeps_the_good_ones(self):
        res = self.upload(("good.jpg", jpeg(), "image/jpeg"), ("bad.jpg", b"not an image", "image/jpeg")).json()["results"]
        self.assertEqual([r["ok"] for r in res], [True, False])
        self.assertIn("valid", res[1]["error"])

    def test_capture_date_mismatch_flagged(self):
        r = self.upload(("trip.jpg", jpeg(exif=gps_exif()), "image/jpeg")).json()["results"][0]
        self.assertEqual((r["capture_date"], r["mismatch"]), ("2026-10-20", True))
        moved = self.client.patch(f"/api/photos/{r['photo']['id']}", json={"day": "2026-10-20"}).json()
        self.assertEqual(moved["day"], "2026-10-20")
        row = store.photo_get(moved["id"])
        self.assertTrue(row["path"].startswith("2026/10/"))
        self.assertIsNotNone(photos.resolve(row, "full"))

    def test_traversal_and_bad_ids_are_404(self):
        for path in ("/api/photos/..%2F..%2F.env/full", "/api/photos/../../.env/full", "/api/photos/%2e%2e/thumb",
                     "/api/photos/" + "A" * 32 + "/full", "/api/photos/" + "0" * 32 + "/full", "/api/photos/abc/full"):
            with self.subTest(path):
                self.assertIn(self.client.get(path).status_code, (404, 422))
        self.assertEqual(self.client.get("/api/photos/" + "0" * 32 + "/../../coach.db").status_code, 404)

    def test_resolve_refuses_paths_outside_the_photos_folder(self):
        self.assertIsNone(photos.resolve({"path": "../coach.db", "thumb": "../coach.db"}, "full"))
        self.assertIsNone(photos.resolve({"path": "/etc/hosts", "thumb": "/etc/hosts"}, "full"))

    def test_bad_day_and_tag_rejected(self):
        self.assertEqual(self.upload(("a.jpg", jpeg(), "image/jpeg"), day="../2026").status_code, 422)
        pid = self.upload(("a.jpg", jpeg(), "image/jpeg")).json()["results"][0]["photo"]["id"]
        self.assertEqual(self.client.patch(f"/api/photos/{pid}", json={"tag": "Secret"}).status_code, 422)
        self.assertEqual(self.client.patch(f"/api/photos/{pid}", json={"tag": "Progress", "caption": " hi "}).json()["caption"], "hi")
        self.assertEqual(self.client.delete(f"/api/photos/{pid}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/photos/{pid}/full").status_code, 404)


class Migration(unittest.TestCase):
    def test_goals_and_settings_move_into_sqlite_unchanged(self):
        d = Path(tempfile.mkdtemp(prefix="coach-mig-"))
        goals = [{"id": "a1", "type": "weight", "target": 88.0, "deadline": "2027-01-01", "notes": "ünïcode"},
                 {"id": "b2", "type": "custom", "title": "Half marathon", "manual_progress": 30}]
        (d / "goals.json").write_text(json.dumps(goals))
        (d / "settings.json").write_text(json.dumps({"units": "us", "theme": "dark"}))
        before = (d / "goals.json").read_bytes()
        code = ("import json, goals, store; print(json.dumps([goals.list_goals(), goals.load_settings(), "
                "[dict(r) for r in store._rows('SELECT key FROM meta')]]))")
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env={**os.environ, "COACH_DATA_DIR": str(d)},
                             capture_output=True, text=True, check=True).stdout
        got_goals, got_settings, meta = json.loads(out)
        self.assertEqual(got_goals, goals)
        self.assertEqual(got_settings, {"units": "us", "theme": "dark"})
        self.assertEqual(meta, [{"key": "json_migrated"}])
        self.assertEqual((d / "goals.json").read_bytes(), before)  # backup untouched


if __name__ == "__main__":
    unittest.main()
