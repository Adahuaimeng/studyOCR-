"""Study pages, saved selections, and the portable working draft format."""

from dataclasses import dataclass, field, replace
from enum import StrEnum
from hashlib import sha256
import json
from uuid import uuid4
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from PIL import Image

from ocr_core import Rect, Scan, TextKind, TextLine
from ocr_quality import ANGLES, rotated_bounds, rotated_image


MAX_PAGES = 200
MAX_PIXELS = 250_000_000


class Extraction(StrEnum):
    ALL = "全部"
    PRINTED = "仅印刷体"
    HANDWRITTEN = "仅手写体"

    def includes(self, kind: TextKind) -> bool:
        if self == Extraction.ALL:
            return kind != TextKind.OMIT
        return kind == (TextKind.PRINTED if self == Extraction.PRINTED else TextKind.HANDWRITTEN)


class BlockFormat(StrEnum):
    EQUATION = "原生公式"
    TABLE = "可编辑表格"
    INLINE = "正文与行内公式"


@dataclass(frozen=True)
class StructuredBlock:
    bounds: Rect
    kind: TextKind
    format: BlockFormat
    value: str


@dataclass
class StudyPage:
    name: str
    image: Image.Image
    id: str = field(default_factory=lambda: uuid4().hex)
    scan: Scan | None = None
    regions: tuple[Rect, ...] = ()
    blocks: tuple[StructuredBlock, ...] = ()
    rotation: int = 0
    quality: dict | None = None
    figure_exclusions: tuple[Rect, ...] = ()
    figure_captions: tuple[tuple[Rect, Rect], ...] = ()
    duplicate: bool = False
    handout_title: str = ""

    @property
    def bounds(self) -> Rect:
        return Rect(0, 0, *self.image.size)

    def export_sheet(self) -> "ExportSheet | None":
        if self.scan is None:
            return None
        return ExportSheet(self.scan, self.regions, self.blocks, self.figure_exclusions, self.figure_captions,
                           self.id, self.name, self.bounds, self.handout_title)

    def photo_sheet(self):
        scan = self.scan
        if scan is None:
            return ExportSheet(Scan(self.image, self.bounds, (), 0), source_id=self.id, source_name=self.name)
        lines = tuple(replace(line, bounds=line.bounds.translated(scan.area.left, scan.area.top))
                      for line in scan.lines)
        return replace(self.export_sheet(), scan=Scan(self.image, self.bounds, lines, scan.seconds))


@dataclass(frozen=True)
class ExportSheet:
    scan: Scan
    regions: tuple[Rect, ...] = ()
    blocks: tuple[StructuredBlock, ...] = ()
    figure_exclusions: tuple[Rect, ...] = ()
    figure_captions: tuple[tuple[Rect, Rect], ...] = ()
    source_id: str | None = None
    source_name: str = ""
    source_extent: Rect | None = None
    handout_title: str = ""


@dataclass(frozen=True)
class Selection:
    name: str
    page_id: str
    area: Rect
    lines: tuple[TextLine, ...]
    regions: tuple[Rect, ...]
    blocks: tuple[StructuredBlock, ...]
    figure_exclusions: tuple[Rect, ...] = ()
    figure_captions: tuple[tuple[Rect, Rect], ...] = ()

    def sheet(self, page: StudyPage):
        image = self.area.crop(page.image)
        return ExportSheet(Scan(image, self.area, self.lines, 0), self.regions, self.blocks,
                           self.figure_exclusions, self.figure_captions, page.id, self.name, page.bounds)


@dataclass
class StudyDraft:
    pages: list[StudyPage] = field(default_factory=list)
    selections: list[Selection] = field(default_factory=list)
    active_index: int = 0
    export_order: tuple[str, ...] = ()

    def can_add(self, image):
        loaded_pixels = sum(page.image.width * page.image.height for page in self.pages)
        return (len(self.pages) < MAX_PAGES
                and loaded_pixels + image.width * image.height <= MAX_PIXELS)

    def selection_sheets(self):
        pages = {page.id: page for page in self.pages}
        return tuple(selection.sheet(pages[selection.page_id]) for selection in self.selections)

    def export_pages(self):
        pages = {page.id: page for page in self.pages}
        return tuple(pages[page_id] for page_id in self.export_order if page_id in pages)


