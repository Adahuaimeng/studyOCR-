"""Editing models that preserve the original Word and PDF objects."""

from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from io import BytesIO
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.shared import Cm
from docx.table import Table
from docx.text.paragraph import Paragraph
from docx.text.run import Run
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Highlight, Text
from pypdf.generic import ArrayObject, FloatObject, RectangleObject

from exports import export_path


@dataclass(frozen=True)
class WordText:
    paragraph: Paragraph
    runs: tuple[Run, ...]
    location: str

    @property
    def text(self):
        return "".join(run.text for run in self.runs)

    def update(self, value):
        former = self.text
        if former == value:
            return
        indices = [number for number, run in enumerate(self.runs) for _ in run.text]
        revised = [[] for run in self.runs]
        offset = 0
        for operation, first, last, start, stop in SequenceMatcher(None, former, value, autojunk=False).get_opcodes():
            if operation == "equal":
                for run_index, character in zip(indices[first:last], value[start:stop], strict=True):
                    revised[run_index].append(character)
                if last:
                    offset = indices[last - 1]
            elif operation != "delete":
                offset = indices[first] if first < len(indices) else offset
                revised[offset].append(value[start:stop])
        for run, fragments in zip(self.runs, revised, strict=True):
            new_text = "".join(fragments)
            if run.text != new_text:
                run.text = new_text


@dataclass(frozen=True)
class WordImage:
    paragraph: Paragraph
    drawing: object
    location: str

    @property
    def relation(self):
        return self.drawing.xpath(".//a:blip[@r:embed]")[0]

    @property
    def blob(self):
        rid = self.relation.get(qn("r:embed"))
        return self.paragraph.part.related_parts[rid].blob

    @property
    def extent(self):
        return self.drawing.xpath(".//wp:extent")[0]

    @property
    def width_cm(self):
        return int(self.extent.get("cx")) / Cm(1)

    def resize(self, width_cm):
        if not 0.1 <= width_cm <= 40:
            raise ValueError("图片宽度须在 0.1 到 40 厘米之间")
        extent = self.extent
        old_width = int(extent.get("cx"))
        if old_width <= 0:
            raise ValueError("图片尺寸无效")
        new_width = int(Cm(width_cm))
        self.set_dimensions(new_width, round(new_width / old_width * int(extent.get("cy"))))

    def set_dimensions(self, new_width, new_height):
        for element in [self.extent, *self.drawing.xpath(".//pic:spPr/a:xfrm/a:ext")]:
            element.set("cx", str(new_width))
            element.set("cy", str(new_height))

    def replace(self, path):
        # Update this occurrence only. The original relationship may be shared.
        old = self.relation.get(qn("r:embed"))
        rid, image = self.paragraph.part.get_or_add_image(str(path))
        extent = self.extent
        width = int(extent.get("cx"))
        height = round(width * image.px_height / image.px_width)
        self.relation.set(qn("r:embed"), rid)
        self.set_dimensions(width, height)
        if old != rid:
            self.drop_unused_relation(old)

    def remove(self):
        old = self.relation.get(qn("r:embed"))
        self.drawing.getparent().remove(self.drawing)
        self.drop_unused_relation(old)

    def drop_unused_relation(self, rid):
        part = self.paragraph.part
        attributes = tuple(qn(name) for name in ("r:embed", "r:id", "r:link"))
        if not any(element.get(name) == rid for element in part.element.iter() for name in attributes):
            part.drop_rel(rid)


_EDITABLE_RUN = {qn(f"w:{kind}") for kind in ("rPr", "t", "tab", "br", "cr")}


def editable_run(element):
    return (element.tag == qn("w:r")
            and all(child.tag in _EDITABLE_RUN for child in element)
            and all(child.get(qn("w:type")) in (None, "textWrapping")
                    for child in element if child.tag == qn("w:br")))


def paragraph_items(paragraph, label):
    spans = []
    field_depth = 0
    for item in paragraph._p:
        fld_chars = item.xpath("./w:fldChar") if item.tag == qn("w:r") else ()
        if not field_depth and not fld_chars and editable_run(item):
            run = Run(item, paragraph)
            if run.text:
                spans.append(run)
            continue
        if spans:
            yield WordText(paragraph, tuple(spans), label)
            spans.clear()
        if fld_chars:
            for field in fld_chars:
                action = field.get(qn("w:fldCharType"))
                if action == "begin":
                    field_depth += 1
                elif action == "end":
                    field_depth = max(0, field_depth - 1)
            continue
        if not field_depth and item.tag == qn("w:r"):
            for drawing in item.xpath("./w:drawing"):
                if drawing.xpath(".//a:blip[@r:embed]") and drawing.xpath(".//wp:extent"):
                    yield WordImage(paragraph, drawing, label)
    if spans:
        yield WordText(paragraph, tuple(spans), label)


def word_items(container, prefix="正文"):
    for index, child in enumerate(container.iter_inner_content(), 1):
        label = f"{prefix} · {index}"
        if isinstance(child, Paragraph):
            yield from paragraph_items(child, label)
        elif isinstance(child, Table):
            cells = set()
            for row_index, row in enumerate(child.rows, 1):
                for column_index, cell in enumerate(row.cells, 1):
                    if cell._tc in cells:
                        continue
                    cells.add(cell._tc)
                    yield from word_items(cell, f"{label} 表格 {row_index},{column_index}")


