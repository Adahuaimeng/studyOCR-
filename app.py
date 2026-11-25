from dataclasses import dataclass, field, replace
from datetime import datetime
import json
from math import ceil, floor
from pathlib import Path
from queue import Empty, Queue
from shutil import copyfile
from threading import Event, Thread
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

from PIL import Image, ImageTk

from ai_ui import Assistant
from app_paths import ASSETS, default_file_directory
from batch_jobs import BatchJournal, folder_images, pending_indices, run_batch
from document_preview import DocumentExport, prepare_document
from export_page_ui import ExportPagesDialog
from exports import covers, export_path, image_regions, local_blocks, page_layout, sheet_caption_links
from handout import HandoutOptions, join_lines
from page_structure import ROLE_NAMES, enclosing_bounds, text_blocks
from ocr_core import LocalOcr, Rect, Scan, TextKind, TextLine, open_image
from ocr_quality import ScanAttempt, quality_result, recognize_page, rotated_bounds
from reconstruction import reconstruction_queue, selection_proposal, validate_confirmation
from structured import structured_text
from table_review_ui import TableReviewDialog
from study import (
    BlockFormat, Extraction, StructuredBlock, StudyDraft, StudyPage,
    collect_selection, image_key, read_draft, rotate_page, write_draft,
)
from workspace_ui import (Command, ShortcutBar, ShortcutDialog, load_preferences, load_shortcuts,
                          save_settings, save_shortcuts, settings_path)


