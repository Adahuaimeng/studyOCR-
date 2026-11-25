"""Conservative, opt-in proposals for editable formula and table objects."""

from dataclasses import dataclass
import re

import cv2
import numpy as np
from illustrations import illustration_role, pixel_area
from ocr_core import Rect, TextKind
from structured import formula_nodes, inline_fragments, suggest_table, table_rows, validate_block
from study import BlockFormat


_SUPERS = "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻"
_SUBS = "₀₁₂₃₄₅₆₇₈₉₊₋"
_DIGITS = "0123456789+-"
_NOTATION = re.compile(r"[\\^_{}√=≠≤≥<>²³⁴⁵⁶⁷⁸⁹₀-₉]")
_FRAGMENT = re.compile(r"[A-Za-z0-9α-ωΑ-Ω∞., ()+\-−×÷*/=<>≤≥≠±∈√π^_{}\\]+")
_PREFIX = re.compile(r"^(.{1,50}[：:])\s*(.*?)([。.]?)$")


@dataclass(frozen=True)
class Proposal:
    bounds: Rect
    format: BlockFormat
    value: str
    reason: str
    method: str
    kind: TextKind = TextKind.REVIEW


def broken_math(text):
    """Flag likely OCR markup failures while preserving ordinary dollar amounts."""
    value = text.strip()
    if re.fullmatch(r"\$\d+(?:[.,]\d+)?", value):
        return False
    if "$" in value:
        if value.count("$") % 2:
            return bool(re.match(r"^\$[A-Za-zα-ωΑ-Ω({\\]", value) or _NOTATION.search(value) or re.fullmatch(
                r"\$?[A-Za-z0-9()]+[+−/\-][A-Za-z0-9()]+\$?|\$[A-Za-z]{1,3}|[A-Za-z]{1,3}\$",
                value))
        fragments = value.split("$")[1::2]
        try:
            return any(not part.strip() or not formula_nodes(part)
                       or (re.search(r"[，。；：]", part) and re.search(r"[A-Za-zα-ω0-9]", part))
                       for part in fragments)
        except ValueError:
            return True
    if re.search(r"\\[A-Za-z]*[{}]|\^[{}]|_\{", value):
        try:
            formula_nodes(value)
        except ValueError:
            return True
    if value.count("{") != value.count("}"):
        return bool(re.search(r"[A-Za-z0-9²³][{}]|[{}][A-Za-z0-9²³]", value)
                    and re.search(r"[=<>≤≥√²³]|[A-Za-z0-9][+\-*/][A-Za-z0-9]", value))
    return False


def validate_confirmation(block):
    validate_block(block)
    values = (tuple(cell for row in table_rows(block.value) for cell in row)
              if block.format == BlockFormat.TABLE else (block.value,))
    if any(broken_math(value) for value in values if value != "^"):
        raise ValueError("仍有残缺公式标记。请对照原图修正，或取消以保留截图。")


def normalize_equation(value):
    value = value.strip()
    if not _FRAGMENT.fullmatch(value) or not _NOTATION.search(value):
        # Unicode scripts also include characters outside the ASCII expression grammar.
        if not any(char in value for char in _SUPERS + _SUBS):
            return ""
    for symbols, operation in ((_SUPERS, "^"), (_SUBS, "_")):
        translation = str.maketrans(symbols, _DIGITS)
        value = re.sub(rf"(?<=[\w)\}}])[{symbols}]+",
                       lambda match: operation + "{" + match[0].translate(translation) + "}", value)
    value = re.sub(r"√\(([^()]+)\)", r"\\sqrt{\1}", value)
    value = re.sub(r"√([A-Za-z0-9α-ω])(?![\w{.(])", r"\\sqrt{\1}", value)
    if ("√" in value or not _FRAGMENT.fullmatch(value) or value.count("(") != value.count(")")
            or re.match(r"[+*/=<>≤≥≠]", value)
            or re.search(r"[+−\-*/=<>≤≥≠^_]\s*$", value)):
        return ""
    try:
        formula_nodes(value)
    except ValueError:
        return ""
    return value


