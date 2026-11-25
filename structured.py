"""Editable table and Office Math support for reviewed selections."""

import re
from dataclasses import dataclass
from statistics import median

import cv2
import numpy as np
from docx.oxml import OxmlElement
from ocr_core import Rect

from study import BlockFormat


HEADER = "[表头]"
CELL_CROP = re.compile(r"@crop\((\d{1,4}),(\d{1,4}),(\d{1,4}),(\d{1,4})\)")


def cell_crop(value):
    if match := CELL_CROP.fullmatch(value):
        left, top, right, bottom = map(int, match.groups())
        if 0 <= left < right <= 1000 and 0 <= top < bottom <= 1000:
            return Rect(left, top, right, bottom)
    if value.startswith("@crop("):
        raise ValueError("单元格截图范围无效，请在表格编辑器中重新框选。")
    return None


def crop_token(bounds):
    return f"@crop({bounds.left},{bounds.top},{bounds.right},{bounds.bottom})"


def cell_crop_bounds(value, table):
    box = cell_crop(value)
    if box is None:
        return None
    left = min(table.right - 1, table.left + round(box.left * table.width / 1000))
    top = min(table.bottom - 1, table.top + round(box.top * table.height / 1000))
    return Rect(left, top, max(left + 1, table.left + round(box.right * table.width / 1000)),
                max(top + 1, table.top + round(box.bottom * table.height / 1000)))


def rotate_cell_crops(value, angle):
    from ocr_quality import rotated_bounds

    def rotate(match):
        box = cell_crop(match[1])
        return crop_token(rotated_bounds(box, (1000, 1000), angle))

    return re.sub(r"(?<![^\t|\n])[ ]*(@crop\(\d{1,4},\d{1,4},\d{1,4},\d{1,4}\))[ ]*(?=[\t|\r\n]|$)",
                  rotate, value)


def table_data(value):
    lines = [row for row in value.splitlines() if row.strip()]
    header = bool(lines and lines[0].strip() == HEADER)
    rows = tuple(tuple(cell.strip() for cell in row.split("\t" if "\t" in row else "|"))
                 for row in lines[int(header):])
    if not rows or not 2 <= len(rows[0]) <= 20 or len(rows) > 40:
        raise ValueError("请填写 2–20 列、最多 40 行的表格，用 Tab 或 | 分隔单元格")
    for index, row in enumerate(rows):
        if len(row) != len(rows[0]):
            raise ValueError("每行的列数必须一致。空单元格也要保留分隔符")
        if index == 0 and "^" in row:
            raise ValueError("第一行不能向上合并")
        for cell in row:
            cell_crop(cell)
    return rows, header


def table_rows(value):
    return table_data(value)[0]


def _bands(mask, axis, threshold):
    counts = np.count_nonzero(mask, axis=axis)
    indices = np.flatnonzero(counts >= threshold)
    if not len(indices):
        return []
    groups = np.split(indices, np.where(np.diff(indices) > 4)[0] + 1)
    return [int(median(group)) for group in groups]


@dataclass(frozen=True)
class TableSuggestion:
    bounds: Rect
    value: str
    method: str