class ImagePreview(ttk.Frame):
    def __init__(self, parent, selected, *, selectable=True):
        super().__init__(parent)
        self.selected = selected
        self.image = None
        self.photo = None
        self.selection = None
        self.regions = ()
        self.structured = ()
        self.detected = ()
        self.review = None
        self.anchor = None
        self.scale = (1, 1)
        self.zoom = 1
        self.offset = (0, 0)

        self.canvas = tk.Canvas(self, background="#e6ebf0", highlightthickness=0)
        vertical = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        horizontal = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(xscrollcommand=horizontal.set, yscrollcommand=vertical.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.canvas.bind("<Configure>", lambda event: self.draw())
        if selectable:
            self.canvas.bind("<ButtonPress-1>", self.start_drag)
            self.canvas.bind("<B1-Motion>", self.drag)
            self.canvas.bind("<ButtonRelease-1>", self.finish_drag)
            self.canvas.bind("<Motion>", self.update_cursor)
        self.canvas.bind("<MouseWheel>", self.scroll)

    def show_image(self, image):
        self.image = image
        self.zoom = 1
        self.selection = None
        self.regions = ()
        self.structured = ()
        self.detected = ()
        self.review = None
        self.anchor = None
        self.canvas.configure(cursor="")
        self.draw()
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)

    def set_zoom(self, zoom):
        self.zoom = zoom
        self.draw()

    def draw(self):
        self.canvas.delete("all")
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        if self.image is None:
            self.canvas.create_text(
                width / 2, height / 2, text="打开一张图片开始识别", fill="#526276",
                font=("Microsoft YaHei UI", 12),
            )
            return

        fit = min(max(1, width - 24) / self.image.width,
                  max(1, height - 24) / self.image.height)
        display_width = max(1, round(self.image.width * fit * self.zoom))
        display_height = max(1, round(self.image.height * fit * self.zoom))
        self.scale = (display_width / self.image.width, display_height / self.image.height)
        x, y = max(12, (width - display_width) // 2), max(12, (height - display_height) // 2)
        self.offset = (x, y)
        self.photo = ImageTk.PhotoImage(
            self.image.resize((display_width, display_height), Image.Resampling.LANCZOS)
        )
        self.canvas.create_image(x, y, anchor="nw", image=self.photo)
        self.canvas.configure(scrollregion=(
            0, 0, max(width, x + display_width + 12), max(height, y + display_height + 12),
        ))
        self.draw_boxes()

    def draw_boxes(self):
        self.canvas.delete("box")
        x, y = self.offset
        sx, sy = self.scale
        for region in self.detected:
            self.canvas.create_rectangle(
                x + region.left * sx, y + region.top * sy,
                x + region.right * sx, y + region.bottom * sy,
                outline="#087f83", width=2, dash=(5, 3), tags="box",
            )
        for region in self.structured:
            self.canvas.create_rectangle(
                x + region.left * sx, y + region.top * sy,
                x + region.right * sx, y + region.bottom * sy,
                outline="#7857ba", width=2, tags="box",
            )
        if self.review:
            region = self.review
            self.canvas.create_rectangle(
                x + region.left * sx, y + region.top * sy,
                x + region.right * sx, y + region.bottom * sy,
                outline="#c37324", width=3, tags="box",
            )
        for region in self.regions:
            self.canvas.create_rectangle(
                x + region.left * sx, y + region.top * sy,
                x + region.right * sx, y + region.bottom * sy,
                outline="#168659", width=2, tags="box",
            )
        if self.selection:
            region = self.selection
            self.canvas.create_rectangle(
                x + region.left * sx, y + region.top * sy,
                x + region.right * sx, y + region.bottom * sy,
                outline="#186bcd", width=2, dash=(7, 4), tags="box",
            )

    def image_point(self, event):
        x, y = self.offset
        sx, sy = self.scale
        return (self.canvas.canvasx(event.x) - x) / sx, (self.canvas.canvasy(event.y) - y) / sy

    def update_cursor(self, event):
        cursor = ""
        if self.image is not None:
            x, y = self.image_point(event)
            if 0 <= x <= self.image.width and 0 <= y <= self.image.height:
                cursor = "crosshair"
        self.canvas.configure(cursor=cursor)

    def start_drag(self, event):
        if self.image is None:
            return
        x, y = self.image_point(event)
        if 0 <= x <= self.image.width and 0 <= y <= self.image.height:
            self.previous_selection = self.selection
            self.anchor = (x, y)

    def drag(self, event):
        if self.anchor is None:
            return
        x, y = self.image_point(event)
        ax, ay = self.anchor
        self.selection = Rect(
            max(0, floor(min(ax, x))), max(0, floor(min(ay, y))),
            min(self.image.width, ceil(max(ax, x))),
            min(self.image.height, ceil(max(ay, y))),
        )
        self.draw_boxes()

    def finish_drag(self, event):
        if self.anchor is None:
            return
        self.drag(event)
        self.anchor = None
        if self.selection.width < 6 or self.selection.height < 6:
            self.selection = self.previous_selection
        self.draw_boxes()
        self.selected(self.selection)

    def scroll(self, event):
        if event.state & 1:
            self.canvas.xview_scroll(-int(event.delta / 120), "units")
        else:
            self.canvas.yview_scroll(-int(event.delta / 120), "units")



class ReviewDialog(simpledialog.Dialog):
    def __init__(self, parent, title, fields, validator=None, *, source_image=None, hint=""):
        self.fields = fields
        self.validator = validator
        self.source_image = source_image
        self.hint = hint
        self.variables = {}
        self.inputs = {}
        super().__init__(parent, title)

    def body(self, parent):
        parent.columnconfigure(1, weight=1)
        first = None
        row = 0
        if self.source_image is not None:
            self.source_image.thumbnail((530, 220))
            self.source_photo = ImageTk.PhotoImage(self.source_image, master=self)
            ttk.Label(parent, image=self.source_photo).grid(row=row, columnspan=2, pady=4)
            row += 1
        if self.hint:
            ttk.Label(parent, text=self.hint, wraplength=520).grid(row=row, columnspan=2, pady=4, sticky="w")
            row += 1
        for index, (key, label, value, choices) in enumerate(self.fields, row):
            ttk.Label(parent, text=label).grid(row=index, column=0, padx=(0, 10),
                                                 pady=4, sticky="nw")
            if choices:
                variable = tk.StringVar(value=value)
                widget = ttk.Combobox(parent, textvariable=variable, values=choices, state="readonly")
                self.variables[key] = variable
            elif key == "body":
                widget = tk.Text(parent, width=57, height=9, wrap="word")
                widget.insert("1.0", value)
            else:
                variable = tk.StringVar(value=value)
                widget = ttk.Entry(parent, textvariable=variable, width=48)
                self.variables[key] = variable
            widget.grid(row=index, column=1, sticky="ew", pady=4)
            self.inputs[key] = widget
            if first is None:
                first = widget
        return first

    def buttonbox(self):
        controls = ttk.Frame(self, padding=(10, 4, 10, 8))
        ttk.Button(controls, text="确定", command=self.ok).pack(side="left", padx=4)
        ttk.Button(controls, text="取消", command=self.cancel).pack(side="left", padx=4)
        controls.pack()
        self.bind("<Escape>", self.cancel)

    def values(self):
        values = {key: var.get() for key, var in self.variables.items()}
        if "body" in self.inputs:
            values["body"] = self.inputs["body"].get("1.0", "end-1c")
        return values

    def validate(self):
        if self.validator is None:
            return True
        try:
            self.validator(self.values())
        except ValueError as error:
            messagebox.showerror("内容格式有误", str(error), parent=self)
            return False
        return True

    def apply(self):
        self.result = self.values()


KINDS = {kind.label: kind for kind in TextKind}
IMAGE_FILETYPES = [("图片", "*.jpg *.jpeg *.png *.bmp *.tif *.tiff *.webp *.heic *.heif"), ("所有文件", "*.*")]
DRAFT_FILETYPES = [("OCR 工作稿", "*.ocrproj")]
CURRENT, COLLECTION, ALL_PAGES = "当前识别范围", "错题集", "全部已识别页"
CUSTOM_PAGES = "自选页面"
CLEAN_PDF, ORIGINAL_PDF, WORD_PDF = "白底黑字", "照片版", "Word 同版 PDF"
ORIGINAL_LAYOUT, HANDOUT_LAYOUT = "贴近原图", "A4 讲义"
PHOTO_FULL, PHOTO_SHARE = "原分辨率保真", "150 DPI 分享版"
REVIEW_ALL, REVIEW_PENDING, REVIEW_STRUCTURE = "全部结果", "低置信与待确认", "待确认结构"
REVIEW_TEXT, REVIEW_FORMULAS, REVIEW_TABLES = "文字低置信", "待核对公式", "待核对表格"
REVIEW_CAPTIONS, REVIEW_LAYOUT = "未关联图注", "版面异常"
MODES = ("混排识别", "通用识别", "手写增强")
ZOOM_LEVELS = {"适应窗口": 1, "放大 1.5 倍": 1.5, "放大 2 倍": 2}
DEFAULT_SHORTCUTS = (
    "open_button", "phone_button", "save_draft_button", "full_button", "crop_button",
    "word_preview_button", "pdf_preview_button", "word_button", "pdf_button",
)


@dataclass
class BatchProgress:
    organize: bool
    total: int
    events: Queue = field(default_factory=Queue)
    cancel: Event = field(default_factory=Event)
    done: int = 0
    errors: list[str] = field(default_factory=list)


class Application:
    def __init__(self, root):
        self.root = root
        self.root.title("图片识别与错题整理")
        icon = ASSETS / "StudyOCR.ico"
        if icon.is_file():
            self.root.iconbitmap(default=str(icon))
        width = min(1300, max(1050, root.winfo_screenwidth() - 36))
        height = min(870, max(650, root.winfo_screenheight() - 92))
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(1050, 650)
        self.root.option_add("*Font", ("Microsoft YaHei UI", 10))
        self.engine = LocalOcr()
        self.inbox = Queue()
        self.phone_inbox = Queue(maxsize=2)
        self.camera_window = None
        self.phone_window = None
        self.phone_page = None
        self.capture_count = 0
        self.draft = StudyDraft()
        self.draft_path = None
        self.source_paths = set()
        self.image_keys = set()
        self.review_history = {}
        self.batch = None
        self.batch_journal = None
        self.editors = []
        self.embedded_editors = {}
        self.document_previews = {}
        self.document_job = None
        self.last_layout_report = None
        self.agent_help_window = None
        self.menu_controls = []
        self.dirty = False
        self.busy = False
        self.selection = None
        self.status = tk.StringVar(value="导入图片或打开工作稿")
        self.scope = tk.StringVar(value="识别结果")
        self.selection_label = tk.StringVar(value="导入图片后，可以在预览上框选区域")
        self.zoom = tk.StringVar(value="适应窗口")
        self.mode = tk.StringVar(value="混排识别")
        self.extraction = tk.StringVar(value=Extraction.ALL)
        self.target = tk.StringVar(value=CURRENT)
        self.pdf_style = tk.StringVar(value=CLEAN_PDF)
        self.photo_quality = tk.StringVar(value=PHOTO_FULL)
        self.review_filter = tk.StringVar(value=REVIEW_ALL)
        self.export_layout = tk.StringVar(value=ORIGINAL_LAYOUT)
        self.handout_cover = tk.BooleanVar(value=False)
        self.handout_contents = tk.BooleanVar(value=False)
        self.auto_figures = tk.BooleanVar(value=True)
        self.tools_visible = tk.BooleanVar(value=False)
        self.collection_title = tk.StringVar(value="错题集")
        self.shortcut_path = settings_path()
        self.recovery_directory = self.shortcut_path.parent / "recovery"
        self.preferences_path = self.shortcut_path.with_name("export.json")
        self.restore_preferences()
        self.ai = Assistant(self, self.shortcut_path.with_name("ai.json"))
        self.create_commands()
        self.shortcuts = load_shortcuts(self.shortcut_path, self.commands, DEFAULT_SHORTCUTS)
        self.build()
        self.update_buttons()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.phone_poll = self.root.after(180, self.collect_phone_photos)

    @property
    def page(self):
        if self.draft.pages:
            return self.draft.pages[self.draft.active_index]
        return None

    @property
    def image(self):
        return self.page.image if self.page else None

    @property
    def scan(self):
        return self.page.scan if self.page else None

    @property
    def regions(self):
        return self.page.regions if self.page else ()

    def combo(self, parent, var, values, width, callback=None):
        field = ttk.Combobox(parent, textvariable=var, values=values, width=width, state="readonly")
        if callback:
            field.bind("<<ComboboxSelected>>", lambda event: callback())
        field.pack(side="left", padx=(0, 6))
        return field

    def create_commands(self):
        self.commands = {}
        for name, label, run in (
            ("open_button", "导入图片", self.choose_images),
            ("folder_button", "导入图片文件夹…", self.choose_folder),
            ("batch_button", "继续批量识别", self.start_batch),
            ("failed_button", "重试失败页", lambda: self.start_batch(failed_only=True)),
            ("cancel_batch_button", "停止批量识别", self.stop_batch),
            ("recover_batch_button", "恢复上次批量任务…", self.recover_batch),
            ("organize_button", "一键整理为 A4 讲义", lambda: self.start_batch(organize=True)),
            ("choose_export_pages", "自选导出页面…", self.choose_export_pages_dialog),
            ("set_handout_title", "设置本页讲义标题…", self.set_page_handout_title),
            ("camera_button", "摄像头拍照", self.open_camera),
            ("phone_button", "手机拍照", self.open_phone),
            ("open_draft_button", "打开工作稿", self.open_draft),
            ("save_draft_button", "保存工作稿", self.save_draft),
            ("full_button", "识别整图", self.start_scan),
            ("crop_button", "识别选区", lambda: self.start_scan(self.selection)),
            ("retry_button", "重试此页…", self.retry_page),
            ("select_button", "框选区域", self.guide_selection),
            ("clear_button", "清除框选", lambda: self.set_selection(None)),
            ("save_button", "另存截图", self.save_crop),
            ("keep_button", "保留原图区域", self.keep_crop),
            ("remove_button", "清除保留", self.clear_regions),
            ("exclude_figure", "排除框中的自动图表", self.exclude_detected_figures),
            ("restore_figures", "恢复本页自动图表", self.restore_detected_figures),
            ("turn_left", "左转 90°", lambda: self.rotate(True)),
            ("turn_right", "右转 90°", lambda: self.rotate(False)),
            ("manual_button", "框选补录", self.add_manual),
            ("native_button", "表格或公式", self.add_native),
            ("table_grid_button", "逐格编辑选区表格…", lambda: self.add_native(force_table=True)),
            ("collect_button", "收录选区", lambda: self.add_selection(False)),
            ("picture_button", "仅收录原图", lambda: self.add_selection(True)),
            ("previous_page", "上一张", lambda: self.move_page(-1)),
            ("next_page", "下一张", lambda: self.move_page(1)),
            ("edit_button", "校对", self.edit_selected),
            ("label_button", "批量改类别", self.change_labels),
            ("assume_printed", "将本页文字标为印刷体", self.assume_page_printed),
            ("link_caption", "将所选文字关联到框内图表", self.link_figure_caption),
            ("unlink_caption", "清除所选文字的图注关联", self.unlink_figure_caption),
            ("split_button", "拆分文字块", self.split_selected),
            ("merge_button", "合并所选文字块…", self.merge_selected),
            ("role_button", "修改文字角色…", self.change_roles),
            ("reading_up", "阅读顺序上移", lambda: self.move_text_block(-1)),
            ("reading_down", "阅读顺序下移", lambda: self.move_text_block(1)),
            ("undo_review_button", "撤销本页校对", self.undo_review),
            ("next_review_button", "下一处待复核", self.next_review),
            ("delete_block", "移除对象", self.remove_block),
            ("show_collection", "错题集", lambda: self.result_tabs.select(self.collection_panel)),
            ("clip_jump", "定位选题原图", self.jump_to_selection),
            ("clip_up", "选题上移", lambda: self.move_clip(-1)),
            ("clip_down", "选题下移", lambda: self.move_clip(1)),
            ("clip_remove", "移除选题", self.remove_clip),
            ("word_button", "导出 Word", lambda: self.export(".docx")),
            ("pdf_button", "导出 PDF", lambda: self.export(".pdf")),
            ("word_preview_button", "Word 预览", lambda: self.generate_document(".docx", preview=True)),
            ("pdf_preview_button", "PDF 预览", lambda: self.generate_document(".pdf", preview=True)),
            ("word_edit_split", "当前 Word · 并排编辑", lambda: self.generate_document(".docx", embedded=True)),
            ("word_edit_full", "当前 Word · 全屏编辑", lambda: self.generate_document(".docx")),
            ("pdf_edit_split", "当前 PDF · 并排编辑", lambda: self.generate_document(".pdf", embedded=True)),
            ("pdf_edit_full", "当前 PDF · 全屏编辑", lambda: self.generate_document(".pdf")),
            ("ai_request", "AI 校对或结构建议…", self.ai.request),
            ("ai_settings", "AI 联网辅助设置…", self.ai.settings),
            ("ai_undo", "撤销上次 AI 修改", self.ai.undo),
        ):
            command = self.commands[name] = Command(label, run)
            setattr(self, name, command)

    def build(self):
        shell = ttk.Frame(self.root, padding=12)
        shell.pack(fill="both", expand=True)
        self.shortcut_bar = ShortcutBar(shell, self.commands, self.toggle_tools)
        self.shortcut_bar.pack(fill="x", pady=(0, 5))
        self.shortcut_bar.set_shortcuts(self.shortcuts)
        workspace = ttk.Frame(shell)
        panes = self.panes = ttk.Panedwindow(workspace, orient="horizontal")
        self.tools_side = self.build_tools(workspace)
        image_side = ttk.Frame(panes, padding=(0, 0, 9, 0))
        result_side = ttk.Frame(panes, padding=(6, 0, 0, 0))
        panes.add(image_side, weight=3)
        panes.add(result_side, weight=3)

        navigation = ttk.Frame(image_side)
        navigation.pack(side="bottom", fill="x", pady=(5, 0))
        self.previous_page.button(navigation).pack(side="left", padx=(0, 5))
        self.pages = self.combo(navigation, tk.StringVar(), (), 18, self.select_page)
        self.next_page.button(navigation).pack(side="left")
        self.selection_hint = ttk.Label(image_side, textvariable=self.selection_label, foreground="#28547f")
        self.selection_hint.pack(side="bottom", anchor="w", pady=(3, 0))
        image_side.bind("<Configure>", lambda e: self.selection_hint.configure(wraplength=max(1, e.width - 12)))
        self.phone_photo_button = ttk.Button(image_side, command=self.show_phone_photo)
        self.preview = ImagePreview(image_side, self.set_selection)
        self.preview.pack(fill="both", expand=True)

        document_tools = ttk.Menubutton(result_side, text="文档")
        menu = tk.Menu(document_tools, tearoff=False)
        self.fill_document_menu(menu, include_previews=True)
        document_tools.configure(menu=menu)
        document_tools.pack(side="bottom", anchor="e", pady=(5, 0))
        ttk.Style(self.root).configure("Workspace.TNotebook.Tab", padding=(4, 3))
        self.result_tabs = ttk.Notebook(result_side, style="Workspace.TNotebook")
        self.result_tabs.pack(fill="both", expand=True)
        self.ocr_panel = ttk.Frame(self.result_tabs, padding=5)
        self.result_tabs.add(self.ocr_panel, text="识别结果")
        ttk.Label(self.ocr_panel, textvariable=self.scope, font=("Microsoft YaHei UI", 11, "bold")).pack(
            anchor="w", pady=(2, 5)
        )
        notebook = ttk.Notebook(self.ocr_panel)
        review = ttk.Frame(notebook)
        textview = ttk.Frame(notebook)
        notebook.add(review, text="原图校对")
        notebook.add(textview, text="识别文字")
        review_controls = ttk.Frame(review)
        review_controls.pack(fill="x", pady=(0, 4))
        self.combo(review_controls, self.review_filter, (
            REVIEW_ALL, REVIEW_PENDING, REVIEW_STRUCTURE, REVIEW_TEXT, REVIEW_FORMULAS,
            REVIEW_TABLES, REVIEW_CAPTIONS, REVIEW_LAYOUT,
        ), 16, self.show_result)
        self.next_review_button.button(review_controls).pack(side="left")
        self.results = ttk.Treeview(review, columns=("kind", "text"), show="headings", selectmode="extended")
        self.results.heading("kind", text="类别")
        self.results.heading("text", text="文字或对象")
        self.results.column("kind", width=75, minwidth=65, stretch=False)
        self.results.column("text", width=310, minwidth=110)
        self.results.tag_configure(TextKind.HANDWRITTEN.value, foreground="#16695b")
        self.results.tag_configure(TextKind.REVIEW.value, foreground="#93530b")
        self.results.tag_configure(TextKind.OMIT.value, foreground="#777777")
        self.results.tag_configure("structured", foreground="#66469f")
        self.results.tag_configure("proposal", foreground="#93530b")
        scroll = ttk.Scrollbar(review, orient="vertical", command=self.results.yview)
        self.results.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.results.pack(side="left", fill="both", expand=True)
        self.results.bind("<<TreeviewSelect>>", lambda event: self.focus_region())
        self.results.bind("<Double-1>", lambda event: self.edit_selected())
        self.results.bind("<Control-z>", lambda event: self.undo_review_button.invoke())
        self.result = tk.Text(textview, width=28, wrap="word", borderwidth=0,
                              background="#ffffff", padx=8, pady=6, state="disabled")
        text_scroll = ttk.Scrollbar(textview, orient="vertical", command=self.result.yview)
        self.result.configure(yscrollcommand=text_scroll.set)
        text_scroll.pack(side="right", fill="y")
        self.result.pack(side="left", fill="both", expand=True)
        review_actions = ttk.Frame(self.ocr_panel)
        self.edit_button.button(review_actions).pack(side="left", padx=(0, 4))
        self.command_popup(review_actions, "校对操作", (
            self.label_button, self.assume_printed, self.split_button, self.merge_button, self.role_button,
            self.reading_up, self.reading_down, self.undo_review_button, self.delete_block,
            self.link_caption, self.unlink_caption, self.table_grid_button, self.ai_request,
        ))

        collection = self.collection_panel = ttk.Frame(self.result_tabs, padding=8)
        self.result_tabs.add(collection, text="错题集")
        ttk.Label(collection, textvariable=self.collection_title).pack(anchor="w")
        actions = ttk.Frame(collection)
        actions.pack(side="bottom", fill="x")
        list_area = ttk.Frame(collection)
        list_area.pack(fill="both", expand=True, pady=(6, 6))
        self.clips = tk.Listbox(list_area, height=4, width=25, exportselection=False)
        clip_scroll = ttk.Scrollbar(list_area, orient="vertical", command=self.clips.yview)
        self.clips.configure(yscrollcommand=clip_scroll.set)
        clip_scroll.pack(side="right", fill="y")
        self.clips.pack(side="left", fill="both", expand=True)
        self.clips.bind("<Double-1>", lambda event: self.jump_to_selection())
        self.clips.bind("<<ListboxSelect>>", lambda event: self.update_buttons())
        self.clip_jump.button(actions).pack(side="left", padx=(0, 4))
        self.command_popup(actions, "选题操作", (self.clip_up, self.clip_down, self.clip_remove))
        review_actions.pack(side="bottom", fill="x", pady=5)
        notebook.pack(fill="both", expand=True)

        footer = ttk.Frame(shell, padding=(0, 6, 0, 0))
        footer.pack(side="bottom", fill="x")
        ttk.Label(footer, textvariable=self.status).pack(side="left")
        self.draft_label = ttk.Label(footer, foreground="#526276")
        self.draft_label.pack(side="right")
        self.update_draft_label()
        workspace.pack(fill="both", expand=True)
        panes.pack(side="left", fill="both", expand=True)
        self.build_menus()

    def build_tools(self, parent):
        side = ttk.Frame(parent, padding=(0, 0, 8, 0))
        tabs = ttk.Notebook(side)
        tabs.pack(fill="both", expand=True)
        options = ttk.Frame(tabs, padding=8)
        image_tools = ttk.Frame(tabs, padding=8)
        tabs.add(options, text="识别与导出")
        tabs.add(image_tools, text="原图工具")
        for label, variable, values, callback in self.option_fields():
            ttk.Label(options, text=label).pack(anchor="w", pady=(0, 3))
            row = ttk.Frame(options)
            row.pack(fill="x", pady=(0, 8))
            self.combo(row, variable, values, 15, callback)
            if variable is self.target:
                self.choose_export_pages.button(options).pack(anchor="w", pady=(0, 8))
        ttk.Checkbutton(options, text="自动保留图表", variable=self.auto_figures,
                        command=self.refresh_regions).pack(anchor="w")
        ttk.Checkbutton(options, text="讲义封面", variable=self.handout_cover).pack(anchor="w")
        ttk.Checkbutton(options, text="讲义目录", variable=self.handout_contents).pack(anchor="w")
        ttk.Button(options, text="记住导出偏好", command=self.store_preferences).pack(anchor="w", pady=(5, 0))
        for index, button in enumerate((self.turn_left, self.turn_right, self.select_button, self.clear_button,
                                        self.save_button, self.keep_button, self.remove_button, self.manual_button,
                                        self.native_button, self.collect_button, self.picture_button)):
            button.button(image_tools).grid(row=index // 2, column=index % 2, sticky="ew", padx=2, pady=3)
        return side

    def restore_preferences(self):
        values = load_preferences(self.preferences_path)
        for key, variable, choices in (
            ("layout", self.export_layout, (ORIGINAL_LAYOUT, HANDOUT_LAYOUT)),
            ("pdf_style", self.pdf_style, (CLEAN_PDF, ORIGINAL_PDF, WORD_PDF)),
            ("photo_quality", self.photo_quality, (PHOTO_FULL, PHOTO_SHARE)),
        ):
            if values.get(key) in choices:
                variable.set(values[key])
        for key, variable in (("cover", self.handout_cover), ("contents", self.handout_contents),
                              ("figures", self.auto_figures)):
            if type(values.get(key)) is bool:
                variable.set(values[key])

    def store_preferences(self):
        try:
            save_settings(self.preferences_path, {
                "layout": self.export_layout.get(), "pdf_style": self.pdf_style.get(),
                "photo_quality": self.photo_quality.get(), "cover": self.handout_cover.get(),
                "contents": self.handout_contents.get(), "figures": self.auto_figures.get(),
            })
        except OSError as error:
            messagebox.showerror("保存导出偏好失败", str(error), parent=self.root)
        else:
            self.status.set("已记住导出偏好")

    def option_fields(self):
        return (
            ("识别模式", self.mode, MODES, None),
            ("提取类别", self.extraction, tuple(Extraction), self.change_export_options),
            ("导出范围", self.target, (CURRENT, ALL_PAGES, CUSTOM_PAGES, COLLECTION), self.change_export_scope),
            ("文档排版", self.export_layout, (ORIGINAL_LAYOUT, HANDOUT_LAYOUT), None),
            ("PDF 样式", self.pdf_style, (CLEAN_PDF, ORIGINAL_PDF, WORD_PDF), self.update_buttons),
            ("照片 PDF 质量", self.photo_quality, (PHOTO_FULL, PHOTO_SHARE), None),
            ("原图缩放", self.zoom, tuple(ZOOM_LEVELS), self.change_zoom),
        )

    def toggle_tools(self):
        self.tools_visible.set(not self.tools_visible.get())
        self.show_tools()

    def show_tools(self):
        if self.tools_visible.get():
            self.tools_side.pack(side="left", fill="y", before=self.panes)
        else:
            self.tools_side.pack_forget()
        self.shortcut_bar.tools.configure(text="收起工具" if self.tools_visible.get() else "展开工具")

    def customize_shortcuts(self):
        ShortcutDialog(self.root, self.commands, self.shortcuts, DEFAULT_SHORTCUTS, self.apply_shortcuts)

    def apply_shortcuts(self, keys):
        save_shortcuts(self.shortcut_path, keys)
        self.shortcuts = keys
        self.shortcut_bar.set_shortcuts(keys)

    def add_menu_button(self, menu, button):
        self.menu_controls.append((menu, button.add_to_menu(menu), button))

    def command_popup(self, parent, title, commands):
        control = ttk.Menubutton(parent, text=title)
        menu = tk.Menu(control, tearoff=False)
        for command in commands:
            self.add_menu_button(menu, command)
        control.configure(menu=menu)
        control.pack(side="left")

    def build_menus(self):
        bar = tk.Menu(self.root)
        file_menu, edit_menu, view_menu, help_menu = (tk.Menu(bar, tearoff=False) for _ in range(4))
        for label, menu in zip(("文件", "编辑", "视图", "帮助"),
                               (file_menu, edit_menu, view_menu, help_menu), strict=True):
            bar.add_cascade(label=label, menu=menu)
        for button in (self.open_button, self.folder_button, self.camera_button, self.phone_button):
            self.add_menu_button(file_menu, button)
        file_menu.add_separator()
        for button in (self.open_draft_button, self.save_draft_button, self.word_button, self.pdf_button):
            self.add_menu_button(file_menu, button)
        self.add_menu_button(file_menu, self.full_button)
        self.add_menu_button(file_menu, self.crop_button)
        self.add_menu_button(file_menu, self.retry_button)
        batch_menu = tk.Menu(file_menu, tearoff=False)
        file_menu.add_cascade(label="批量处理", menu=batch_menu)
        for button in (self.batch_button, self.failed_button, self.cancel_batch_button,
                       self.recover_batch_button, self.organize_button):
            self.add_menu_button(batch_menu, button)
        file_menu.add_command(label="打开 Word / PDF 编辑", command=self.open_document)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self.close)
        for button in (self.edit_button, self.label_button, self.assume_printed, self.split_button,
                       self.merge_button, self.role_button, self.reading_up, self.reading_down,
                       self.undo_review_button, self.next_review_button,
                       self.link_caption, self.unlink_caption,
                       self.delete_block, self.select_button, self.clear_button, self.keep_button, self.save_button,
                       self.remove_button, self.exclude_figure, self.restore_figures,
                       self.turn_left, self.turn_right, self.manual_button, self.native_button, self.table_grid_button,
                       self.collect_button, self.picture_button):
            self.add_menu_button(edit_menu, button)
        collection = tk.Menu(edit_menu, tearoff=False)
        edit_menu.add_cascade(label="错题集", menu=collection)
        for button in (self.show_collection, self.clip_jump, self.clip_up, self.clip_down, self.clip_remove):
            self.add_menu_button(collection, button)
        self.fill_document_menu(edit_menu)
        ai_menu = tk.Menu(edit_menu, tearoff=False)
        edit_menu.add_cascade(label="AI 联网辅助", menu=ai_menu)
        for command in (self.ai_settings, self.ai_request, self.ai_undo):
            self.add_menu_button(ai_menu, command)
        view_menu.add_checkbutton(label="侧边工作栏", variable=self.tools_visible, command=self.show_tools)
        view_menu.add_command(label="自定义快捷栏…", command=self.customize_shortcuts)
        view_menu.add_separator()
        view_menu.add_command(label="识别结果", command=self.show_ocr_panel)
        for button in (self.show_collection, self.previous_page, self.next_page,
                       self.word_preview_button, self.pdf_preview_button):
            self.add_menu_button(view_menu, button)
        options = tk.Menu(view_menu, tearoff=False)
        view_menu.add_cascade(label="识别与导出选项", menu=options)
        for label, variable, values, callback in self.option_fields():
            submenu = tk.Menu(options, tearoff=False)
            for value in values:
                submenu.add_radiobutton(label=value, value=value, variable=variable, command=callback)
            options.add_cascade(label=label, menu=submenu)
        options.add_checkbutton(label="自动保留图表", variable=self.auto_figures, command=self.refresh_regions)
        help_menu.add_command(label="工作区使用说明", command=lambda: messagebox.showinfo(
            "工作区使用说明", "点「展开工具」打开识别和原图选项。\n"
            "在「视图 → 自定义快捷栏」选择常用按钮，右侧可切换识别结果和错题集。\n"
            "在「编辑」或右侧「文档」菜单选择并排编辑或独立窗口。", parent=self.root,
        ))
        help_menu.add_command(label="外部 AI Agent 调用说明", command=self.show_agent_help)
        self.root.configure(menu=bar)

    def show_agent_help(self):
        if self.agent_help_window and self.agent_help_window.winfo_exists():
            self.agent_help_window.lift()
            return
        path = Path(__file__).with_name("外部AI调用说明.md")
        try:
            description = path.read_text(encoding="utf-8")
        except OSError as error:
            messagebox.showerror("无法打开调用说明", str(error), parent=self.root)
            return
        window = self.agent_help_window = tk.Toplevel(self.root)
        window.title("外部 AI Agent 调用说明")
        window.geometry("800x560")
        window.minsize(520, 360)
        document = ScrolledText(window, wrap="word", font=("Microsoft YaHei UI", 10), padx=12, pady=8)
        document.insert("1.0", description)
        document.configure(state="disabled")
        document.pack(fill="both", expand=True, padx=8, pady=(8, 0))
        ttk.Button(window, text="关闭", command=window.destroy).pack(anchor="e", padx=8, pady=8)
        window.bind("<Escape>", lambda _event: window.destroy())

    def fill_document_menu(self, menu, *, include_previews=False):
        self.add_menu_button(menu, self.choose_export_pages)
        self.add_menu_button(menu, self.set_handout_title)
        menu.add_command(label="最近的版面检查", command=self.show_layout_report)
        if include_previews:
            for command in (self.word_preview_button, self.pdf_preview_button):
                self.add_menu_button(menu, command)
            opened = tk.Menu(menu, tearoff=False)
            opened.configure(postcommand=lambda: self.list_open_documents(opened))
            menu.add_cascade(label="切换已打开的文档", menu=opened)
            menu.add_separator()
        for suffix, name in ((".docx", "Word 编辑"), (".pdf", "PDF 编辑")):
            submenu = tk.Menu(menu, tearoff=False)
            self.fill_editor_menu(submenu, suffix)
            menu.add_cascade(label=name, menu=submenu)

    def list_open_documents(self, menu):
        menu.delete(0, "end")
        for suffix, title in ((".docx", "Word"), (".pdf", "PDF")):
            preview = self.document_previews.get(suffix)
            if preview and preview.window.winfo_exists():
                menu.add_command(label=f"{title} 预览", command=lambda tab=preview.window: self.result_tabs.select(tab))
            editor = self.embedded_editors.get(suffix)
            if editor and editor.window.winfo_exists():
                menu.add_command(label=f"{title} 编辑", command=lambda tab=editor.host: self.result_tabs.select(tab))
        if menu.index("end") is None:
            menu.add_command(label="当前没有打开的文档", state="disabled")

    def fill_editor_menu(self, menu, suffix):
        commands = (self.word_edit_split, self.word_edit_full) if suffix == ".docx" else (
            self.pdf_edit_split, self.pdf_edit_full
        )
        for command in commands:
            self.add_menu_button(menu, command)
        menu.add_separator()
        menu.add_command(label="打开已有文件 · 并排", command=lambda: self.open_document(suffix, embedded=True))
        menu.add_command(label="打开已有文件 · 全屏", command=lambda: self.open_document(suffix, maximized=True))

    def show_ocr_panel(self):
        self.result_tabs.select(self.ocr_panel)

    def mark_dirty(self):
        self.dirty = True
        self.update_draft_label()

    def update_draft_label(self):
        name = self.draft_path.name if self.draft_path else "未命名工作稿"
        suffix = " · 有未保存修改" if self.dirty else ""
        self.draft_label.configure(text=name + suffix)

    def choose_images(self):
        paths = filedialog.askopenfilenames(title="导入图片", filetypes=IMAGE_FILETYPES,
                                            initialdir=str(default_file_directory()), parent=self.root)
        for value in paths:
            self.load_image(Path(value))

    def choose_folder(self):
        picked = filedialog.askdirectory(title="导入图片文件夹", initialdir=str(default_file_directory()),
                                         parent=self.root)
        if not picked:
            return
        count, skipped = self.import_folder(Path(picked))
        self.status.set(f"已追加 {count} 张图片 · 跳过 {skipped} 张")

    def import_folder(self, folder):
        count = skipped = 0
        decision = None
        try:
            paths = folder_images(folder)
        except OSError as error:
            messagebox.showerror("读取文件夹失败", str(error), parent=self.root)
            return 0, 0
        for path in paths:
            try:
                image = open_image(path)
            except (OSError, ValueError, Image.DecompressionBombError):
                skipped += 1
                continue
            key = image_key(image)
            duplicate = key in self.image_keys
            if duplicate and decision is None:
                decision = messagebox.askyesno("发现相同图片", f"「{path.name}」与已导入页相同。"
                                               "是否保留本次导入的所有重复页，并标记供校对？",
                                               parent=self.root)
            if duplicate and not decision:
                skipped += 1
            elif self.add_page_image(path.name, image, path, activate=False, notify=False, key=key,
                                     duplicate=duplicate):
                count += 1
            else:
                skipped += 1
        if count:
            self.display_page(len(self.draft.pages) - count)
        return count, skipped

    def load_image(self, path):
        try:
            image = open_image(path)
        except (OSError, ValueError, Image.DecompressionBombError) as error:
            messagebox.showerror("打开图片失败", str(error), parent=self.root)
            return
        key = image_key(image)
        duplicate = key in self.image_keys
        if duplicate and not messagebox.askyesno("发现相同图片", f"「{path.name}」与已导入页相同。仍要保留吗？",
                                                 parent=self.root):
            return
        self.add_page_image(path.name, image, path, key=key, duplicate=duplicate)

    def add_page_image(self, name, image, source_path=None, *, activate=True, notify=True, key=None, duplicate=None):
        if self.busy:
            messagebox.showinfo("识别正在进行", "请等当前识别结束后再导入页面。", parent=self.root)
            return False
        if not self.draft.can_add(image):
            if notify:
                messagebox.showerror("工作稿已满", "请保存当前工作稿，再用单独的工作稿导入这张图片。",
                                     parent=self.root)
            return False
        if source_path is not None:
            self.source_paths.add(source_path.resolve())
        key = key or image_key(image)
        duplicate = key in self.image_keys if duplicate is None else duplicate
        self.image_keys.add(key)
        self.draft.pages.append(StudyPage(name, image, duplicate=duplicate))
        self.mark_dirty()
        if activate or len(self.draft.pages) == 1:
            self.display_page(len(self.draft.pages) - 1)
        else:
            self.refresh_page_choices()
        return True

    def open_camera(self):
        from capture_ui import CameraWindow

        if self.camera_window and self.camera_window.window.winfo_exists():
            self.camera_window.window.lift()
        else:
            previous_feed = self.camera_window.feed if self.camera_window else None
            self.camera_window = CameraWindow(
                self.root, lambda image: self.add_capture("摄像头", image), lambda: not self.busy,
                previous_feed,
            )

    def open_phone(self):
        from capture_ui import PhoneWindow

        if self.phone_window and self.phone_window.window.winfo_exists():
            self.phone_window.window.lift()
        else:
            self.phone_window = PhoneWindow(self.root, self.phone_inbox)
            if self.phone_page is not None:
                self.phone_window.received(self.phone_page + 1)

    def add_capture(self, source, image, *, activate=True, notify=True):
        self.capture_count += 1
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return self.add_page_image(f"{source}_{stamp}_{self.capture_count:03d}.jpg", image,
                                   activate=activate, notify=notify)

    def collect_phone_photos(self):
        self.phone_poll = self.root.after(180, self.collect_phone_photos)
        # Tcl also reports grabs from native Tk popups that have no Tkinter widget object.
        if self.busy or self.root.tk.call("grab", "current", self.root._w):
            return
        try:
            upload = self.phone_inbox.get_nowait()
        except Empty:
            return
        phone_window = self.phone_window if self.phone_window and self.phone_window.window.winfo_exists() else None
        image = upload.image
        if not self.add_capture("手机", image, activate=False, notify=False):
            upload.page.set_result(None)
            self.status.set("工作稿已满。请保存并更换工作稿后，重新发送手机照片。")
            if phone_window:
                phone_window.rejected()
            return
        number = len(self.draft.pages)
        self.phone_page = number - 1
        upload.page.set_result(number)
        self.phone_photo_button.configure(text=f"查看手机照片（第 {number} 页）")
        self.phone_photo_button.pack(side="bottom", before=self.preview, anchor="w", pady=(3, 0))
        self.update_buttons()
        self.status.set(f"手机照片已加入 · 第 {number} 页 · {image.width} × {image.height} 像素")
        if phone_window:
            phone_window.received(number)

    def show_phone_photo(self):
        if self.phone_page is not None and not self.busy and self.draft.active_index != self.phone_page:
            self.display_page(self.phone_page)

    def open_document(self, suffix=None, *, embedded=False, maximized=False):
        types = [("Word 文档", "*.docx"), ("PDF 文档", "*.pdf")]
        if suffix:
            types = [types[0] if suffix == ".docx" else types[1]]
        else:
            types.insert(0, ("Word 或 PDF", "*.docx *.pdf"))
        filename = filedialog.askopenfilename(
            parent=self.root, title="打开要编辑的文档", filetypes=types,
        )
        if not filename:
            return
        path = Path(filename)
        try:
            if suffix and path.suffix.lower() != suffix:
                raise ValueError(f"请选择 {suffix} 文件")
            self.create_editor(path, embedded=embedded, maximized=maximized)
        except Exception as error:
            messagebox.showerror("打开文档失败", str(error), parent=self.root)

    def create_editor(self, path, *, embedded=False, maximized=False, resource=None):
        from document_edit import PdfEditor, WordEditor

        suffix = path.suffix.lower()
        if suffix == ".docx":
            model = WordEditor(path)
        elif suffix == ".pdf":
            model = PdfEditor(path)
        else:
            raise ValueError("请选择 .docx 或 .pdf 文件")
        return self.place_editor(suffix, model, embedded=embedded, maximized=maximized, resource=resource)

    def place_editor(self, suffix, model, *, embedded, maximized, resource, transfer=None):
        from document_ui import PdfWindow, WordWindow

        if suffix == ".docx":
            factory, arguments, name = WordWindow, (model,), "Word 编辑"
        else:
            factory, arguments, name = PdfWindow, (model, ImagePreview, self.add_page_image), "PDF 编辑"
        previous = self.embedded_editors.get(suffix) if embedded else None
        if previous and previous.window.winfo_exists() and not previous.close():
            self.result_tabs.select(previous.host)
            return False

        host = ttk.Frame(self.result_tabs) if embedded else None
        editor = None
        try:
            editor = factory(self.root, *arguments, host=host, resource=resource, maximized=maximized,
                             switch_layout=self.switch_editor_layout)
            if transfer:
                editor.refresh(transfer.handoff())
                editor.dirty = transfer.dirty
        except Exception:
            if editor:
                editor.resource = None
                editor.dispose()
            elif host:
                host.destroy()
            raise
        if transfer:
            transfer.resource = None
            transfer.dispose()
        if host:
            self.result_tabs.add(host, text=name)
            self.result_tabs.select(host)
            self.embedded_editors[suffix] = editor
            self.root.lift()
        self.editors = [window for window in self.editors if window.window.winfo_exists()]
        self.editors.append(editor)
        return True

    def switch_editor_layout(self, editor):
        embedded = editor.host is None
        try:
            self.place_editor(editor.suffix, editor.model, embedded=embedded, maximized=not embedded,
                              resource=editor.resource, transfer=editor)
        except Exception as error:
            messagebox.showerror("切换编辑布局失败", str(error), parent=editor.dialog_parent)

    def generate_document(self, suffix, *, preview=False, embedded=False):
        if self.document_job is not None or self.busy:
            return
        spec = self.checked_document_options(suffix)
        if spec is None:
            return
        self.document_job = Queue(maxsize=1)
        inbox = self.document_job
        name = "Word" if suffix == ".docx" else "PDF"
        self.status.set(f"正在生成 {name} {'预览' if preview else '编辑副本'}…")
        self.update_buttons()

        def work():
            try:
                inbox.put(prepare_document(spec, preview=preview))
            except Exception as error:
                inbox.put(error)

        Thread(target=work, daemon=True).start()
        self.root.after(80, self.finish_document, suffix, preview, embedded)

    def finish_document(self, suffix, preview, embedded):
        try:
            result = self.document_job.get_nowait()
        except Empty:
            self.root.after(80, self.finish_document, suffix, preview, embedded)
            return
        self.document_job = None
        self.update_buttons()
        try:
            if isinstance(result, Exception):
                raise result
            if result.report:
                result.report.inspect_pages()
            self.last_layout_report = result.report
            if self.review_filter.get() == REVIEW_LAYOUT:
                self.show_result()
            if preview:
                self.show_document_preview(suffix, result)
                self.status.set(f"已生成预览 · {result.filename}" + self.filter_status(result.report))
            elif self.create_editor(result.source, embedded=embedded, maximized=not embedded, resource=result):
                self.status.set(f"已打开编辑副本 · {result.filename}")
            else:
                result.close()
                self.status.set("保留当前编辑文档")
        except Exception as error:
            if not isinstance(result, Exception):
                result.close()
            self.status.set("文档未打开")
            messagebox.showerror("无法打开文档", str(error), parent=self.root)

    def show_document_preview(self, suffix, resource):
        from document_ui import DocumentPreview

        host = ttk.Frame(self.result_tabs)
        try:
            viewer = DocumentPreview(host, resource, ImagePreview, self.save_preview, self.show_layout_report)
        except Exception:
            host.destroy()
            raise
        previous = self.document_previews.get(suffix)
        if previous and previous.window.winfo_exists():
            previous.close()
        self.document_previews[suffix] = viewer
        label = "Word 讲义示意" if resource.preview_is_handout_pdf else (
            "Word 预览" if suffix == ".docx" else "PDF 预览")
        self.result_tabs.add(host, text=label)
        self.result_tabs.select(host)

    def show_layout_report(self, report=None, viewer=None):
        report = report or self.last_layout_report
        if report is None:
            messagebox.showinfo("版面检查", "选择 A4 讲义排版后生成预览或导出，即可查看检查结果。", parent=self.root)
            return
        window = tk.Toplevel(self.root)
        window.title("讲义版面检查")
        window.geometry("720x375")
        lines = "、".join(report.checks)
        ttk.Label(window, text=lines, wraplength=670, padding=10).pack(fill="x")
        count = len(report.issues)
        status = f"发现 {count} 项需要确认" if count else "已执行的检查均通过"
        if report.pages is not None:
            status += f" · {report.pages} 页"
        ttk.Label(window, text=status, padding=(10, 0, 10, 7)).pack(anchor="w")
        table = ttk.Treeview(window, columns=("page", "source", "message"), show="headings", selectmode="browse")
        for column, heading, width in (("page", "输出页", 60), ("source", "原图", 145),
                                       ("message", "检查结果", 480)):
            table.heading(column, text=heading)
            table.column(column, width=width)
        for index, issue in enumerate(report.issues):
            table.insert("", "end", iid=str(index), values=(
                issue["output_page"] or "—", issue["source_name"] or "—", issue["message"]))
        table.pack(fill="both", expand=True, padx=10, pady=(0, 5))

        def locate(_event=None):
            if not table.selection():
                return
            issue = report.issues[int(table.selection()[0])]
            page_number = issue["output_page"]
            if page_number and viewer and viewer.window.winfo_exists():
                viewer.show(page_number - 1)
            if not self.busy and issue["source_id"]:
                for index, page in enumerate(self.draft.pages):
                    if page.id == issue["source_id"]:
                        self.display_page(index)
                        if issue["source_bounds"]:
                            self.set_selection(Rect(*issue["source_bounds"]))
                        break

        table.bind("<Double-1>", locate)
        ttk.Button(window, text="定位", command=locate).pack(side="left", padx=10, pady=(0, 8))
        if report.asset_audit is not None:
            def save_assets():
                target = filedialog.asksaveasfilename(parent=window, title="保存裁切清单",
                                                      defaultextension=".json", initialfile="讲义裁切清单.json",
                                                      filetypes=[("JSON 清单", "*.json")])
                if target:
                    def write(path):
                        with export_path(path) as temporary:
                            temporary.write_text(json.dumps(report.asset_audit, ensure_ascii=False, indent=2),
                                                 encoding="utf-8")

                    self.save_file(target, write)

            ttk.Button(window, text="保存裁切清单", command=save_assets).pack(side="left", padx=5, pady=(0, 8))
        ttk.Button(window, text="关闭", command=window.destroy).pack(side="right", padx=10, pady=(0, 8))

    def save_preview(self, resource):
        suffix = resource.source.suffix
        path = self.choose_export_path(resource.filename, suffix)
        if path:
            def write(target):
                with export_path(target) as temporary:
                    copyfile(resource.source, temporary)

            self.save_file(path, write)

    def select_page(self):
        index = self.pages.current()
        if index >= 0 and not self.busy:
            self.display_page(index)

    def move_page(self, step):
        index = self.draft.active_index + step
        if not self.busy and 0 <= index < len(self.draft.pages):
            self.display_page(index)

    def refresh_page_choices(self):
        choices = [f"{n + 1}. {page.name}" + (
            f" · {page.quality['reason']}" if page.quality and page.quality["status"] != "ok" else ""
        ) + (" · 重复页" if page.duplicate else "") for n, page in enumerate(self.draft.pages)]
        self.pages.configure(values=choices)
        if choices:
            self.pages.current(self.draft.active_index)

    def display_page(self, index):
        self.draft.active_index = index
        self.refresh_page_choices()
        self.results.selection_remove(self.results.selection())
        self.zoom.set("适应窗口")
        self.preview.show_image(self.image)
        self.set_selection(None)
        self.show_result()
        self.status.set(self.page_status())

    def rotate(self, left):
        if not self.page or self.busy:
            return
        angle = 270 if left else 90
        selected = rotated_bounds(self.selection, self.image.size, angle) if self.selection else None
        rotate_page(self.draft, self.page, angle)
        self.review_history.pop(self.page.id, None)
        self.mark_dirty()
        self.display_page(self.draft.active_index)
        if selected:
            self.set_selection(selected)

    def change_zoom(self):
        self.preview.set_zoom(ZOOM_LEVELS[self.zoom.get()])

    def guide_selection(self):
        self.preview.canvas.focus_set()
        self.selection_label.set("框选已就绪。在照片上按住鼠标左键拖动")

    def set_selection(self, area):
        self.selection = area
        self.preview.selection = area
        self.refresh_regions()

    def refresh_regions(self):
        self.preview.regions = self.regions
        self.preview.structured = tuple(block.bounds for block in self.page.blocks) if self.page else ()
        self.preview.detected = ()
        if self.scan and self.auto_figures.get() and self.extraction.get() == Extraction.ALL:
            area = self.scan.area
            sheet = self.page.export_sheet()
            blocks = local_blocks(sheet, Extraction.ALL)
            all_regions = image_regions(sheet, blocks, auto_figures=True)
            manual = image_regions(sheet, blocks, auto_figures=False)
            self.preview.detected = tuple(box.translated(area.left, area.top)
                                          for box in all_regions[len(manual):])
        self.preview.draw_boxes()
        tip = "导入图片后，可以在预览上框选区域"
        if self.selection:
            tip = f"选区 {self.selection.width} × {self.selection.height} 像素"
        elif self.page:
            tip = "在照片上按住鼠标左键拖动，松开后可识别选区或另存截图"
        if self.regions:
            tip += f" · 原图标记 {len(self.regions)} 处"
        if self.preview.detected:
            tip += f" · 自动图表 {len(self.preview.detected)} 处"
        if self.page and self.page.figure_exclusions:
            tip += f" · 已排除 {len(self.page.figure_exclusions)} 处"
        if self.page and self.page.figure_captions:
            tip += f" · 手动图注 {len(self.page.figure_captions)} 条"
        self.selection_label.set(tip)
        self.update_buttons()

    def keep_crop(self):
        if self.selection and self.selection not in self.regions:
            self.page.regions += (self.selection,)
            self.mark_dirty()
            self.refresh_regions()

    def clear_regions(self):
        self.page.regions = ()
        self.mark_dirty()
        self.refresh_regions()

    def exclude_detected_figures(self):
        regions = tuple(bounds for bounds in self.preview.detected if self.selection and
                        (overlap := bounds.intersection(self.selection)) and
                        overlap.width * overlap.height >= bounds.width * bounds.height * 0.5)
        if regions:
            self.page.figure_exclusions += regions
            self.mark_dirty()
            self.refresh_regions()
            self.update_buttons()
        else:
            self.status.set("请框住需要排除的自动图表的大部分区域")

    def restore_detected_figures(self):
        self.page.figure_exclusions = ()
        self.mark_dirty()
        self.refresh_regions()
        self.update_buttons()

    def selected_caption_line(self):
        selected = self.results.selection()
        if self.scan and len(selected) == 1 and selected[0].startswith("line:"):
            line = self.scan.lines[int(selected[0].split(":")[1])]
            return line.bounds.translated(self.scan.area.left, self.scan.area.top)
        return None

    def link_figure_caption(self):
        caption = self.selected_caption_line()
        regions = []
        for bounds in self.caption_targets():
            overlap = self.selection.intersection(bounds)
            if overlap:
                ratio = overlap.width * overlap.height / (bounds.width * bounds.height)
                if ratio >= 0.5:
                    regions.append((ratio, bounds))
        if not regions:
            self.status.set("请框住图表的大部分，再在右侧选中一行图注")
            return
        figure = max(regions, key=lambda item: item[0])[1]
        self.remember_review()
        self.page.figure_captions = tuple((box, line) for box, line in self.page.figure_captions
                                          if line != caption) + ((figure, caption),)
        self.mark_dirty()
        self.show_result()
        self.status.set("已保存图注关联")
        self.update_buttons()

    def unlink_figure_caption(self):
        caption = self.selected_caption_line()
        self.remember_review()
        self.page.figure_captions = tuple((box, line) for box, line in self.page.figure_captions
                                          if line != caption)
        self.mark_dirty()
        self.show_result()
        self.update_buttons()

    def caption_targets(self):
        tables = tuple(block.bounds for block in self.page.blocks if block.format == BlockFormat.TABLE)
        return (*tables, *self.regions, *self.preview.detected)

    def assume_page_printed(self):
        lines = tuple(replace(line, kind=TextKind.PRINTED) if line.kind != TextKind.OMIT else line
                      for line in self.scan.lines)
        self.use_lines(lines)
        self.status.set("已将本页可用文字标为印刷体")
        self.update_buttons()

    def update_buttons(self):
        ready = not self.busy
        has_page = self.page is not None
        has_box = self.selection is not None
        selected = self.results.selection()
        is_text = bool(selected) and all(key.startswith("line:") for key in selected)
        is_block = len(selected) == 1 and selected[0].startswith("block:")
        is_proposal = len(selected) == 1 and selected[0].startswith("proposal:")
        clips = self.clips.curselection()
        if self.target.get() == COLLECTION:
            available = bool(self.draft.selections)
        elif self.target.get() == ALL_PAGES:
            available = any(page.scan is not None for page in self.draft.pages)
        elif self.target.get() == CUSTOM_PAGES:
            available = bool(self.draft.export_pages())
        else:
            available = self.scan is not None
        can_generate = ready and available and self.document_job is None
        for button, enabled in (
            (self.open_button, ready), (self.open_draft_button, ready),
            (self.folder_button, ready),
            (self.choose_export_pages, ready and has_page),
            (self.set_handout_title, ready and has_page),
            (self.batch_button, ready and bool(pending_indices(self.draft))),
            (self.failed_button, ready and bool(pending_indices(self.draft, failed_only=True))),
            (self.cancel_batch_button, self.batch is not None and not self.batch.cancel.is_set()),
            (self.recover_batch_button, ready),
            (self.organize_button, ready and has_page and self.document_job is None),
            (self.save_draft_button, ready and has_page),
            (self.previous_page, ready and self.draft.active_index > 0),
            (self.next_page, ready and self.draft.active_index + 1 < len(self.draft.pages)),
            (self.turn_left, ready and has_page), (self.turn_right, ready and has_page),
            (self.full_button, ready and has_page), (self.crop_button, ready and has_box),
            (self.select_button, has_page),
            (self.retry_button, ready and has_page),
            (self.clear_button, has_box), (self.save_button, has_box),
            (self.keep_button, ready and has_box and self.selection not in self.regions),
            (self.remove_button, ready and bool(self.regions)),
            (self.exclude_figure, ready and has_box and bool(self.preview.detected)),
            (self.restore_figures, ready and has_page and bool(self.page.figure_exclusions)),
            (self.manual_button, ready and has_box), (self.native_button, ready and has_box),
            (self.table_grid_button, ready and has_box),
            (self.collect_button, ready and has_box), (self.picture_button, ready and has_box),
            (self.edit_button, ready and ((len(selected) == 1 and is_text) or is_block or is_proposal)),
            (self.label_button, ready and is_text),
            (self.assume_printed, ready and bool(self.scan and any(
                line.kind in (TextKind.REVIEW, TextKind.HANDWRITTEN) for line in self.scan.lines))),
            (self.link_caption, ready and has_box and is_text and len(selected) == 1
             and bool(self.caption_targets())),
            (self.unlink_caption, ready and is_text and len(selected) == 1
             and any(line == self.selected_caption_line() for _, line in self.page.figure_captions)),
            (self.split_button, ready and len(selected) == 1 and is_text),
            (self.merge_button, ready and len(selected) > 1 and is_text),
            (self.role_button, ready and is_text),
            (self.reading_up, ready and is_text and len(selected) == 1),
            (self.reading_down, ready and is_text and len(selected) == 1),
            (self.undo_review_button, ready and has_page and bool(self.review_history.get(self.page.id))),
            (self.next_review_button, ready and has_page),
            (self.delete_block, ready and is_block),
            (self.clip_jump, ready and bool(clips)),
            (self.clip_up, ready and bool(clips) and clips[0] > 0),
            (self.clip_down, ready and bool(clips) and clips[0] < len(self.draft.selections) - 1),
            (self.clip_remove, ready and bool(clips)),
            (self.pdf_button, ready and available), (self.word_button, ready and available),
            (self.word_preview_button, can_generate), (self.pdf_preview_button, can_generate),
            (self.word_edit_split, can_generate), (self.word_edit_full, can_generate),
            (self.pdf_edit_split, can_generate), (self.pdf_edit_full, can_generate),
            (self.ai_request, ready and self.ai.can_request()), (self.ai_undo, ready and self.ai.can_undo()),
        ):
            button.set_enabled(enabled)
        self.phone_photo_button.configure(state="normal" if ready and self.phone_page is not None else "disabled")
        self.pages.configure(state="readonly" if ready and has_page else "disabled")
        for menu, index, button in self.menu_controls:
            menu.entryconfigure(index, state=button.state)

    def retry_page(self):
        choices = {"自动判断": "auto", "保持当前方向": "keep", "顺时针 90°": "90",
                   "旋转 180°": "180", "顺时针 270°": "270"}
        form = ReviewDialog(self.root, "重新识别当前页", [
            ("rotation", "相对当前预览方向", "自动判断", tuple(choices)),
        ]).result
        if form:
            self.start_scan(rotation=choices[form["rotation"]])

    def page_status(self):
        quality = self.page.quality
        number = self.draft.active_index + 1
        if quality:
            if quality["status"] != "ok":
                return f"第 {number} 页 · {quality['reason']} · {quality['action']}"
            if self.scan:
                correction = quality.get("applied_rotation", 0)
                suffix = f" · 已顺时针转正 {correction}°" if correction else ""
                return f"第 {number} 页 · 识别完成 · {len(self.scan.lines)} 个文字块 · {self.scan.seconds:.1f} 秒{suffix}"
        return f"{self.page.name} · {self.image.width} × {self.image.height} 像素"

    def start_scan(self, area=None, *, rotation=None):
        if self.image is None or self.busy:
            return
        page, image = self.page, self.image
        mixed = self.mode.get() == "混排识别"
        handwriting = self.mode.get() == "手写增强"
        rotation = rotation or ("keep" if area is not None else "auto")
        self.busy = True
        self.status.set("正在识别，请稍候。首次使用可能下载模型。")
        self.update_buttons()

        def worker():
            try:
                result = recognize_page(self.engine, image, area, rotation=rotation, handwriting=handwriting, mixed=mixed)
                self.inbox.put((page, result))
            except Exception as error:
                self.inbox.put((page, error))

        Thread(target=worker, daemon=True).start()
        self.root.after(80, self.collect_scan)

    def start_batch(self, *, failed_only=False, organize=False, only_custom=False):
        if self.busy or self.document_job is not None:
            return
        indices = pending_indices(self.draft, failed_only=failed_only)
        if only_custom:
            pending = set(indices)
            by_id = {page.id: i for i, page in enumerate(self.draft.pages)}
            indices = [by_id[page.id] for page in self.draft.export_pages() if by_id[page.id] in pending]
        if not indices:
            if organize:
                self.preview_batch_handout()
            return
        batch = self.batch = BatchProgress(organize, len(indices))
        mixed, handwriting = self.mode.get() == "混排识别", self.mode.get() == "手写增强"
        self.busy = True
        self.status.set(f"正在准备 {batch.total} 页批量识别和恢复记录…")
        self.update_buttons()

        def work():
            try:
                run_batch(self.draft, indices, self.engine, self.recovery_directory, batch.cancel, batch.events.put,
                          mixed=mixed, handwriting=handwriting)
            except Exception as error:
                batch.events.put(("failure", str(error)))
            finally:
                batch.events.put(("done",))

        Thread(target=work, daemon=True).start()
        self.root.after(80, self.collect_batch)

    def stop_batch(self):
        if self.batch:
            self.batch.cancel.set()
            self.status.set("当前页处理结束后停止 · 已完成页自动保留")
            self.update_buttons()

    def collect_batch(self):
        batch = self.batch
        while batch:
            try:
                event, *payload = batch.events.get_nowait()
            except Empty:
                self.root.after(80, self.collect_batch)
                return
            if event == "journal":
                previous, self.batch_journal = self.batch_journal, payload[0]
                if previous:
                    self.release_journal(previous)
            elif event == "progress":
                count, total, name = payload
                if not batch.cancel.is_set():
                    self.status.set(f"批量识别 {count}/{total} · {name}")
            elif event == "page":
                index, result, error = payload
                page = self.draft.pages[index]
                if result:
                    if result.rotation or page.scan is not None:
                        self.review_history.pop(page.id, None)
                    rotate_page(self.draft, page, result.rotation, result.image)
                    page.scan, page.quality = result.scan, result.quality
                else:
                    page.quality = quality_result("error")
                    batch.errors.append(f"{page.name} · {error}")
                batch.done += 1
                self.mark_dirty()
                if index == self.draft.active_index:
                    self.display_page(index)
                else:
                    self.refresh_page_choices()
            elif event == "failure":
                batch.errors.append(payload[0])
            elif event == "done":
                self.batch = None
                self.busy = False
                self.update_buttons()
                summary = "已停止" if batch.cancel.is_set() else "批量处理结束"
                self.status.set(f"{summary} · 完成 {batch.done}/{batch.total} 页 · 失败 {len(batch.errors)}")
                if batch.errors:
                    messagebox.showwarning("批量识别", "\n".join(batch.errors[:12]), parent=self.root)
                if batch.organize and not batch.cancel.is_set() and any(p.scan for p in self.draft.pages):
                    self.preview_batch_handout()
                return

    def preview_batch_handout(self):
        self.target.set(ALL_PAGES)
        self.export_layout.set(HANDOUT_LAYOUT)
        self.pdf_style.set(CLEAN_PDF)
        self.update_buttons()
        self.generate_document(".pdf", preview=True)

    def recover_batch(self):
        available = BatchJournal.recent(self.recovery_directory)
        if not available:
            messagebox.showinfo("批量恢复", "没有未保存的批量任务。", parent=self.root)
            return
        choices = {f"{datetime.fromtimestamp(job.path.stat().st_mtime):%Y-%m-%d %H:%M} · {job.path.name}": job
                   for job in available}
        form = ReviewDialog(self.root, "恢复批量识别", [
            ("job", "恢复记录", next(iter(choices)), tuple(choices)),
        ]).result
        if not form:
            return
        journal = choices[form["job"]]
        try:
            draft = journal.recover()
        except (OSError, ValueError, KeyError) as error:
            messagebox.showerror("恢复失败", str(error), parent=self.root)
            return
        if not self.discard_or_save():
            return
        self.load_workspace(draft, None, journal if journal.base.is_file() else None)
        self.mark_dirty()
        self.status.set(f"已恢复 {len(draft.pages)} 页 · 待处理 {len(pending_indices(draft))} 页")

    def release_journal(self, journal):
        try:
            journal.discard()
        except OSError:
            pass

    def collect_scan(self):
        try:
            page, result = self.inbox.get_nowait()
        except Empty:
            self.root.after(80, self.collect_scan)
            return
        self.busy = False
        if isinstance(result, ScanAttempt):
            self.review_history.pop(page.id, None)
            selected = self.selection
            if selected and result.rotation:
                selected = rotated_bounds(selected, page.image.size, result.rotation)
            rotate_page(self.draft, page, result.rotation, result.image)
            page.scan, page.quality = result.scan, result.quality
            self.mark_dirty()
            if result.rotation:
                self.display_page(self.draft.active_index)
                if selected:
                    self.set_selection(selected)
            else:
                self.refresh_page_choices()
                self.show_result()
        else:
            page.quality = quality_result("error")
            self.mark_dirty()
            self.refresh_page_choices()
            self.show_result()
            messagebox.showerror("识别失败", str(result), parent=self.root)
        self.status.set(self.page_status())
        self.update_buttons()

    def change_export_options(self):
        self.show_result()

    def show_result(self):
        selected = self.results.selection()
        self.results.delete(*self.results.get_children())
        self.proposals = ()
        filter_value = self.review_filter.get()
        if self.scan:
            indices = {id(line): index for index, line in enumerate(self.scan.lines)}
            count = {kind: 0 for kind in TextKind}
            layout = page_layout(self.scan)
            sheet = self.page.export_sheet()
            blocks = local_blocks(sheet, Extraction.ALL)
            regions = image_regions(sheet, blocks, auto_figures=True)
            self.proposals = reconstruction_queue(sheet, regions, blocks)
            unmatched = set()
            if filter_value == REVIEW_CAPTIONS:
                paragraphs = text_blocks(layout)
                links = sheet_caption_links(sheet, regions, blocks, paragraphs)
                attached = {id(line) for group in links.values() for line in group}
                unmatched = {id(line) for item in paragraphs if item.role == "caption"
                             for line in item.lines if line.kind != TextKind.OMIT and id(line) not in attached}
            for index, proposal in enumerate(self.proposals):
                if (filter_value in (REVIEW_ALL, REVIEW_PENDING, REVIEW_STRUCTURE)
                        or filter_value == REVIEW_TABLES and proposal.format == BlockFormat.TABLE
                        or filter_value == REVIEW_FORMULAS and proposal.format != BlockFormat.TABLE):
                    self.results.insert("", "end", iid=f"proposal:{index}", tags=("proposal",),
                                        values=("结构待确认", f"{proposal.format.value} · {proposal.reason}"))
            for line in layout.reading_order:
                kind = line.kind
                count[kind] += 1
                if not (filter_value == REVIEW_ALL
                        or filter_value in (REVIEW_PENDING, REVIEW_TEXT) and self.needs_review(line)
                        or filter_value == REVIEW_CAPTIONS and id(line) in unmatched):
                    continue
                self.results.insert("", "end", iid=f"line:{indices[id(line)]}",
                                    values=(kind.label, f"{ROLE_NAMES[line.role]} · {line.text}" if line.role
                                            else line.text), tags=(kind.value,))
            area = self.scan.area
            scope = "整图" if area == self.page.bounds else f"选区 {area.width} × {area.height}"
            if layout.gutter is not None:
                scope += " · 双栏"
            mode = Extraction(self.extraction.get())
            included = sum(total for kind, total in count.items() if mode.includes(kind))
            self.scope.set(f"{scope} · 印 {count[TextKind.PRINTED]} · 手 {count[TextKind.HANDWRITTEN]}"
                           f" · 待确认 {count[TextKind.REVIEW]}"
                           f" · 分类纳入 {included} / 排除 {sum(count.values()) - included}"
                           f" · 结构复核 {len(self.proposals)}")
            if filter_value == REVIEW_LAYOUT and self.last_layout_report:
                for index, issue in enumerate(self.last_layout_report.issues):
                    if issue["source_id"] == self.page.id:
                        self.results.insert("", "end", iid=f"layout:{index}",
                                            values=("版面异常", issue["message"]), tags=("proposal",))
            elif filter_value == REVIEW_LAYOUT:
                self.scope.set("生成 A4 讲义预览或导出后，可筛选本页的版面问题")
        else:
            self.scope.set("识别结果")
        if self.page and self.review_filter.get() == REVIEW_ALL:
            for index, block in enumerate(self.page.blocks):
                self.results.insert("", "end", iid=f"block:{index}",
                                    values=(block.kind.label, f"{block.format}  {block.value.splitlines()[0]}"),
                                    tags=("structured",))
        if not self.scan and self.page and self.page.quality and self.page.quality["status"] != "ok":
            self.scope.set(f"第 {self.draft.active_index + 1} 页 · {self.page.quality['reason']}")
        retained = [key for key in selected if key.startswith(("line:", "block:")) and self.results.exists(key)]
        if retained:
            self.results.selection_set(retained)
        self.show_text()
        self.refresh_regions()
        self.focus_region()

    def show_text(self):
        self.result.configure(state="normal")
        self.result.delete("1.0", "end")
        mode = Extraction(self.extraction.get())
        if self.scan:
            blocks = local_blocks(self.page.export_sheet(), mode)
            for paragraph in text_blocks(page_layout(self.scan)):
                lines = [line.text for line in paragraph.lines
                         if mode.includes(line.kind) and not any(
                             covers(block.bounds, line.bounds, 0.85) for block in blocks
                         )]
                if lines:
                    self.result.insert("end", "\n".join(lines) + "\n\n")
            for block in blocks:
                self.result.insert("end", structured_text(block) + "\n")
        self.result.configure(state="disabled")

    def focus_region(self):
        selected = self.results.selection()
        self.preview.review = None
        if self.page and len(selected) == 1:
            prefix, key = selected[0].split(":")
            index = int(key)
            if prefix == "line" and self.scan:
                bounds = self.scan.lines[index].bounds
                self.preview.review = bounds.translated(self.scan.area.left, self.scan.area.top)
            elif prefix == "block":
                self.preview.review = self.page.blocks[index].bounds
            elif prefix == "proposal":
                bounds = self.proposals[index].bounds
                self.preview.review = bounds.translated(self.scan.area.left, self.scan.area.top)
            elif prefix == "layout" and self.last_layout_report and index < len(self.last_layout_report.issues):
                bounds = self.last_layout_report.issues[index]["source_bounds"]
                self.preview.review = Rect(*bounds) if bounds else None
        self.preview.draw_boxes()
        self.update_buttons()

    @staticmethod
    def needs_review(line):
        return line.kind != TextKind.OMIT and (line.kind == TextKind.REVIEW or line.confidence < 0.78)

    def next_review(self):
        if self.review_filter.get() == REVIEW_ALL:
            self.review_filter.set(REVIEW_PENDING)
            self.show_result()
        current = self.results.selection()
        children = self.results.get_children()
        index = children.index(current[0]) + 1 if current and current[0] in children else 0
        if index < len(children):
            self.select_result(children[index])
            return
        total = len(self.draft.pages)
        first = self.draft.active_index
        for step in range(1, total):
            index = (first + step) % total
            page = self.draft.pages[index]
            if not page.scan and not page.quality:
                continue
            self.display_page(index)
            if children := self.results.get_children():
                self.select_result(children[0])
                return
            if page.quality and page.quality["status"] not in ("ok", "blank"):
                return
        if self.draft.active_index != first:
            self.display_page(first)
        self.status.set("已到末项，可再次点击从本页第一处复核")
        self.results.selection_remove(self.results.selection())

    def select_result(self, key):
        self.results.selection_set(key)
        self.results.see(key)
        self.focus_region()

    def remember_review(self):
        history = self.review_history.setdefault(self.page.id, [])
        history.append((self.page.scan, self.page.blocks, self.page.figure_captions))
        del history[:-20]

    def undo_review(self):
        history = self.review_history.get(self.page.id) if self.page else None
        if history:
            self.page.scan, self.page.blocks, self.page.figure_captions = history.pop()
            self.results.selection_remove(self.results.selection())
            self.mark_dirty()
            self.show_result()
            self.status.set("已撤销本页最近一次校对")

    def use_lines(self, lines, *, remember=True):
        if remember:
            self.remember_review()
        self.page.scan = replace(self.scan, lines=tuple(lines))
        self.mark_dirty()
        self.show_result()

    def selected_line_indices(self):
        return [int(key.split(":")[1]) for key in self.results.selection() if key.startswith("line:")]

    def change_roles(self):
        indices = self.selected_line_indices()
        if not indices:
            return
        roles = {"自动判断": None, **{name: role for role, name in ROLE_NAMES.items()}}
        old = self.scan.lines[indices[0]].role
        form = ReviewDialog(self.root, "修改文字角色", [
            ("role", "讲义结构", ROLE_NAMES.get(old, "自动判断"), tuple(roles)),
        ]).result
        if form:
            lines = list(self.scan.lines)
            for i in indices:
                lines[i] = replace(lines[i], role=roles[form["role"]])
            self.use_lines(lines)

    def move_text_block(self, direction):
        selected = self.selected_line_indices()
        if len(selected) != 1:
            return
        line = self.scan.lines[selected[0]]
        blocks = list(text_blocks(page_layout(self.scan)))
        current = next(i for i, block in enumerate(blocks) if any(piece is line for piece in block.lines))
        target = current + direction
        if not 0 <= target < len(blocks):
            return
        blocks[current], blocks[target] = blocks[target], blocks[current]
        ranks = {id(piece): i for i, piece in enumerate(line for block in blocks for line in block.lines)}
        self.use_lines([replace(piece, review_order=ranks[id(piece)]) for piece in self.scan.lines])

    def merge_selected(self):
        indices = self.selected_line_indices()
        if len(indices) < 2:
            return
        selected = {id(self.scan.lines[i]) for i in indices}
        layout = page_layout(self.scan)
        neighborhood = next((column for band in layout.bands for column in band.columns
                             if selected <= {id(piece) for piece in column}), ())
        positions = [i for i, piece in enumerate(neighborhood) if id(piece) in selected]
        if not positions or positions[-1] - positions[0] + 1 != len(indices):
            messagebox.showinfo("无法合并", "请选择相邻且位于同栏的文字项。", parent=self.root)
            return
        pieces = tuple(neighborhood[i] for i in positions)
        form = ReviewDialog(self.root, "合并文字块", [
            ("text", "合并后的文字", join_lines(pieces), ()),
            ("kind", "类别", pieces[0].kind.label, tuple(KINDS)),
        ]).result
        if not form or not form["text"].strip():
            return
        rank = min((piece.review_order for piece in pieces if piece.review_order is not None), default=None)
        merged = replace(pieces[0], bounds=enclosing_bounds(pieces), text=form["text"].strip(),
                         kind=KINDS[form["kind"]], confidence=min(piece.confidence for piece in pieces),
                         review_order=rank)
        lines = [merged if i == min(indices) else piece for i, piece in enumerate(self.scan.lines)
                 if i == min(indices) or id(piece) not in selected]
        self.results.selection_remove(self.results.selection())
        self.use_lines(lines)

    def edit_selected(self):
        selected = self.results.selection()
        if len(selected) != 1:
            return
        prefix, key = selected[0].split(":")
        index = int(key)
        if prefix == "block":
            self.add_native(index)
            return
        if prefix == "proposal":
            proposal = self.proposals[index]
            area = proposal.bounds.translated(self.scan.area.left, self.scan.area.top)
            self.set_selection(area)
            self.add_native(proposal=proposal)
            return
        if prefix == "layout":
            self.show_layout_report()
            return
        line = self.scan.lines[index]
        form = ReviewDialog(self.root, "校对文字块", [
            ("text", "文字", line.text, ()), ("kind", "类别", line.kind.label, tuple(KINDS)),
        ]).result
        if form and form["text"].strip():
            lines = list(self.scan.lines)
            lines[index] = replace(line, text=form["text"].strip(), kind=KINDS[form["kind"]])
            self.use_lines(lines)

    def change_labels(self):
        keys = [int(key.split(":")[1]) for key in self.results.selection() if key.startswith("line:")]
        if not keys:
            return
        form = ReviewDialog(self.root, "批量改类别", [
            ("kind", f"{len(keys)} 个文字块", self.scan.lines[keys[0]].kind.label, tuple(KINDS)),
        ]).result
        if form:
            lines = list(self.scan.lines)
            for key in keys:
                lines[key] = replace(lines[key], kind=KINDS[form["kind"]])
            self.use_lines(lines)

    def split_selected(self):
        keys = self.results.selection()
        if len(keys) != 1 or not keys[0].startswith("line:"):
            return
        index = int(keys[0].split(":")[1])
        line = self.scan.lines[index]
        cut = len(line.text) // 2
        position = 50
        if self.selection:
            edge = self.selection.right - self.scan.area.left
            if line.bounds.left < edge < line.bounds.right:
                position = round(100 * (edge - line.bounds.left) / line.bounds.width)
                cut = round(len(line.text) * position / 100)
        form = ReviewDialog(self.root, "拆分同一文字块", [
            ("at", "从左起分割位置（%）", str(position), ()),
            ("left", "左侧文字", line.text[:cut], ()),
            ("leftkind", "左侧类别", line.kind.label, tuple(KINDS)),
            ("right", "右侧文字", line.text[cut:], ()),
            ("rightkind", "右侧类别", TextKind.REVIEW.label, tuple(KINDS)),
        ]).result
        if not form:
            return
        try:
            at = int(form["at"])
        except ValueError:
            at = 0
        b = line.bounds
        edge = b.left + round(b.width * at / 100) if 1 <= at <= 99 else b.left
        if not b.left < edge < b.right or not form["left"].strip() or not form["right"].strip():
            messagebox.showerror("无法拆分", "请输入 1–99 的百分比以及两侧文字。", parent=self.root)
            return
        lines = list(self.scan.lines)
        lines[index:index + 1] = [
            replace(line, bounds=Rect(b.left, b.top, edge, b.bottom), text=form["left"].strip(),
                    kind=KINDS[form["leftkind"]]),
            replace(line, bounds=Rect(edge, b.top, b.right, b.bottom), text=form["right"].strip(),
                    kind=KINDS[form["rightkind"]]),
        ]
        self.use_lines(lines)

    def include_area(self, bounds):
        if self.scan is None:
            self.page.scan = Scan(self.image, self.page.bounds, (), 0)
        elif bounds.intersection(self.scan.area) != bounds:
            area = self.scan.area
            lines = tuple(replace(line, bounds=line.bounds.translated(area.left, area.top))
                          for line in self.scan.lines)
            self.page.scan = Scan(self.image, self.page.bounds, lines, self.scan.seconds)

    def add_manual(self):
        if self.selection is None:
            return
        form = ReviewDialog(self.root, "框选补录文字", [
            ("text", "原图里的文字", "", ()),
            ("kind", "类别", TextKind.HANDWRITTEN.label, tuple(KINDS)),
        ]).result
        if form and form["text"].strip():
            self.remember_review()
            self.include_area(self.selection)
            b = self.selection.relative_to(self.scan.area)
            line = TextLine(b, form["text"].strip(), 1.0, KINDS[form["kind"]])
            self.use_lines(self.scan.lines + (line,), remember=False)

    def add_native(self, index=None, *, proposal=None, force_table=False):
        old = self.page.blocks[index] if index is not None else None
        area = old.bounds if old else self.selection
        if area is None:
            return
        proposed_area = area
        if self.scan and not old and not proposal:
            origin = self.scan.area
            match = area.intersection(origin)
            if match == area:
                proposal = selection_proposal(self.scan, area.relative_to(origin), borderless=True)
        if proposal:
            area = proposal.bounds.translated(self.scan.area.left, self.scan.area.top)
        fmt = old.format if old else proposal.format if proposal else BlockFormat.EQUATION
        kind = old.kind if old else proposal.kind if proposal else TextKind.PRINTED

        def make_block(form):
            return StructuredBlock(area, KINDS[form["kind"]],
                                   BlockFormat(form["format"]), form["body"].strip())

        if fmt == BlockFormat.TABLE or force_table:
            def confirm(value, label):
                table = StructuredBlock(area, KINDS[label], BlockFormat.TABLE, value)
                validate_confirmation(table)
                return table

            value = old.value if old else proposal.value if fmt == BlockFormat.TABLE and proposal else ""
            block = TableReviewDialog(self.root, area.crop(self.image), value, kind.label, tuple(KINDS), confirm).result
        else:
            block = self.review_native_text(make_block, fmt, kind, old, proposal, proposed_area)
        if block is None:
            return
        self.remember_review()
        self.include_area(area)
        blocks = list(self.page.blocks)
        if index is None:
            blocks.append(block)
        else:
            blocks[index] = block
        self.page.blocks = tuple(blocks)
        self.mark_dirty()
        self.show_result()

    def review_native_text(self, make_block, fmt, kind, old, proposal, proposed_area):
        form = ReviewDialog(self.root, "校对选区的表格或公式", [
            ("format", "Word 对象", fmt.value, tuple(item.value for item in BlockFormat)),
            ("kind", "类别", kind.label, tuple(KINDS)),
            ("body", "内容\n表格用 | 分列，^ 向上合并\n首行可填写 [表头]\n行内公式用 $…$ 标记\n公式示例 x^{2} 或 \\frac{1}{2}",
             old.value if old else proposal.value if proposal else "", ()),
        ], validator=lambda values: validate_confirmation(make_block(values)),
            source_image=proposed_area.crop(self.image),
            hint=(proposal.reason if proposal else "") + ("。取消可保留原图。" if proposal else "")).result
        return make_block(form) if form else None

    def remove_block(self):
        selected = self.results.selection()
        if len(selected) == 1 and selected[0].startswith("block:"):
            index = int(selected[0].split(":")[1])
            self.remember_review()
            self.page.blocks = self.page.blocks[:index] + self.page.blocks[index + 1:]
            self.mark_dirty()
            self.refresh_regions()
            self.show_result()

    def add_selection(self, image_only):
        area = self.selection
        if area is None:
            return
        name = simpledialog.askstring(
            "收录到错题集", "选题名称", parent=self.root,
            initialvalue=f"{Path(self.page.name).stem} · 选题 {len(self.draft.selections) + 1}",
        )
        if name and name.strip():
            selection = collect_selection(self.page, area, name.strip(), image_only=image_only)
            self.draft.selections.append(selection)
            self.mark_dirty()
            self.refresh_collection(len(self.draft.selections) - 1)
            result = "已收录原图" if selection.regions == (area,) else f"已收录 · {len(selection.lines)} 个文字块"
            self.status.set(f"{result} · {name.strip()}")

    def refresh_collection(self, selected=None):
        self.clips.delete(0, "end")
        for number, selection in enumerate(self.draft.selections, start=1):
            self.clips.insert("end", f"{number}. {selection.name}")
        if selected is not None and self.draft.selections:
            selected = min(selected, len(self.draft.selections) - 1)
            self.clips.selection_set(selected)
            self.clips.see(selected)
        total = len(self.draft.selections)
        self.collection_title.set(f"{total} 道 · 双击返回源图" if total else "框选题目后收录")
        self.update_buttons()

    def jump_to_selection(self):
        selected = self.clips.curselection()
        if not selected or self.busy:
            return
        clip = self.draft.selections[selected[0]]
        index = next(index for index, page in enumerate(self.draft.pages) if page.id == clip.page_id)
        self.display_page(index)
        self.set_selection(clip.area)

    def move_clip(self, direction):
        index = self.clips.curselection()[0]
        target = index + direction
        if 0 <= target < len(self.draft.selections):
            clip = self.draft.selections.pop(index)
            self.draft.selections.insert(target, clip)
            self.mark_dirty()
            self.refresh_collection(target)

    def remove_clip(self):
        index = self.clips.curselection()[0]
        del self.draft.selections[index]
        self.mark_dirty()
        self.refresh_collection(index)

    def discard_or_save(self):
        if not self.dirty:
            return True
        decision = messagebox.askyesnocancel(
            "保存工作稿", "当前工作稿有未保存修改。是否先保存？", parent=self.root,
        )
        return decision is False or (decision is True and self.save_draft())

    def open_draft(self):
        path = filedialog.askopenfilename(title="打开工作稿", filetypes=DRAFT_FILETYPES, parent=self.root)
        if not path or not self.discard_or_save():
            return
        try:
            candidate = read_draft(Path(path))
        except (OSError, ValueError, Image.DecompressionBombError) as error:
            messagebox.showerror("打开工作稿失败", str(error), parent=self.root)
            return
        self.load_workspace(candidate, Path(path).resolve())
        self.status.set(f"已打开工作稿 · {len(candidate.pages)} 页 · {len(candidate.selections)} 道选题")

    def load_workspace(self, candidate, path, journal=None):
        self.draft, self.draft_path, self.batch_journal = candidate, path, journal
        self.last_layout_report = None
        if candidate.export_order:
            self.target.set(CUSTOM_PAGES)
        elif self.target.get() == CUSTOM_PAGES:
            self.target.set(CURRENT)
        self.image_keys = {image_key(page.image) for page in candidate.pages}
        self.review_history.clear()
        self.ai.receipt = None
        self.phone_page = None
        self.phone_photo_button.pack_forget()
        if self.phone_window and self.phone_window.window.winfo_exists():
            self.phone_window.clear_receipt()
        self.source_paths.clear()
        self.dirty = False
        self.update_draft_label()
        self.display_page(candidate.active_index)
        self.refresh_collection()

    def save_draft(self):
        path = self.draft_path
        if path is None:
            filename = filedialog.asksaveasfilename(
                title="保存工作稿", initialfile="错题工作稿.ocrproj", defaultextension=".ocrproj",
                filetypes=DRAFT_FILETYPES, parent=self.root,
            )
            if not filename:
                return False
            path = Path(filename)

        def write(target):
            with export_path(target) as temporary:
                write_draft(self.draft, temporary)

        if self.save_file(path, write, draft=True):
            self.draft_path = path.resolve()
            self.dirty = False
            self.update_draft_label()
            if self.batch_journal:
                self.release_journal(self.batch_journal)
                self.batch_journal = None
            return True
        return False

    def close(self):
        if self.busy:
            if self.batch:
                self.stop_batch()
            else:
                self.status.set("当前页识别结束后即可关闭")
            return
        if self.document_job is not None:
            self.status.set("正在生成文档，完成后即可关闭")
            return
        if not self.discard_or_save():
            return
        for editor in self.editors:
            if editor.window.winfo_exists() and not editor.close():
                return
        for capture in (self.camera_window, self.phone_window):
            if capture and capture.window.winfo_exists():
                capture.close()
        for viewer in self.document_previews.values():
            if viewer.window.winfo_exists():
                viewer.close()
        self.root.after_cancel(self.phone_poll)
        self.root.destroy()

    def save_crop(self):
        path = filedialog.asksaveasfilename(
            title="另存截图", initialfile=Path(self.page.name).stem + "_截图.png",
            defaultextension=".png", filetypes=[("PNG 图片", "*.png")], parent=self.root,
        )
        if path:
            area, image = self.selection, self.image

            def write(target):
                with export_path(target) as temporary:
                    area.crop(image).save(temporary, format="PNG")

            self.save_file(path, write)

    def change_export_scope(self):
        if self.target.get() == CUSTOM_PAGES and not self.draft.export_order:
            if not self.choose_export_pages_dialog():
                self.target.set(CURRENT)
        self.update_buttons()

    def choose_export_pages_dialog(self):
        if self.busy or not self.draft.pages:
            return False
        selection = ExportPagesDialog(self.root, self.draft.pages, self.draft.export_order).result
        if not selection:
            return False
        if selection != self.draft.export_order:
            self.draft.export_order = selection
            self.mark_dirty()
        self.target.set(CUSTOM_PAGES)
        self.status.set(f"已选 {len(selection)} 张，将按列表顺序合并导出")
        self.update_buttons()
        return True

    def set_page_handout_title(self):
        if not self.page or self.busy:
            return
        value = simpledialog.askstring("本页讲义标题", "最多 100 个字符。留空可恢复自动判断。",
                                       initialvalue=self.page.handout_title, parent=self.root)
        if value is None:
            return
        value = value.strip()
        if len(value) > 100 or any(ord(char) < 32 for char in value):
            messagebox.showinfo("标题格式有误", "请输入不超过 100 个字符的单行标题。", parent=self.root)
        elif value != self.page.handout_title:
            self.page.handout_title = value
            self.mark_dirty()
            self.status.set("已更新本页讲义标题")

    def checked_document_options(self, suffix):
        if self.target.get() != CUSTOM_PAGES:
            return self.document_options(suffix)
        selected = self.draft.export_pages()
        photo = suffix == ".pdf" and self.pdf_style.get() == ORIGINAL_PDF
        pending = [page for page in selected if page.scan is None or
                   (page.quality and page.quality.get("status") == "error")]
        if pending and not photo:
            if messagebox.askyesno("自选图片需要识别",
                                   f"有 {len(pending)} 张自选图片尚未识别成功。现在只识别这些图片吗？"
                                   "\n完成后可按原选页顺序导出。", parent=self.root):
                self.start_batch(only_custom=True)
            return None
        spec = self.document_options(suffix)
        if spec and not photo:
            ready = spec.with_content()
            included = {sheet.source_id for sheet in ready.sheets}
            omitted = [sheet.source_name for sheet in spec.sheets if sheet.source_id not in included]
            if omitted:
                detail = "、".join(omitted[:6])
                if not ready.sheets:
                    messagebox.showinfo("没有可导出的内容", "自选图片在当前提取类别下没有可导出内容。",
                                        parent=self.root)
                    return None
                if not messagebox.askyesno("部分图片没有可导出的内容",
                                           f"{len(omitted)} 张没有符合提取类别的内容（{detail}）。"
                                           "是否合并导出其余图片？", parent=self.root):
                    return None
            return ready
        return spec

    def document_options(self, suffix):
        mode = Extraction(self.extraction.get())
        collection = self.target.get() == COLLECTION
        all_pages = self.target.get() == ALL_PAGES
        custom = self.target.get() == CUSTOM_PAGES
        original = self.pdf_style.get() == ORIGINAL_PDF
        if collection:
            sheets = self.draft.selection_sheets()
        elif all_pages:
            sheets = tuple(page.export_sheet() for page in self.draft.pages if page.scan is not None)
        elif custom:
            pages = self.draft.export_pages()
            if suffix == ".pdf" and original:
                sheets = tuple(page.photo_sheet() for page in pages)
            else:
                if any(page.scan is None for page in pages):
                    return None
                sheets = tuple(page.export_sheet() for page in pages)
        elif self.scan:
            sheets = (self.page.export_sheet(),)
        else:
            return None
        if not sheets:
            return None
        title = "错题集" if collection else "学习讲义" if all_pages else "自选讲义" if custom else Path(self.page.name).stem
        if not (collection or all_pages or custom) and self.scan.area != self.page.bounds:
            title += "_选区"
        handout = None
        if self.export_layout.get() == HANDOUT_LAYOUT and (suffix == ".docx" or not original):
            handout = HandoutOptions(title, self.handout_cover.get(), self.handout_contents.get())
            title += "_A4讲义"
        if mode != Extraction.ALL:
            title += "_" + mode.value
        engine = "word" if self.pdf_style.get() == WORD_PDF else "native"
        if suffix == ".pdf":
            title += "_照片版" if original else "_Word同版" if engine == "word" else "_白底黑字"
        dpi = 150 if original and self.photo_quality.get() == PHOTO_SHARE else None
        return DocumentExport(title + suffix, sheets, mode, original, self.auto_figures.get(), handout, engine, dpi)

    def choose_export_path(self, filename, suffix):
        return filedialog.asksaveasfilename(
            title="导出文档", initialfile=filename, defaultextension=suffix,
            filetypes=[("PDF 文档" if suffix == ".pdf" else "Word 文档", "*" + suffix)],
            parent=self.root,
        )

    @staticmethod
    def filter_status(report):
        issue = next((item for item in report.issues if item["code"] == "content_filtered"), None) if report else None
        return f" · 分类排除 {issue['excluded_lines']} 行" if issue else ""

    def export(self, suffix):
        spec = self.checked_document_options(suffix)
        if spec is None and self.target.get() == CUSTOM_PAGES:
            return
        spec = spec.with_content() if spec else None
        if spec is None or not spec.sheets:
            messagebox.showinfo("没有可导出的内容", "可切换提取类别，或在原图校对中确认文字类别。",
                                parent=self.root)
            return
        path = self.choose_export_path(spec.filename, suffix)
        if path:
            def write(target):
                self.last_layout_report = spec.write(target)
                if self.review_filter.get() == REVIEW_LAYOUT:
                    self.show_result()

            if self.save_file(path, write) and self.last_layout_report is not None:
                report = self.last_layout_report
                self.status.set(f"已保存 · {Path(path).name} · 版面提示 {len(report.issues)} 项"
                                + self.filter_status(report))

    def save_file(self, path, action, *, draft=False):
        target = Path(path)
        resolved = target.resolve()
        if resolved in self.source_paths or (resolved == self.draft_path and not draft):
            messagebox.showerror("无法保存", "请为导出文件选择新的文件名。", parent=self.root)
            return False
        self.root.configure(cursor="watch")
        self.root.update_idletasks()
        try:
            action(target)
        except Exception as error:
            messagebox.showerror("保存失败", str(error), parent=self.root)
            return False
        finally:
            self.root.configure(cursor="")
        self.status.set(f"已保存 · {target.name}")
        return True


if __name__ == "__main__":
    Application(tk.Tk()).root.mainloop()