def rotate_page(draft, page, angle, image=None):
    if not angle:
        return
    size = page.image.size
    image = rotated_image(page.image, angle) if image is None else image

    def transform(bounds):
        return rotated_bounds(bounds, size, angle)

    def transform_lines(lines, old, new):
        return tuple(replace(line, bounds=transform(
            line.bounds.translated(old.left, old.top)
        ).relative_to(new)) for line in lines)

    def transform_blocks(blocks):
        from structured import rotate_cell_crops

        return tuple(replace(block, bounds=transform(block.bounds),
                             value=rotate_cell_crops(block.value, angle)
                             if block.format == BlockFormat.TABLE else block.value) for block in blocks)

    page.image = image
    page.rotation = (page.rotation + angle) % 360
    page.quality = None
    page.regions = tuple(transform(bounds) for bounds in page.regions)
    page.figure_exclusions = tuple(transform(bounds) for bounds in page.figure_exclusions)
    page.figure_captions = tuple((transform(crop), transform(line)) for crop, line in page.figure_captions)
    page.blocks = transform_blocks(page.blocks)
    if page.scan:
        scan = page.scan
        area = transform(scan.area)
        page.scan = Scan(image if area == page.bounds else area.crop(image), area,
                         transform_lines(scan.lines, scan.area, area), scan.seconds)
    for index, selection in enumerate(draft.selections):
        if selection.page_id == page.id:
            area = transform(selection.area)
            draft.selections[index] = replace(
                selection, area=area, lines=transform_lines(selection.lines, selection.area, area),
                regions=tuple(transform(region) for region in selection.regions),
                figure_exclusions=tuple(transform(region) for region in selection.figure_exclusions),
                figure_captions=tuple((transform(crop), transform(line))
                                      for crop, line in selection.figure_captions),
                blocks=transform_blocks(selection.blocks),
            )


def collect_selection(page: StudyPage, area: Rect, title: str, *, image_only=False):
    lines = []
    if page.scan and not image_only:
        origin = page.scan.area
        for line in page.scan.lines:
            source = line.bounds.translated(origin.left, origin.top)
            if source.intersection(area) == source:
                lines.append(replace(line, bounds=source.relative_to(area)))
    blocks = () if image_only else tuple(
        block for block in page.blocks if block.bounds.intersection(area) == block.bounds
    )
    has_content = any(line.kind != TextKind.OMIT for line in lines) or bool(blocks)
    regions = (area,) if image_only or not has_content else page.regions
    bindings = tuple((crop, line) for crop, line in page.figure_captions
                     if area.intersection(line) == line and area.intersection(crop)) if not image_only else ()
    return Selection(title, page.id, area, tuple(lines), regions, blocks, page.figure_exclusions, bindings)


def _rect(box):
    return [box.left, box.top, box.right, box.bottom]


def _line(line):
    return [_rect(line.bounds), line.text, line.confidence, line.kind.value]


def _block(block):
    return [_rect(block.bounds), block.kind.value, block.format.value, block.value]


def image_key(image):
    return image.mode, image.size, sha256(image.tobytes()).digest()


def scan_data(scan):
    value = {"area": _rect(scan.area), "lines": [_line(line) for line in scan.lines], "seconds": scan.seconds}
    if changes := line_review_data(scan.lines):
        value["review"] = changes
    return value


def line_review_data(lines):
    return [[index, line.role, line.review_order] for index, line in enumerate(lines)
            if line.role is not None or line.review_order is not None]


def write_draft(draft, path):
    manifest = {"version": 1, "active": draft.active_index, "pages": [], "selections": []}
    if draft.export_order:
        manifest["export_order"] = list(draft.export_order)
    images = {}
    with ZipFile(path, "w", compression=ZIP_DEFLATED, compresslevel=4) as archive:
        for index, page in enumerate(draft.pages):
            item = {"id": page.id, "name": page.name,
                    "regions": [_rect(b) for b in page.regions],
                    "blocks": [_block(b) for b in page.blocks]}
            if page.rotation:
                item["rotation"] = page.rotation
            if page.duplicate:
                item["duplicate"] = True
            if page.handout_title:
                item["handout_title"] = page.handout_title
            if page.quality is not None:
                item["quality"] = page.quality
            if page.figure_exclusions:
                item["figure_exclusions"] = [_rect(b) for b in page.figure_exclusions]
            if page.figure_captions:
                item["figure_captions"] = [[_rect(crop), _rect(line)] for crop, line in page.figure_captions]
            if page.scan is not None:
                item["scan"] = scan_data(page.scan)
            key = image_key(page.image)
            previous = images.get(key)
            if previous is not None:
                item["image_ref"] = previous
            else:
                images[key] = index
                with archive.open(f"images/{index}.png", "w") as target:
                    page.image.save(target, format="PNG", compress_level=3)
            manifest["pages"].append(item)
        for selection in draft.selections:
            manifest["selections"].append({
                "name": selection.name, "page": selection.page_id, "area": _rect(selection.area),
                "lines": [_line(x) for x in selection.lines],
                "regions": [_rect(x) for x in selection.regions],
                "blocks": [_block(x) for x in selection.blocks],
                "figure_exclusions": [_rect(x) for x in selection.figure_exclusions],
                "figure_captions": [[_rect(crop), _rect(line)] for crop, line in selection.figure_captions],
                "review": line_review_data(selection.lines),
            })
        archive.writestr("draft.json", json.dumps(manifest, ensure_ascii=False, allow_nan=False))


def _validate_rect(values, extent):
    if len(values) != 4 or any(type(v) is not int for v in values):
        raise ValueError("工作稿中的区域坐标无效")
    bounds = Rect(*values)
    if bounds.intersection(extent) != bounds:
        raise ValueError("工作稿中的区域超出图片范围")
    return bounds


