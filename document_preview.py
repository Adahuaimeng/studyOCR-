"""Build local previews and editable snapshots with the same settings as export."""

from dataclasses import dataclass, replace
import os
from pathlib import Path
from shutil import copyfile, which
import subprocess
from tempfile import TemporaryDirectory

from export_audit import document_assets
from exports import document_items, export_path, save_docx_sheets, save_pdf_sheets
from handout import HandoutOptions, sections_for
from handout_render import save_handout_pdf, save_handout_word
from layout_check import LayoutReport, check_captions, check_fonts, check_formula_reviews, check_margin_omissions, check_word
from ocr_core import TextKind
from study import ExportSheet, Extraction


@dataclass(frozen=True)
class DocumentExport:
    filename: str
    sheets: tuple[ExportSheet, ...]
    mode: Extraction
    original: bool
    auto_figures: bool
    handout: HandoutOptions | None = None
    pdf_engine: str = "native"
    photo_dpi: int | None = None
    filter_summary: tuple[int, int, tuple[str, ...]] | None = None

    @property
    def suffix(self):
        return Path(self.filename).suffix

    def with_content(self):
        if self.suffix == ".pdf" and self.original:
            return self
        def has_content(sheet):
            review = [] if self.handout else None
            items = document_items(sheet, sheet.scan.lines, self.mode, auto_figures=self.auto_figures,
                                   math_review=review)
            return any(items) or bool(review)

        included = tuple(sheet for sheet in self.sheets if has_content(sheet))
        return replace(self, sheets=included, filter_summary=self.category_summary(included))

    def category_summary(self, included):
        if self.filter_summary is not None:
            return self.filter_summary
        if self.mode == Extraction.ALL or (self.suffix == ".pdf" and self.original):
            return None
        kept = excluded = 0
        dropped = []
        visible = {id(sheet) for sheet in included}
        for sheet in self.sheets:
            eligible = [line for line in sheet.scan.lines if line.kind != TextKind.OMIT]
            count = sum(self.mode.includes(line.kind) for line in eligible)
            kept += count
            excluded += len(eligible) - count
            if eligible and count == 0 and id(sheet) not in visible:
                dropped.append(sheet.source_name or "未命名图片")
        return kept, excluded, tuple(dropped)

    def category_report(self):
        summary = self.category_summary(self.sheets)
        if not summary:
            return None
        kept, excluded, dropped = summary
        total = kept + excluded
        if not dropped and (not excluded or excluded * 4 < total):
            return None
        report = LayoutReport(checks=["提取类别与文字覆盖"])
        message = f"按类别纳入 {kept} 行，排除 {excluded} 行（{excluded / total:.0%}）。"
        if dropped:
            message += f"{len(dropped)} 张来源没有纳入文字（{'、'.join(dropped[:6])}）。"
        report.add("content_filtered", message)
        report.issues[-1].update(included_lines=kept, excluded_lines=excluded, excluded_sources=list(dropped))
        return report

    def write(self, path, *, inspect_pages=True):
        if self.suffix == ".pdf" and self.original:
            save_pdf_sheets(self.sheets, path, self.mode, original=True, auto_figures=self.auto_figures,
                            photo_dpi=self.photo_dpi)
            return None
        report = self.category_report()
        if self.handout:
            sections = sections_for(self.sheets, self.mode, self.auto_figures)
            if not sections:
                raise ValueError("当前范围没有可生成讲义的内容。")
            report = report or LayoutReport()
            report.asset_audit = document_assets(self, sections)
            report.source_headings = tuple((section.title, section.sheet) for section in sections)
            check_formula_reviews(report, sections)
            check_margin_omissions(report, sections)
            if self.suffix == ".docx":
                save_handout_word(sections, self.handout, path)
                check_word(report, path, sections)
            elif self.pdf_engine == "word":
                self.convert_to_pdf(path, sections)
                report.pending_pdf = (path,)
            else:
                check_fonts(report, sections, self.handout)
                sources, captions = save_handout_pdf(sections, self.handout, path)
                report.pending_pdf = (path, sources)
                check_captions(report, captions)
            if inspect_pages:
                report.inspect_pages()
            return report
        if self.suffix == ".docx":
            save_docx_sheets(self.sheets, path, self.mode, auto_figures=self.auto_figures)
        elif self.pdf_engine == "word":
            self.convert_to_pdf(path)
        else:
            save_pdf_sheets(self.sheets, path, self.mode, original=False, auto_figures=self.auto_figures)
        return report

    def convert_to_pdf(self, path, sections=None):
        office = find_soffice()
        if office is None:
            raise ValueError("Word 同版 PDF 需要 LibreOffice，可切换为白底黑字 PDF。")
        with TemporaryDirectory(prefix="study-ocr-word-pdf-", ignore_cleanup_errors=True) as folder:
            source = Path(folder) / "document.docx"
            if sections is not None:
                save_handout_word(sections, self.handout, source)
            else:
                save_docx_sheets(self.sheets, source, self.mode, auto_figures=self.auto_figures)
            rendered = render_word(source, office)
            with export_path(path) as temporary:
                copyfile(rendered, temporary)


