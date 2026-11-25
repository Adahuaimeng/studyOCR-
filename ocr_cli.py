"""Command-line OCR and document handoff for local AI agents."""

from argparse import ArgumentParser
from contextlib import ExitStack, redirect_stdout
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile

from PIL import Image
from batch_jobs import pending_indices

from document_preview import DocumentExport
from handout import HandoutOptions, sections_for
from export_audit import document_assets
from exports import image_regions, local_blocks, page_layout, sheet_caption_links
from illustrations import illustration_role, pixel_area
from ocr_core import LocalOcr, Rect, TextKind, open_image
from ocr_quality import ROTATIONS, quality_result, recognize_page, rotated_bounds
from page_structure import text_blocks
from reconstruction import reconstruction_queue, validate_confirmation
from study import (BlockFormat, Extraction, MAX_PAGES, MAX_PIXELS, StructuredBlock, StudyDraft,
                   StudyPage, read_draft, rotate_page, write_draft)


SCHEMA_VERSION = 1
EXTRACTIONS = {"all": Extraction.ALL, "printed": Extraction.PRINTED, "handwritten": Extraction.HANDWRITTEN}
MAX_EDITS_FILE = 8 * 1024 * 1024
MAX_EDIT_TEXT = 8000


def json_text(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n"


def rectangle(bounds):
    return [bounds.left, bounds.top, bounds.right, bounds.bottom]


def line_id(page, index, line):
    # A text or category change invalidates old edits without changing the working-draft format.
    data = json.dumps((page.id, index, rectangle(page.scan.area), rectangle(line.bounds), line.kind, line.text),
                      ensure_ascii=True, separators=(",", ":"))
    return sha256(data.encode("ascii")).hexdigest()


def category_counts(lines, mode=Extraction.ALL):
    totals = {kind.value: 0 for kind in TextKind}
    for line in lines:
        totals[line.kind.value] += 1
    included = {kind.value: totals[kind.value] if mode.includes(kind) else 0 for kind in TextKind}
    return {
        "total": totals,
        "included": included,
        "excluded": {key: totals[key] - included[key] for key in totals},
    }


def page_result(page, *, auto_figures):
    scan = page.scan
    input_size = page.image.size[::-1] if page.rotation in (90, 270) else page.image.size

    def input_coordinates(bounds):
        return rectangle(rotated_bounds(bounds, page.image.size, (-page.rotation) % 360))

    data = {
        "page_id": page.id,
        "name": page.name,
        "width": page.image.width,
        "height": page.image.height,
        "scan_area": rectangle(scan.area) if scan else None,
        "ocr_seconds": scan.seconds if scan else None,
        "quality": page.quality,
        "orientation": {"clockwise": page.rotation, "input_size": list(input_size)},
        "input_scan_area": input_coordinates(scan.area) if scan else None,
        "lines": [],
        "figure_regions": [rectangle(region) for region in page.regions],
        "figure_assets": [
            {"bounds": rectangle(region), "role": "figure", "source": "manual", "caption_line_ids": []}
            for region in page.regions
        ],
        "layout": None,
        "blocks": [],
        "structure_review": [],
        "text_counts": category_counts(()),
        "objects": [
            {"bounds": rectangle(block.bounds), "kind": block.kind, "format": block.format, "value": block.value}
            for block in page.blocks
        ],
    }
    if scan:
        def source_coordinates(bounds):
            return rectangle(bounds.translated(scan.area.left, scan.area.top))

        ids = {id(line): line_id(page, index, line) for index, line in enumerate(scan.lines)}
        layout = page_layout(scan)
        paragraphs = text_blocks(layout)
        data["layout"] = {
            "gutter_x": layout.gutter + scan.area.left if layout.gutter is not None else None,
            "bands": [{"columns": len(band.columns)} for band in layout.bands],
        }
        data["blocks"] = [
            {"role": block.role, "bounds": source_coordinates(block.bounds),
             "band": block.band, "column": block.column,
             "line_ids": [ids[id(line)] for line in block.lines]}
            for block in paragraphs
        ]
        data["text_counts"] = category_counts(scan.lines)
        data["lines"] = [{
            "line_id": ids[id(line)], "text": line.text, "kind": line.kind,
            "confidence": line.confidence, "bounds": source_coordinates(line.bounds),
            "input_bounds": input_coordinates(line.bounds.translated(scan.area.left, scan.area.top)),
        } for line in layout.reading_order]
        sheet = page.export_sheet()
        blocks = local_blocks(sheet, Extraction.ALL)
        regions = image_regions(sheet, blocks, auto_figures=auto_figures)
        data["structure_review"] = [
            {"bounds": source_coordinates(item.bounds), "format": item.format.value,
             "kind": item.kind.value, "value": item.value, "reason": item.reason, "method": item.method}
            for item in reconstruction_queue(sheet, regions, blocks)
        ]
        manual_count = len(image_regions(sheet, blocks, auto_figures=False))
        links = sheet_caption_links(sheet, regions, blocks, paragraphs)
        data["figure_regions"] = [source_coordinates(bounds) for bounds in regions]
        data["figure_assets"] = [
            {"bounds": source_coordinates(bounds), "role": illustration_role(scan, bounds),
             "source": "manual" if index < manual_count else "automatic",
             "caption_line_ids": [ids[id(line)] for line in links.get(index, ())]}
            for index, bounds in enumerate(regions)
        ]
        for item, block in zip(data["objects"], page.blocks):
            local = block.bounds.relative_to(scan.area)
            item["line_ids"] = [ids[id(line)] for line in scan.lines
                                if pixel_area(local.intersection(line.bounds)) >= pixel_area(line.bounds) * 0.8]
            block_index = next((i for i, candidate in enumerate(blocks) if candidate.bounds == local), None)
            item["caption_line_ids"] = ([ids[id(line)] for line in links.get(len(regions) + block_index, ())]
                                        if block_index is not None else [])
    return data


def result_for(draft, command, project, *, auto_figures):
    return {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "project": str(project) if project else None,
        "pages": [page_result(page, auto_figures=auto_figures) for page in draft.pages],
        "collection_count": len(draft.selections),
        "warnings": [{"page": number, "page_id": page.id, **page.quality}
                     for number, page in enumerate(draft.pages, 1)
                     if page.quality and page.quality["status"] not in ("ok", "blank")],
    }


def output_destinations(requested, inputs):
    resolved = {}
    sources = {path.resolve() for path in inputs}
    for key, (path, suffix) in requested.items():
        if path is None:
            continue
        path = path.resolve()
        if path.suffix.lower() != suffix:
            raise ValueError(f"输出文件需要使用 {suffix} 扩展名：{path}")
        if path in sources or path in resolved.values():
            raise ValueError(f"输入和输出必须使用不同的文件名：{path}")
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"输出文件已存在，请选择新文件名：{path}")
        if not path.parent.is_dir():
            raise FileNotFoundError(f"输出目录不存在：{path.parent}")
        resolved[key] = path
    return resolved


