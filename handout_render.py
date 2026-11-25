"""Word and PDF renderers for the shared handout sections."""

from io import BytesIO
import re
from xml.sax.saxutils import escape

from docx import Document
from docx.enum.section import WD_SECTION_START
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING, WD_TAB_ALIGNMENT, WD_TAB_LEADER
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor
from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (BaseDocTemplate, Flowable, Frame, Image, KeepTogether,
                                 NextPageTemplate, PageBreak, PageTemplate, Paragraph, Spacer, Table, TableStyle)

from exports import add_word_cell_crop, export_path, pdf_fonts, pdf_text_runs, table_edges
from handout import (CONTENT_WIDTH, FONT, HEIGHT, MARGIN, STYLES, WIDTH,
                     contents_entries, picture_size)
from pdf_math import horizontal, math_box, text_box
from structured import append_equation, cell_crop_bounds, cell_fragments, formula_nodes, table_data


def image_data(section, block):
    content = BytesIO()
    source = section.sheet.scan.image
    if len(block.picture_parts) > 1:
        crop = PILImage.new("RGB", (block.bounds.width, block.bounds.height), "white")
        for part in block.picture_parts:
            crop.paste(part.crop(source), (part.left - block.bounds.left, part.top - block.bounds.top))
    else:
        crop = block.bounds.crop(source)
    crop.thumbnail((1800, 1800))
    crop.save(content, format="PNG")
    content.seek(0)
    return content


def fragments(block):
    return tuple((part.latex is not None, part.latex or part.text, part.state == "needs_review")
                 for part in block.inlines)


def xml_child(parent, tag, fields):
    child = OxmlElement(tag)
    for key, value in fields.items():
        child.set(qn(key), str(value))
    parent.append(child)
    return child


def word_styles(document):
    styles = {}
    for role, preset in STYLES.items():
        name = {"title": "Title", "source": "Heading 1", "heading": "Heading 2"}.get(role)
        style = document.styles[name] if name else document.styles.add_style("讲义 " + role, WD_STYLE_TYPE.PARAGRAPH)
        heading = role in ("title", "source", "heading", "callout_label")
        style.font.name = FONT if heading else "Times New Roman"
        style.font.size = Pt(preset.size)
        style.font.bold = preset.bold
        style.font.color.rgb = RGBColor(0, 0, 0)
        props = style._element.get_or_add_rPr()
        props.rFonts.set(qn("w:eastAsia"), FONT if heading else "SimSun")
        border = style._element.find(".//" + qn("w:pBdr"))
        if border is not None:
            border.getparent().remove(border)
        fmt = style.paragraph_format
        fmt.space_after, fmt.space_before = Pt(preset.after), Pt(preset.before)
        fmt.left_indent = Pt(preset.indent)
        fmt.first_line_indent = Pt(preset.first_line)
        fmt.line_spacing = Pt(preset.leading)
        fmt.line_spacing_rule = WD_LINE_SPACING.AT_LEAST
        fmt.widow_control = True
        fmt.keep_with_next = role in ("source", "heading")
        styles[role] = style
    return styles


def word_rich(paragraph, values):
    for math, value, *_ in values:
        if math:
            append_equation(paragraph, value)
        else:
            paragraph.add_run(value)


def word_table(document, block, styles, source):
    rows, header = table_data(block.value)
    table = document.add_table(rows=len(rows), cols=len(rows[0]))
    table.style = "Table Grid"
    table.autofit = False
    edges = table_edges(rows)
    for col, column in enumerate(table.columns):
        column.width = Pt(CONTENT_WIDTH * (edges[col + 1] - edges[col]))
    for r, row in enumerate(rows):
        if header and r == 0:
            xml_child(table.rows[0]._tr.get_or_add_trPr(), "w:tblHeader", {})
        for c, value in enumerate(row):
            cell = table.cell(r, c)
            cell.width = table.columns[c].width
            if value == "^":
                table.cell(r - 1, c).merge(cell)
                continue
            para = cell.paragraphs[0]
            para.style = styles["table"]
            if crop := cell_crop_bounds(value, block.bounds):
                add_word_cell_crop(para, source, crop, cell.width.pt - 12, 115)
            else:
                word_rich(para, cell_fragments(value))
            if header and r == 0:
                xml_child(cell._tc.get_or_add_tcPr(), "w:shd", {"w:fill": "EEEEEE"})
                for run in para.runs:
                    run.bold = True


