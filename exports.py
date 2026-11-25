from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import cache
from io import BytesIO
from itertools import groupby
from os import environ
from pathlib import Path
import re
from statistics import median
from tempfile import NamedTemporaryFile
from unicodedata import east_asian_width

from docx import Document
from docx.enum.section import WD_SECTION_START
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt
from PIL import Image, ImageFont
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from illustrations import illustration_role, pixel_area
from ocr_core import Rect, Scan, TextLine
from page_structure import apply_caption_bindings, caption_links, group_rows, page_layout, text_blocks
from pdf_math import draw_formula
from reconstruction import broken_math
from structured import (append_equation, cell_crop_bounds, cell_fragments, inline_fragments,
                        linear_equation, structured_text, table_data)
from study import BlockFormat, ExportSheet, Extraction, StructuredBlock


@contextmanager
def export_path(path: Path):
    with NamedTemporaryFile(dir=path.parent, suffix=path.suffix, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        yield temporary
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def pdf_fonts(*, serif=False) -> tuple[str, ...]:
    from app_paths import ASSETS

    font_dir = Path(environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    groups = {
        "OCRChinese": ("simhei.ttf", "simsun.ttc", "Deng.ttf", "msyh.ttc"),
        "OCRMath": ("seguisym.ttf", "segoeui.ttf"),
    }
    if serif:
        groups = {"OCRLatinSerif": ("times.ttf",), "OCRChineseSerif": ("simsun.ttc",), **groups}
    registered = []
    for name, filenames in groups.items():
        if name not in pdfmetrics.getRegisteredFontNames():
            candidates = [font_dir / filename for filename in filenames]
            if name == "OCRChinese":
                candidates.append(ASSETS / "NotoSansSC.ttf")
            for font in candidates:
                if font.exists():
                    pdfmetrics.registerFont(TTFont(name, str(font)))
                    break
            else:
                continue
        registered.append(name)
    if "OCRChinese" not in registered:
        raise RuntimeError("未找到中文字体。请在 Windows 中安装简体中文补充字体。")
    return tuple(registered)


def pdf_text_runs(value, fonts):
    characters = []
    faces = [(name, pdfmetrics.getFont(name).face.charWidths) for name in fonts]
    for character in value:
        for name, widths in faces:
            if ord(character) in widths:
                characters.append((name, character))
                break
        else:
            characters.append((fonts[0], "?"))
    return [(name, "".join(character for _, character in group))
            for name, group in groupby(characters, key=lambda part: part[0])]


def save_pdf(scan: Scan, path: Path):
    save_pdf_sheets((ExportSheet(scan),), path)


def local_blocks(sheet, mode):
    area = sheet.scan.area
    return [replace(block, bounds=block.bounds.relative_to(area))
            for block in sheet.blocks
            if mode.includes(block.kind) and block.bounds.intersection(area) == block.bounds]


def covers(bounds, line_bounds, width_ratio=0.96):
    overlap = bounds.intersection(line_bounds)
    return (overlap and overlap.width >= line_bounds.width * width_ratio
            and overlap.height >= line_bounds.height * 0.65)


def draw_pdf_text(pdf, value, bounds, height, scale, fonts, *, visible, font_size=None):
    size = max(1, bounds.height * scale * 0.82)
    runs = pdf_text_runs(value, fonts)
    units = sum(pdfmetrics.stringWidth(fragment, font, 1) for font, fragment in runs)
    if font_size is not None:
        size = font_size
    elif visible:
        fitted = bounds.width * scale / max(units, 0.5)
        size = max(1, min(24, size, max(fitted, size * 0.72)))
    logical_width = units * size
    text = pdf.beginText(bounds.left * scale, (height - bounds.bottom) * scale + size * 0.18)
    text.setTextRenderMode(0 if visible else 3)
    if logical_width:
        factor = 100 * bounds.width * scale / logical_width
        text.setHorizScale(min(factor, 100) if visible else factor)
    for font, fragment in runs:
        text.setFont(font, size)
        text.textOut(fragment)
    pdf.drawText(text)


def draw_pdf_block(pdf, block, height, scale, fonts, visible, source):
    rect = block.bounds
    if block.format != BlockFormat.TABLE:
        if visible:
            fragments = (inline_fragments(block.value) if block.format == BlockFormat.INLINE
                         else ((True, block.value),))
            draw_formula(pdf, fragments, rect, height, scale, lambda text: pdf_text_runs(text, fonts))
        else:
            draw_pdf_text(pdf, structured_text(block), rect, height, scale, fonts, visible=False)
        return
    rows, header = table_data(block.value)
    edges = table_edges(rows)
    for row_index, row in enumerate(rows):
        for col_index, value in enumerate(row):
            if value == "^":
                continue
            left = rect.left + round(rect.width * edges[col_index])
            right = rect.left + round(rect.width * edges[col_index + 1])
            top = rect.top + round(rect.height * row_index / len(rows))
            end = row_index + 1
            while end < len(rows) and rows[end][col_index] == "^":
                end += 1
            bottom = rect.top + round(rect.height * end / len(rows))
            if visible:
                if header and row_index == 0:
                    pdf.saveState()
                    pdf.setFillGray(0.92)
                pdf.rect(left * scale, (height - bottom) * scale,
                         (right - left) * scale, (bottom - top) * scale,
                         fill=int(header and row_index == 0))
                if header and row_index == 0:
                    pdf.restoreState()
            if value:
                pad_x = max(1, (right - left) // 30)
                pad_y = max(1, (bottom - top) // 6)
                cell = Rect(left + pad_x, top + pad_y, right - pad_x, bottom - pad_y)
                if cell.width and cell.height:
                    table_font = max(5, min(11, round(rect.height * scale / len(rows) * 1.08) / 2))
                    crop = cell_crop_bounds(value, rect)
                    if crop:
                        if visible:
                            factor = min(cell.width * scale / crop.width, cell.height * scale / crop.height)
                            pdf.drawImage(ImageReader(crop.crop(source)), cell.left * scale,
                                          (height - cell.bottom) * scale,
                                          crop.width * factor, crop.height * factor)
                        continue
                    fragments = cell_fragments(value)
                    if visible and any(math for math, _ in fragments):
                        draw_formula(pdf, fragments, cell, height, scale, lambda text: pdf_text_runs(text, fonts),
                                     max_size=table_font)
                    else:
                        text = "".join(linear_equation(part) if math else part for math, part in fragments)
                        if visible:
                            draw_pdf_cell(pdf, text, cell, height, scale, fonts, table_font)
                        else:
                            draw_pdf_text(pdf, text, cell, height, scale, fonts, visible=False)


def table_edges(rows):
    weights = []
    for values in zip(*rows):
        lengths = [text_units("".join(linear_equation(part) if math else part
                                      for math, part in cell_fragments(value))) for value in values]
        weights.append(min(32, max(4, max(lengths))))
    total = sum(weights)
    return [0.0] + [sum(weights[:index]) / total for index in range(1, len(weights) + 1)]


def draw_pdf_cell(pdf, value, cell, height, scale, fonts, size):
    def wrap(points):
        available = cell.width * scale / points
        rows = []
        for source in value.splitlines():
            row = ""
            units = 0.0
            for font, run in pdf_text_runs(source, fonts):
                for char in run:
                    width = pdfmetrics.stringWidth(char, font, 1)
                    if row and units + width > available:
                        rows.append(row)
                        row, units = "", 0.0
                    row += char
                    units += width
            rows.append(row)
        return rows

    rows = wrap(size)
    for _ in range(4):
        fitted = cell.height * scale / max(1.2, len(rows) * 1.2)
        if fitted >= size:
            break
        size = fitted
        rows = wrap(size)
    step = cell.height / max(len(rows), 1)
    for index, row in enumerate(rows):
        line = Rect(cell.left, cell.top + round(index * step), cell.right,
                    cell.top + round((index + 1) * step))
        draw_pdf_text(pdf, row, line, height, scale, fonts, visible=True, font_size=size)


def photo_reader(image, page_width, page_height, dpi):
    if dpi is None:
        return ImageReader(image)
    picture = image.copy()
    picture.thumbnail((max(1, round(page_width * dpi / 72)), max(1, round(page_height * dpi / 72))),
                      Image.Resampling.LANCZOS)
    stream = BytesIO()
    picture.convert("RGB").save(stream, format="JPEG", quality=87, optimize=True, subsampling=0)
    stream.seek(0)
    return ImageReader(stream)


def save_pdf_sheets(sheets, path, mode=Extraction.ALL, *, original=True, auto_figures=True, photo_dpi=None):
    if not sheets:
        raise ValueError("没有可导出的页面")
    fonts = pdf_fonts()
    with export_path(path) as temporary:
        pdf = canvas.Canvas(str(temporary))
        for sheet in sheets:
            scan = sheet.scan
            width, height = scan.image.size
            scale = min(595.28 / width, 841.89 / height)
            pdf.setPageSize((width * scale, height * scale))
            layout = page_layout(scan)
            lines = layout.reading_order
            typography = {}
            if original:
                pdf.drawImage(photo_reader(scan.image, width * scale, height * scale, photo_dpi),
                              0, 0, width * scale, height * scale)
                blocks = local_blocks(sheet, mode)
                lines = [line for line in lines if mode.includes(line.kind) and not any(
                    covers(block.bounds, line.bounds, 0.85) for block in blocks
                )]
            else:
                lines, objects = document_items(sheet, lines, mode, auto_figures=auto_figures)
                typography = section_typography(layout, lines, scale)
                blocks = []
                for item in objects:
                    if isinstance(item, Picture):
                        rect = item.bounds
                        pdf.drawImage(ImageReader(rect.crop(scan.image)), rect.left * scale,
                                      (height - rect.bottom) * scale, rect.width * scale,
                                      rect.height * scale)
                    else:
                        blocks.append(item)
            visible = {id(line) for line in lines}
            for columns in word_bands(layout, blocks, width):
                for column in columns:
                    row_items = (item for item in column if not isinstance(item, TextLine) or id(item) in visible)
                    for row in group_rows(row_items):
                        for item in row if original else merge_ocr_row(row, typography):
                            if isinstance(item, TextLine):
                                draw_pdf_text(pdf, item.text, item.bounds, height, scale, fonts,
                                              visible=not original,
                                              font_size=typography.get(id(item), (None, False))[0])
                            else:
                                draw_pdf_block(pdf, item, height, scale, fonts, not original, scan.image)
            pdf.showPage()
        pdf.save()


@dataclass(frozen=True)
class Picture:
    bounds: Rect
    captions: tuple[TextLine, ...] = ()


def table_remainder(region, table):
    overlap = region.intersection(table)
    if overlap is None or overlap.width < region.width * 0.78:
        return (region,)
    minimum = max(13, region.height * 0.12)
    parts = (Rect(region.left, region.top, region.right, overlap.top),
             Rect(region.left, overlap.bottom, region.right, region.bottom))
    return tuple(part for part in parts if part.height >= minimum)


def formula_remainder(region, bounds, *, minimum_overlap=0.25):
    overlap = region.intersection(bounds)
    if not overlap or pixel_area(overlap) < pixel_area(region) * minimum_overlap:
        return (region,)
    parts = (
        Rect(region.left, region.top, overlap.left, region.bottom),
        Rect(overlap.right, region.top, region.right, region.bottom),
        Rect(overlap.left, region.top, overlap.right, overlap.top),
        Rect(overlap.left, overlap.bottom, overlap.right, region.bottom),
    )
    return tuple(part for part in parts
                 if part.width >= max(10, region.width * 0.1)
                 and part.height >= max(10, region.height * 0.1))


def image_regions(sheet, blocks, *, auto_figures, review_regions=None):
    scan = sheet.scan
    manual = []
    for source in sheet.regions:
        overlap = source.intersection(scan.area)
        if overlap:
            local = overlap.relative_to(scan.area)
            if not any(covers(block.bounds, local, 0.85) for block in blocks):
                manual.append(local)
    if not auto_figures:
        return manual

    exclusions = [bounds.relative_to(scan.area) for bounds in sheet.figure_exclusions]
    automatic = []
    for candidate in scan.illustrations:
        if any(pixel_area(candidate.intersection(region)) >= pixel_area(candidate) * 0.58
               for region in manual):
            continue
        if any((block.format == BlockFormat.TABLE and covers(block.bounds, candidate, 0.82))
               or pixel_area(block.bounds.intersection(candidate)) >= pixel_area(candidate) * 0.93
               for block in blocks):
            continue
        remaining = [candidate]
        inspect_role = review_regions is not None or any(
            block.format != BlockFormat.TABLE and block.bounds.intersection(candidate) for block in blocks)
        is_formula = inspect_role and illustration_role(scan, candidate) == "formula_crop"
        for block in blocks:
            if block.format == BlockFormat.TABLE:
                remaining = [part for region in remaining for part in table_remainder(region, block.bounds)]
            elif is_formula:
                remaining = [part for region in remaining for part in formula_remainder(region, block.bounds)]
        for region in remaining:
            if any(pixel_area(region.intersection(box)) >= pixel_area(region) * 0.55
                   for box in (*manual, *exclusions)):
                continue
            target = review_regions if is_formula and review_regions is not None else automatic
            if not any(pixel_area(region.intersection(other)) >= min(pixel_area(region), pixel_area(other)) * 0.85
                       for other in target):
                target.append(region)
    return manual + automatic


def document_items(sheet, lines, mode, *, auto_figures=True, math_review=None):
    blocks = local_blocks(sheet, mode)
    text_lines = [line for line in lines if mode.includes(line.kind) and not any(
        covers(block.bounds, line.bounds, 0.85) for block in blocks
    )]
    damaged = [line for line in text_lines if broken_math(line.text)]
    pictures = []
    if mode == Extraction.ALL:
        regions = image_regions(sheet, blocks, auto_figures=auto_figures, review_regions=math_review)
        matches = sheet_caption_links(sheet, regions, blocks)
        pictures = [Picture(bounds, tuple(line for line in matches.get(index, ()) if mode.includes(line.kind)))
                    for index, bounds in enumerate(regions)]
    if auto_figures and mode == Extraction.ALL and math_review is None:
        for line in damaged:
            area = pixel_area(line.bounds)
            occupied = blocks + pictures
            if any(pixel_area(line.bounds.intersection(item.bounds)) >= area * 0.76 for item in occupied):
                continue
            remaining = [line.bounds]
            for item in occupied:
                remaining = [part for region in remaining
                             for part in formula_remainder(region, item.bounds, minimum_overlap=0)]
            pictures.extend(Picture(bounds) for bounds in remaining)
    invalid = {id(line) for line in damaged} if math_review is None else set()
    text_lines = [line for line in text_lines if id(line) not in invalid
                  and not any(pixel_area(picture.bounds.intersection(line.bounds)) >= pixel_area(line.bounds) * 0.76
                              for picture in pictures)]
    return text_lines, blocks + pictures


def sheet_caption_links(sheet, regions, blocks, paragraphs=None):
    if not regions and not blocks:
        return {}
    scan = sheet.scan
    if paragraphs is None:
        paragraphs = text_blocks(page_layout(scan))
    targets = [*regions, *(block.bounds for block in blocks)]
    table_targets = {index for index, block in enumerate(blocks, len(regions)) if block.format == BlockFormat.TABLE}
    for i, region in enumerate(regions):
        if illustration_role(scan, region) == "table_crop":
            table_targets.add(i)
    candidate_ids = list(range(len(regions))) + [i for i in sorted(table_targets) if i >= len(regions)]
    matches = caption_links(targets, paragraphs, candidate_ids, table_targets)
    manual = apply_caption_bindings(
        {i: matches.get(target, []) for i, target in enumerate(candidate_ids)},
        [targets[i] for i in candidate_ids], scan.lines, [
        (crop.relative_to(scan.area), line.relative_to(scan.area))
        for crop, line in sheet.figure_captions
    ])
    return {candidate_ids[i]: lines for i, lines in manual.items() if lines}

@cache
def word_measurement_font():
    fonts = Path(environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    for filename in ("msyh.ttc", "simhei.ttf"):
        try:
            return ImageFont.truetype(str(fonts / filename), size=200)
        except OSError:
            continue
    return None


def text_units(value):
    font = word_measurement_font()
    if font is not None:
        return font.getlength(value) / 200
    return sum(1 if east_asian_width(char) in {"W", "F"} else 0.65 for char in value)




def section_typography(layout, visible, scale):
    """Estimate one font size for each contiguous block of body text."""
    allowed = {id(line) for line in visible}
    body = [line for line in layout.reading_order if len(line.text.strip()) >= 9]
    typical = median([line.bounds.height for line in body]
                     or [line.bounds.height for line in layout.reading_order] or [13 / scale])

    def measure(lines):
        samples = [min(line.bounds.height * 0.68,
                       line.bounds.width * 0.96 / max(text_units(line.text), 1))
                   for line in lines if len(line.text.strip()) >= 8]
        return median(samples) if samples else typical * 0.61

    baseline = measure(body)
    result = {}

    def record(lines, heading=False):
        if not lines:
            return
        measured = measure(lines)
        if heading:
            size = max(baseline * 1.16, min(measured, baseline * 1.55))
        else:
            size = min(max(measured, baseline * 0.90), baseline * 1.10)
            if len([line for line in lines if len(line.text.strip()) >= 8]) < 2:
                size = baseline
        points = round(max(5.5, min(24 if heading else 17, size * scale)) * 2) / 2
        for line in lines:
            if id(line) in allowed:
                result[id(line)] = (points, heading)

    for block in text_blocks(layout):
        record(block.lines, heading=block.role == "heading")
    return result


def merge_ocr_row(row, typography):
    pieces = []
    for line in row:
        if pieces and isinstance(line, TextLine) and isinstance(pieces[-1], TextLine):
            former = pieces[-1]
            a, b = former.bounds, line.bounds
            gap = b.left - a.right
            threshold = min(a.height, b.height) * 0.42
            if (-threshold <= gap <= threshold and typography[id(former)] == typography[id(line)]
                    and not re.match(r"^[A-D][.．、)]", line.text)):
                tails = former.text[-1:] + line.text[:1]
                separator = " " if tails.isascii() and tails.isalnum() and gap > threshold * 0.28 else ""
                merged = TextLine(
                    Rect(a.left, min(a.top, b.top), max(a.right, b.right), max(a.bottom, b.bottom)),
                    former.text + separator + line.text, min(former.confidence, line.confidence),
                    former.kind,
                )
                typography[id(merged)] = typography[id(former)]
                pieces[-1] = merged
                continue
        pieces.append(line)
    return pieces


def word_font(run, points, available_width=None):
    run.font.name = "Microsoft YaHei"
    run.font.size = Pt(points)
    properties = run._element.get_or_add_rPr()
    properties.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    if available_width is not None and run.text:
        unscaled = text_units(run.text) * points
        stretch = int(max(35, min(100, available_width * 96 / max(unscaled, 1))))
        if stretch < 100:
            width = OxmlElement("w:w")
            width.set(qn("w:val"), str(stretch))
            properties.insert_element_before(width, "w:kern", "w:position", "w:sz", "w:szCs")


def add_word_cell_crop(paragraph, source, crop, max_width, max_height):
    data = BytesIO()
    crop.crop(source).save(data, format="PNG")
    data.seek(0)
    width = min(max_width, max_height * crop.width / crop.height)
    return paragraph.add_run().add_picture(data, width=Pt(max(1, width)))


def add_native_table(container, paragraph, block, source, width, height, indent, style):
    rows, header = table_data(block.value)
    table = container.add_table(rows=len(rows), cols=len(rows[0]))
    table.style = "Table Grid"
    table.autofit = False
    indent_xml = OxmlElement("w:tblInd")
    indent_xml.set(qn("w:w"), str(round(indent * 20)))
    indent_xml.set(qn("w:type"), "dxa")
    table._tbl.tblPr.append(indent_xml)
    edges = table_edges(rows)
    widths = [width * (right - left) for left, right in zip(edges, edges[1:])]
    table_size = max(5, min(11, round(height / len(rows) * 1.08) / 2))
    for column, col_width in zip(table.columns, widths):
        column.width = Pt(col_width)
    for row_index, row in enumerate(rows):
        if header and row_index == 0:
            table.rows[0]._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
        for col_index, value in enumerate(row):
            col_width = widths[col_index]
            cell = table.cell(row_index, col_index)
            cell.width = Pt(col_width)
            if value == "^":
                table.cell(row_index - 1, col_index).merge(cell)
                continue
            cell_paragraph = cell.paragraphs[0]
            cell_paragraph.style = style
            if crop := cell_crop_bounds(value, block.bounds):
                add_word_cell_crop(cell_paragraph, source, crop, col_width - 6, height / len(rows))
                continue
            for math, part in cell_fragments(value):
                if math:
                    append_equation(cell_paragraph, part)
                else:
                    run = cell_paragraph.add_run(part)
                    word_font(run, table_size, col_width - 5)
                    run.bold = header and row_index == 0
    paragraph.paragraph_format.line_spacing = Pt(2)
    paragraph._p.addnext(table._tbl)
    return max(height, len(rows) * 14) + 2


def add_word_rows(container, rows, scan, scale, origin_x, origin_y, available_width, style, typography):
    used_height = 0.0
    paragraphs = [container.paragraphs[0]] if hasattr(container, "_tc") else []
    for row in rows:
        groups = []
        text_items = []
        for item in row:
            if isinstance(item, Picture) or (
                isinstance(item, StructuredBlock) and item.format == BlockFormat.TABLE
            ):
                if text_items:
                    groups.append(text_items)
                    text_items = []
                groups.append([item])
            else:
                text_items.append(item)
        if text_items:
            groups.append(text_items)
        for group in groups:
            top = min(item.bounds.top for item in group)
            bottom = max(item.bounds.bottom for item in group)
            paragraph = paragraphs.pop() if paragraphs else container.add_paragraph()
            paragraph.style = style
            formatting = paragraph.paragraph_format
            gap = max(0, (top - origin_y) * scale - used_height)
            formatting.space_before = Pt(gap)
            first = group[0]
            indent = min(available_width * 0.75, max(0, (first.bounds.left - origin_x) * scale))
            formatting.left_indent = Pt(indent)
            if isinstance(first, Picture):
                data = BytesIO()
                first.bounds.crop(scan.image).save(data, format="PNG")
                data.seek(0)
                image_width = min(first.bounds.width * scale, available_width - indent)
                run = paragraph.add_run()
                run.font.size = Pt(2)
                image = run.add_picture(data, width=Pt(image_width))
                if first.captions:
                    image._inline.docPr.set("descr", "\n".join(line.text for line in first.captions))
                row_height = image_width * first.bounds.height / first.bounds.width + 3
                formatting.line_spacing = 1
            elif isinstance(first, StructuredBlock) and first.format == BlockFormat.TABLE:
                row_height = add_native_table(
                    container, paragraph, first, scan.image,
                    min(first.bounds.width * scale, available_width - indent),
                    first.bounds.height * scale, indent, style,
                )
            else:
                has_equation = False
                group = merge_ocr_row(group, typography)
                for index, line in enumerate(group):
                    if index:
                        position = (line.bounds.left - origin_x) * scale
                        if position >= available_width - 8:
                            paragraph.add_run("  ")
                        else:
                            formatting.tab_stops.add_tab_stop(Pt(position))
                            paragraph.add_run("\t")
                    if isinstance(line, StructuredBlock):
                        fragments = (inline_fragments(line.value) if line.format == BlockFormat.INLINE
                                     else ((True, line.value),))
                        for math, value in fragments:
                            if math:
                                append_equation(paragraph, value)
                            else:
                                word_font(paragraph.add_run(value), max(7, min(17, line.bounds.height * scale * 0.68)))
                        has_equation = True
                        continue
                    # Some option rows arrive as one OCR box. Keep the labels distinct.
                    option_text = re.sub(r"(?<=\S)([BCD][.．、])", r"  \1", line.text) if re.match(
                        r"A[.．、]", line.text
                    ) else line.text
                    offset = max(0, (line.bounds.left - origin_x) * scale)
                    limit = available_width - offset
                    if index + 1 < len(group):
                        next_start = group[index + 1].bounds.left
                        if next_start > line.bounds.left:
                            limit = min(limit, (next_start - line.bounds.left) * scale)
                    width = min(line.bounds.width * scale, limit)
                    size, bold = typography[id(line)]
                    run = paragraph.add_run(option_text)
                    word_font(run, size, width)
                    run.bold = bold
                row_height = max(7, (bottom - top) * scale)
                if has_equation:
                    row_height = max(20, row_height)
                    formatting.line_spacing = 1
                else:
                    formatting.line_spacing = Pt(row_height)
            used_height += gap + row_height


def save_docx(scan: Scan, path: Path, regions: tuple[Rect, ...] = ()):
    save_docx_sheets((ExportSheet(scan, regions),), path)


def save_docx_sheets(sheets, path, mode=Extraction.ALL, *, auto_figures=True):
    if not sheets:
        raise ValueError("没有可导出的页面")
    doc = Document()
    style = doc.styles.add_style("OCR Text", WD_STYLE_TYPE.PARAGRAPH)
    style.base_style = doc.styles["Normal"]
    style.font.size = Pt(2)
    style.paragraph_format.space_after = Pt(0)
    style.paragraph_format.keep_with_next = False
    style.paragraph_format.widow_control = False

    for index, sheet in enumerate(sheets):
        section = doc.sections[0] if index == 0 else doc.add_section(WD_SECTION_START.NEW_PAGE)
        if index:
            # The section-break paragraph must take negligible vertical space.
            separator = doc.paragraphs[-1]
            separator.style = style
            separator.paragraph_format.line_spacing = Pt(2)
        add_word_page(doc, section, style, sheet, mode, auto_figures=auto_figures)
    with export_path(path) as temporary:
        doc.save(str(temporary))


def add_word_page(doc, section, style, sheet, mode, *, auto_figures):
    scan = sheet.scan
    layout = page_layout(scan)
    lines, pictures = document_items(sheet, layout.reading_order, mode, auto_figures=auto_figures)
    width, height = scan.image.size
    page_scale = min(595.28 / width, 841.89 / height)
    page_width = max(90, width * page_scale)
    page_height = max(72, height * page_scale)
    margin_x, margin_y = 14, 10
    usable_width = page_width - margin_x * 2
    inner_height = page_height - margin_y * 2
    vertical_reserve = 8
    if pictures:
        # Word can push the final inline object to a new page when the reflowed
        # paragraphs leave too little room for that object as a whole.
        vertical_reserve += min(72, inner_height * 0.10)
    scale = min(usable_width / width, (inner_height - vertical_reserve) / height)

    section.page_width, section.page_height = Pt(page_width), Pt(page_height)
    section.left_margin = section.right_margin = Pt(margin_x)
    section.top_margin = section.bottom_margin = Pt(margin_y)
    typography = section_typography(layout, lines, scale)
    visible = {id(line) for line in lines}
    bands = word_bands(layout, pictures, width)
    previous_bottom = 0
    for columns in bands:
        top = min(item.bounds.top for column in columns for item in column)
        if len(columns) == 1:
            items = [item for item in columns[0] if not isinstance(item, TextLine) or id(item) in visible]
            add_word_rows(doc, group_rows(items), scan, scale, 0, previous_bottom,
                          usable_width, style, typography)
        else:
            gap = (top - previous_bottom) * scale
            if gap > 2:
                spacer = doc.add_paragraph(style=style)
                spacer.paragraph_format.line_spacing = Pt(gap)
            table = word_column_table(doc)
            edges = (0, layout.gutter, width)
            for index, column in enumerate(columns):
                cell = table.cell(0, index)
                column_width = usable_width * (edges[index + 1] - edges[index]) / width
                cell.width = table.columns[index].width = Pt(column_width)
                items = [item for item in column if not isinstance(item, TextLine) or id(item) in visible]
                add_word_rows(cell, group_rows(items), scan, scale, edges[index], top,
                              column_width, style, typography)
        previous_bottom = max(previous_bottom, max(item.bounds.bottom for column in columns for item in column))


def word_column_table(doc):
    table = doc.add_table(rows=1, cols=2)
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    table.autofit = False
    cell_margins = OxmlElement("w:tblCellMar")
    for edge in ("top", "left", "bottom", "right"):
        margin = OxmlElement(f"w:{edge}")
        margin.set(qn("w:w"), "0")
        margin.set(qn("w:type"), "dxa")
        cell_margins.append(margin)
    table._tbl.tblPr.append(cell_margins)
    return table


def word_bands(layout, objects, width):
    bands = [[list(column) for column in band.columns] for band in layout.bands]
    if layout.gutter is None:
        bands[0][0].extend(objects)
        return bands if bands[0][0] else ()

    text_extents = [(min(line.bounds.top for column in band for line in column),
                     max(line.bounds.bottom for column in band for line in column)) for band in bands]
    spanning = [[] for _ in bands]
    for item in objects:
        b = item.bounds
        index = min(range(len(bands)), key=lambda i: max(
            0, text_extents[i][0] - b.center_y, b.center_y - text_extents[i][1]
        ))
        band = bands[index]
        if len(band) == 1:
            band[0].append(item)
        elif b.left < layout.gutter - width * 0.05 and b.right > layout.gutter + width * 0.05:
            spanning[index].append(item)
        else:
            band[int(b.left + b.right >= 2 * layout.gutter)].append(item)

    result = []
    for band, full_width in zip(bands, spanning):
        for item in sorted(full_width, key=lambda item: item.bounds.center_y):
            before = [[line for line in column if line.bounds.center_y < item.bounds.center_y]
                      for column in band]
            if any(before):
                result.append(before)
            result.append([[item]])
            band = [[line for line in column if line.bounds.center_y >= item.bounds.center_y]
                    for column in band]
        if any(band):
            result.append(band)
    return result