@dataclass
class GeneratedDocument:
    folder: TemporaryDirectory
    source: Path
    filename: str
    preview_path: Path | None = None
    report: LayoutReport | None = None
    preview_is_handout_pdf: bool = False

    def close(self):
        self.folder.cleanup()


def find_soffice():
    configured = os.environ.get("OCR_SOFFICE")
    locations = [configured, which("soffice.com"), which("soffice"), which("libreoffice")]
    if os.name == "nt":
        import winreg

        key_name = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\soffice.exe"
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, key_name) as key:
                    value, _ = winreg.QueryValueEx(key, "")
                executable = Path(value.strip('"'))
                locations.extend((executable.with_suffix(".com"), executable))
            except OSError:
                continue
        for name in ("ProgramFiles", "ProgramFiles(x86)"):
            if name in os.environ:
                locations.append(Path(os.environ[name]) / "LibreOffice" / "program" / "soffice.com")
    return next((Path(location).resolve() for location in locations if location and Path(location).is_file()), None)


def render_word(source, office):
    output = source.with_suffix(".pdf")
    command = [
        str(office), f"-env:UserInstallation={(source.parent / 'lo-profile').as_uri()}",
        "--headless", "--convert-to", "pdf:writer_pdf_Export", "--outdir", str(source.parent), str(source),
    ]
    try:
        result = subprocess.run(command, capture_output=True, timeout=60,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as error:
        raise ValueError("Word 排版预览超时，请缩小导出范围后重试。") from error
    if result.returncode or not output.is_file() or not output.stat().st_size:
        raise ValueError("LibreOffice 未能生成 Word 排版预览。可检查本机的 LibreOffice 后重试。")
    return output


def prepare_document(spec, *, preview):
    spec = spec.with_content()
    if not spec.sheets:
        raise ValueError("当前范围没有可导出的内容。可切换提取类别，或在原图校对中确认文字类别。")
    office = None
    if preview and spec.suffix == ".docx":
        office = find_soffice()
        if office is None and not spec.handout:
            raise ValueError("Word 排版预览需要 LibreOffice。PDF 预览、Word 导出和编辑仍可使用。")
    folder = TemporaryDirectory(prefix="study-ocr-document-", ignore_cleanup_errors=True)
    source = Path(folder.name) / ("document" + spec.suffix)
    prepared = GeneratedDocument(folder, source, spec.filename)
    try:
        prepared.report = spec.write(source, inspect_pages=False)
        if preview:
            if office:
                prepared.preview_path = render_word(source, office)
                if prepared.report is not None and spec.handout:
                    prepared.report.pending_pdf = (prepared.preview_path,)
            elif spec.suffix == ".docx":
                prepared.preview_path = source.with_suffix(".pdf")
                preview_spec = replace(spec, filename=prepared.preview_path.name, original=False, pdf_engine="native")
                preview_spec.write(prepared.preview_path, inspect_pages=False)
                prepared.preview_is_handout_pdf = True
            else:
                prepared.preview_path = source
        return prepared
    except Exception:
        prepared.close()
        raise
