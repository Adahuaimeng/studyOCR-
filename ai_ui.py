"""Confirm the scope of an AI request and review suggestions before applying them."""

from dataclasses import dataclass, replace
from queue import Empty, Queue
from threading import Thread
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

from PIL import Image, ImageTk

from ai_assist import (
    AIConfig, CORRECT, FORMULA, MAX_LINES, MAX_TEXT, TABLE, TRANSCRIBE, WindowsCredentials,
    api_endpoint, request_suggestions,
)
from ocr_core import Rect, Scan, TextKind, TextLine
from structured import validate_block
from study import BlockFormat, StructuredBlock, StudyPage


KINDS = {kind.label: kind for kind in (TextKind.REVIEW, TextKind.PRINTED, TextKind.HANDWRITTEN)}


class AssistDialog(simpledialog.Dialog):
    confirm_label = "确认"
    dialog_refs = ()

    def buttonbox(self):
        buttons = ttk.Frame(self, padding=8)
        ttk.Button(buttons, text=self.confirm_label, command=self.ok).pack(side="left", padx=4)
        ttk.Button(buttons, text="取消", command=self.cancel).pack(side="left", padx=4)
        buttons.pack()
        self.bind("<Escape>", self.cancel)

    def error(self, message):
        messagebox.showerror(self.title(), message, parent=self)
        return False

    def destroy(self):
        super().destroy()
        # Release Tk variables and image references on the UI thread before an API worker starts.
        for name in self.dialog_refs:
            self.__dict__.pop(name, None)


class SettingsDialog(AssistDialog):
    confirm_label = "保存设置"
    dialog_refs = ("endpoint", "model", "key", "enabled", "clear_key")

    def __init__(self, root, config, save):
        self.config, self.save = config, save
        super().__init__(root, "AI 联网辅助设置")

    def body(self, parent):
        self.endpoint = tk.StringVar(value=self.config.endpoint)
        self.model = tk.StringVar(value=self.config.model)
        self.key = tk.StringVar()
        self.enabled = tk.BooleanVar(value=self.config.enabled)
        self.clear_key = tk.BooleanVar()
        fields = (("API 地址", self.endpoint), ("模型", self.model), ("API 密钥", self.key))
        for row, (name, variable) in enumerate(fields):
            ttk.Label(parent, text=name).grid(row=row, column=0, sticky="w", padx=8, pady=4)
            entry = ttk.Entry(parent, textvariable=variable, width=54, show="*" if row == 2 else "")
            entry.grid(row=row, column=1, sticky="ew", padx=8, pady=4)
        ttk.Label(parent, text="支持 OpenAI 兼容地址，例如 https://服务商地址/v1。密钥留空会保留该地址的旧密钥。",
                  wraplength=500).grid(row=3, column=0, columnspan=2, sticky="w", padx=8, pady=5)
        ttk.Checkbutton(parent, text="清除该地址保存的密钥", variable=self.clear_key).grid(
            row=4, column=0, columnspan=2, sticky="w", padx=8, pady=3)
        ttk.Checkbutton(parent, text="启用 AI 联网辅助", variable=self.enabled).grid(
            row=5, column=0, columnspan=2, sticky="w", padx=8, pady=3)
        return parent

    def validate(self):
        try:
            endpoint = self.endpoint.get().strip()
            model = self.model.get().strip()
            raw_key = self.key.get().strip()
            key = "" if self.clear_key.get() else raw_key or None
            if endpoint or model or key is not None or self.enabled.get():
                endpoint = api_endpoint(endpoint)
                if not model or len(model) > 200:
                    raise ValueError("请填写有效的模型名称。")
            self.save(AIConfig(endpoint, model, self.enabled.get()), key)
        except (OSError, ValueError) as error:
            return self.error(str(error))
        return True


def set_text(widget, value, *, readonly=True):
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    widget.insert("1.0", value)
    if readonly:
        widget.configure(state="disabled")


