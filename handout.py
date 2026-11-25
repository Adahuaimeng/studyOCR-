"""Shared content and typography for the A4 handout."""

from dataclasses import dataclass, replace
from itertools import groupby
import re

from reportlab.lib.pagesizes import A4

from exports import Picture, covers, document_items, sheet_caption_links, word_bands
from illustrations import pixel_area, rect_union
from ocr_core import Rect, TextLine
from page_structure import CALLOUT, HEADING, enclosing_bounds, group_rows, page_layout, text_blocks
from reconstruction import broken_math
from structured import inline_fragments, linear_equation
from study import BlockFormat, ExportSheet


MARGIN = 51
WIDTH, HEIGHT = A4
CONTENT_WIDTH = WIDTH - 2 * MARGIN
FONT = "Microsoft YaHei"
FORMULA_REVIEW = "〔公式待核对〕"
PAIRED_MATH = re.compile(r"\$([^$\r\n]+)\$(?!\d)")
CURRENCY = re.compile(r"\$\d+(?:[.,]\d+)?(?=$|[\s,，、；。])")


@dataclass(frozen=True)
class Style:
    size: float
    leading: float
    after: float
    before: float = 0
    indent: float = 0
    bold: bool = False
    first_line: float = 0


STYLES = {
    "title": Style(25, 37, 20, bold=True),
    "source": Style(17, 25, 12, bold=True),
    "heading": Style(14, 21, 7, 9, bold=True),
    "question": Style(11.5, 17, 5, 6, bold=True),
    "paragraph": Style(11, 17, 5, first_line=22),
    "subquestion": Style(11, 16, 4, indent=12),
    "list_item": Style(11, 16, 5, indent=10),
    "callout_label": Style(11, 16, 4, 5, bold=True),
    "option": Style(11, 16, 4, indent=16),
    "formula": Style(11, 17, 5, indent=12),
    "caption": Style(9, 14, 8),
    "table": Style(10, 15, 4),
    "contents": Style(11, 17, 5),
}


@dataclass(frozen=True)
class HandoutOptions:
    title: str
    cover: bool = False
    contents: bool = False


@dataclass(frozen=True)
class Inline:
    text: str
    bounds: Rect
    line_indices: tuple[int, ...] = ()
    latex: str | None = None
    state: str = "text"
    source_text: str = ""
    ocr_confidence: float | None = None


@dataclass(frozen=True)
class Block:
    role: str
    bounds: Rect
    value: str = ""
    captions: tuple[str, ...] = ()
    anchor: str = ""
    picture_parts: tuple[Rect, ...] = ()
    review_order: int | None = None
    formula_reviews: tuple[Rect, ...] = ()
    manual_role: bool = False
    inlines: tuple[Inline, ...] = ()


@dataclass(frozen=True)
class FormulaEvidence:
    bounds: Rect


@dataclass(frozen=True)
class Section:
    sheet: ExportSheet
    blocks: tuple[Block, ...]
    title: str
    anchor: str
    titled: bool = True
    omitted_margins: tuple[Rect, ...] = ()


def join_lines(lines, value_for=lambda line: line.text):
    rows = ["  ".join(value_for(line).strip() for line in row) for row in group_rows(lines)]
    value = ""
    for row in rows:
        row = row.strip()
        if not row:
            continue
        if value[-1:].isascii() and row[0].isascii() and value[-1:].isalnum() and row[0].isalnum():
            value += " "
        value += row
    return value


def review_text(line):
    text = line.text.strip()
    if "$" in text:
        prefix, _, remainder = text.partition("$")
        suffix = remainder.rsplit("$", 1)[-1] if "$" in remainder else ""
        if not re.search(r"[\u3400-\u9fff]|[A-Za-z]{2,}\s+[A-Za-z]{2,}", prefix):
            prefix = ""
        if not re.search(r"[\u3400-\u9fff]|[A-Za-z]{2,}\s+[A-Za-z]{2,}|[。！？]$", suffix):
            suffix = ""
    else:
        match = re.match(r"^[\u3400-\u9fff，。；：、\s]*", text)
        prefix, suffix = match[0], ""
    return prefix + FORMULA_REVIEW + suffix