def publish_files(outputs):
    """Finish all writes before publishing. Existing files are never overwritten."""
    with ExitStack() as cleanup:
        staged = []
        for target, write in outputs:
            with NamedTemporaryFile(dir=target.parent, suffix=target.suffix, delete=False) as handle:
                pending = Path(handle.name)
            cleanup.callback(pending.unlink, missing_ok=True)
            write(pending)
            staged.append((pending, target, pending.stat()))
        published = []
        try:
            for pending, target, stamp in staged:
                if os.name == "nt":
                    pending.rename(target)
                else:
                    os.link(pending, target)
                published.append((target, stamp))
        except OSError:
            for target, stamp in published:
                try:
                    if os.path.samestat(stamp, target.lstat()):
                        target.unlink()
                except OSError:
                    pass
            raise


def publish_result(result, destinations, draft=None):
    written = []
    if "project" in destinations:
        written.append((destinations["project"], lambda path: write_draft(draft, path)))
    if "json" in destinations:
        written.append((destinations["json"], lambda path: path.write_text(json_text(result), encoding="utf-8")))
    publish_files(written)
    return result


def recognize(args):
    paths = args.images
    if len(paths) > MAX_PAGES:
        raise ValueError(f"一次最多识别 {MAX_PAGES} 张图片。")
    if args.region and len(paths) != 1:
        raise ValueError("--region 需要与一张图片一同使用。")
    targets = output_destinations({"project": (args.project, ".ocrproj"), "json": (args.json, ".json")}, paths)
    engine = LocalOcr()
    draft = StudyDraft()
    pixels = 0
    for number, path in enumerate(paths, 1):
        if args.progress:
            print(f"OCR {number}/{len(paths)} · {path.name}", file=sys.stderr)
        image = open_image(path, max_pixels=MAX_PIXELS - pixels)
        pixels += image.width * image.height
        page = StudyPage(path.name, image)
        area = Rect(*args.region) if args.region else None
        if area and area.intersection(page.bounds) != area:
            raise ValueError("选区必须是转正后图片内宽高均大于零的矩形。")
        draft.pages.append(page)
        apply_recognition(engine, draft, page, args, area)
    data = result_for(draft, "recognize", targets.get("project"), auto_figures=not args.no_figures)
    return publish_result(data, targets, draft)


