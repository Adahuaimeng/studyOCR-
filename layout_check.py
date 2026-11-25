"""Check the generated handout and link findings to the source."""

from dataclasses import dataclass, field

from docx import Document
import pypdfium2 as pdfium
from pypdfium2 import raw
from reportlab.pdfbase import pdfmetrics

from exports import pdf_fonts
from handout import CONTENT_WIDTH, HEIGHT, MARGIN, WIDTH
from structured import cell_crop, cell_fragments, inline_fragments, linear_equation, table_data, table_rows


@dataclass
class LayoutReport:
    checks: list[str] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    pages: int | None = None
    pending_pdf: tuple | None = field(default=None, repr=False)
    source_headings: tuple = field(default=(), repr=False)
    asset_audit: dict | None = field(default=None, repr=False)

    def add(self, code, message, *, page=None, sheet=None, bounds=None, candidates=()):
        source = bounds.translated(sheet.scan.area.left, sheet.scan.area.top) if bounds and sheet else None
        self.issues.append({"code": code, "message": message, "output_page": page,
                            "source_id": sheet.source_id if sheet else None,
                            "source_name": sheet.source_name if sheet else
                            "、".join(dict.fromkeys(item.source_name for item in candidates)) or None,
                            "source_bounds": [source.left, source.top, source.right, source.bottom]
                            if source else None})

    def data(self):
        return {"checked": self.checks, "pages": self.pages, "issues": self.issues}

    def inspect_pages(self):
        if self.pending_pdf:
            check_pdf(self, *self.pending_pdf)
            self.pending_pdf = None
        self.source_headings = ()


def check_fonts(report, sections, options):
    faces = [pdfmetrics.getFont(name).face.charWidths for name in pdf_fonts(serif=True)]
    report.checks.append("PDF 字形覆盖")

    def missing(value):
        return "".join(dict.fromkeys(char for char in value if not char.isspace()
                                     and not any(ord(char) in face for face in faces)))

    def to_text(values):
        return "".join(linear_equation(value) if math else value for math, value in values)

    if options.cover and (absent := missing(options.title)):
        report.add("missing_glyph", f"封面的 PDF 字体缺少字符 {absent[:15]}")
    for section in sections:
        if absent := missing(section.title):
            report.add("missing_glyph", f"标题的 PDF 字体缺少字符 {absent[:15]}", sheet=section.sheet)
        for block in section.blocks:
            if block.role == "equation":
                value = linear_equation(block.value)
            elif block.role == "inline":
                value = to_text(inline_fragments(block.value))
            elif block.role == "table":
                value = "".join(to_text(cell_fragments(cell)) for row in table_rows(block.value)
                                for cell in row if cell != "^") + "".join(block.captions)
            else:
                value = block.value + "".join(block.captions)
            if absent := missing(value):
                report.add("missing_glyph", f"PDF 字体缺少字符 {absent[:15]}", sheet=section.sheet,
                           bounds=block.bounds)


def check_word(report, path, sections):
    document = Document(path)
    report.checks.extend(("Word 纸型与页边距", "Word 图像宽度", "Word 标题与目录引用"))
    for section in document.sections:
        if (abs(section.page_width.pt - WIDTH) > 1 or abs(section.page_height.pt - HEIGHT) > 1
                or section.left_margin.pt < MARGIN - 1 or section.right_margin.pt < MARGIN - 1):
            report.add("paper_size", "Word 纸张或页边距与 A4 讲义模板不一致")
    if any(image.width.pt > CONTENT_WIDTH + 1 for image in document.inline_shapes):
        report.add("overflow", "Word 图片宽度超出正文区域")
    if not any(p.style.name == "Heading 1" for p in document.paragraphs):
        report.add("missing_heading", "Word 文档没有可导航的分节标题")
    report.checks.extend(("Word 图表与表头", "Word 段落孤行设置"))
    blocks = [block for section in sections for block in section.blocks]
    tables = [block for block in blocks if block.role == "table"]
    cell_pictures = sum(cell_crop(cell) is not None for block in tables
                        for row in table_rows(block.value) for cell in row)
    if (len(document.tables) != len(tables)
            or len(document.inline_shapes) != sum(block.role == "picture" for block in blocks) + cell_pictures):
        report.add("missing_object", "Word 文件的图表数量与讲义内容不一致")
    headers = sum(table_data(block.value)[1] for block in tables)
    if len(document._element.xpath(".//w:tblHeader")) != headers:
        report.add("missing_table_header", "Word 表头的重复规则与讲义内容不一致")
    if any(p.style.paragraph_format.widow_control is not True for p in document.paragraphs if p.text):
        report.add("widow_settings", "存在未启用孤行控制的讲义段落")
    bookmarks = {node.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}name")
                 for node in document._element.xpath(".//w:bookmarkStart")}
    if any(node.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}anchor") not in bookmarks
           for node in document._element.xpath(".//w:hyperlink[@w:anchor]")):
        report.add("missing_anchor", "Word 目录中存在失效链接")


def check_captions(report, groups):
    report.checks.append("PDF 图注同页")
    for sheet, bounds, pages in groups.values():
        if len(pages) > 1:
            report.add("split_caption", "图注和图片分散在不同页", page=min(pages), sheet=sheet, bounds=bounds)


