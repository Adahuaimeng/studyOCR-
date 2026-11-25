"""Content-based page direction and per-page OCR diagnostics."""

from dataclasses import dataclass
from hashlib import sha256
from os import environ
from pathlib import Path
import sqlite3
from time import perf_counter

from PIL import Image, ImageStat

from ocr_core import Rect, Scan


ANGLES = (0, 90, 180, 270)
ROTATIONS = ("auto", "keep", "0", "90", "180", "270")
TRANSPOSE = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_90}
DIRECTION_PROBE_VERSION = "direction-v2"
QUALITY_TEXT = {
    "ok": ("识别完成", ""),
    "blank": ("近乎空白", "可保留空白页，或检查原图后重试。"),
    "no_text": ("未检测到文字", "请检查方向、画质或选择有文字的区域重试。"),
    "low_quality": ("识别结果需复核", "请检查原图，或切换识别模式后重试。"),
    "direction_uncertain": ("方向疑似有误或无法确认", "可在原图校对方向后，只重试此页。"),
    "error": ("识别失败", "请检查模型和图像，随后重试此页。"),
}


def rotated_image(image, angle):
    return image.transpose(TRANSPOSE[angle]) if angle else image


def rotated_bounds(bounds, size, angle):
    left, top, right, bottom = bounds.left, bounds.top, bounds.right, bounds.bottom
    w, h = size
    if angle == 90:
        return Rect(h - bottom, left, h - top, right)
    if angle == 180:
        return Rect(w - right, h - bottom, w - left, h - top)
    if angle == 270:
        return Rect(top, w - right, bottom, w - left)
    return bounds


@dataclass(frozen=True)
class ScanAttempt:
    image: Image.Image
    scan: Scan
    rotation: int
    quality: dict


def quality_result(code, *, mean=None, probe_seconds=0, rotation=0):
    label, action = QUALITY_TEXT[code]
    return {"status": code, "reason": label, "action": action,
            "mean_confidence": mean, "probe_seconds": round(probe_seconds, 3), "applied_rotation": rotation}


def thumbnail(image, limit=1050):
    ratio = min(1, limit / max(image.size))
    if ratio == 1:
        return image
    return image.resize(tuple(max(1, round(side * ratio)) for side in image.size),
                        Image.Resampling.BICUBIC, reducing_gap=2.0)


def is_blank(image):
    return ImageStat.Stat(image.convert("L")).stddev[0] < 2


def horizontal(line):
    return line.bounds.width >= line.bounds.height * 1.3


def direction_score(scan):
    return sum(min(25, len(line.text)) * max(0, line.confidence - 0.58) ** 2
               for line in scan.lines if horizontal(line))


def direction_key(image, model_key):
    if not model_key:
        return None
    digest = sha256(f"{DIRECTION_PROBE_VERSION}:auto:{model_key}:{image.mode}:{image.size}:".encode())
    digest.update(image.tobytes())
    return digest.hexdigest()


def direction_cache(key, angle=None):
    if key is None:
        return None
    directory = Path(environ.get("LOCALAPPDATA") or Path.home()) / "StudyOCR"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(directory / "directions.sqlite3", timeout=0.2)
        try:
            with connection:
                connection.execute("CREATE TABLE IF NOT EXISTS orientation (key TEXT UNIQUE, angle INTEGER)")
                if angle is None:
                    row = connection.execute("SELECT angle FROM orientation WHERE key=?", (key,)).fetchone()
                    return row[0] if row and row[0] in ANGLES else None
                connection.execute("INSERT OR REPLACE INTO orientation VALUES (?, ?)", (key, angle))
                connection.execute("DELETE FROM orientation WHERE rowid IN "
                                   "(SELECT rowid FROM orientation ORDER BY rowid DESC LIMIT -1 OFFSET 512)")
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return None


def find_rotation(engine, photo):
    initial = engine.recognize(photo)
    score = direction_score(initial)
    strong = sum(line.confidence >= .82 and horizontal(line) for line in initial.lines)
    if strong >= 8 and strong >= .55 * len(initial.lines) and score >= 6:
        return 0, False
    scores = {0: score}
    for angle in ANGLES[1:]:
        scores[angle] = direction_score(engine.recognize(rotated_image(photo, angle)))
    winner, runner = sorted(ANGLES, key=scores.get, reverse=True)[:2]
    if scores[winner] < 1 or scores[winner] < scores[runner] * 1.12:
        return 0, True
    return winner, False


def probe_rotation(engine, image):
    quick = thumbnail(image, 520)
    first = thumbnail(image, 820) if image.height >= image.width else quick
    initial = engine.recognize(first)
    score = direction_score(initial)
    strong = sum(line.confidence >= .82 and horizontal(line) for line in initial.lines)
    if strong >= 8 and strong >= .55 * len(initial.lines) and score >= 6:
        return 0, False
    scores = {0: direction_score(engine.recognize(quick)) if first is not quick else score}
    for angle in ANGLES[1:]:
        scores[angle] = direction_score(engine.recognize(rotated_image(quick, angle)))
    winner, runner = sorted(ANGLES, key=scores.get, reverse=True)[:2]
    if scores[winner] >= 1.5 and scores[winner] >= scores[runner] * 1.35:
        return winner, False
    return find_rotation(engine, thumbnail(image))


def inspect_scan(scan, uncertain):
    if not scan.lines:
        return "no_text", None
    mean = sum(line.confidence for line in scan.lines) / len(scan.lines)
    wide = sum(horizontal(line) for line in scan.lines)
    if uncertain or (len(scan.lines) >= 6 and wide < len(scan.lines) * .4):
        return "direction_uncertain", round(mean, 4)
    if mean < .75:
        return "low_quality", round(mean, 4)
    return "ok", round(mean, 4)


def recognize_page(engine, image, area=None, *, rotation="auto", handwriting=False, mixed=False):
    began = perf_counter()
    explicit = rotation not in ("auto", "keep")
    angle = int(rotation) if explicit else 0
    uncertain = False
    photo = thumbnail(area.crop(image) if area else image)
    blank = is_blank(photo)
    if not blank and rotation == "auto":
        key = direction_key(image, getattr(engine, "direction_model_key", None))
        cached = direction_cache(key)
        if cached is not None:
            angle = cached
        else:
            angle, uncertain = probe_rotation(engine, image)
            if not uncertain:
                direction_cache(key, angle)
    probe_seconds = perf_counter() - began
    size = image.size
    image = rotated_image(image, angle)
    area = rotated_bounds(area, size, angle) if area else Rect(0, 0, *image.size)
    if blank:
        scan = Scan(area.crop(image), area, (), 0)
        status, mean = "blank", None
    else:
        scan = engine.recognize(image, area, handwriting=handwriting, mixed=mixed)
        status, mean = inspect_scan(scan, uncertain)
    return ScanAttempt(image, scan, angle, quality_result(status, mean=mean, probe_seconds=probe_seconds, rotation=angle))