class SendDialog(AssistDialog):
    confirm_label = "发送到 API"
    dialog_refs = ("task", "send_image", "include_image", "photo", "preview")

    def __init__(self, root, config, originals, crop, tasks):
        self.config, self.originals, self.crop, self.tasks = config, originals, crop, tasks
        super().__init__(root, "确认发送范围")

    def body(self, parent):
        self.task = tk.StringVar(value=self.tasks[0])
        self.send_image = tk.BooleanVar(value=not self.originals)
        ttk.Label(parent, text=f"发送到 {self.config.endpoint}\n模型 {self.config.model}",
                  wraplength=550).pack(anchor="w", pady=(0, 6))
        choice = ttk.Combobox(parent, textvariable=self.task, values=self.tasks, state="readonly", width=20)
        choice.pack(anchor="w", pady=(0, 6))
        choice.bind("<<ComboboxSelected>>", self.change_task)
        ttk.Label(parent, text="下方是本次发送的 OCR 文字。截图仅发送勾选时预览的范围。").pack(anchor="w")
        words = ScrolledText(parent, wrap="word", width=68, height=6)
        words.pack(fill="both", expand=True, pady=4)
        set_text(words, "\n".join(f"{index + 1}. {text}" for index, text in enumerate(self.originals))
                 or "无 OCR 文字")
        if self.crop is not None:
            self.include_image = ttk.Checkbutton(parent, text=f"附带选区截图（{self.crop.width} × {self.crop.height}）",
                                                 variable=self.send_image, command=self.show_image)
            self.include_image.pack(anchor="w", pady=4)
            small = self.crop.copy()
            small.thumbnail((460, 200))
            self.photo = ImageTk.PhotoImage(small)
            self.preview = ttk.Label(parent, image=self.photo)
            self.change_task()
        return choice

    def change_task(self, _event=None):
        required = self.task.get() == TRANSCRIBE or not self.originals
        if self.crop is not None:
            if required:
                self.send_image.set(True)
            self.include_image.configure(state="disabled" if required else "normal")
            self.show_image()

    def show_image(self):
        if self.send_image.get():
            self.preview.pack(pady=(3, 0))
        else:
            self.preview.pack_forget()

    def apply(self):
        self.result = (self.task.get(), self.send_image.get())


class SuggestionsDialog(AssistDialog):
    confirm_label = "应用所选建议"
    dialog_refs = ("kind", "entries", "before", "after")

    def __init__(self, root, task, originals, suggestions, validate_object):
        self.task, self.originals, self.suggestions = task, originals, suggestions
        self.validate_object = validate_object
        self.ids = sorted(suggestions)
        super().__init__(root, "核对 AI 建议")

    def body(self, parent):
        self.kind = tk.StringVar(value=TextKind.REVIEW.label)
        if self.task == CORRECT:
            toolbar = ttk.Frame(parent)
            toolbar.pack(fill="x", pady=(0, 6))
            ttk.Label(toolbar, text="勾选要应用的文字块").pack(side="left")
            ttk.Button(toolbar, text="全选建议", command=lambda: self.entries.selection_set(0, "end")).pack(side="right")
            self.entries = tk.Listbox(parent, height=min(6, len(self.ids)), exportselection=False, selectmode="extended")
            self.entries.pack(fill="x")
            for index in self.ids:
                self.entries.insert("end", f"{index + 1}. {self.originals[index][:60]}")
            self.entries.bind("<<ListboxSelect>>", self.show_suggestion)
            self.entries.selection_set(0)
        else:
            row = ttk.Frame(parent)
            row.pack(fill="x", pady=5)
            ttk.Label(row, text="文字类别").pack(side="left")
            ttk.Combobox(row, textvariable=self.kind, values=tuple(KINDS), state="readonly", width=13).pack(
                side="left", padx=6)
        panes = ttk.Frame(parent)
        panes.pack(fill="both", expand=True, pady=(6, 0))
        self.before = self.text_panel(panes, "原 OCR 文字")
        self.after = self.text_panel(panes, "建议" if self.task == CORRECT else "建议（可直接修改）")
        self.show_suggestion()
        return self.entries if self.task == CORRECT else self.after

    @staticmethod
    def text_panel(parent, label):
        panel = ttk.Frame(parent, padding=(4, 0))
        panel.pack(side="left", fill="both", expand=True)
        ttk.Label(panel, text=label).pack(anchor="w", pady=(0, 4))
        widget = ScrolledText(panel, width=33, height=9, wrap="word")
        widget.pack(fill="both", expand=True)
        return widget

    def show_suggestion(self, _event=None):
        if self.task == CORRECT:
            selection = self.entries.curselection()
            if selection:
                index = self.ids[selection[-1]]
                set_text(self.before, self.originals[index])
                set_text(self.after, self.suggestions[index])
        else:
            set_text(self.before, "\n".join(self.originals) or "无 OCR 文字")
            set_text(self.after, self.suggestions[0], readonly=False)

    def validate(self):
        if self.task == CORRECT:
            return bool(self.entries.curselection()) or self.error("请先选择要应用的建议。")
        try:
            self.validate_object(self.after.get("1.0", "end-1c").strip(), KINDS[self.kind.get()])
        except ValueError as error:
            return self.error(str(error))
        return True

    def apply(self):
        if self.task == CORRECT:
            self.result = {self.ids[index]: self.suggestions[self.ids[index]] for index in self.entries.curselection()}
        else:
            self.result = (self.after.get("1.0", "end-1c").strip(), KINDS[self.kind.get()])


