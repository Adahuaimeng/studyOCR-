"""Derive reading order and semantic roles without changing OCR lines."""

from dataclasses import dataclass
from itertools import groupby
import re
from statistics import median

from ocr_core import Rect, TextLine


HEADING = re.compile(r"^(?:第[一二三四五六七八九十百\d]+[章节篇]|(?:题型|层级|策略|专题)[一二三四五六七八九十\d]+)")
CALLOUT = re.compile(r"^(?:知识[·・ ]*诊断自测|知识拓展|诊断自测|拓展视野|感悟提升|考点\s*聚焦突破)")
QUESTION = re.compile(r"^(?:第\d+题|(?:典例|例题?|训练|练习)(?=[\d\s（(已若：:]|$))")
NUMBERED = re.compile(r"^\d+[.．、](?!\d)")
SUBQUESTION = re.compile(r"^[（(][一二三四五六七八九十\d]+[）)]|^[①②③④⑤⑥⑦⑧⑨]")
OPTION = re.compile(r"^[A-H][.．、)）]|^[（(][A-H][）)]")
CAPTION = re.compile(r"^(?:(?:第\s*)?[（(]?\d+[）)]?\s*题图|(?:图|表)\s*[一二三四五六七八九十\d]+(?:[-—.．]\d+)*(?!\d))")
MATH = re.compile(r"[=＝≠≥≤<>√∑∫±²³]|\\(?:frac|sqrt)|\$|[+−/]")
ROLE_NAMES = {"heading": "标题", "paragraph": "正文", "question": "题干", "subquestion": "小问",
              "option": "选项", "formula": "公式", "caption": "图注", "list_item": "编号知识点",
              "callout_label": "栏目提示"}
KNOWLEDGE = re.compile(r"^(?:(?:从图形看|定义|分类|概念|公式|性质|终边相同|定极值点|构造函数|判断单调性|比较大小)|[^。；：=<>]{1,22}(?:的定义|的推广|的方法|的概念|的公式)|(?:弧度制|象限角|轴线角|任意角的三角函数)(?:$|[的，。]))")
TASK = re.compile(r"(?:已知|求证|试证|证明[：:]?|下列|填空|多选|思考辨析|求.{0,12}(?:范围|值|方程)|的象限是|括号内打)")


@dataclass(frozen=True)
class Band:
    columns: tuple[tuple[TextLine, ...], ...]


@dataclass(frozen=True)
class PageLayout:
    gutter: int | None
    bands: tuple[Band, ...]

    @property
    def reading_order(self):
        lines = tuple(line for band in self.bands for column in band.columns for line in column)
        return tuple(sorted(lines, key=lambda line: line.review_order if line.review_order is not None else float("inf")))


@dataclass(frozen=True)
class TextBlock:
    role: str
    bounds: Rect
    band: int
    column: int | None
    lines: tuple[TextLine, ...]


def vertically_stacked(a, b):
    horizontal = min(a.right, b.right) - max(a.left, b.left)
    return (horizontal > min(a.width, b.width) * 0.45
            and abs(a.center_y - b.center_y) > min(a.height, b.height) * 0.28)


def group_rows(items):
    rows = []
    for item in sorted(items, key=lambda item: item.bounds.center_y):
        if rows:
            first = rows[-1][0].bounds
            distance = abs(item.bounds.center_y - first.center_y)
            if distance <= min(first.height, item.bounds.height) * 0.6 and not any(
                vertically_stacked(other.bounds, item.bounds) for other in rows[-1]
            ):
                rows[-1].append(item)
                continue
        rows.append([item])
    return [sorted(row, key=lambda item: item.bounds.left) for row in rows]


def ordered(lines):
    return tuple(line for row in group_rows(lines) for line in row)