def line_proposal(line):
    value = line.text.strip()
    if broken_math(value):
        return Proposal(line.bounds, BlockFormat.EQUATION, "", "公式字符或边界不完整，请对照原图录入",
                        "source_crop", line.kind)
    threshold = 0.84 if line.kind == TextKind.HANDWRITTEN else 0.76
    if line.confidence < threshold:
        return None
    if "$" in value:
        if value.startswith("$") and value.endswith("$") and value.count("$") == 2:
            value = value[1:-1]
        elif value.count("$") >= 2:
            try:
                fragments = inline_fragments(value)
            except ValueError:
                return None
            normalized = [(math, normalize_equation(part) if math else part) for math, part in fragments]
            if all(part for _, part in normalized):
                value = "".join(f"${part}$" if math else part for math, part in normalized)
                return Proposal(line.bounds, BlockFormat.INLINE, value, "请核对公式与正文边界", "ocr_notation",
                                line.kind)
            return None
    match = _PREFIX.fullmatch(value)
    if match and re.search(r"[\u3400-\u9fff]", match[1]):
        equation = normalize_equation(match[2])
        if equation:
            return Proposal(line.bounds, BlockFormat.INLINE, f"{match[1]}${equation}${match[3]}",
                            "请核对公式与正文边界", "ocr_notation", line.kind)
    equation = normalize_equation(value.removesuffix("."))
    if equation:
        return Proposal(line.bounds, BlockFormat.EQUATION, equation, "请核对数学符号和作用范围",
                        "ocr_notation", line.kind)
    return None


def selection_proposal(scan, area, *, borderless=False):
    lines = [line for line in scan.lines if line.kind != TextKind.OMIT]
    table = suggest_table(scan.image, area, lines, borderless=borderless)
    if table:
        reason = "请核对表格行列、合并和单元格内容"
        if any(broken_math(cell) for row in table.value.splitlines() for cell in row.split("|")):
            reason += "，草稿含未识全的公式"
        if "如图" in table.value:
            reason += "，单元格中的图形可保留原图"
        return Proposal(table.bounds, BlockFormat.TABLE, table.value, reason, table.method, TextKind.PRINTED)
    inside = [line for line in lines if line.bounds.intersection(area) == line.bounds]
    if len(inside) == 1:
        return line_proposal(inside[0])
    if len(inside) == 2:
        return fraction_proposal(scan.image, area, inside)
    return None


def fraction_proposal(image, area, lines):
    numerator, denominator = sorted(lines, key=lambda line: line.bounds.center_y)
    a, b = numerator.bounds, denominator.bounds
    width = max(a.width, b.width)
    gap = b.top - a.bottom
    if not (3 <= gap <= max(a.height, b.height) * 1.5
            and abs(a.left + a.right - b.left - b.right) <= width * 0.6):
        return None
    values = [line.text.strip() for line in (numerator, denominator)]
    if any(line.confidence < 0.8 for line in lines) or any(
        not re.fullmatch(r"[A-Za-zα-ω0-9()+−-]{1,18}", value) for value in values
    ):
        return None
    band = Rect(max(area.left, min(a.left, b.left) - width // 4), a.bottom,
                min(area.right, max(a.right, b.right) + width // 4), b.top)
    gray = np.asarray(band.crop(image).convert("L"))
    ink = np.uint8(gray < max(0, int(np.median(gray)) - 25))
    bar = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((1, max(4, width * 4 // 5)), np.uint8))
    if np.max(np.count_nonzero(bar, axis=1)) < width * 0.8:
        return None
    return Proposal(area, BlockFormat.EQUATION, rf"\frac{{{values[0]}}}{{{values[1]}}}",
                    "按横线和上下文字重建，请核对分子与分母", "fraction_geometry", numerator.kind)


def reconstruction_queue(sheet, regions, blocks):
    scan = sheet.scan
    result = []
    for area in regions:
        role = illustration_role(scan, area)
        if role == "table_crop":
            proposal = selection_proposal(scan, area)
            result.append(proposal or Proposal(area, BlockFormat.TABLE, "", "表格边界不清，请对照原图录入",
                                               "source_crop"))
        elif role == "formula_crop":
            lines = [line for line in scan.lines if line.kind != TextKind.OMIT
                     and line.bounds.intersection(area) == line.bounds]
            proposal = line_proposal(lines[0]) if len(lines) == 1 else None
            if proposal and proposal.value and proposal.format == BlockFormat.EQUATION:
                result.append(proposal)
            else:
                result.append(Proposal(area, BlockFormat.EQUATION, "", "空间公式结构不明确，请对照原图录入",
                                       "source_crop"))
    for line in scan.lines:
        if line.kind == TextKind.OMIT:
            continue
        if any(pixel_area(line.bounds.intersection(area)) >= pixel_area(line.bounds) * 0.72
               for area in (*regions, *(block.bounds for block in blocks))):
            continue
        proposal = line_proposal(line)
        if proposal:
            result.append(proposal)
    return tuple(sorted(result, key=lambda proposal: (proposal.bounds.top, proposal.bounds.left)))