def _load_lines(values, area, review=()):
    extent = Rect(0, 0, area.width, area.height)
    lines = [TextLine(_validate_rect(bounds, extent), str(value), float(score), TextKind(kind))
             for bounds, value, score, kind in values]
    from page_structure import ROLE_NAMES
    for index, role, order in review:
        if (type(index) is not int or not 0 <= index < len(lines)
                or role is not None and role not in ROLE_NAMES
                or order is not None and (type(order) is not int or order < 0)):
            raise ValueError("工作稿中的文字角色或阅读顺序无效")
        lines[index] = replace(lines[index], role=role, review_order=order)
    return tuple(lines)


def restore_scan(image, saved):
    extent = Rect(0, 0, *image.size)
    area = _validate_rect(saved["area"], extent)
    return Scan(image if area == extent else area.crop(image), area, _load_lines(saved["lines"], area,
                                                                                saved.get("review", ())),
                float(saved["seconds"]))


def _load_blocks(values, extent):
    from structured import validate_block

    blocks = tuple(StructuredBlock(_validate_rect(bounds, extent), TextKind(kind),
                                   BlockFormat(fmt), str(value))
                   for bounds, kind, fmt, value in values)
    for block in blocks:
        validate_block(block)
    return blocks


def read_draft(path):
    try:
        with ZipFile(path) as archive:
            if archive.getinfo("draft.json").file_size > 32 * 1024 * 1024:
                raise ValueError("工作稿数据过大")
            manifest = json.loads(archive.read("draft.json"))
            if manifest["version"] != 1:
                raise ValueError("工作稿版本不受支持")
            entries = manifest["pages"]
            if not 1 <= len(entries) <= MAX_PAGES:
                raise ValueError("工作稿页数无效")
            pages = []
            by_id = {}
            total_pixels = 0
            for index, item in enumerate(entries):
                reference = item.get("image_ref", index)
                if type(reference) is not int or not 0 <= reference <= index:
                    raise ValueError("工作稿中的图片引用无效")
                if reference < index:
                    image = pages[reference].image
                    total_pixels += image.width * image.height
                    if total_pixels > MAX_PIXELS:
                        raise ValueError("工作稿图片总像素过大")
                else:
                    member = f"images/{index}.png"
                    if archive.getinfo(member).file_size > 128 * 1024 * 1024:
                        raise ValueError("工作稿中的图片过大")
                    with archive.open(member) as source, Image.open(source) as image_file:
                        total_pixels += image_file.width * image_file.height
                        if total_pixels > MAX_PIXELS:
                            raise ValueError("工作稿图片总像素过大")
                        image = image_file.convert("RGB")
                page = StudyPage(str(item["name"]), image, str(item["id"]))
                page.duplicate = item.get("duplicate") is True
                title = item.get("handout_title", "")
                if not isinstance(title, str) or len(title) > 100 or any(ord(c) < 32 for c in title):
                    raise ValueError("工作稿中的讲义标题无效")
                page.handout_title = title.strip()
                rotation = item.get("rotation", 0)
                if type(rotation) is not int or rotation not in ANGLES:
                    raise ValueError("工作稿旋转角度无效")
                page.rotation = rotation
                quality = item.get("quality")
                if isinstance(quality, dict) and all(isinstance(quality.get(key), str)
                                                      for key in ("status", "reason", "action")):
                    page.quality = quality
                if page.id in by_id:
                    raise ValueError("工作稿中的页面编号重复")
                extent = page.bounds
                page.regions = tuple(_validate_rect(x, extent) for x in item["regions"])
                page.figure_exclusions = tuple(_validate_rect(x, extent)
                                               for x in item.get("figure_exclusions", ()))
                page.figure_captions = tuple((_validate_rect(crop, extent), _validate_rect(line, extent))
                                             for crop, line in item.get("figure_captions", ()))
                page.blocks = _load_blocks(item["blocks"], extent)
                if "scan" in item:
                    page.scan = restore_scan(image, item["scan"])
                pages.append(page)
                by_id[page.id] = page
            selections = []
            for item in manifest["selections"]:
                page = by_id[item["page"]]
                area = _validate_rect(item["area"], page.bounds)
                selections.append(Selection(
                    str(item["name"]), page.id, area, _load_lines(item["lines"], area, item.get("review", ())),
                    tuple(_validate_rect(x, page.bounds) for x in item["regions"]),
                    _load_blocks(item["blocks"], page.bounds),
                    tuple(_validate_rect(x, page.bounds) for x in item.get("figure_exclusions", ())),
                    tuple((_validate_rect(crop, page.bounds), _validate_rect(line, page.bounds))
                          for crop, line in item.get("figure_captions", ())),
                ))
            active = int(manifest["active"])
            if not 0 <= active < len(pages):
                raise ValueError("工作稿页码无效")
            order = manifest.get("export_order", [])
            if (not isinstance(order, list) or any(not isinstance(key, str) or key not in by_id for key in order)
                    or len(set(order)) != len(order)):
                raise ValueError("工作稿的自选导出顺序无效")
            return StudyDraft(pages, selections, active, tuple(order))
    except (BadZipFile, KeyError, TypeError, IndexError, AttributeError) as error:
        raise ValueError("无法读取工作稿或工作稿结构已损坏") from error