def find_gutter(lines, width):
    # Use substantial lines as evidence. Short formulas and answer choices can fill
    # the gap without marking the start of another text column.
    candidates = [line.bounds for line in lines
                  if line.bounds.width >= width * 0.145 and len(line.text.strip()) >= 7]
    if len(candidates) < 6:
        return None
    typical = median(box.height for box in candidates)
    scores = []
    for step in range(29, 72):
        x = round(width * step / 100)
        tolerance = width * 0.009
        left = [b for b in candidates if b.right <= x + tolerance and b.left < x - width * 0.12]
        right = [b for b in candidates if b.left >= x - tolerance and b.right > x + width * 0.12]
        if min(len(left), len(right)) < 3:
            continue
        shared_top = max(min(b.top for b in left), min(b.top for b in right))
        shared_bottom = min(max(b.bottom for b in left), max(b.bottom for b in right))
        if shared_bottom - shared_top < typical * 1.6:
            continue
        near = sum(b.left <= x + width * 0.095 and b.bottom >= shared_top
                   and b.top <= shared_bottom for b in right)
        if near < 2:
            continue
        crossings = sum(b.left < x - tolerance and b.right > x + tolerance
                        and b.bottom > shared_top and b.top < shared_bottom for b in candidates)
        if crossings > max(2, min(len(left), len(right)) * 0.38):
            continue
        clearance = min(x - max(b.right for b in left), min(b.left for b in right) - x)
        scores.append((min(len(left), len(right)) + near * 0.25 - crossings * 2,
                       clearance, -abs(x - width / 2), x))
    return max(scores)[-1] if scores else None


def page_layout(scan):
    lines = scan.lines
    width = scan.image.width
    gutter = find_gutter(lines, width)
    if gutter is None:
        return PageLayout(None, (Band((ordered(lines),)),))

    def spans(line):
        b = line.bounds
        crossing = b.left < gutter - width * 0.035 and b.right > gutter + width * 0.035
        return crossing and (b.width >= width * 0.48 or bool(HEADING.match(line.text.strip()))
                             or (b.top <= scan.image.height * 0.09
                                 and b.left > width * 0.22 and b.width >= width * 0.2))

    bands = []
    for shared, rows in groupby(group_rows(lines), key=lambda row: any(spans(line) for line in row)):
        lines_in_run = [line for row in rows for line in row]
        if shared:
            bands.append(Band((tuple(lines_in_run),)))
        else:
            # Row calculation happens again within each column, so equal baselines
            # in opposite columns never become one text fragment.
            left, right = [], []
            for line in lines_in_run:
                b = line.bounds
                (left if b.left + b.right < 2 * gutter else right).append(line)
            if not (left and right):
                bands.append(Band((ordered(left + right),)))
            elif not bands:
                start = max(min(l.bounds.top for l in left), min(l.bounds.top for l in right))
                typical = median(line.bounds.height for line in lines_in_run)
                first = min(line.bounds.top for line in lines_in_run)
                opening = [line for line in lines_in_run if start - first <= typical * 4
                           and line.bounds.bottom < start - typical * 0.15]
                if opening:
                    bands.append(Band((ordered(opening),)))
                    ids = {id(line) for line in opening}
                    left = [line for line in left if id(line) not in ids]
                    right = [line for line in right if id(line) not in ids]
                bands.append(Band((ordered(left), ordered(right))))
            else:
                bands.append(Band((ordered(left), ordered(right))))
    return PageLayout(gutter, tuple(bands))


def enclosing_bounds(lines):
    boxes = [line.bounds for line in lines]
    return Rect(min(b.left for b in boxes), min(b.top for b in boxes),
                max(b.right for b in boxes), max(b.bottom for b in boxes))


