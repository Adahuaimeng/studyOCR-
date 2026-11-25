"""Document previews and editors for the right pane or an independent window."""

from io import BytesIO
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from PIL import Image, ImageTk

from app_paths import default_file_directory
from document_edit import WordImage, WordText, render_pdf_page, render_sheet


PDF_FILES = [("PDF 文档", "*.pdf")]
IMAGE_FILES = [("图片", "*.png *.jpg *.jpeg *.bmp *.gif *.tif *.tiff")]


class DocumentWindow:
    def __init__(self, root, title, *, host=None, resource=None, maximized=False, switch_layout=None):
        self.host = host
        self.resource = resource
        if host is None:
            self.window = tk.Toplevel(root)
            self.window.title(title)
            self.window.geometry("1020x720")
            self.window.minsize(850, 580)
            self.window.protocol("WM_DELETE_WINDOW", self.close)
            if maximized:
                self.window.state("zoomed")
        else:
            self.window = ttk.Frame(host)
            self.window.pack(fill="both", expand=True)
        self.dialog_parent = self.window.winfo_toplevel()
        self.layout = ttk.Frame(self.window, padding=8 if host is not None else 12)
        self.layout.pack(fill="both", expand=True)
        self.status = tk.StringVar(master=self.window)
        self.dirty = False
        if host is not None or switch_layout:
            header = ttk.Frame(self.layout)
            header.pack(fill="x", pady=(0, 7))
            if host is not None:
                ttk.Button(header, text="关闭编辑", command=self.close).pack(side="right")
                ttk.Label(header, text=title, width=16).pack(side="left", fill="x", expand=True)
            if switch_layout:
                text = "全屏编辑" if host is not None else "并排编辑"
                ttk.Button(header, text=text, command=lambda: switch_layout(self)).pack(side="right", padx=(0, 5))

    def guard(self, title, action):
        self.window.configure(cursor="watch")
        self.window.update_idletasks()
        try:
            action()
        except Exception as error:
            messagebox.showerror(title, str(error), parent=self.dialog_parent)
            return False
        finally:
            self.window.configure(cursor="")
        return True

    def save_as(self, editor, source, suffix):
        name = Path(self.resource.filename).stem if self.resource else source.stem
        directory = default_file_directory() if self.resource else source.parent
        path = filedialog.asksaveasfilename(
            parent=self.dialog_parent, title="另存副本", defaultextension=suffix,
            initialdir=str(directory), initialfile=f"{name}_编辑{suffix}",
            filetypes=[("Word 文档" if suffix == ".docx" else "PDF 文档", "*" + suffix)],
        )
        if path and self.guard("保存失败", lambda: editor.save(Path(path))):
            self.dirty = False
            self.status.set(f"已保存 · {Path(path).name}")
            return True
        return False

    def close(self):
        if self.dirty:
            action = messagebox.askyesnocancel("保存文档", "当前有未保存的编辑，是否先另存副本？",
                                               parent=self.dialog_parent)
            if action is None or (action and not self.save()):
                return False
        self.dispose()
        return True

    def dispose(self):
        (self.host if self.host is not None else self.window).destroy()
        if self.resource:
            self.resource.close()
            self.resource = None

    @staticmethod
    def button(parent, text, callback):
        button = ttk.Button(parent, text=text, command=callback)
        button.pack(side="left", padx=(0, 5))
        return button

    def footer(self):
        ttk.Label(self.layout, textvariable=self.status, padding=(0, 6, 0, 0)).pack(anchor="w")