def check_formula_reviews(report, sections):
    report.checks.append("公式复核")
    for section in sections:
        for block in section.blocks:
            for bounds in block.formula_reviews:
                report.add("formula_review", "公式需要对照原图，讲义已保留待核对位置。",
                           sheet=section.sheet, bounds=bounds)


def check_margin_omissions(report, sections):
    report.checks.append("讲义页边内容")
    for section in sections:
        if section.omitted_margins:
            report.add("margin_omitted", f"讲义筛除了 {len(section.omitted_margins)} 条疑似书眉、页码或页边碎片。",
                       sheet=section.sheet, bounds=section.omitted_margins[0])


def check_pdf(report, path, sources=None):
    report.checks.extend(("A4 纸型", "正文越界", "图文遮挡", "意外空页", "PDF 提取字符", "中文行首与孤行",
                          "末页留白"))
    document = pdfium.PdfDocument(path)
    try:
        report.pages = len(document)
        report.checks.append("PDF 书签目标")
        for bookmark in document.get_toc():
            destination = bookmark.get_dest()
            if not destination or not 0 <= destination.get_index() < report.pages:
                report.add("missing_anchor", f"PDF 书签「{bookmark.get_title()}」缺少有效目标页。")
        if sources is None and report.source_headings:
            sources = sources_from_outline(document, report.source_headings)
        for index in range(len(document)):
            page = document[index]
            try:
                check_pdf_page(report, page, index + 1, (sources or {}).get(index + 1, ()),
                               last=index == len(document) - 1)
            finally:
                page.close()
    finally:
        document.close()


def sources_from_outline(document, headings):
    starts = []
    for bookmark in document.get_toc():
        if len(starts) == len(headings):
            break
        title, sheet = headings[len(starts)]
        if bookmark.level == 0 and bookmark.get_title() == title:
            destination = bookmark.get_dest()
            if destination is not None and destination.get_index() >= 0:
                starts.append((destination.get_index() + 1, sheet))
    if len(starts) != len(headings):
        return {}
    mapped = {}
    for index, (start, sheet) in enumerate(starts):
        stop = starts[index + 1][0] if index + 1 < len(starts) else len(document) + 1
        for page in range(start, max(start + 1, stop)):
            mapped.setdefault(page, []).append(sheet)
    return mapped


def check_pdf_page(report, page, number, sources, *, last=False):
    width, height = page.get_size()
    options = {"page": number, "sheet": sources[0] if len(sources) == 1 else None,
               "candidates": sources}
    if abs(width - WIDTH) > 1 or abs(height - HEIGHT) > 1:
        report.add("paper_size", "PDF 页面与 A4 尺寸不一致", **options)
    text_boxes, images = [], []
    numbered_footer = False
    for item in page.get_objects(filter=[raw.FPDF_PAGEOBJ_TEXT, raw.FPDF_PAGEOBJ_IMAGE]):
        left, bottom, right, top = item.get_bounds()
        if item.type == raw.FPDF_PAGEOBJ_TEXT and top < 37 and WIDTH * 0.4 < left < right < WIDTH * 0.6:
            numbered_footer = True
            continue
        if item.type == raw.FPDF_PAGEOBJ_IMAGE:
            images.append((left, bottom, right, top))
        elif right - left > 0.1 and top - bottom > 0.1:
            text_boxes.append((left, bottom, right, top))
    if not text_boxes and not images:
        report.add("blank_page", "PDF 页没有正文或图像", **options)
    elif last and sources:
        boxes = text_boxes + images
        span = (max(box[3] for box in boxes) - min(box[1] for box in boxes)) / max(1, height - 2 * MARGIN)
        if span < .35:
            report.add("sparse_tail", f"末页自然排版后，内容纵向占版心约 {span:.0%}。", **options)
    if any(l < MARGIN - 3 or r > width - MARGIN + 3 or b < MARGIN - 3 or t > height - MARGIN + 3
           for l, b, r, t in (*text_boxes, *images)):
        report.add("overflow", "PDF 文字或图像超出正文边界", **options)
    for l, b, r, t in text_boxes:
        if any(max(0, min(r, ir) - max(l, il)) * max(0, min(t, it) - max(b, ib))
               > (r - l) * (t - b) * 0.26 for il, ib, ir, it in images):
            report.add("overlap", "PDF 页有文字和图片的范围明显重叠", **options)
            break
    text = page.get_textpage()
    try:
        value = text.get_text_range()
        if "\ufffd" in value:
            report.add("unreadable_glyph", "PDF 提取文字包含替代字符", **options)
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        if numbered_footer and lines and lines[0].isdigit():
            lines.pop(0)
        elif numbered_footer and lines and lines[-1].isdigit():
            lines.pop()
        if any(line[0] in "，。；、：！？）】》”" and len(line) >= 3 for line in lines):
            report.add("punctuation_wrap", "有中文行以闭合标点开头，可对照原图检查断行", **options)
        if sources and len(lines) == 1 and not images:
            report.add("short_page", "本页只剩一行正文，可在预览中确认分页", **options)
    finally:
        text.close()