def text_blocks(layout):
    typical = median([line.bounds.height for line in layout.reading_order] or [1])
    result = []
    for band_index, band in enumerate(layout.bands):
        for index, column in enumerate(band.columns):
            column_index = index if len(band.columns) > 1 else None
            active = []
            role = None
            last = None
            in_question = False
            rows = group_rows(column)
            values = ["".join(line.text for line in row).strip() for row in rows]
            for row_index, (row, value) in enumerate(zip(rows, values)):
                bounds = enclosing_bounds(row)
                override = next((line.role for line in row if line.role is not None), None)
                has_options = any(OPTION.match(text) for text in values[row_index + 1:row_index + 4])
                row_role = override or classify_row(value, in_question=in_question, has_options=has_options)
                if row_role in ("question", "heading", "callout_label") or (
                    row_role == "list_item" and NUMBERED.match(value)
                ):
                    in_question = row_role == "question"
                continuation = (last is not None and row_role == "paragraph"
                                and role in ("paragraph", "question", "subquestion", "option", "list_item")
                                and -typical * 0.35 <= bounds.top - last.bottom <= typical * 0.9
                                and abs(bounds.left - last.left) <= typical * 2.1)
                if active and not continuation:
                    result.append(TextBlock(role, enclosing_bounds(active), band_index,
                                            column_index, tuple(active)))
                    active = []
                if not active:
                    role = row_role
                active.extend(row)
                last = bounds
            if active:
                result.append(TextBlock(role, enclosing_bounds(active), band_index,
                                        column_index, tuple(active)))
    return tuple(sorted(result, key=lambda block: min((line.review_order for line in block.lines
                                                       if line.review_order is not None), default=float("inf"))))


def classify_row(value, *, in_question=False, has_options=False):
    if CAPTION.match(value):
        return "caption"
    if QUESTION.match(value):
        return "question"
    if CALLOUT.match(value):
        return "callout_label"
    if number := NUMBERED.match(value):
        body = re.sub(r"\s+", "", value[number.end():])
        return "question" if not KNOWLEDGE.match(body) and (has_options or TASK.search(body)
                             or re.match(r"[（(].{0,25}(?:改编|调研|质检|模拟|多选|T\d)", body)
                             or body.startswith("若")) else "list_item"
    if number := SUBQUESTION.match(value):
        body = re.sub(r"\s+", "", value[number.end():])
        return "subquestion" if (in_question or TASK.search(body)) and not KNOWLEDGE.match(body) else "list_item"
    if OPTION.match(value):
        return "option"
    if HEADING.match(value) and len(value) <= 40 and not MATH.search(value):
        return "heading"
    if MATH.search(value) and not re.search(r"[\u3400-\u9fff]", value):
        return "formula"
    return "paragraph"


def caption_links(regions, blocks, candidate_ids=None, table_targets=()):
    """Match a nearby figure above its caption or a table below its caption."""
    matches = {}
    for block in blocks:
        if block.role != "caption":
            continue
        b = block.bounds
        table = block.lines[0].text.lstrip().startswith("表")
        distances = []
        for index in range(len(regions)) if candidate_ids is None else candidate_ids:
            region = regions[index]
            overlap = min(b.right, region.right) - max(b.left, region.left)
            gap = region.top - b.bottom if table else b.top - region.bottom
            if overlap >= min(b.width, region.width) * 0.5 and -b.height * 1.5 <= gap <= b.height * 3:
                category_mismatch = bool(table_targets) and (index in table_targets) != table
                distances.append((category_mismatch, abs(gap), -overlap, index))
        if distances:
            index = min(distances)[-1]
            matches.setdefault(index, []).extend(block.lines)
    return matches


def apply_caption_bindings(matches, regions, lines, bindings):
    def nearest(target, candidates):
        overlaps = []
        for index, bounds in enumerate(candidates):
            overlap = target.intersection(bounds)
            if overlap:
                score = (overlap.width * overlap.height
                         / max(target.width * target.height, bounds.width * bounds.height))
                if score >= 0.5:
                    overlaps.append((score, index))
        return max(overlaps)[1] if overlaps else None

    for figure, caption in bindings:
        index = nearest(figure, regions)
        line_index = nearest(caption, [line.bounds for line in lines])
        if index is None or line_index is None:
            continue
        line = lines[line_index]
        for refs in matches.values():
            refs[:] = [ref for ref in refs if ref is not line]
        matches.setdefault(index, []).append(line)
    return matches