class WordWindow(DocumentWindow):
    suffix = ".docx"

    def __init__(self, root, model, *, resource=None, **placement):
        name = resource.filename if resource else model.source.name
        super().__init__(root, f"Word 编辑 · {name}", resource=resource, **placement)
        compact = self.host is not None
        self.model = model
        self.item = None
        self.current_index = None
        self.thumbnail = None
        controls = ttk.Frame(self.layout)
        controls.pack(fill="x", pady=(0, 8))
        self.button(controls, "另存副本" if compact else "另存 Word 副本", self.save)
        self.button(controls, "插入图片", self.insert_image)
        self.replace_button = self.button(controls, "替换图片" if compact else "替换所选图片", self.replace_image)
        self.remove_button = self.button(controls, "移除图片" if compact else "移除所选图片", self.remove_image)

        panes = ttk.Panedwindow(self.layout, orient="vertical" if compact else "horizontal")
        panes.pack(fill="both", expand=True)
        left = ttk.Frame(panes)
        right = ttk.Frame(panes, padding=(0, 8, 0, 0) if compact else (10, 0, 0, 0))
        panes.add(left, weight=2)
        panes.add(right, weight=3)
        ttk.Label(left, text="正文、表格文字与图片").pack(anchor="w", pady=(0, 5))
        list_frame = ttk.Frame(left)
        list_frame.pack(fill="both", expand=True)
        self.entries = tk.Listbox(list_frame, exportselection=False, height=4 if compact else 10)
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.entries.yview)
        self.entries.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.entries.pack(side="left", fill="both", expand=True)
        self.entries.bind("<<ListboxSelect>>", lambda event: self.select_entry())
        self.detail = ttk.Label(right, text="选择要编辑的文字或图片")
        self.detail.pack(anchor="w", pady=(0, 5))

        self.text_frame = ttk.Frame(right)
        self.entry = tk.Text(self.text_frame, wrap="word", padx=8, pady=8,
                             height=7 if compact else 14, width=28 if compact else 80)
        yscroll = ttk.Scrollbar(self.text_frame, orient="vertical", command=self.entry.yview)
        self.entry.configure(yscrollcommand=yscroll.set)
        yscroll.pack(side="right", fill="y")
        self.entry.pack(side="left", fill="both", expand=True)
        self.apply = ttk.Button(right, text="应用文字修改", command=self.apply_text)

        self.image_frame = ttk.Frame(right)
        self.picture = ttk.Label(self.image_frame, anchor="center")
        self.picture.pack(fill="both", expand=True)
        settings = ttk.Frame(self.image_frame)
        settings.pack(fill="x")
        ttk.Label(settings, text="宽度（厘米）").pack(side="left")
        self.width = tk.StringVar()
        ttk.Entry(settings, textvariable=self.width, width=8).pack(side="left", padx=6)
        self.button(settings, "调整尺寸", self.resize_image)
        self.footer()
        self.status.set(name)
        self.refresh()

    def refresh(self, select=0):
        self.items = self.model.items()
        self.entries.delete(0, "end")
        for item in self.items:
            self.entries.insert("end", self.label(item))
        if self.items:
            index = min(select, len(self.items) - 1)
            self.entries.selection_set(index)
            self.entries.see(index)
            self.show(index)
        else:
            self.show(None)

    @staticmethod
    def label(item):
        preview = item.text.strip().replace("\n", " ") if isinstance(item, WordText) else "［图片］"
        return f"{item.location}   {preview[:75]}"

    def select_entry(self):
        selection = self.entries.curselection()
        if selection and selection[0] != self.current_index:
            self.apply_text()
            self.show(selection[0])

    def show(self, index):
        self.current_index = index
        self.item = self.items[index] if index is not None else None
        self.text_frame.pack_forget()
        self.apply.pack_forget()
        self.image_frame.pack_forget()
        image = isinstance(self.item, WordImage)
        state = "normal" if image else "disabled"
        self.replace_button.configure(state=state)
        self.remove_button.configure(state=state)
        if isinstance(self.item, WordText):
            self.detail.configure(text=self.item.location)
            self.entry.delete("1.0", "end")
            self.entry.insert("1.0", self.item.text)
            self.text_frame.pack(fill="both", expand=True)
            self.apply.pack(anchor="w", pady=(6, 0))
        elif image:
            self.detail.configure(text=f"{self.item.location} · 图片")
            try:
                with Image.open(BytesIO(self.item.blob)) as preview:
                    preview.thumbnail((350, 200) if self.host is not None else (490, 440))
                    self.thumbnail = ImageTk.PhotoImage(preview.convert("RGB"))
                self.picture.configure(image=self.thumbnail, text="")
            except OSError:
                self.picture.configure(image="", text="此图片格式无法预览")
            self.width.set(f"{self.item.width_cm:.2f}")
            self.image_frame.pack(fill="both", expand=True)
        else:
            self.detail.configure(text="此文档没有可编辑的普通正文或图片，可插入图片")

    def apply_text(self):
        if not isinstance(self.item, WordText):
            return
        value = self.entry.get("1.0", "end-1c")
        if value == self.item.text:
            return
        self.item.update(value)
        self.dirty = True
        self.status.set("文字已修改")
        current = self.current_index
        selected = self.entries.curselection()
        self.entries.delete(current)
        self.entries.insert(current, self.label(self.item))
        if selected:
            self.entries.selection_set(selected[0])

    def image_path(self):
        path = filedialog.askopenfilename(parent=self.dialog_parent, title="选择图片", filetypes=IMAGE_FILES)
        return Path(path) if path else None

    def insert_image(self):
        self.apply_text()
        path = self.image_path()
        if path and self.guard("插图失败", lambda: self.model.insert_image(
            path, self.item.paragraph if self.item else None,
        )):
            self.dirty = True
            self.refresh((self.current_index if self.current_index is not None else -1) + 1)

    def replace_image(self):
        path = self.image_path()
        if path and self.guard("替换图片失败", lambda: self.item.replace(path)):
            self.dirty = True
            self.show(self.current_index)

    def resize_image(self):
        if self.guard("图片尺寸无效", lambda: self.item.resize(float(self.width.get()))):
            self.dirty = True
            self.show(self.current_index)

    def remove_image(self):
        self.model.remove_image(self.item)
        self.dirty = True
        self.refresh(self.current_index)

    def save(self):
        self.apply_text()
        return self.save_as(self.model, self.model.source, ".docx")

    def close(self):
        self.apply_text()
        return super().close()

    def handoff(self):
        self.apply_text()
        return self.current_index or 0