def apply_recognition(engine, draft, page, args, area=None):
    result = recognize_page(engine, page.image, area, rotation=args.rotation,
                            mixed=args.mode == "mixed", handwriting=args.mode == "handwriting")
    rotate_page(draft, page, result.rotation, result.image)
    page.scan, page.quality = result.scan, result.quality
    if args.assume_printed and page.scan is not None:
        page.scan = replace(page.scan, lines=tuple(replace(line, kind=TextKind.PRINTED)
                                                  for line in page.scan.lines))


def read_project(path):
    if path.suffix.lower() != ".ocrproj":
        raise ValueError("请输入 .ocrproj 工作稿。")
    return read_draft(path)


def inspect(args):
    targets = output_destinations({"json": (args.json, ".json")}, (args.project,))
    draft = read_project(args.project)
    data = result_for(draft, "inspect", args.project.resolve(), auto_figures=not args.no_figures)
    if args.document_flow:
        pages = {page.id: page for page in draft.pages if page.scan is not None}
        sections = sections_for([page.export_sheet() for page in pages.values()], Extraction.ALL, not args.no_figures)
        data["document_flow"] = [flow_section(section, pages[section.sheet.source_id]) for section in sections]
    return publish_result(data, targets)


def flow_section(section, page):
    scan = section.sheet.scan
    ids = [line_id(page, i, line) for i, line in enumerate(scan.lines)]

    def source(bounds):
        return rectangle(bounds.translated(scan.area.left, scan.area.top))

    return {
        "page_id": page.id,
        "title": section.title,
        "anchor": section.anchor,
        "blocks": [{
            "role": block.role, "bounds": source(block.bounds), "value": block.value,
            "inlines": [{
                "state": part.state, "text": part.text, "latex": part.latex, "bounds": source(part.bounds),
                "source_text": part.source_text, "ocr_confidence": part.ocr_confidence,
                "line_ids": [ids[i] for i in part.line_indices],
            } for part in block.inlines],
        } for block in section.blocks],
    }


def retry(args):
    targets = output_destinations({"project": (args.output, ".ocrproj"), "json": (args.json, ".json")},
                                  (args.project,))
    draft = read_project(args.project)
    if args.unfinished:
        indices = pending_indices(draft)
    else:
        if not 1 <= args.page <= len(draft.pages):
            raise ValueError("--page 超出工作稿的页码范围。")
        indices = [args.page - 1]
    engine = LocalOcr()
    failed = []
    for number, index in enumerate(indices, 1):
        page = draft.pages[index]
        if args.progress:
            print(f"OCR {number}/{len(indices)} · {page.name}", file=sys.stderr)
        try:
            apply_recognition(engine, draft, page, args)
        except Exception as error:
            if not args.unfinished:
                raise
            page.quality = quality_result("error")
            failed.append({"page": index + 1, "error": str(error)})
    data = result_for(draft, "retry", targets["project"], auto_figures=not args.no_figures)
    if args.unfinished:
        data["retry_summary"] = {"attempted": len(indices), "failed": failed}
    return publish_result(data, targets, draft)


