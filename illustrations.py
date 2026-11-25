"""Locate two-dimensional ink such as plots, ruled grids and diagrams."""

from math import ceil, floor
import re
from statistics import median

import cv2
import numpy as np

from ocr_core import Rect


def rect_union(a, b):
    return Rect(min(a.left, b.left), min(a.top, b.top),
                max(a.right, b.right), max(a.bottom, b.bottom))


def pixel_area(bounds):
    return bounds.width * bounds.height if bounds else 0


def merge_regions(boxes, related):
    merged = []
    for box in boxes:
        index = 0
        while index < len(merged):
            if related(box, merged[index]):
                box = rect_union(box, merged.pop(index))
                index = 0
            else:
                index += 1
        merged.append(box)
    return merged


def detect_illustrations(image, lines):
    """Return source-pixel crops for shapes with continuous, sparse 2-D strokes."""
    width, height = image.size
    ratio = min(1.0, 1500 / max(width, height))
    w, h = max(1, round(width * ratio)), max(1, round(height * ratio))
    if min(w, h) < 48:
        return ()
    sx, sy = w / width, h / height
    gray = cv2.resize(np.asarray(image.convert("L")), (w, h), interpolation=cv2.INTER_AREA)
    # Remove the sheet's lighting gradient while retaining thin ink and table rules.
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (17, 17))
    background = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, kernel)
    ink = np.uint8(cv2.subtract(background, gray) >= 15) * 255
    ink = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))

    def reduced(rect):
        return Rect(floor(rect.left * sx), floor(rect.top * sy),
                    ceil(rect.right * sx), ceil(rect.bottom * sy))

    prose = []
    labels = []
    recognized = []
    for line in lines:
        if line.text.strip():
            recognized.append(reduced(line.bounds))
        if sum("\u3400" <= char <= "\u9fff" for char in line.text) >= 5:
            prose.append(reduced(line.bounds))
        elif "题图" in line.text or len(line.text.strip()) == 1:
            labels.append(reduced(line.bounds))
    glyph = max(9, min(31, round(median([rect.height for rect in prose] or [36]) * 0.43)))
    min_side = max(32, glyph * 1.55)
    _, _, statistics, _ = cv2.connectedComponentsWithStats(ink)
    shapes = []
    for x, y, dx, dy, count in statistics[1:]:
        if not (
            min(dx, dy) >= min_side and dx + dy >= glyph * 5
            and dx * dy >= 2000 and 0.013 <= count / (dx * dy) <= 0.19
            and 0.24 <= dx / dy <= 4.4
        ):
            continue
        # A photographed page edge can be connected to a horizontal header rule.
        if (dy > h * 0.91 or dx > w * 0.94
                or (min(x, w - x - dx) <= 2 and dy > h * 0.63)):
            continue
        bounds = Rect(int(x), int(y), int(x + dx), int(y + dy))
        text_overlap = [pixel_area(bounds.intersection(text)) for text in prose]
        if (max(text_overlap, default=0) >= pixel_area(bounds) * 0.55
                or sum(text_overlap) >= pixel_area(bounds) * 0.76):
            continue
        shapes.append(bounds)

    def same_row(a, b):
        vertical = min(a.bottom, b.bottom) - max(a.top, b.top)
        horizontal_gap = max(a.left, b.left) - min(a.right, b.right)
        return (vertical >= min(a.height, b.height) * 0.38
                and horizontal_gap <= glyph * 2.8)

    results = []
    for core in merge_regions(shapes, same_row):
        pad = max(7, round(glyph * 0.9))
        left, right = max(0, core.left - pad), min(w, core.right + pad)
        top, bottom = max(0, core.top - pad), min(h, core.bottom + round(glyph * 1.8))
        if not prose and core.width >= w * 0.72 and core.height >= h * 0.5 and bottom >= h * 0.81:
            # The crop may be just one figure, with labels beyond the connected strokes.
            bottom = h
        for label in labels:
            if (core.left <= (label.left + label.right) / 2 <= core.right
                    and core.bottom <= label.center_y <= core.bottom + glyph * 2.5):
                bottom = max(bottom, min(h, label.bottom + 3))
        for text in prose:
            shared = min(core.right, text.right) - max(core.left, text.left)
            if shared <= min(core.width, text.width) * 0.2:
                continue
            if text.bottom <= core.top:
                top = max(top, text.bottom)
            elif text.top >= core.bottom:
                bottom = min(bottom, text.top)
        extent = Rect(left, top, right, bottom)
        # A warm shadow or finger can be mistaken for a stroke at the page edge.
        if (extent.bottom > h * 0.97 and extent.top > h * 0.83
                and not any(extent.intersection(text) for text in recognized)):
            source = Rect(floor(left / sx), floor(top / sy), min(width, ceil(right / sx)),
                          min(height, ceil(bottom / sy)))
            rgb = np.asarray(source.crop(image).convert("RGB").resize((64, 64)), dtype=np.int16)
            if np.mean(rgb[:, :, 0] - rgb[:, :, 1] > 15) >= 0.075:
                continue
        results.append(Rect(floor(left / sx), floor(top / sy),
                            min(width, ceil(right / sx)), min(height, ceil(bottom / sy))))
    return tuple(sorted(results, key=lambda rect: (rect.top, rect.left)))


def illustration_role(scan, bounds):
    """Label a crop conservatively, leaving ambiguous fragments as figures."""
    lines = [line.text.strip() for line in scan.lines
             if pixel_area(bounds.intersection(line.bounds)) >= pixel_area(line.bounds) * 0.4]
    if any(re.match(r"^(?:续表|表\s*\d+)", text) for text in lines):
        return "table_crop"
    if (bounds.width >= scan.image.width * 0.25
            and sum(sum("\u3400" <= char <= "\u9fff" for char in text) >= 2 for text in lines) >= 3
            and has_table_rules(bounds.crop(scan.image))):
        return "table_crop"
    if bounds.width < scan.image.width * 0.16 and bounds.height < scan.image.height * 0.08:
        equation = any(re.search(r"[√=²³⁴≠<>]|\\frac|[+-](?=[a-z\d])", text) for text in lines)
        equation |= any("$" in text for text in lines) and bounds.width < bounds.height
        axis = any(re.fullmatch(r"[O0xyXY]|[fy]\s*[=(].*", text.strip("$"))
                   for text in lines)
        if equation and not axis:
            return "formula_crop"
    return "figure"


def has_table_rules(image):
    scale = min(1, 450 / max(image.size))
    width, height = (max(1, round(length * scale)) for length in image.size)
    gray = cv2.resize(np.asarray(image.convert("L")), (width, height), interpolation=cv2.INTER_AREA)
    edges = cv2.Canny(gray, 30, 90)
    segments = cv2.HoughLinesP(edges, 1, np.pi / 360, 35,
                               minLineLength=max(25, min(width, height) // 3), maxLineGap=16)
    if segments is None:
        return False
    rows, columns = [], []
    for x, y, right, bottom in segments.reshape(-1, 4):
        dx, dy = abs(right - x), abs(bottom - y)
        if dx >= width * 0.42 and dy <= dx * 0.13:
            rows.append((y + bottom) / 2)
        if dy >= height * 0.52 and dx <= dy * 0.13:
            columns.append((x + right) / 2)
    if not rows or not columns:
        return False
    top, bottom = min(rows), max(rows)
    return (max(columns) - min(columns) >= width * 0.3
            and any(top + height * 0.09 <= row <= bottom - height * 0.09 for row in rows))