def page_numbers(value, maximum):
    numbers = {}
    for part in value.replace("，", ",").split(","):
        parts = part.strip().split("-", maxsplit=1)
        try:
            first, last = int(parts[0]), int(parts[-1])
        except ValueError as error:
            raise ValueError("页码示例 1、1-3 或 1,3-5") from error
        if not 1 <= first <= last <= maximum:
            raise ValueError("页码超出当前 PDF 范围")
        numbers.update(dict.fromkeys(range(first - 1, last)))
    return tuple(numbers)


class PdfWindow(DocumentWindow):
    suffix = ".pdf"

    def __init__(self, root, model, preview_factory, import_to_ocr, *, resource=None, **placement):
        name = resource.filename if resource else model.original.name
        super().__init__(root, f"PDF 编辑 · {name}", resource=resource, **placement)
        compact = self.host is not None
        self.model = model
        self.import_to_ocr = import_to_ocr
        self.index = 0
        self.selected_area = None
        self.undo_button = None
        self.cache = {}
        filebar = ttk.Frame(self.layout)
        filebar.pack(fill="x", pady=(0, 5))
        self.button(filebar, "另存副本" if compact else "另存 PDF 副本", self.save)
        self.button(filebar, "合并 PDF", self.append_files)
        self.button(filebar, "选页送入 OCR", self.send_ocr)
        self.undo_button = self.button(filebar, "撤销", self.undo)

        panes = ttk.Panedwindow(self.layout, orient="vertical" if compact else "horizontal")
        panes.pack(fill="both", expand=True)
        sidebar = ttk.Frame(panes)
        pane = ttk.Frame(panes, padding=(0, 6, 0, 0) if compact else (8, 0, 0, 0))
        panes.add(sidebar, weight=0 if compact else 1)
        panes.add(pane, weight=5)
        page_list = ttk.Frame(sidebar)
        page_list.pack(fill="both", expand=True, pady=(0, 5))
        self.list = tk.Listbox(page_list, exportselection=False, width=24, height=3 if compact else 10)
        scroll = ttk.Scrollbar(page_list, orient="vertical", command=self.list.yview)
        self.list.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.list.pack(side="left", fill="both", expand=True)
        self.list.bind("<<ListboxSelect>>", lambda event: self.select())
        rows = ttk.Frame(sidebar)
        rows.pack(fill="x")
        self.up = self.button(rows, "上移", lambda: self.move(-1))
        self.down = self.button(rows, "下移", lambda: self.move(1))
        self.delete_button = self.button(rows, "删页", self.delete)

        toolbar = ttk.Frame(pane)
        toolbar.pack(fill="x", pady=(0, 5))
        self.button(toolbar, "左转 90°", lambda: self.rotate(-90))
        self.button(toolbar, "右转 90°", lambda: self.rotate(90))
        self.crop_button = self.button(toolbar, "裁剪到选区", self.crop)
        if compact:
            toolbar = ttk.Frame(pane)
            toolbar.pack(fill="x", pady=(0, 5))
        self.highlight = self.button(toolbar, "高亮选区", lambda: self.mark(False))
        self.comment = self.button(toolbar, "添加批注", lambda: self.mark(True))
        self.preview = preview_factory(pane, self.choose_area)
        self.preview.pack(fill="both", expand=True)
        self.footer()
        self.refresh()

    def select(self):
        selected = self.list.curselection()
        if selected and selected[0] != self.index:
            self.index = selected[0]
            self.show()

    def refresh(self, index=0):
        self.index = min(index, len(self.model.pages) - 1)
        self.list.delete(0, "end")
        for number, sheet in enumerate(self.model.pages, 1):
            self.list.insert("end", f"{number}. {sheet.source.stem} / {sheet.number + 1}")
        self.list.selection_set(self.index)
        self.list.see(self.index)
        self.show()

    def show(self):
        sheet = self.model.pages[self.index]

        def show_preview():
            if sheet not in self.cache:
                if len(self.cache) == 4:
                    del self.cache[next(iter(self.cache))]
                self.cache[sheet] = render_sheet(sheet)
            self.preview.show_image(self.cache[sheet])

        self.choose_area(None)
        if self.guard("预览 PDF 失败", show_preview):
            self.status.set(f"第 {self.index + 1} / {len(self.model.pages)} 页 · 拖框裁剪或批注")
        else:
            self.preview.show_image(None)
        self.update_controls()

    def choose_area(self, region):
        self.selected_area = region
        self.update_controls()

    def update_controls(self):
        for button, enabled in (
            (self.crop_button, self.selected_area is not None),
            (self.highlight, self.selected_area is not None),
            (self.comment, self.selected_area is not None),
            (self.up, self.index > 0),
            (self.down, self.index < len(self.model.pages) - 1),
            (self.delete_button, len(self.model.pages) > 1),
            (self.undo_button, bool(self.model.history)),
        ):
            button.configure(state="normal" if enabled else "disabled")

    def edited(self, index):
        self.dirty = True
        self.refresh(index)

    def append_files(self):
        files = filedialog.askopenfilenames(parent=self.dialog_parent, title="追加 PDF", filetypes=PDF_FILES)
        if files and self.guard("合并失败", lambda: self.model.add(files, self.index)):
            self.edited(self.index)

    def rotate(self, degrees):
        self.model.rotate(self.index, degrees)
        self.edited(self.index)

    def delete(self):
        self.model.delete(self.index)
        self.edited(self.index)

    def move(self, offset):
        target = self.index + offset
        self.model.move(self.index, target)
        self.edited(target)

    def undo(self):
        if self.model.history:
            self.edited(self.model.undo())

    def crop(self):
        self.model.crop(self.index, self.selected_area, self.preview.image.size)
        self.edited(self.index)

    def mark(self, note):
        text = None
        if note:
            text = simpledialog.askstring("添加批注", "批注内容", parent=self.dialog_parent)
            if not text or not text.strip():
                return
        self.model.mark(self.index, self.selected_area, self.preview.image.size, text)
        self.edited(self.index)

    def send_ocr(self):
        value = simpledialog.askstring(
            "选页送入 OCR", "页码例 1,3-5。按当前 PDF 页序选择。",
            initialvalue=str(self.index + 1), parent=self.dialog_parent,
        )
        if not value:
            return

        def send():
            numbers = page_numbers(value, len(self.model.pages))
            imported = 0
            for number in numbers:
                page = self.model.pages[number]
                image = render_sheet(page, max_side=3100, draw_annotations=False)
                name = f"{page.source.stem}_第{page.number + 1}页.png"
                if not self.import_to_ocr(name, image, page.source):
                    break
                imported += 1
            self.status.set(f"已送入 OCR {imported} 页")
            if imported:
                self.window.master.winfo_toplevel().lift()

        self.guard("导入 OCR 失败", send)

    def save(self):
        return self.save_as(self.model, self.model.original, ".pdf")

    def handoff(self):
        return self.index