def valid_line_text(text):
    return isinstance(text, str) and 0 < len(text) <= MAX_EDIT_TEXT and bool(text.strip()) and all(
        character in "\t\n\r" or 0x20 <= ord(character) <= 0xD7FF or 0xE000 <= ord(character) <= 0xFFFD
        or 0x10000 <= ord(character) <= 0x10FFFF for character in text
    )


def change_lines(draft, edits_path):
    with edits_path.open("rb") as source:
        raw = source.read(MAX_EDITS_FILE + 1)
    if len(raw) > MAX_EDITS_FILE:
        raise ValueError("校对 JSON 过大。")
    data = json.loads(raw)
    if (not isinstance(data, dict) or type(data.get("schema_version")) is not int
            or data["schema_version"] != SCHEMA_VERSION or not isinstance(data.get("edits"), list)):
        raise ValueError("校对 JSON 需要 schema_version 为 1，并包含 edits 列表。")
    pending = {}
    for entry in data["edits"]:
        if (not isinstance(entry, dict) or not isinstance(entry.get("line_id"), str)
                or not entry.keys() <= {"line_id", "text", "kind"} or len(entry) < 2):
            raise ValueError("每条校对需要 line_id，以及 text 或 kind。")
        key = entry["line_id"]
        if key in pending:
            raise ValueError("校对 JSON 包含重复的 line_id。")
        if "text" in entry and not valid_line_text(entry["text"]):
            raise ValueError(f"校对文字需为 1 到 {MAX_EDIT_TEXT} 个字符的非空正文。")
        changes = {name: entry[name] for name in ("text", "kind") if name in entry}
        if "kind" in changes:
            changes["kind"] = TextKind(changes["kind"])
        pending[key] = changes
    updates = []
    count = 0
    for page in draft.pages:
        if not page.scan:
            continue
        original = page.scan.lines
        lines = list(original)
        for index, line in enumerate(original):
            changes = pending.pop(line_id(page, index, line), None)
            if changes:
                lines[index] = replace(line, **changes)
                count += lines[index] != line
        updates.append((page, tuple(lines)))
    if pending:
        raise ValueError("有 line_id 不在当前工作稿中或已过期，请重新获取识别结果后校对。")
    for page, lines in updates:
        if lines != page.scan.lines:
            page.scan = replace(page.scan, lines=lines)
    return count


def edit(args):
    targets = output_destinations({"project": (args.output, ".ocrproj"), "json": (args.json, ".json")},
                                  (args.project, args.edits))
    draft = read_project(args.project)
    count = change_lines(draft, args.edits)
    data = result_for(draft, "edit", targets["project"], auto_figures=not args.no_figures)
    data["changed_lines"] = count
    return publish_result(data, targets, draft)


def confirm_objects(args):
    targets = output_destinations({"project": (args.output, ".ocrproj"), "json": (args.json, ".json")},
                                  (args.project, args.objects))
    draft = read_project(args.project)
    with args.objects.open("rb") as source:
        raw = source.read(MAX_EDITS_FILE + 1)
    if len(raw) > MAX_EDITS_FILE:
        raise ValueError("校对 JSON 过大。")
    payload = json.loads(raw)
    if (not isinstance(payload, dict) or type(payload.get("schema_version")) is not int
            or payload["schema_version"] != SCHEMA_VERSION or not isinstance(payload.get("objects"), list)):
        raise ValueError("确认 JSON 需要 schema_version 为 1，并包含 objects 列表。")
    pages = {page.id: page for page in draft.pages}
    pending = []
    known = {(page.id, block.bounds) for page in draft.pages for block in page.blocks}
    for item in payload["objects"]:
        if not isinstance(item, dict) or set(item) != {"page_id", "bounds", "format", "kind", "value"}:
            raise ValueError("每个对象需要 page_id、bounds、format、kind 和 value。")
        page_id, bounds = item["page_id"], item["bounds"]
        page = pages.get(page_id) if isinstance(page_id, str) else None
        if (not page or not page.scan or not isinstance(bounds, list) or len(bounds) != 4
                or any(type(coord) is not int for coord in bounds) or not valid_line_text(item["value"])):
            raise ValueError("对象需要已识别页面、四个整数坐标和可用正文。")
        area = Rect(*bounds)
        if area.intersection(page.scan.area) != area or (page.id, area) in known:
            raise ValueError("对象区域越界，或工作稿中已有相同位置的对象。")
        if not isinstance(item["kind"], str) or not isinstance(item["format"], str):
            raise ValueError("kind 和 format 必须为字符串。")
        block = StructuredBlock(area, TextKind(item["kind"]), BlockFormat(item["format"]), item["value"])
        validate_confirmation(block)
        known.add((page.id, area))
        pending.append((page, block))
    for page, block in pending:
        page.blocks += (block,)
    data = result_for(draft, "confirm-objects", targets["project"], auto_figures=not args.no_figures)
    data["confirmed_objects"] = len(pending)
    return publish_result(data, targets, draft)