@dataclass(frozen=True)
class Scope:
    page: StudyPage
    scan: Scan | None
    blocks: tuple[StructuredBlock, ...]
    image: Image.Image
    area: Rect | None
    indices: tuple[int, ...]

    @property
    def texts(self):
        return tuple(self.scan.lines[index].text for index in self.indices) if self.scan else ()

    def still_current(self, app):
        return app.page is self.page and app.scan is self.scan and app.page.blocks is self.blocks and app.image is self.image


class Assistant:
    def __init__(self, app, path):
        self.app, self.path = app, path
        self.config = AIConfig.load(path)
        self.inbox = None
        self.receipt = None

    def settings(self):
        SettingsDialog(self.app.root, self.config, self.save_settings)

    def save_settings(self, config, key):
        credentials = WindowsCredentials() if key is not None else None
        if credentials:
            previous = credentials.get(config.endpoint)
            credentials.set(config.endpoint, key)
        try:
            config.save(self.path)
        except OSError:
            if credentials:
                credentials.set(config.endpoint, previous)
            raise
        self.config = config
        self.app.update_buttons()

    def can_request(self):
        return self.config.enabled and self.inbox is None and (
            self.app.selection is not None or (self.app.scan
                and any(key.startswith("line:") for key in self.app.results.selection()))
        )

    def scope(self):
        app, scan, area = self.app, self.app.scan, self.app.selection
        indices = ()
        if scan and area:
            local_area = area.relative_to(scan.area)
            indices = tuple(index for index, line in enumerate(scan.lines)
                            if line.kind != TextKind.OMIT and line.bounds.intersection(local_area) == line.bounds)
        elif scan:
            indices = tuple(int(key.split(":")[1]) for key in app.results.selection() if key.startswith("line:"))
        return Scope(app.page, scan, app.page.blocks, app.image, area, indices)

    def request(self):
        if not self.can_request() or self.app.busy:
            return
        scope = self.scope()
        originals = scope.texts
        tasks = [CORRECT] if originals else []
        if scope.area:
            if not scope.scan or not any(
                line.bounds.intersection(scope.area.relative_to(scope.scan.area))
                for line in scope.scan.lines if line.kind != TextKind.OMIT
            ):
                tasks.append(TRANSCRIBE)
            if not any(block.bounds.intersection(scope.area) and block.bounds != scope.area for block in scope.blocks):
                tasks.extend((TABLE, FORMULA))
        if not tasks or len(originals) > MAX_LINES or sum(map(len, originals)) > MAX_TEXT:
            messagebox.showinfo("请调整选区", "请框出更小的区域，或只选要校对的文字块。", parent=self.app.root)
            return
        config = self.config
        crop = scope.area.crop(scope.image) if scope.area else None
        decision = SendDialog(self.app.root, config, originals, crop, tasks).result
        if not decision:
            return
        try:
            key = WindowsCredentials().get(config.endpoint)
        except OSError as error:
            messagebox.showerror("读取 API 密钥失败", str(error), parent=self.app.root)
            return
        task, send_image = decision
        inbox = self.inbox = Queue(maxsize=1)
        self.app.status.set("正在请求 AI 建议…")
        self.app.update_buttons()

        def work():
            try:
                inbox.put(request_suggestions(config, key, task, originals, crop if send_image else None))
            except Exception as error:
                inbox.put(error)

        Thread(target=work, daemon=True).start()
        self.app.root.after(120, self.finish, scope, task)

    def finish(self, scope, task):
        app = self.app
        if app.busy or app.root.tk.call("grab", "current", app.root._w):
            app.root.after(120, self.finish, scope, task)
            return
        try:
            result = self.inbox.get_nowait()
        except Empty:
            app.root.after(120, self.finish, scope, task)
            return
        self.inbox = None
        app.update_buttons()
        if isinstance(result, Exception):
            app.status.set("AI 请求失败")
            text = str(result) if isinstance(result, ValueError) else "请求或处理 AI 建议时发生错误。"
            messagebox.showerror("AI 联网辅助", text, parent=app.root)
            return
        if not scope.still_current(app):
            app.status.set("原稿已变化，请重新选择需要 AI 辅助的范围")
            return
        if not result:
            app.status.set("AI 已完成检查，没有文字修改建议")
            return
        app.status.set("请核对 AI 建议后选择应用")
        choice = SuggestionsDialog(app.root, task, scope.texts, result,
                                   lambda value, kind: self.make_object(scope, task, value, kind)).result
        if choice and scope.still_current(app):
            self.apply(scope, task, choice)
        else:
            app.status.set("已取消 AI 编辑")

    @staticmethod
    def make_object(scope, task, value, kind):
        if not value or len(value) > MAX_TEXT:
            raise ValueError("内容为空或过长。")
        if task == TRANSCRIBE:
            return TextLine(scope.area, value, 1.0, kind)
        block = StructuredBlock(scope.area, kind, BlockFormat.TABLE if task == TABLE else BlockFormat.EQUATION, value)
        validate_block(block)
        return block

    def apply(self, scope, task, choice):
        app = self.app
        if task == CORRECT:
            lines = list(scope.scan.lines)
            for key, value in choice.items():
                index = scope.indices[key]
                lines[index] = replace(lines[index], text=value)
            app.use_lines(lines)
        else:
            item = self.make_object(scope, task, *choice)
            app.include_area(scope.area)
            if task == TRANSCRIBE:
                line = replace(item, bounds=scope.area.relative_to(app.scan.area))
                app.use_lines(app.scan.lines + (line,))
            else:
                app.page.blocks = tuple(block for block in app.page.blocks if block.bounds != scope.area) + (item,)
                app.mark_dirty()
                app.show_result()
        self.receipt = (scope, app.scan, app.page.blocks)
        app.status.set(f"已应用 AI {task}，可从编辑菜单撤销")
        app.update_buttons()

    def can_undo(self):
        if not self.receipt:
            return False
        scope, scan, blocks = self.receipt
        app = self.app
        return app.page is scope.page and app.scan is scan and app.page.blocks is blocks and app.image is scope.image

    def undo(self):
        if not self.can_undo():
            return
        scope = self.receipt[0]
        self.receipt = None
        scope.page.scan, scope.page.blocks = scope.scan, scope.blocks
        self.app.mark_dirty()
        self.app.show_result()
        self.app.status.set("已撤销上次 AI 修改")