def ocr_text_parts(value):
    def replace_formula(match):
        inner = match[1].strip()
        if re.search(r"[\u3400-\u9fff]", inner) or (
            re.match(r"\d", inner) and re.search(r"(?<!\\)\b(?!(?:sin|cos|tan|log|ln)\b)[A-Za-z]{2,}\b", inner)
        ):
            return None
        if (re.search(r"[\\^_{}√=≠≤≥<>+−*/²³₀-₉]", inner)
                or re.fullmatch(r"[A-Za-zα-ω]\d*(?:\s*,\s*[A-Za-zα-ω]\d*)*|(?:[A-Za-zα-ω]\d+){2,}|\d+(?:\.\d+)?", inner)):
            try:
                return linear_equation(inner)
            except ValueError:
                pass
        return None

    parts = []
    start = 0
    while match := PAIRED_MATH.search(value, start):
        if match.start() > start:
            parts.append((value[start:match.start()], False, value[start:match.start()]))
        replacement = replace_formula(match)
        if replacement is None:
            parts.append(("$", False, "$"))
            start = match.start() + 1
        else:
            parts.append((replacement, True, match[0]))
            start = match.end()
    if start < len(value):
        parts.append((value[start:], False, value[start:]))
    return parts


def display_ocr_text(value):
    return "".join(part for part, _, _ in ocr_text_parts(value))


def needs_formula_review(value):
    compact = re.sub(r"\s+", "", CURRENCY.sub("", value))
    return broken_math(compact) or bool(re.fullmatch(r"[+−-]?[A-Za-zα-ω]\d+\$", compact))


def refs_for(bounds, scan):
    return tuple(i for i, line in enumerate(scan.lines)
                 if pixel_area(bounds.intersection(line.bounds)) >= pixel_area(line.bounds) * .45)


def confirmed_inlines(item, scan, refs):
    pieces = inline_fragments(item.value) if item.format == BlockFormat.INLINE else ((True, item.value),)
    sources = tuple(scan.lines[index] for index in refs)
    source = " ".join(line.text for line in sources)
    confidence = min((line.confidence for line in sources), default=None)
    return tuple(Inline(linear_equation(value) if math else value, item.bounds, refs,
                        value if math else None, "confirmed" if math else "text", source, confidence)
                 for math, value in pieces)


def attach_inline_roles(native, visible, roles, scan):
    """Attach explicit inline objects to a unique paragraph, using original OCR anchors."""
    references = {}
    for item in native:
        if item.format not in (BlockFormat.INLINE, BlockFormat.EQUATION):
            continue
        refs = references[id(item)] = refs_for(item.bounds, scan)
        if item.format != BlockFormat.INLINE:
            continue
        candidates = {roles[id(scan.lines[index])] for index in refs if id(scan.lines[index]) in roles}
        if len(candidates) == 1:
            owner = next(iter(candidates))
            if owner[1] not in ("heading", "caption", "callout_label", "formula") and not any(
                roles.get(id(line)) == owner and pixel_area(item.bounds.intersection(line.bounds))
                >= pixel_area(line.bounds) * .35 for line in visible
            ):
                roles[id(item)] = owner
    return references


def paragraph_inlines(group, sheet, indexes, references, damaged, displayed):
    result = []
    for row in group_rows(group):
        for n, item in enumerate(row):
            if not isinstance(item, TextLine):
                parts = confirmed_inlines(item, sheet.scan, references[id(item)])
            else:
                index = indexes[id(item)]
                if id(item) in damaged:
                    tokens = [(text, False, item.text) for text in review_text(item).partition(FORMULA_REVIEW) if text]
                else:
                    tokens = displayed[id(item)]
                parts = tuple(Inline(text, damaged[id(item)] if text == FORMULA_REVIEW and id(item) in damaged
                                     else item.bounds, (index,),
                                     state="needs_review" if text == FORMULA_REVIEW and id(item) in damaged
                                     else "text_only" if math else "text",
                                     source_text=raw, ocr_confidence=item.confidence)
                              for text, math, raw in tokens)
            if result and parts and (n or (result[-1].text[-1:].isascii() and parts[0].text[:1].isascii()
                                   and result[-1].text[-1:].isalnum() and parts[0].text[:1].isalnum())):
                if not result[-1].text[-1:].isspace() and not parts[0].text[:1].isspace():
                    result.append(Inline(" " if not n else "  ", item.bounds))
            result.extend(parts)
    return tuple(result)


def margin_omissions(sheets):
    """Return running headers and clearly peripheral fragments by source line identity."""
    central_headings = {re.sub(r"\s+", "", line.text.strip())
                        for sheet in sheets if sheet.source_extent == sheet.scan.area
                        for line in sheet.scan.lines
                        if line.bounds.top < sheet.scan.image.height * .065
                        and line.bounds.left <= sheet.scan.image.width * .65
                        and line.bounds.right >= sheet.scan.image.width * .28}
    seen_headings = set()
    omissions = set()
    for sheet in sheets:
        if sheet.source_extent != sheet.scan.area:
            continue
        width, height = sheet.scan.image.size
        found = set()
        for line in sheet.scan.lines:
            if line.role:
                continue
            box = line.bounds
            value = re.sub(r"\s+", "", line.text.strip())
            outer = box.right <= width * 0.023 or box.left >= width * 0.977
            edge_fragment = outer and re.fullmatch(r"[\u3400-\u9fff]{1,3}", value)
            folio = (box.top >= height * 0.975 and abs((box.left + box.right) / 2 - width / 2) <= width * .08
                     and re.fullmatch(r"\d{1,3}", value))
            publisher = (box.top < height * .06 and box.left < width * .06
                         and re.fullmatch(r".{0,10}高考总复习.{1,12}", value))
            chapter = box.top < height * .065 and len(value) >= 7 and HEADING.match(value)
            offset_header = chapter and (box.left > width * .65 or box.right < width * .28)
            if edge_fragment or folio or publisher or (offset_header and value in seen_headings | central_headings):
                omissions.add(id(line))
            if chapter:
                found.add(value)
        seen_headings.update(found)
    return omissions