def export(args):
    targets = output_destinations({"docx": (args.docx, ".docx"), "pdf": (args.pdf, ".pdf")}, (args.project,))
    if not targets:
        raise ValueError("至少提供 --docx 或 --pdf 中的一项。")
    draft = read_project(args.project)
    if args.scope == "collection":
        sheets = draft.selection_sheets()
    else:
        sheets = tuple(page.export_sheet() for page in draft.pages if page.scan is not None)
    if args.layout != "handout" and (args.cover or args.contents or args.title):
        raise ValueError("--cover、--contents 和 --title 需要 --layout handout。")
    if args.pdf_style == "photo" and args.pdf_engine == "word":
        raise ValueError("照片 PDF 使用内置导出。")
    if args.pdf_dpi is not None and (args.pdf_style != "photo" or not args.pdf
                                     or not 72 <= args.pdf_dpi <= 600):
        raise ValueError("--pdf-dpi 需要照片 PDF，且范围为 72–600。")
    title = args.title or "学习讲义"
    options = HandoutOptions(title, args.cover, args.contents) if args.layout == "handout" else None
    written, counts, checks, audits = [], {}, {}, {}
    extraction = EXTRACTIONS[args.text_kind]
    for key, target in targets.items():
        spec = DocumentExport(target.name.lower(), sheets, extraction,
                              args.pdf_style == "photo", not args.no_figures,
                              options, args.pdf_engine, args.pdf_dpi).with_content()
        if not spec.sheets:
            raise ValueError(f"当前范围没有可导出的 {key.upper()} 内容。")
        def write(path, key=key, spec=spec):
            report = spec.write(path)
            if report is not None:
                checks[key] = report.data()
            if args.asset_audit:
                audits[key] = (report.asset_audit if report and report.asset_audit is not None
                               else document_assets(spec))

        written.append((target, write))
        counts[key] = len(spec.sheets)
    publish_files(written)
    data = {
        "schema_version": SCHEMA_VERSION, "command": "export", "project": str(args.project.resolve()),
        "files": {key: str(path) for key, path in targets.items()},
        "sheets": counts,
        "layout_checks": checks,
        "text_counts": category_counts((line for sheet in sheets for line in sheet.scan.lines), extraction),
    }
    if args.asset_audit:
        data["asset_audit"] = audits
    return data


def figures(args):
    draft = read_project(args.project)
    data = result_for(draft, "figures", args.project.resolve(), auto_figures=not args.no_figures)
    if not args.output_dir.is_dir():
        raise FileNotFoundError(f"输出目录不存在：{args.output_dir}")
    written = []
    for number, (page, metadata) in enumerate(zip(draft.pages, data["pages"]), 1):
        for index, asset in enumerate(metadata["figure_assets"], 1):
            path = args.output_dir / f"page-{number:03d}-figure-{index:03d}.png"
            target = output_destinations({"figure": (path, ".png")}, (args.project,))["figure"]
            crop = Rect(*asset["bounds"])
            written.append((target, lambda dest, image=page.image, bounds=crop:
                            bounds.crop(image).save(dest, format="PNG")))
            asset["file"] = str(target)
    publish_files(written)
    data["asset_count"] = len(written)
    return data


