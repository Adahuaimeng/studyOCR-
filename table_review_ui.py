"""Review native table cells against their source crop."""

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from PIL import ImageTk

from ocr_core import Rect
from structured import HEADER, cell_crop, crop_token, inline_fragments, table_data


class TableReviewDialog(simpledialog.Dialog):
    def __init__(self, parent, image, value, kind, kinds, validator):
        self.image, self.value = image, value
        self.kind, self.kinds, self.validator = kind, kinds, validator
        self.active = (0, 0)
        self.anchor = self.crop = None
        super().__init__(parent, "逐格校对表格")

    def body(self, parent):
        self.image.thumbnail((650, 210))
        self.photo = ImageTk.PhotoImage(self.image, master=self)
        self.source = tk.Canvas(parent, width=self.image.width, height=self.image.height, highlightthickness=0)
        self.source.grid(sticky="n", pady=(0, 4))
        self.source.create_image(0, 0, anchor="nw", image=self.photo)
        self.source.bind("<ButtonPress-1>", self.begin_crop)
        self.source.bind("<B1-Motion>", self.draw_crop)
        self.source.bind("<ButtonRelease-1>", self.draw_crop)
        ttk.Label(parent, text="双击格内可选中文字。用 $…$ 标记公式。选中单元格后可框住上方原图区域。").grid(
            sticky="w", pady=4)

        controls = ttk.Frame(parent)
        controls.grid(sticky="ew")
        rows, header = table_data(self.value) if self.value.strip() else ((("", ""), ("", "")), False)
        self.header = tk.BooleanVar(self, header)
        self.category = tk.StringVar(self, self.kind)
        ttk.Checkbutton(controls, text="首行作表头", variable=self.header).pack(side="left", padx=2)
        ttk.Combobox(controls, width=8, state="readonly", values=self.kinds,
                     textvariable=self.category).pack(side="left", padx=2)
        for label, command in (
            ("加行", lambda: self.resize(0, 1)), ("减末行", lambda: self.resize(0, -1)),
            ("加列", lambda: self.resize(1, 1)), ("减末列", lambda: self.resize(1, -1)),
            ("向上合并", self.merge_up), ("拆分", self.unmerge), ("放入原图截图", self.assign_crop),
        ):
            ttk.Button(controls, text=label, command=command).pack(side="left", padx=2)

        viewport = ttk.Frame(parent)
        viewport.grid(sticky="nsew", pady=5)
        self.canvas = tk.Canvas(viewport, width=680, height=218, highlightthickness=0)
        ybar = ttk.Scrollbar(viewport, command=self.canvas.yview)
        xbar = ttk.Scrollbar(viewport, command=self.canvas.xview, orient="horizontal")
        self.canvas.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        self.grid_frame = ttk.Frame(self.canvas)
        self.canvas.create_window((0, 0), anchor="nw", window=self.grid_frame)
        self.grid_frame.bind("<Configure>", lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.fill(rows)
        return self.entries[0][0]

    def fill(self, rows):
        for child in self.grid_frame.winfo_children():
            child.destroy()
        self.cells, self.entries = [], []
        for col in range(len(rows[0])):
            ttk.Label(self.grid_frame, text=f"第 {col + 1} 列").grid(row=0, column=col + 1)
        for row, values in enumerate(rows):
            ttk.Label(self.grid_frame, text=f"{row + 1}").grid(row=row + 1, column=0, padx=4)
            self.cells.append([])
            self.entries.append([])
            for col, value in enumerate(values):
                variable = tk.StringVar(self, value)
                entry = ttk.Entry(self.grid_frame, textvariable=variable, width=19)
                entry.grid(row=row + 1, column=col + 1, padx=1, pady=1)
                entry.bind("<FocusIn>", lambda _, r=row, c=col: self.activate(r, c))
                self.cells[row].append(variable)
                self.entries[row].append(entry)

    def activate(self, row, col):
        self.active = row, col
        self.crop = None
        self.source.delete("selection")
        try:
            box = cell_crop(self.cells[row][col].get())
        except ValueError:
            return
        if box:
            self.paint_crop(box)

    def resize(self, axis, change):
        rows = [[variable.get() for variable in row] for row in self.cells]
        count = len(rows) if axis == 0 else len(rows[0])
        minimum, maximum = (1, 40) if axis == 0 else (2, 20)
        if not minimum <= count + change <= maximum:
            return
        tail = rows[-1] if axis == 0 else [row[-1] for row in rows]
        if change < 0 and any(value.strip() for value in tail):
            if not messagebox.askyesno("移除单元格", "末行或末列含内容，确定移除？", parent=self):
                return
        if axis == 0:
            if change > 0:
                rows.append([""] * len(rows[0]))
            else:
                rows.pop()
        else:
            for row in rows:
                if change > 0:
                    row.append("")
                else:
                    row.pop()
        self.fill(rows)
        row, col = self.active
        self.entries[min(row, len(rows) - 1)][min(col, len(rows[0]) - 1)].focus_set()

    def merge_up(self):
        row, col = self.active
        if row:
            variable = self.cells[row][col]
            if not variable.get().strip() or variable.get() == "^" or messagebox.askyesno(
                "向上合并", "合并会清除当前格内容，确定继续？", parent=self
            ):
                variable.set("^")

    def unmerge(self):
        row, col = self.active
        if self.cells[row][col].get().strip() == "^":
            self.cells[row][col].set("")

    def begin_crop(self, event):
        self.anchor = self.crop_point(event)
        self.draw_crop(event)

    def crop_point(self, event):
        return (max(0, min(1000, round(event.x * 1000 / self.image.width))),
                max(0, min(1000, round(event.y * 1000 / self.image.height))))

    def draw_crop(self, event):
        if self.anchor is None:
            return
        x, y = self.crop_point(event)
        ax, ay = self.anchor
        self.crop = Rect(min(x, ax), min(y, ay), max(x, ax), max(y, ay))
        self.paint_crop(self.crop)

    def paint_crop(self, box):
        self.source.delete("selection")
        self.source.create_rectangle(box.left * self.image.width / 1000, box.top * self.image.height / 1000,
                                     box.right * self.image.width / 1000, box.bottom * self.image.height / 1000,
                                     outline="#007a9a", width=2, tags="selection")

    def assign_crop(self):
        if not self.crop or min(self.crop.width, self.crop.height) < 4:
            messagebox.showinfo("选取原图", "先选中单元格，再在上方原图框住要保留的区域。", parent=self)
            return
        row, col = self.active
        self.cells[row][col].set(crop_token(self.crop))

    def validate(self):
        rows = [[cell.get().strip() for cell in row] for row in self.cells]
        if any("|" in cell or "\t" in cell for row in rows for cell in row):
            messagebox.showerror("单元格内容", "请用全角竖线 ｜ 表示单元格中的竖线。", parent=self)
            return False
        lines = [HEADER] if self.header.get() else []
        lines.extend("|".join(row) for row in rows)
        if not any(cell for row in rows for cell in row if cell != "^"):
            messagebox.showerror("单元格内容", "请至少填写一个单元格或放入截图。", parent=self)
            return False
        try:
            for row in rows:
                for cell in row:
                    if cell.count("$") >= 2:
                        inline_fragments(cell, require_prose=False)
            self.result = self.validator("\n".join(lines), self.category.get())
        except ValueError as error:
            messagebox.showerror("内容格式有误", str(error), parent=self)
            return False
        return True