def join_title_continuations(blocks):
    result = []
    for block in blocks:
        previous = result[-1] if result else None
        if (previous and previous.role == "heading" and block.role == "paragraph"
                and not (previous.manual_role or block.manual_role)
                and re.fullmatch(r"[\u3400-\u9fff]{3,12}", block.value.strip())
                and abs(block.bounds.center_y - previous.bounds.center_y) <= previous.bounds.height * 1.1
                and block.bounds.height >= previous.bounds.height * .6
                and abs(block.bounds.left + block.bounds.right - previous.bounds.left - previous.bounds.right)
                <= previous.bounds.width * .16):
            result[-1] = Block("heading", rect_union(previous.bounds, block.bounds),
                               previous.value + block.value, anchor=previous.anchor,
                               review_order=previous.review_order, inlines=previous.inlines + block.inlines)
        else:
            result.append(block)
    return result


def bundle_pictures(pictures):
    """Keep adjacent pieces of one cut formula together on the reflowed page."""
    bundles = [(item, (item.bounds,)) for item in pictures]

    def adjacent(a, b):
        shared_y = min(a.bottom, b.bottom) - max(a.top, b.top)
        gap_x = max(a.left, b.left) - min(a.right, b.right)
        height = min(a.height, b.height)
        width = min(a.width, b.width)
        side_by_side = (min(width, height) >= 120 and shared_y >= height * 0.82
                        and max(a.height, b.height) <= height * 1.25 and 0 <= gap_x <= width * 0.22)
        return shared_y >= height * 0.5 and -6 <= gap_x <= 5 or side_by_side

    result = []
    while bundles:
        picture, parts = bundles.pop(0)
        if not picture.captions:
            index = 0
            while index < len(bundles):
                other, slices = bundles[index]
                if not other.captions and any(adjacent(a, b) for a in parts for b in slices):
                    parts += slices
                    picture = Picture(rect_union(picture.bounds, other.bounds))
                    bundles.pop(index)
                    index = 0
                else:
                    index += 1
        result.append((picture, parts))
    return result