def suggest_table(image, area, lines, *, borderless=False):
    """Deskew the crop, locate rules, and assign OCR text to the cells."""
    gray = np.asarray(area.crop(image).convert("L"))
    h, w = gray.shape
    if min(h, w) < 64:
        return None
    factor = min(1, 1100 / max(h, w))
    gray = cv2.resize(gray, (round(w * factor), round(h * factor)))
    h, w = gray.shape
    edges = cv2.Canny(cv2.GaussianBlur(gray, (3, 3), 0), 24, 75)
    segments = cv2.HoughLinesP(edges, 1, np.pi / 720, 45,
                               minLineLength=max(30, round(min(w, h) * 0.23)), maxLineGap=35)
    horizontal_slopes, vertical_slopes = [], []
    if segments is not None:
        for x, y, right, bottom in segments.reshape(-1, 4):
            dx, dy = right - x, bottom - y
            if abs(dx) >= w * 0.52 and abs(dy) <= abs(dx) * 0.18:
                horizontal_slopes.append(dy / dx)
            if abs(dy) >= h * 0.4 and abs(dx) <= abs(dy) * 0.18:
                vertical_slopes.append(dx / dy)
    slope_y, slope_x = median(horizontal_slopes or [0]), median(vertical_slopes or [0])
    transform = np.array([[1, -slope_x, slope_x * h / 2], [-slope_y, 1, slope_y * w / 2]])
    gray = cv2.warpAffine(gray, transform, (w, h), borderMode=cv2.BORDER_REPLICATE)
    ink = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                cv2.THRESH_BINARY_INV, 25, 6)
    horizontal_ink = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, np.ones((1, 9), np.uint8))
    vertical_ink = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, np.ones((9, 1), np.uint8))
    hz = cv2.morphologyEx(horizontal_ink, cv2.MORPH_OPEN, np.ones((1, max(12, w // 6)), np.uint8))
    vt = cv2.morphologyEx(vertical_ink, cv2.MORPH_OPEN, np.ones((max(12, h // 6), 1), np.uint8))
    drift = max(5, round(h * 0.022))
    horizontal = cv2.dilate(hz, np.ones((drift, 1), np.uint8))
    vertical = cv2.dilate(vt, np.ones((1, max(5, round(w * 0.012))), np.uint8))
    ys = _bands(horizontal, 1, w * 0.48)
    xs = _bands(vertical, 0, h * 0.36)

    def limits(points, size):
        retained = []
        for point in points:
            if not retained or point - retained[-1] >= size * 0.044:
                retained.append(point)
        return retained

    xs, ys = limits(xs, w), limits(ys, h)
    if (not (3 <= len(xs) <= 12 and 3 <= len(ys) <= 26)
            or xs[-1] - xs[0] < w * 0.62 or ys[-1] - ys[0] < h * 0.55):
        return suggest_borderless(area, lines) if borderless else None
    cells = [[[] for _ in xs[1:]] for _ in ys[1:]]
    for line in sorted(lines, key=lambda item: (item.bounds.top, item.bounds.left)):
        center_x = (line.bounds.left + line.bounds.right) / 2
        center_y = line.bounds.center_y
        if not (area.left <= center_x < area.right and area.top <= center_y < area.bottom):
            continue
        x, y = transform @ [(center_x - area.left) * factor, (center_y - area.top) * factor, 1]
        if xs[0] <= x < xs[-1] and ys[0] <= y < ys[-1]:
            col = int(np.searchsorted(xs, x, side="right") - 1)
            row = int(np.searchsorted(ys, y, side="right") - 1)
            cells[row][col].append(cell_text(line))
    if sum(bool(cell) for row in cells for cell in row) < 2:
        return None
    for col, (left, right) in enumerate(zip(xs, xs[1:])):
        active = 0
        inset = max(2, (right - left) // 12)
        for row, y in enumerate(ys[1:-1], 1):
            segment = horizontal_ink[max(0, y - drift):y + drift + 1, left + inset:right - inset]
            if not segment.size:
                active = row
                continue
            segment = cv2.morphologyEx(segment, cv2.MORPH_OPEN,
                                       np.ones((1, max(5, (right - left) // 4)), np.uint8))
            coverage = np.count_nonzero(np.any(segment, axis=0)) / max(1, segment.shape[1])
            if coverage < 0.68 and (cells[active][col] or cells[row][col]):
                cells[active][col].extend(cells[row][col])
                cells[row][col] = ["^"]
            else:
                active = row
    corners = np.array([[x, y, 1] for x in (xs[0], xs[-1]) for y in (ys[0], ys[-1])])
    restored = corners @ cv2.invertAffineTransform(transform).T / factor
    x0, y0 = np.floor(restored.min(axis=0)).astype(int)
    x1, y1 = np.ceil(restored.max(axis=0)).astype(int)
    tighter = Rect(max(area.left, area.left + int(x0)), max(area.top, area.top + int(y0)),
                   min(area.right, area.left + int(x1)), min(area.bottom, area.top + int(y1)))
    return TableSuggestion(tighter, format_cells(cells), "ruled")


def cell_text(line):
    return re.sub(r"[\t\r\n]+", " ", line.text.replace("|", "｜")).strip()


def format_cells(cells):
    return "\n".join("|".join(" ".join(cell) for cell in row) for row in cells)


def suggest_borderless(area, lines):
    from page_structure import group_rows

    contained = [line for line in lines if line.bounds.intersection(area) == line.bounds]
    rows = group_rows(contained)
    if len(rows) < 3 or len(rows) > 40 or not 2 <= len(rows[0]) <= 12:
        return None
    starts = [line.bounds.left for line in rows[0]]
    tolerance = median(line.bounds.height for line in contained) * 0.8
    for row in rows:
        if (len(row) != len(starts)
                or any(abs(line.bounds.left - start) > tolerance for line, start in zip(row, starts))
                or any(b.bounds.left - a.bounds.right < tolerance for a, b in zip(row, row[1:]))):
            return None
    return TableSuggestion(area, format_cells([[[cell_text(line)] for line in row] for row in rows]),
                           "aligned_text")


_SYMBOLS = {
    "pi": "π", "infty": "∞", "times": "×", "cdot": "·", "pm": "±",
    "neq": "≠", "ne": "≠", "le": "≤", "ge": "≥", "in": "∈", "cup": "∪",
    "leq": "≤", "geq": "≥", "notin": "∉", "cap": "∩", "div": "÷", "approx": "≈",
    "sin": "sin", "cos": "cos", "tan": "tan", "ln": "ln", "log": "log",
    "alpha": "α", "beta": "β", "theta": "θ",
}
_TOKEN = re.compile(r"\\[A-Za-z]+|[{}^_\\]|[^{}^_\\]")


def formula_nodes(value):
    value = value.strip().removeprefix("$").removesuffix("$")
    if not value or len(value) > 700:
        raise ValueError("请输入不超过 700 字的公式")
    tokens = iter(_TOKEN.findall(value))
    token = next(tokens, None)

    def advance():
        nonlocal token
        previous = token
        token = next(tokens, None)
        return previous

    def group(depth):
        if token != "{":
            raise ValueError(r"请用花括号标明参数，例如 \frac{1}{2} 或 x^{2}")
        advance()
        nodes = sequence(depth + 1)
        if token != "}" or not nodes:
            raise ValueError("公式花括号不成对或参数为空")
        advance()
        return nodes

    def sequence(depth):
        if depth > 20:
            raise ValueError("公式嵌套过深")
        nodes = []
        while token is not None and token != "}":
            if token == "{":
                nodes.append(("group", group(depth)))
                continue
            part = advance()
            if part in ("^", "_"):
                if not nodes:
                    raise ValueError("上下标前缺少主体")
                nodes.append(("sup" if part == "^" else "sub", (nodes.pop(),), group(depth)))
            elif part in (r"\frac", r"\dfrac", r"\tfrac"):
                nodes.append(("fraction", group(depth), group(depth)))
            elif part == r"\sqrt":
                nodes.append(("root", group(depth)))
            elif part.startswith("\\"):
                command = part[1:]
                if command not in _SYMBOLS:
                    raise ValueError(f"暂不支持公式命令 {part}")
                nodes.append(("text", _SYMBOLS[command]))
            else:
                nodes.append(("text", part))
        return tuple(nodes)

    parsed = sequence(0)
    if token is not None:
        raise ValueError("公式多了一个右花括号")
    return parsed


def _xml(parent, name):
    node = OxmlElement("m:" + name)
    parent.append(node)
    return node


def _append_math(parent, nodes):
    for item in nodes:
        match item:
            case ("text", value):
                text = _xml(_xml(parent, "r"), "t")
                text.text = value
                if value.isspace():
                    text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            case ("group", content):
                _append_math(parent, content)
            case ("fraction", numerator, denominator):
                fraction = _xml(parent, "f")
                _append_math(_xml(fraction, "num"), numerator)
                _append_math(_xml(fraction, "den"), denominator)
            case ("sup" | "sub" as script, base, exponent):
                math = _xml(parent, "sSup" if script == "sup" else "sSub")
                _append_math(_xml(math, "e"), base)
                _append_math(_xml(math, script), exponent)
            case ("root", content):
                radical = _xml(parent, "rad")
                hidden = _xml(_xml(radical, "radPr"), "degHide")
                hidden.set("{http://schemas.openxmlformats.org/officeDocument/2006/math}val", "1")
                _xml(radical, "deg")
                _append_math(_xml(radical, "e"), content)


def append_equation(paragraph, value):
    _append_math(_xml(paragraph._p, "oMath"), formula_nodes(value))


def _plain_math(nodes):
    pieces = []
    for item in nodes:
        match item:
            case ("text", value):
                pieces.append(value)
            case ("group", content):
                pieces.append(_plain_math(content))
            case ("fraction", numerator, denominator):
                pieces.append(f"({_plain_math(numerator)})/({_plain_math(denominator)})")
            case ("sup" | "sub" as script, base, exponent):
                mark = "^" if script == "sup" else "_"
                pieces.append(f"{_plain_math(base)}{mark}({_plain_math(exponent)})")
            case ("root", content):
                pieces.append(f"√({_plain_math(content)})")
    return "".join(pieces)


def linear_equation(value):
    return _plain_math(formula_nodes(value))


def inline_fragments(value, *, require_prose=True):
    parts = tuple((bool(index % 2), part) for index, part in enumerate(value.split("$")))
    if (len(parts) < 3 or len(parts) % 2 == 0
            or require_prose and not any(part.strip() for math, part in parts if not math)):
        raise ValueError("请保留正文，并用成对的 $ 标出行内公式，例如 求证：$x^{2}>0$")
    for math, part in parts:
        if math:
            formula_nodes(part)
    return tuple((math, part) for math, part in parts if part)


def cell_fragments(value):
    if cell_crop(value):
        return ()
    if value.count("$") >= 2:
        try:
            return inline_fragments(value, require_prose=False)
        except ValueError:
            pass
    return ((False, value),)


def structured_text(block):
    if block.format == BlockFormat.TABLE:
        return "\n".join("  |  ".join("" if cell == "^" else "".join(
            linear_equation(value) if math else value for math, value in cell_fragments(cell)
        ) for cell in row)
                         for row in table_rows(block.value))
    if block.format == BlockFormat.INLINE:
        return "".join(linear_equation(value) if math else value
                       for math, value in inline_fragments(block.value))
    return linear_equation(block.value)


def validate_block(block):
    if block.format == BlockFormat.TABLE:
        table_rows(block.value)
    elif block.format == BlockFormat.INLINE:
        inline_fragments(block.value)
    else:
        formula_nodes(block.value)