class WordEditor:
    def __init__(self, path):
        self.source = Path(path).resolve()
        self.document = Document(self.source)
        self.inserted_paragraphs = set()

    def items(self):
        return tuple(word_items(self.document))

    def insert_image(self, path, after=None):
        paragraph = self.document.add_paragraph()
        try:
            paragraph.add_run().add_picture(str(path), width=Cm(8))
        except Exception:
            paragraph._p.getparent().remove(paragraph._p)
            raise
        if after is not None:
            after._p.addnext(paragraph._p)
        self.inserted_paragraphs.add(paragraph._p)

    def remove_image(self, image):
        image.remove()
        paragraph = image.paragraph._p
        if paragraph in self.inserted_paragraphs:
            paragraph.getparent().remove(paragraph)
            self.inserted_paragraphs.remove(paragraph)

    def save(self, path):
        if path.resolve() == self.source:
            raise ValueError("请选择与原文件不同的文件名")
        with export_path(path) as temporary:
            self.document.save(temporary)


@dataclass(frozen=True)
class PdfMark:
    corners: tuple[tuple[float, float], ...]
    text: str | None = None

    def annotation(self):
        x, y = zip(*self.corners, strict=True)
        rect = min(x), min(y), max(x), max(y)
        if self.text is not None:
            return Text(rect=rect, text=self.text)
        quad = ArrayObject([FloatObject(coord) for point in self.corners for coord in point])
        return Highlight(rect=rect, quad_points=quad, highlight_color="ffdb31", printing=True)


@dataclass(frozen=True)
class PdfSheet:
    source: Path
    reader: PdfReader
    number: int
    rotation: int
    crop: tuple[float, float, float, float]
    marks: tuple[PdfMark, ...] = ()

    @property
    def page(self):
        return self.reader.pages[self.number]

    def view_point(self, u, v):
        left, bottom, right, top = self.crop
        w, h = right - left, top - bottom
        if self.rotation == 90:
            return left + v * w, bottom + u * h
        if self.rotation == 180:
            return right - u * w, bottom + v * h
        if self.rotation == 270:
            return right - v * w, top - u * h
        return left + u * w, top - v * h

    def area(self, region, size):
        width, height = size
        return tuple(self.view_point(x / width, y / height) for x, y in (
            (region.left, region.top), (region.right, region.top),
            (region.left, region.bottom), (region.right, region.bottom),
        ))

    def add_to(self, writer):
        page = writer.add_page(self.page)
        page.rotation = self.rotation
        page.cropbox = RectangleObject(self.crop)
        for mark in self.marks:
            writer.add_annotation(page, mark.annotation())


def sheets_from(path):
    source = Path(path).resolve()
    reader = PdfReader(source)
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("此 PDF 需要密码，请先在阅读器中另存可打开的副本")
    return tuple(PdfSheet(source, reader, number, page.rotation % 360,
                          tuple(float(v) for v in page.cropbox))
                 for number, page in enumerate(reader.pages))


def render_pdf_page(page, max_side, *, draw_annotations=True):
    scale = min(4, max_side / max(page.get_size()))
    bitmap = page.render(scale=scale, draw_annots=draw_annotations)
    try:
        return bitmap.to_pil().convert("RGB")
    finally:
        bitmap.close()


def render_sheet(sheet, max_side=1250, *, draw_annotations=True):
    import pypdfium2 as pdfium

    writer = PdfWriter()
    sheet.add_to(writer)
    output = BytesIO()
    writer.write(output)
    doc = pdfium.PdfDocument(output.getvalue())
    try:
        page = doc[0]
        try:
            return render_pdf_page(page, max_side, draw_annotations=draw_annotations)
        finally:
            page.close()
    finally:
        doc.close()


class PdfEditor:
    def __init__(self, path):
        self.pages = list(sheets_from(path))
        if not self.pages:
            raise ValueError("PDF 中没有页面")
        self.sources = {sheet.source for sheet in self.pages}
        self.original = self.pages[0].source
        self.history = []

    def remember(self, current):
        self.history.append((tuple(self.pages), current))

    def undo(self):
        pages, selected = self.history.pop()
        self.pages = list(pages)
        return selected

    def add(self, paths, current):
        added = [sheet for path in paths for sheet in sheets_from(path)]
        if added:
            self.remember(current)
            self.pages.extend(added)
            self.sources.update(sheet.source for sheet in added)

    def rotate(self, index, degrees):
        self.remember(index)
        sheet = self.pages[index]
        self.pages[index] = replace(sheet, rotation=(sheet.rotation + degrees) % 360)

    def delete(self, index):
        if len(self.pages) <= 1:
            raise ValueError("请至少保留一页")
        self.remember(index)
        del self.pages[index]

    def move(self, source, target):
        if 0 <= target < len(self.pages) and source != target:
            self.remember(source)
            self.pages.insert(target, self.pages.pop(source))

    def crop(self, index, region, size):
        sheet = self.pages[index]
        x, y = zip(*sheet.area(region, size), strict=True)
        self.remember(index)
        self.pages[index] = replace(sheet, crop=(min(x), min(y), max(x), max(y)))

    def mark(self, index, region, size, text=None):
        sheet = self.pages[index]
        annotation = PdfMark(sheet.area(region, size), text)
        self.remember(index)
        self.pages[index] = replace(sheet, marks=sheet.marks + (annotation,))

    def save(self, path):
        if path.resolve() in self.sources:
            raise ValueError("请选择与源 PDF 不同的文件名")
        writer = PdfWriter()
        info = self.pages[0].reader.metadata
        if info:
            metadata = {key: str(value) for key, value in info.items()
                        if key in {"/Title", "/Author", "/Subject", "/Keywords"} and value}
            if metadata:
                writer.add_metadata(metadata)
        for sheet in self.pages:
            sheet.add_to(writer)
        with export_path(path) as temporary:
            writer.write(temporary)