def save_handout_word(sections, options, path):
    document = Document()
    document.core_properties.title = options.title
    section = document.sections[0]
    section.page_width, section.page_height = Pt(WIDTH), Pt(HEIGHT)
    section.left_margin = section.right_margin = section.top_margin = section.bottom_margin = Pt(MARGIN)
    section.footer_distance = Pt(24)
    styles = word_styles(document)
    bookmark_number = 0

    def heading(text, role, anchor, new_page=False):
        nonlocal bookmark_number
        para = document.add_paragraph(style=styles[role])
        para.paragraph_format.page_break_before = new_page
        if anchor:
            bookmark_number += 1
            xml_child(para._p, "w:bookmarkStart", {"w:id": bookmark_number, "w:name": anchor})
        para.add_run(text)
        if anchor:
            xml_child(para._p, "w:bookmarkEnd", {"w:id": bookmark_number})
        return para

    if options.cover:
        cover = heading(options.title, "title", "")
        cover.paragraph_format.space_before = Pt(205)
        cover.alignment = WD_ALIGN_PARAGRAPH.CENTER
    entries = tuple(contents_entries(sections)) if options.contents else ()
    if entries:
        heading("目录", "title", "", options.cover)
        for value, anchor, depth in entries:
            para = document.add_paragraph(style=styles["contents"])
            para.paragraph_format.left_indent = Pt(depth * 16)
            para.paragraph_format.tab_stops.add_tab_stop(Pt(CONTENT_WIDTH - depth * 16),
                                                        WD_TAB_ALIGNMENT.RIGHT, WD_TAB_LEADER.DOTS)
            link = xml_child(para._p, "w:hyperlink", {"w:anchor": anchor})
            xml_child(xml_child(link, "w:r", {}), "w:t", {}).text = value
            para.add_run("\t")
            xml_child(para._p, "w:fldSimple", {"w:instr": f" PAGEREF {anchor} \\h ", "w:dirty": "true"})
        settings = document.settings.element
        update = OxmlElement("w:updateFields")
        update.set(qn("w:val"), "true")
        settings.insert_element_before(update, "w:compat")
    if options.cover or entries:
        section = document.add_section(WD_SECTION_START.NEW_PAGE)
        section.footer.is_linked_to_previous = False
    page_number = OxmlElement("w:pgNumType")
    page_number.set(qn("w:start"), "1")
    section._sectPr.insert_element_before(page_number, "w:cols", "w:formProt", "w:docGrid")
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    xml_child(footer._p, "w:fldSimple", {"w:instr": "PAGE"})
    for source in sections:
        heading(source.title, "source", source.anchor)
        for block in source.blocks:
            if block.role == "heading":
                heading(block.value, "heading", block.anchor)
            elif block.role == "picture":
                para = document.add_paragraph(style=styles["caption"])
                para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                shape = para.add_run().add_picture(image_data(source, block), width=Pt(picture_size(block.bounds)[0]))
                if block.captions:
                    shape._inline.docPr.set("descr", "\n".join(block.captions))
                    para.paragraph_format.keep_with_next = True
                    for index, caption in enumerate(block.captions):
                        cap = document.add_paragraph(caption, styles["caption"])
                        cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
                        cap.paragraph_format.keep_with_next = index < len(block.captions) - 1
            elif block.role == "table":
                for value in block.captions:
                    cap = document.add_paragraph(value, styles["caption"])
                    cap.paragraph_format.keep_with_next = True
                word_table(document, block, styles, source.sheet.scan.image)
            elif block.inlines:
                role = {"equation": "formula", "inline": "paragraph"}.get(block.role, block.role)
                para = document.add_paragraph(style=styles[role])
                word_rich(para, fragments(block))
            else:
                document.add_paragraph(block.value, style=styles[block.role])
    with export_path(path) as temporary:
        document.save(temporary)


