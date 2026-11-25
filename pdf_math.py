"""Lay out confirmed formula fragments as vector PDF text and rules."""

from dataclasses import dataclass
from typing import Callable

from reportlab.pdfbase import pdfmetrics

from structured import formula_nodes


@dataclass(frozen=True)
class MathBox:
    width: float
    above: float
    below: float
    draw: Callable


def text_box(text, size, font_runs):
    runs = [(font, value, pdfmetrics.stringWidth(value, font, size)) for font, value in font_runs(text)]

    def draw(pdf, x, y):
        for font, value, width in runs:
            pdf.setFont(font, size)
            pdf.drawString(x, y, value)
            x += width

    return MathBox(sum(width for _, _, width in runs), size * 0.82, size * 0.23, draw)


def horizontal(boxes):
    boxes = tuple(boxes)

    def draw(pdf, x, y):
        for box in boxes:
            box.draw(pdf, x, y)
            x += box.width

    return MathBox(sum(box.width for box in boxes), max((b.above for b in boxes), default=0),
                   max((b.below for b in boxes), default=0), draw)


def math_box(nodes, size, font_runs):
    def item_box(item):
        match item:
            case ("text", value):
                return text_box(value, size, font_runs)
            case ("group", content):
                return math_box(content, size, font_runs)
            case ("fraction", numerator, denominator):
                a = math_box(numerator, size * 0.77, font_runs)
                b = math_box(denominator, size * 0.77, font_runs)
                width = max(a.width, b.width) + size * 0.24
                bar = size * 0.21
                upper, lower = bar + size * 0.15 + a.below, bar - size * 0.15 - b.above

                def draw(pdf, x, y):
                    a.draw(pdf, x + (width - a.width) / 2, y + upper)
                    b.draw(pdf, x + (width - b.width) / 2, y + lower)
                    pdf.setLineWidth(size * 0.055)
                    pdf.line(x, y + bar, x + width, y + bar)

                return MathBox(width, upper + a.above, b.below - lower, draw)
            case ("sup" | "sub" as kind, base, script):
                a = math_box(base, size, font_runs)
                b = math_box(script, size * 0.68, font_runs)
                shift = size * (0.57 if kind == "sup" else -0.28)

                def draw(pdf, x, y):
                    a.draw(pdf, x, y)
                    b.draw(pdf, x + a.width, y + shift)

                return MathBox(a.width + b.width, max(a.above, b.above + shift),
                               max(a.below, b.below - shift), draw)
            case ("root", content):
                inner = math_box(content, size, font_runs)
                width = inner.width + size * 0.64
                top = inner.above + size * 0.1
                bottom = max(inner.below, size * 0.2)

                def draw(pdf, x, y):
                    inner.draw(pdf, x + size * 0.59, y)
                    pdf.setLineWidth(size * 0.06)
                    path = pdf.beginPath()
                    path.moveTo(x, y + size * 0.13)
                    path.lineTo(x + size * 0.12, y + size * 0.24)
                    path.lineTo(x + size * 0.27, y - bottom)
                    path.lineTo(x + size * 0.53, y + top)
                    path.lineTo(x + width, y + top)
                    pdf.drawPath(path)

                return MathBox(width, top, bottom, draw)

    return horizontal(item_box(item) for item in nodes)


def draw_formula(pdf, fragments, rect, height, scale, font_runs, *, max_size=22):
    size = max(5, min(max_size, rect.height * scale * 0.82))
    box = horizontal(math_box(formula_nodes(value), size, font_runs) if math
                     else text_box(value, size, font_runs) for math, value in fragments)
    ratio = min(1, rect.width * scale / max(box.width, 0.1),
                rect.height * scale / max(box.above + box.below, 0.1))
    pdf.saveState()
    pdf.translate(rect.left * scale, (height - rect.bottom) * scale +
                  (rect.height * scale - (box.above + box.below) * ratio) / 2 + box.below * ratio)
    pdf.scale(ratio, ratio)
    box.draw(pdf, 0, 0)
    pdf.restoreState()
