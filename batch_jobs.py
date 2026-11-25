"""Sequential OCR batches with a lossless, incremental local recovery journal."""

from pathlib import Path
import json
import re
from tempfile import mkdtemp

from exports import export_path
from ocr_quality import quality_result, recognize_page
from study import read_draft, restore_scan, rotate_page, scan_data, write_draft


IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp", ".heic", ".heif"})


def natural_key(value):
    return tuple((1, int(part)) if part.isdecimal() else (0, part.casefold())
                 for part in re.split(r"(\d+)", value))


def folder_images(folder):
    return sorted((p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS),
                  key=lambda path: (natural_key(path.name), path.name))


def pending_indices(draft, *, failed_only=False):
    failed = lambda page: bool(page.quality and page.quality["status"] == "error")
    return [i for i, page in enumerate(draft.pages) if failed(page) or (not failed_only and page.scan is None)]


class BatchJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.base = self.path / "workspace.ocrproj"

    @classmethod
    def begin(cls, directory, draft):
        directory.mkdir(parents=True, exist_ok=True)
        journal = cls(mkdtemp(prefix="batch-", dir=directory))
        with export_path(journal.base) as temporary:
            write_draft(draft, temporary)
        return journal

    @classmethod
    def recent(cls, directory):
        found = [cls(path.parent) for path in directory.glob("batch-*/workspace.ocrproj")]
        return sorted(found, key=lambda job: job.path.stat().st_mtime_ns, reverse=True)

    def record(self, index, page, attempt):
        data = {"id": page.id, "rotation": attempt.rotation if attempt else 0,
                "quality": attempt.quality if attempt else quality_result("error")}
        if attempt:
            data["scan"] = scan_data(attempt.scan)
        target = self.path / f"page-{index:04d}.json"
        with export_path(target) as temporary:
            temporary.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False), encoding="utf-8")

    def recover(self):
        draft = read_draft(self.base)
        for file in sorted(self.path.glob("page-*.json")):
            index = int(file.stem.split("-")[1])
            data = json.loads(file.read_text(encoding="utf-8"))
            if not 0 <= index < len(draft.pages) or draft.pages[index].id != data["id"]:
                raise ValueError("恢复记录与工作稿页面不匹配。")
            page = draft.pages[index]
            if "scan" in data:
                rotate_page(draft, page, data["rotation"])
                page.scan = restore_scan(page.image, data["scan"])
            page.quality = data["quality"]
        return draft

    def discard(self):
        for path in self.path.glob("page-*.json"):
            path.unlink(missing_ok=True)
        self.base.unlink(missing_ok=True)
        self.path.rmdir()


def run_batch(draft, indices, engine, directory, cancel, report, *, mixed, handwriting):
    journal = BatchJournal.begin(directory, draft)
    report(("journal", journal))
    for count, index in enumerate(indices, 1):
        if cancel.is_set():
            break
        page = draft.pages[index]
        report(("progress", count, len(indices), page.name))
        try:
            result = recognize_page(engine, page.image, rotation="auto", mixed=mixed, handwriting=handwriting)
        except Exception as error:
            journal.record(index, page, None)
            report(("page", index, None, str(error)))
        else:
            journal.record(index, page, result)
            report(("page", index, result, ""))