def sections_for(sheets, mode, auto_figures):
    sections = []
    omitted = margin_omissions(sheets)
    for number, sheet in enumerate(sheets, 1):
        margin_lines = tuple(line for line in sheet.scan.lines if id(line) in omitted and mode.includes(line.kind))
        scan = (replace(sheet.scan, lines=tuple(line for line in sheet.scan.lines if id(line) not in omitted))
                if margin_lines else sheet.scan)
        layout = page_layout(scan)
        review_regions = []
        visible, objects = document_items(sheet, layout.reading_order, mode, auto_figures=auto_figures,
                                          math_review=review_regions)
        bundled = bundle_pictures([item for item in objects if isinstance(item, Picture)])
        pictures = [item for item, _ in bundled]
        pieces = {id(item): bounds for item, bounds in bundled}
        native = [item for item in objects if not isinstance(item, Picture)]
        links = sheet_caption_links(sheet, [item.bounds for item in pictures], native)
        table_links = {id(item): links.get(len(pictures) + index, ())
                       for index, item in enumerate(native) if item.format == BlockFormat.TABLE}
        attached = {id(line) for item in pictures for line in item.captions}
        attached.update(id(line) for lines in table_links.values() for line in lines if mode.includes(line.kind))
        displayed = {id(line): ocr_text_parts(line.text.strip()) for line in visible}
        damaged = {id(line): line.bounds for line in visible
                   if needs_formula_review("".join(value for value, _, _ in displayed[id(line)]))}
        covered = {id(line) for line in visible if id(line) not in damaged
                   and len(re.findall(r"[\u3400-\u9fff]", line.text)) < 3
                   and any(pixel_area(region.intersection(line.bounds)) >= pixel_area(line.bounds) * 0.65
                           for region in review_regions)}
        allowed = {id(line) for line in visible} - attached - covered
        evidence = []
        for region in review_regions:
            matched = next((key for key, bounds in damaged.items() if region.intersection(bounds)
                            and pixel_area(region.intersection(bounds)) >= pixel_area(region) * 0.35), None)
            if matched is None:
                evidence.append(FormulaEvidence(region))
            else:
                damaged[matched] = rect_union(region, damaged[matched])
        roles = {id(line): (index, block.role) for index, block in enumerate(text_blocks(layout))
                 for line in block.lines}
        indexes = {id(line): index for index, line in enumerate(sheet.scan.lines)}
        references = attach_inline_roles(native, visible, roles, sheet.scan)
        blocks = []
        for band in word_bands(layout, [*native, *pictures, *evidence], sheet.scan.image.width):
            for column in band:
                ordered = [item for row in group_rows(column) for item in row
                           if not isinstance(item, TextLine) or id(item) in allowed]
                for key, items in groupby(ordered, key=lambda item: roles.get(id(item), (id(item), None))):
                    group = tuple(items)
                    item = group[0]
                    if isinstance(item, TextLine) or id(item) in roles:
                        role = key[1] or "paragraph"
                        anchor = f"section{number}part{len(blocks) + 1}" if role == "heading" else ""
                        rank = min((line.review_order for line in group
                                    if isinstance(line, TextLine) and line.review_order is not None), default=None)
                        reviews = tuple(damaged[id(line)] for line in group if id(line) in damaged)
                        if reviews and role == "heading":
                            role, anchor = "paragraph", ""
                        parts = paragraph_inlines(group, sheet, indexes, references, damaged, displayed)
                        value = "".join(part.text for part in parts).strip()
                        manual_role = any(isinstance(line, TextLine) and line.role for line in group)
                        if role == "callout_label" and not manual_role and all(part.state == "text" for part in parts):
                            match = CALLOUT.match(value)
                            if match and (body := value[match.end():].strip()):
                                indexes_used = tuple(dict.fromkeys(i for part in parts for i in part.line_indices))
                                bounds = enclosing_bounds(group)
                                label = Inline(match[0], bounds, indexes_used)
                                blocks.append(Block(role, bounds, match[0], review_order=rank, inlines=(label,)))
                                role, value = "paragraph", body
                                parts = (Inline(body, bounds, indexes_used),)
                        blocks.append(Block(role, enclosing_bounds(group), value, anchor=anchor,
                                            review_order=rank, formula_reviews=reviews, manual_role=manual_role,
                                            inlines=parts))
                    elif isinstance(item, FormulaEvidence):
                        indexes_used = refs_for(item.bounds, sheet.scan)
                        raw = " ".join(sheet.scan.lines[index].text for index in indexes_used)
                        blocks.append(Block("formula", item.bounds, FORMULA_REVIEW, formula_reviews=(item.bounds,),
                                            inlines=(Inline(FORMULA_REVIEW, item.bounds, indexes_used,
                                                            state="needs_review", source_text=raw),)))
                    elif isinstance(item, Picture):
                        captions = tuple(line.text.strip() for line in item.captions if line.text.strip())
                        blocks.append(Block("picture", item.bounds, captions=captions,
                                            picture_parts=pieces[id(item)]))
                    else:
                        role = {BlockFormat.TABLE: "table", BlockFormat.INLINE: "inline",
                                BlockFormat.EQUATION: "equation"}[item.format]
                        captions = tuple(line.text.strip() for line in table_links.get(id(item), ())
                                         if mode.includes(line.kind) and line.text.strip())
                        parts = confirmed_inlines(item, sheet.scan, references[id(item)]) if role != "table" else ()
                        blocks.append(Block(role, item.bounds, item.value, captions, inlines=parts))
        if blocks:
            blocks = join_title_continuations(blocks)
            ranked = iter(sorted((block for block in blocks if block.review_order is not None),
                                 key=lambda block: block.review_order))
            blocks = [next(ranked) if block.review_order is not None else block for block in blocks]
            opening = next((block for block in blocks[:2] if block.role == "heading"
                            and block.bounds.top < sheet.scan.image.height * .18), None)
            title = sheet.handout_title or (opening.value if opening else "")
            if opening and title == opening.value:
                blocks.remove(opening)
            # A title-only sheet still has its title as printable content.
            sections.append(Section(sheet, tuple(blocks), title or f"原图 {number}", f"section{number}",
                                    bool(title), tuple(line.bounds for line in margin_lines)))
    return tuple(sections)


def contents_entries(sections):
    for section in sections:
        if section.titled:
            yield section.title, section.anchor, 0
        for block in section.blocks:
            if block.anchor:
                yield block.value, block.anchor, int(section.titled)


def picture_size(bounds):
    narrow = bounds.width < bounds.height * 0.5
    limit = 85 if narrow else 275
    width = min(CONTENT_WIDTH, max(12, bounds.width * 0.26), bounds.width * limit / bounds.height)
    return width, width * bounds.height / bounds.width