def build_parser():
    parser = ArgumentParser(description="本地 OCR 与文档交接工具")
    commands = parser.add_subparsers(dest="action", required=True)
    scan = commands.add_parser("recognize", help="识别图片并输出 JSON，可另存工作稿")
    scan.add_argument("images", type=Path, nargs="+")
    scan.add_argument("--region", type=int, nargs=4, metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"))
    scan.add_argument("--project", type=Path)
    retry_page = commands.add_parser("retry", help="指定方向重试工作稿中的一页")
    retry_page.add_argument("project", type=Path)
    target_pages = retry_page.add_mutually_exclusive_group(required=True)
    target_pages.add_argument("--page", type=int)
    target_pages.add_argument("--unfinished", action="store_true", help="只重试未识别页及失败页")
    retry_page.add_argument("--output", required=True, type=Path)
    for tool, default in ((scan, "keep"), (retry_page, "auto")):
        tool.add_argument("--mode", choices=("mixed", "general", "handwriting"), default="mixed")
        tool.add_argument("--rotation", choices=ROTATIONS, default=default,
                          help="相对当前图像的方向。数字表示顺时针角度")
        tool.add_argument("--assume-printed", action="store_true", help="明确将本次识别结果全部标为印刷体")
        tool.add_argument("--progress", action="store_true", help="进度单独写入标准错误")
    read = commands.add_parser("inspect", help="从已有工作稿提取 JSON")
    read.add_argument("project", type=Path)
    read.add_argument("--document-flow", action="store_true", help="附加 A4 讲义文档流和行内公式")
    change = commands.add_parser("edit", help="按 line_id 校对文字或类别，另存工作稿")
    change.add_argument("project", type=Path)
    change.add_argument("--edits", type=Path, required=True)
    change.add_argument("--output", type=Path, required=True)
    docs = commands.add_parser("export", help="从已有工作稿导出 Word 或 PDF")
    docs.add_argument("project", type=Path)
    docs.add_argument("--docx", type=Path)
    docs.add_argument("--pdf", type=Path)
    docs.add_argument("--text-kind", choices=EXTRACTIONS, default="all")
    docs.add_argument("--pdf-style", choices=("clean", "photo"), default="clean")
    docs.add_argument("--layout", choices=("original", "handout"), default="original")
    docs.add_argument("--pdf-engine", choices=("native", "word"), default="native")
    docs.add_argument("--pdf-dpi", type=int, help="照片 PDF 分享版目标 DPI，默认保真")
    docs.add_argument("--title")
    docs.add_argument("--cover", action="store_true")
    docs.add_argument("--contents", action="store_true")
    docs.add_argument("--scope", choices=("pages", "collection"), default="pages")
    docs.add_argument("--asset-audit", action="store_true", help="附加实际导出的裁切与表格替换清单")
    confirm = commands.add_parser("confirm-objects", help="保存人工核对过的原生公式和表格")
    confirm.add_argument("project", type=Path)
    confirm.add_argument("--objects", type=Path, required=True)
    confirm.add_argument("--output", type=Path, required=True)
    assets = commands.add_parser("figures", help="将保留的图表或公式截图导出为 PNG")
    assets.add_argument("project", type=Path)
    assets.add_argument("--output-dir", type=Path, required=True)
    for tool, run in ((scan, recognize), (retry_page, retry), (read, inspect), (change, edit),
                      (docs, export), (assets, figures), (confirm, confirm_objects)):
        tool.add_argument("--no-figures", action="store_true", help="跳过图表的自动检测")
        tool.set_defaults(run=run)
        if tool not in (docs, assets):
            tool.add_argument("--json", type=Path, help="同时将 JSON 保存到指定文件")
    return parser


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = build_parser().parse_args(argv)
    try:
        with redirect_stdout(sys.stderr):
            result = args.run(args)
            output = json_text(result)
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        print(json_text({"schema_version": SCHEMA_VERSION, "error": str(error)}).rstrip(), file=sys.stderr)
        return 1
    except Exception as error:
        print(json_text({"schema_version": SCHEMA_VERSION, "error": f"OCR 或文档处理失败：{error}"}).rstrip(),
              file=sys.stderr)
        return 1
    sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
