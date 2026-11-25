"""Choose source pages and their order for a single document."""

import tkinter as tk
from tkinter import simpledialog, ttk


class ExportPagesDialog(simpledialog.Dialog):
    def __init__(self, parent, pages, selected):
        self.pages = {page.id: page for page in pages}
        self.numbers = {page.id: i for i, page in enumerate(pages, 1)}
        self.selected = set(selected or self.pages)
        self.order = list(dict.fromkeys((*selected, *self.pages)))
        super().__init__(parent, "自选导出页面")

    def body(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        ttk.Label(parent, text="勾选要导出的图片。列表从上到下是合并顺序。").grid(
            row=0, column=0, sticky="w", pady=(0, 7))
        self.table = ttk.Treeview(parent, columns=("use", "page", "name", "ocr"), show="headings",
                                  selectmode="browse", height=11)
        for key, label, width in (("use", "导出", 55), ("page", "导入序号", 75),
                                  ("name", "图片", 285), ("ocr", "状态", 110)):
            self.table.heading(key, text=label)
            self.table.column(key, width=width, stretch=key == "name", anchor="w")
        self.table.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(parent, orient="vertical", command=self.table.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.table.configure(yscrollcommand=scroll.set)
        buttons = ttk.Frame(parent)
        buttons.grid(row=2, column=0, sticky="ew", pady=(7, 0))
        for label, command in (("勾选／取消", self.toggle_current), ("上移", lambda: self.move(-1)),
                               ("下移", lambda: self.move(1)), ("全选", lambda: self.select_all(True)),
                               ("清空", lambda: self.select_all(False)), ("恢复导入顺序", self.restore_order)):
            ttk.Button(buttons, text=label, command=command).pack(side="left", padx=(0, 4))
        self.caption = ttk.Label(parent)
        self.caption.grid(row=3, column=0, sticky="w", pady=(7, 0))
        self.table.bind("<ButtonRelease-1>", self.toggle_cell)
        self.table.bind("<space>", lambda _event: self.toggle_current())
        self.refresh(self.order[0] if self.order else None)
        return self.table

    def refresh(self, focus=None):
        self.table.delete(*self.table.get_children())
        for key in self.order:
            page = self.pages[key]
            if page.quality and page.quality.get("status") == "error":
                status = "识别失败"
            elif page.scan is None:
                status = "未识别"
            elif page.scan.area != page.bounds:
                status = "已识别选区"
            else:
                status = "已识别"
            self.table.insert("", "end", iid=key, values=(
                "✓" if key in self.selected else "—", self.numbers[key], page.name, status))
        if focus:
            self.table.selection_set(focus)
            self.table.focus(focus)
            self.table.see(focus)
        self.caption.configure(text=f"已勾选 {len(self.selected)}／{len(self.pages)} 张")

    def toggle_cell(self, event):
        if self.table.identify_column(event.x) == "#1" and self.table.identify_row(event.y):
            key = self.table.identify_row(event.y)
            self.table.selection_set(key)
            self.toggle_current()

    def toggle_current(self):
        if selected := self.table.selection():
            self.selected.symmetric_difference_update(selected)
            self.refresh(selected[0])
        return "break"

    def move(self, direction):
        if selected := self.table.selection():
            key = selected[0]
            at = self.order.index(key)
            target = at + direction
            if 0 <= target < len(self.order):
                self.order[at], self.order[target] = self.order[target], self.order[at]
                self.refresh(key)

    def select_all(self, enabled):
        self.selected = set(self.pages) if enabled else set()
        self.refresh(next(iter(self.table.selection()), None))

    def restore_order(self):
        focus = next(iter(self.table.selection()), None)
        self.order = list(self.pages)
        self.refresh(focus)

    def validate(self):
        if self.selected:
            return True
        self.caption.configure(text="请至少勾选一张图片。")
        return False

    def apply(self):
        self.result = tuple(key for key in self.order if key in self.selected)
