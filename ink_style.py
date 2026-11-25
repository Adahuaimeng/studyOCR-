"""Suggest text styles from local glyph shapes and OCR character positions."""
from dataclasses import dataclass
from functools import lru_cache
from os import environ
from pathlib import Path
import re
from statistics import median

import cv2
import numpy as np
from PIL import ImageFont

from ocr_core import Rect, TextKind, TextLine


@dataclass(frozen=True)
class Glyph:
    text: str
    confidence: float
    bounds: Rect


def character_mask(gray):
    if min(gray.shape[:2]) < 5:
        return None
    threshold, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    if threshold < 12:
        return None
    ys, xs = np.where(mask > 0)
    if len(xs) < 8 or len(xs) > gray.size * 0.69:
        return None
    x0, x1 = np.quantile(xs, [0.025, 0.975]).astype(int)
    y0, y1 = np.quantile(ys, [0.025, 0.975]).astype(int)
    if x1 - x0 < 3 or y1 - y0 < 3:
        return None
    tight = mask[y0:y1 + 1, x0:x1 + 1]
    resized = cv2.resize(tight, (28, 28), interpolation=cv2.INTER_AREA) >= 96
    return np.pad(resized, 2)


@lru_cache(maxsize=1)
def typefaces():
    folder = Path(environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    faces = []
    for name in ("simsun.ttc", "simhei.ttf", "simfang.ttf", "simkai.ttf", "msyh.ttc"):
        try:
            faces.append(ImageFont.truetype(str(folder / name), size=54))
        except OSError:
            continue
    return tuple(faces)


@lru_cache(maxsize=4096)
def type_samples(character):
    masks = []
    for face in typefaces():
        raw = face.getmask(character, mode="L")
        if min(raw.size) < 5:
            continue
        gray = 255 - np.asarray(raw, dtype=np.uint8).reshape(raw.size[1], raw.size[0])
        gray = np.pad(gray, 3, constant_values=255)
        mask = character_mask(gray)
        if mask is not None:
            masks.append(mask)
    return tuple(masks)


def resemblance(gray, glyph):
    if len(glyph.text) != 1 or not "\u3400" <= glyph.text <= "\u9fff":
        return None
    b = glyph.bounds
    sample = character_mask(gray[b.top:b.bottom, b.left:b.right])
    templates = type_samples(glyph.text)
    if sample is None or not templates:
        return None
    return max(
        2 * np.count_nonzero(shifted & template) / (shifted.sum() + template.sum())
        for template in templates
        for dx, dy in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1))
        for shifted in (np.roll(sample, (dx, dy), axis=(0, 1)),)
    )


def glyph_scores(gray, words):
    return [resemblance(gray, glyph) for glyph in words]


def bounds_union(rectangles):
    rectangles = tuple(rectangles)
    return Rect(min(b.left for b in rectangles), min(b.top for b in rectangles),
                max(b.right for b in rectangles), max(b.bottom for b in rectangles))


def text_pieces(line, words, kinds):
    pieces = []
    cursor = 0
    spans = []
    for glyph in words:
        start = line.text.find(glyph.text, cursor)
        if start < 0:
            return (line,)
        spans.append((cursor, start + len(glyph.text)))
        cursor = spans[-1][1]
    spans[-1] = (spans[-1][0], len(line.text))
    first = 0
    for end in range(1, len(words) + 1):
        if end < len(words) and kinds[end] == kinds[first]:
            continue
        value = line.text[spans[first][0]:spans[end - 1][1]].strip()
        if value:
            group = words[first:end]
            pieces.append(TextLine(bounds_union(glyph.bounds for glyph in group), value,
                                   median(glyph.confidence for glyph in group),
                                   kinds[first] or TextKind.REVIEW))
        first = end
    return tuple(pieces) or (line,)


def stroke_weight(gray, glyph):
    b = glyph.bounds
    roi = gray[b.top:b.bottom, b.left:b.right]
    if min(roi.shape) < 5:
        return 0.0
    _, mask = cv2.threshold(roi, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    distance = cv2.distanceTransform(mask, cv2.DIST_L2, 3)
    positive = distance[mask > 0]
    return float(np.quantile(positive, 0.80)) if len(positive) else 0.0


def ink_runs(indices):
    current = []
    for index in indices:
        if current and index - current[-1] > 2:
            yield current
            current = []
        current.append(index)
    if current:
        yield current


def classify(gray, line, words, typical_height):
    if not words:
        return (line,)
    scores = glyph_scores(gray, words)
    chinese = [score for score in scores if score is not None]
    anchors = [index for index, score in enumerate(scores) if score is not None and score >= 0.65]
    value = line.text
    marked_answer = re.search(r"[（(]([X×√✓✔VvBbYyACD])[）)]$", value)
    split_answer = (
        marked_answer and len(words) >= 3 and words[-2].text == marked_answer.group(1)
        and (len(words) == 3 or len(anchors) >= 3)
        and (len(words) > 3 or line.bounds.width >= 1.45 * line.bounds.height)
    )
    if len(chinese) >= 2 and not anchors and (
        median(chinese) < 0.45
        or (line.bounds.height >= typical_height * 1.4 and median(chinese) < 0.57)
    ):
        return (TextLine(line.bounds, value, line.confidence, TextKind.HANDWRITTEN),)
    if len(anchors) < 2 and not split_answer:
        return (line,)

    kinds = [TextKind.PRINTED] * len(words)
    pending = []
    for index, score in enumerate(scores + [1.0]):
        if score is None:
            continue
        if score < 0.61:
            pending.append(index)
            continue
        if sum(scores[i] < 0.56 for i in pending) >= 2:
            for i in range(pending[0], pending[-1] + 1):
                kinds[i] = TextKind.REVIEW
        pending.clear()

    if len(anchors) >= 2:
        strokes = [stroke_weight(gray, glyph) for glyph in words]
        reference = median(strokes[index] for index in anchors)
        heavy = max(reference * 1.22, reference + 0.5)
        for run in ink_runs(index for index, width in enumerate(strokes)
                            if width >= heavy and (scores[index] is None or scores[index] < 0.66)):
            start, end = run[0], run[-1]
            distinctive = [i for i in run if words[i].text not in "，。；：、,.!?()（）“”"]
            weak_shape = any(scores[i] is not None and scores[i] < 0.61 for i in distinctive)
            if len(distinctive) >= 3 or (len(distinctive) >= 2 and weak_shape):
                next_index = end + 1
                if (next_index < len(words) and scores[next_index] is None
                    and words[next_index].text.isalnum() and words[next_index].confidence < 0.73
                    and strokes[next_index] >= heavy * 0.94):
                    end = next_index
                for i in range(start, end + 1):
                    if words[i].text not in "，。；：、“”" or start < i < end:
                        kinds[i] = TextKind.HANDWRITTEN
            elif len(distinctive) == 1:
                i = distinctive[0]
                word = words[i]
                if (i == 0 and word.text.isascii() and word.text.isalnum() and word.confidence < 0.55
                    and words[1].bounds.left - word.bounds.right > word.bounds.width * 0.25):
                    kinds[i] = TextKind.HANDWRITTEN
                elif word.confidence < 0.55:
                    kinds[i] = TextKind.REVIEW

    if split_answer:
        kinds[-3:] = [TextKind.PRINTED, TextKind.HANDWRITTEN, TextKind.PRINTED]
    if all(kind == TextKind.PRINTED for kind in kinds):
        return (TextLine(line.bounds, value, line.confidence, TextKind.PRINTED),)
    return text_pieces(line, words, kinds)