class FormulaLine(Flowable):
    def __init__(self, values, font_runs, preset):
        super().__init__()
        self.values, self.font_runs, self.preset = values, font_runs, preset
        self.spaceAfter = preset.after
        self.spaceBefore = preset.before
        self.prepared_rows = None

    def wrap(self, width, height):
        if self.prepared_rows is not None:
            self.rows = self.prepared_rows
            self.height = sum(row[2] for row in self.rows)
            return width, self.height
        size = self.preset.size
        boxes = []
        for math, value, *flags in self.values:
            if math:
                boxes.append(math_box(formula_nodes(value), size, self.font_runs))
            elif flags and flags[0]:
                boxes.append(text_box(value, size, self.font_runs))
            else:
                boxes.extend(text_box(part, size, self.font_runs)
                             for part in re.findall(r"[\u3400-\u9fff]|[^\s\u3400-\u9fff]+|\s+", value))
        rows = [[]]
        used = 0
        available = max(1, width - self.preset.indent)
        for box in boxes:
            first_indent = self.preset.first_line if len(rows) == 1 else 0
            if used and used + box.width > available - first_indent:
                rows.append([])
                used = 0
            rows[-1].append(box)
            used += box.width
        self.rows = []
        for index, parts in enumerate(rows):
            box = horizontal(parts)
            first_indent = self.preset.first_line if index == 0 else 0
            ratio = min(1, (available - first_indent) / max(box.width, 1))
            self.rows.append((box, ratio, max(self.preset.leading, ratio * (box.above + box.below + 3)),
                              self.preset.indent + first_indent))
        self.height = sum(row[2] for row in self.rows)
        return width, self.height

    def split(self, width, height):
        self.wrap(width, height)
        used = 0
        count = 0
        for row in self.rows:
            if used + row[2] > height:
                break
            used += row[2]
            count += 1
        if len(self.rows) - count == 1:
            count -= 1
        if count < 2 or count >= len(self.rows):
            return []
        head = FormulaLine(self.values, self.font_runs, self.preset)
        tail = FormulaLine(self.values, self.font_runs, self.preset)
        head.prepared_rows, tail.prepared_rows = self.rows[:count], self.rows[count:]
        head.spaceAfter = tail.spaceBefore = 0
        return [head, tail]

    def draw(self):
        top = self.height
        for box, ratio, leading, indent in self.rows:
            top -= leading
            self.canv.saveState()
            self.canv.translate(indent, top + (leading - ratio * (box.above - box.below)) / 2)
            self.canv.scale(ratio, ratio)
            box.draw(self.canv, 0, 0)
            self.canv.restoreState()