class DocumentPreview:
    def __init__(self, host, resource, preview_factory, save_snapshot, review_layout):
        import pypdfium2

        self.window = host
        self.resource = resource
        self.index = 0
        self.doc = pypdfium2.PdfDocument(resource.preview_path)
        try:
            self.build(preview_factory, save_snapshot, review_layout)
            self.show(0)
        except Exception:
            self.doc.close()
            raise

    def build(self, preview_factory, save_snapshot, review_layout):
        header = ttk.Frame(self.window, padding=(6, 6))
        header.pack(fill="x")
        ttk.Button(header, text="关闭预览", command=self.close).pack(side="right")
        ttk.Button(header, text="保存此版本", command=lambda: save_snapshot(self.resource)).pack(side="right", padx=5)
        if self.resource.report is not None:
            count = len(self.resource.report.issues)
            ttk.Button(header, text=f"版面检查 ({count})",
                       command=lambda: review_layout(self.resource.report, self)).pack(side="right")
        name = Path(self.resource.filename).name
        if self.resource.preview_is_handout_pdf:
            name += " · 讲义 PDF 示意"
        ttk.Label(header, text=name, width=20).pack(side="left", fill="x", expand=True)
        tools = ttk.Frame(self.window, padding=(6, 0, 6, 6))
        tools.pack(fill="x")
        self.previous = DocumentWindow.button(tools, "上一页", lambda: self.move(-1))
        self.next = DocumentWindow.button(tools, "下一页", lambda: self.move(1))
        self.page_label = ttk.Label(tools)
        self.page_label.pack(side="left", padx=(2, 8))
        self.zoom = ttk.Combobox(tools, values=("适应窗口", "放大 1.5 倍", "放大 2 倍"), state="readonly", width=10)
        self.zoom.current(0)
        self.zoom.bind("<<ComboboxSelected>>", lambda event: self.set_zoom())
        self.zoom.pack(side="left")
        self.image = preview_factory(self.window, None, selectable=False)
        self.image.pack(fill="both", expand=True)

    def set_zoom(self):
        self.image.set_zoom((1, 1.5, 2)[self.zoom.current()])

    def show(self, index):
        page = self.doc[index]
        try:
            picture = render_pdf_page(page, 1800)
        finally:
            page.close()
        self.index = index
        self.image.show_image(picture)
        self.set_zoom()
        self.page_label.configure(text=f"{index + 1} / {len(self.doc)}")
        self.previous.configure(state="normal" if index > 0 else "disabled")
        self.next.configure(state="normal" if index + 1 < len(self.doc) else "disabled")

    def move(self, step):
        index = self.index + step
        if 0 <= index < len(self.doc):
            try:
                self.show(index)
            except Exception as error:
                messagebox.showerror("预览页面失败", str(error), parent=self.window.winfo_toplevel())

    def close(self):
        self.doc.close()
        self.window.destroy()
        self.resource.close()
