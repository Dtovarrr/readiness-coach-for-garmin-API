"""Daily photos: validation, metadata stripping, HEIC conversion, thumbnails, local storage.

Photos never leave this machine: they're stored under .data/photos/YYYY/MM/ and served only by
the local server. Stored copies are re-encoded from pixels, so EXIF, XMP and GPS are dropped; only
the capture *date* is read first (to suggest which day a photo belongs to) and the ICC colour
profile is kept. File names are our own random IDs - the uploaded name is never used for paths.
"""
from __future__ import annotations

import io
import re
import time
import uuid
from datetime import date, datetime
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener

import store

register_heif_opener()

MAX_BYTES = 25 * 1024 * 1024
MAX_PIXELS = 80_000_000
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif"}
ALLOWED_FORMATS = {"JPEG", "MPO", "PNG", "HEIF"}  # iPhone JPEGs often open as MPO
THUMB_PX = 480
ID_RE = re.compile(r"^[0-9a-f]{32}$")
EXIF_DATETIME_ORIGINAL, EXIF_DATETIME, EXIF_IFD = 0x9003, 0x0132, 0x8769


class PhotoError(ValueError):
    """A user-facing reason an upload was rejected."""


def photos_dir() -> Path:
    return store.DATA_DIR / "photos"


def _capture_date(img: Image.Image) -> str | None:
    try:
        exif = img.getexif()
        raw = exif.get_ifd(EXIF_IFD).get(EXIF_DATETIME_ORIGINAL) or exif.get(EXIF_DATETIME)
        return datetime.strptime(str(raw).strip()[:10], "%Y:%m:%d").date().isoformat() if raw else None
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def _clean_copy(img: Image.Image) -> tuple[Image.Image, str]:
    """Pixels only (EXIF orientation applied), ready to save without metadata."""
    img = ImageOps.exif_transpose(img)
    has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    if has_alpha:
        return img.convert("RGBA"), "PNG"
    return img.convert("RGB"), "JPEG"


def _save(img: Image.Image, fmt: str, path: Path, icc: bytes | None) -> int:
    buf = io.BytesIO()
    kw = {"icc_profile": icc} if icc else {}
    if fmt == "JPEG":
        img.save(buf, "JPEG", quality=88, optimize=True, progressive=True, **kw)
    else:
        img.save(buf, "PNG", optimize=True, **kw)  # no text chunks / eXIf: only what we pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(buf.getvalue())
    tmp.replace(path)
    return len(buf.getvalue())


def validate(data: bytes, filename: str) -> Image.Image:
    """Raise PhotoError unless `data` is a real JPEG/PNG/HEIC within the limits."""
    if not data:
        raise PhotoError("The file is empty.")
    if len(data) > MAX_BYTES:
        raise PhotoError(f"Too large ({len(data) / 1048576:.0f} MB); the limit is {MAX_BYTES // 1048576} MB.")
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise PhotoError("Only JPEG, PNG and HEIC photos are supported.")
    try:
        img = Image.open(io.BytesIO(data))
        if img.format not in ALLOWED_FORMATS:
            raise PhotoError("Only JPEG, PNG and HEIC photos are supported.")
        if img.width * img.height > MAX_PIXELS:
            raise PhotoError("That image is too large to process (over 80 megapixels).")
        img.load()
    except PhotoError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise PhotoError("That doesn't look like a valid JPEG, PNG or HEIC photo.")
    return img


def _paths(photo_id: str, day: str, fmt: str) -> tuple[Path, Path]:
    folder = photos_dir() / day[:4] / day[5:7]
    return folder / f"{photo_id}.{'png' if fmt == 'PNG' else 'jpg'}", folder / f"{photo_id}_t.jpg"


def add(data: bytes, filename: str, day: str) -> dict:
    """Validate, strip metadata, store full + thumbnail, record in the DB. Returns the row."""
    date.fromisoformat(day)  # caller validated; this is a second guard
    img = validate(data, filename)
    captured = _capture_date(img)
    icc = img.info.get("icc_profile")
    clean, fmt = _clean_copy(img)
    photo_id = uuid.uuid4().hex
    full, thumb = _paths(photo_id, day, fmt)
    size = _save(clean, fmt, full, icc)
    t = clean.convert("RGB")
    t.thumbnail((THUMB_PX, THUMB_PX))
    _save(t, "JPEG", thumb, icc)
    row = dict(id=photo_id, day=day, capture_date=captured, caption="", tag="Other", width=clean.width, height=clean.height,
               bytes=size, path=str(full.relative_to(photos_dir())), thumb=str(thumb.relative_to(photos_dir())),
               created=time.strftime("%Y-%m-%dT%H:%M:%S"))
    store.photo_insert(row)
    return row


def resolve(photo: dict, which: str) -> Path | None:
    """Absolute path of a stored file, guaranteed to be inside the photos folder."""
    root = photos_dir().resolve()
    p = (root / photo["thumb" if which == "thumb" else "path"]).resolve()
    return p if p.is_relative_to(root) and p.is_file() else None


def move(photo: dict, day: str) -> dict:
    """File a photo under another day (moves it into that YYYY/MM folder)."""
    date.fromisoformat(day)
    fmt = "PNG" if photo["path"].endswith(".png") else "JPEG"
    new_full, new_thumb = _paths(photo["id"], day, fmt)
    for old, new in ((resolve(photo, "full"), new_full), (resolve(photo, "thumb"), new_thumb)):
        if old and old != new:
            new.parent.mkdir(parents=True, exist_ok=True)
            old.replace(new)
    fields = dict(day=day, path=str(new_full.relative_to(photos_dir())), thumb=str(new_thumb.relative_to(photos_dir())))
    store.photo_update(photo["id"], fields)
    return {**photo, **fields}


def remove(photo: dict) -> None:
    for which in ("full", "thumb"):
        p = resolve(photo, which)
        if p:
            p.unlink(missing_ok=True)
    store.photo_delete(photo["id"])


def public(photo: dict) -> dict:
    """What the page needs (no filesystem paths)."""
    return {k: photo[k] for k in ("id", "day", "capture_date", "caption", "tag", "width", "height", "created")} | {
        "thumb_url": f"/api/photos/{photo['id']}/thumb", "full_url": f"/api/photos/{photo['id']}/full"}