class HandoutPDF(BaseDocTemplate):
    def __init__(self, path, title, front_matter):
        super().__init__(path, pagesize=(WIDTH, HEIGHT), title=title,
                         leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN, bottomMargin=MARGIN)
        self.sources = {}
        self.source = None
        self.caption_pages = {}
        self.anchor_pages = {}
        self.body_start = None
        self.front_matter = front_matter

    def afterFlowable(self, flowable):
        if hasattr(flowable, "anchor"):
            key, title, depth = flowable.anchor
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(title, key, level=depth)
            self.anchor_pages[key] = self.page - self.body_start + 1
        if hasattr(flowable, "source"):
            self.source = flowable.source
        if hasattr(flowable, "caption_group"):
            key, bounds = flowable.caption_group
            pages = self.caption_pages.setdefault(key, (self.source, bounds, set()))[2]
            pages.add(self.page)
        if self.source:
            sheets = self.sources.setdefault(self.page, [])
            if not sheets or sheets[-1] is not self.source:
                sheets.append(self.source)

    def footer(self, canvas, document):
        if self.body_start is None:
            self.body_start = document.page
        canvas.setFont("Helvetica", 9)
        canvas.drawCentredString(WIDTH / 2, 25, str(document.page - self.body_start + 1))

    def render(self, story):
        frame = Frame(MARGIN, MARGIN, CONTENT_WIDTH, HEIGHT - 2 * MARGIN,
                      leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
        body = PageTemplate("body", (frame,), onPage=self.footer)
        if self.front_matter:
            self.addPageTemplates(PageTemplate("front", (frame,)))
        self.addPageTemplates(body)
        self.build(story)


def save_handout_pdf(sections, options, path):
    sans = pdf_fonts()
    fonts = pdf_fonts(serif=True)
    font_runs = lambda value: pdf_text_runs(value, fonts)
    styles = {role: ParagraphStyle(role, fontName=(sans if role in ("title", "source", "heading", "callout_label")
                                                  else fonts)[0], fontSize=style.size, leading=style.leading,
                                    wordWrap="CJK", leftIndent=style.indent, spaceAfter=style.after,
                                    firstLineIndent=style.first_line,
                                    spaceBefore=style.before, splitLongWords=True,
                                    allowWidows=0, allowOrphans=0, keepWithNext=role in ("source", "heading"),
                                    alignment=1 if role == "caption" else 0)
              for role, style in STYLES.items()}

    def para(value, role, *, anchor="", depth=0, target="", source=None):
        selected = sans if role in ("title", "source", "heading", "callout_label") else fonts
        markup = "".join(f'<font name="{font}">{escape(part)}</font>' for font, part in pdf_text_runs(value, selected))
        if target:
            markup = f'<link href="#{target}">{markup}</link>'
        token = Paragraph(markup, styles[role])
        if anchor:
            token.anchor = (anchor, value, depth)
        if source:
            token.source = source
        return token

    def pdf_table(block, source):
        rows, header = table_data(block.value)
        style = [("GRID", (0, 0), (-1, -1), 0.4, colors.black), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                 ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]
        if header:
            style.append(("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke))
        edges = table_edges(rows)
        widths = [CONTENT_WIDTH * (b - a) for a, b in zip(edges, edges[1:])]
        cells = []
        for r, row in enumerate(rows):
            line = []
            for c, value in enumerate(row):
                if value == "^":
                    line.append("")
                    continue
                end = r + 1
                while end < len(rows) and rows[end][c] == "^":
                    end += 1
                if end > r + 1:
                    style.append(("SPAN", (c, r), (c, end - 1)))
                if crop := cell_crop_bounds(value, block.bounds):
                    factor = min(max(1, widths[c] - 12) / crop.width, 115 / crop.height)
                    data = BytesIO()
                    crop.crop(source).save(data, format="PNG")
                    data.seek(0)
                    line.append(Image(data, width=crop.width * factor, height=crop.height * factor))
                else:
                    parts = cell_fragments(value)
                    line.append(FormulaLine(parts, font_runs, STYLES["table"]) if any(math for math, _ in parts)
                                else para(value, "table"))
            cells.append(line)
        return Table(cells, colWidths=widths,
                     repeatRows=int(header), splitInRow=1, style=TableStyle(style), spaceAfter=8)

    entries = tuple(contents_entries(sections)) if options.contents else ()
    front_matter = options.cover or bool(entries)

    def story_for(pages):
        story = []
        if options.cover:
            cover = para(options.title, "title")
            cover.style = cover.style.clone("cover", alignment=1)
            story.extend((Spacer(1, 205), cover))
        if entries:
            if options.cover:
                story.append(PageBreak())
            story.append(para("目录", "title"))
            rows = []
            for value, anchor, depth in entries:
                label = para(("　" if depth else "") + value, "contents", target=anchor)
                number = para(str(pages.get(anchor, 1)), "contents", target=anchor)
                number.style = number.style.clone("contents-number", alignment=2)
                rows.append([label, number])
            story.append(Table(rows, colWidths=[CONTENT_WIDTH - 38, 38], hAlign="LEFT",
                               style=[("VALIGN", (0, 0), (-1, -1), "TOP")]))
        if front_matter:
            story.extend((NextPageTemplate("body"), PageBreak()))
        for section in sections:
            story.append(para(section.title, "source", anchor=section.anchor, source=section.sheet))
            for block_number, block in enumerate(section.blocks):
                caption_group = (f"{section.anchor}-{block_number}", block.bounds)
                if block.role == "picture":
                    width, height = picture_size(block.bounds)
                    picture = Image(image_data(section, block), width=width, height=height)
                    picture.hAlign = "CENTER"
                    picture.spaceAfter = 5
                    captions = [para(value, "caption") for value in block.captions]
                    if captions:
                        picture.caption_group = caption_group
                        for caption in captions:
                            caption.caption_group = caption_group
                    story.append(KeepTogether([picture, *captions]) if captions else picture)
                elif block.role == "table":
                    for value in block.captions:
                        caption = para(value, "caption")
                        caption.keepWithNext = True
                        story.append(caption)
                    story.append(pdf_table(block, section.sheet.scan.image))
                elif any(inline.latex or inline.state == "needs_review" for inline in block.inlines):
                    role = {"equation": "formula", "inline": "paragraph"}.get(block.role, block.role)
                    story.append(FormulaLine(fragments(block), font_runs, STYLES[role]))
                else:
                    story.append(para(block.value, block.role, anchor=block.anchor, depth=1))
        return story

    pages = {}
    if entries:
        temporary_doc = HandoutPDF(BytesIO(), options.title, front_matter)
        temporary_doc.render(story_for(pages))
        pages = temporary_doc.anchor_pages
    with export_path(path) as temporary:
        document = HandoutPDF(str(temporary), options.title, front_matter)
        document.render(story_for(pages))
        if entries and pages != document.anchor_pages:
            raise ValueError("讲义目录分页改变，请检查目录条目长度。")
    return document.sources, document.caption_pages
