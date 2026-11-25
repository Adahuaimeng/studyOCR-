"""Commands and a configurable two-row shortcut bar for the OCR workspace."""

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
import tkinter as tk
from tkinter import messagebox, ttk


def settings_path():
    directory = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    return directory / "StudyOCR" / "workspace.json"


def load_shortcuts(path, commands, defaults):
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return tuple(defaults)
    if not isinstance(values, list):
        return tuple(defaults)
    return tuple(dict.fromkeys(key for key in values if isinstance(key, str) and key in commands))


def save_shortcuts(path, shortcuts):
    save_settings(path, shortcuts)


def load_preferences(path):
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return values if isinstance(values, dict) else {}


def save_settings(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as target:
        temporary = Path(target.name)
        json.dump(data, target, ensure_ascii=False)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class Command:
    def __init__(self, label, run):
        self.label = label
        self.run = run
        self.enabled = True
        self.buttons = []

    @property
    def state(self):
        return "normal" if self.enabled else "disabled"

    def invoke(self):
        if self.enabled:
            return self.run()

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        for button in self.buttons:
            button.configure(state=self.state)

    def button(self, parent):
        button = ttk.Button(parent, text=self.label, command=self.invoke, state=self.state)
        self.buttons.append(button)
        button.bind("<Destroy>", lambda event: self.buttons.remove(button), add=True)
        return button

    def add_to_menu(self, menu):
        menu.add_command(label=self.label, command=self.invoke, state=self.state)
        return menu.index("end")


class ShortcutBar(ttk.Frame):
    def __init__(self, parent, commands, toggle_tools):
        super().__init__(parent)
        self.commands = commands
        self.slots = []
        self.layout = None
        self.overflow_keys = ()
        self.rows = (ttk.Frame(self), ttk.Frame(self))
        self.rows[0].pack(fill="x")
        self.tools = ttk.Button(self, text="展开工具", command=toggle_tools)
        self.more = ttk.Menubutton(self, text="更多")
        self.more_menu = tk.Menu(self.more, tearoff=False, postcommand=self.fill_more)
        self.more.configure(menu=self.more_menu)
        self.bind("<Configure>", self.reflow)

    def set_shortcuts(self, keys):
        for _, button in self.slots:
            button.destroy()
        self.slots = [(key, self.commands[key].button(self)) for key in keys]
        self.layout = None
        self.reflow()

    def reflow(self, _event=None):
        width = self.winfo_width()
        if width <= 1:
            return
        space = width - self.more.winfo_reqwidth() - 5
        cells = [(0, self.tools)]
        row, used = 0, self.tools.winfo_reqwidth() + 5
        for index, (_, button) in enumerate(self.slots):
            required = button.winfo_reqwidth() + 5
            if used + required > (width if row == 0 else space):
                row, used = row + 1, 0
            if row == 2:
                remaining = tuple(key for key, _ in self.slots[index:])
                self.apply_layout(cells + [(1, self.more)], remaining)
                return
            cells.append((row, button))
            used += required
        self.apply_layout(cells, ())

    def apply_layout(self, cells, remaining):
        if self.layout == cells and self.overflow_keys == remaining:
            return
        self.layout, self.overflow_keys = cells, remaining
        for frame in self.rows:
            for widget in frame.pack_slaves():
                widget.pack_forget()
        if cells[-1][0] == 1:
            self.rows[1].pack(fill="x")
        else:
            self.rows[1].pack_forget()
        for row, widget in cells:
            widget.pack(in_=self.rows[row], side="left", padx=(0, 5), pady=(0, 4))

    def fill_more(self):
        self.more_menu.delete(0, "end")
        for key in self.overflow_keys:
            self.commands[key].add_to_menu(self.more_menu)


class ShortcutDialog(tk.Toplevel):
    def __init__(self, root, commands, selected, defaults, save):
        super().__init__(root)
        self.title("自定义快捷栏")
        self.transient(root)
        self.resizable(False, False)
        self.commands, self.selected, self.defaults = commands, list(selected), defaults
        self.save = save
        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        self.available = self.make_list(body, "可选功能", 0)
        self.visible = self.make_list(body, "已固定 · 从左到右", 2)
        controls = ttk.Frame(body, padding=(8, 0))
        controls.grid(row=0, column=1)
        for title, callback in (("添加 →", self.add), ("← 移除", self.remove),
                                ("上移", lambda: self.move(-1)), ("下移", lambda: self.move(1))):
            ttk.Button(controls, text=title, command=callback).pack(fill="x", pady=3)
        message = "快捷栏最多两排，放不下的按钮会进入「更多」。全部功能仍可从菜单调用。"
        ttk.Label(body, text=message).grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 5))
        footer = ttk.Frame(body)
        footer.grid(row=2, column=0, columnspan=3, sticky="ew")
        ttk.Button(footer, text="恢复默认", command=self.reset).pack(side="left")
        ttk.Button(footer, text="保存", command=self.commit).pack(side="right")
        ttk.Button(footer, text="取消", command=self.destroy).pack(side="right", padx=(0, 5))
        self.available.bind("<Double-1>", lambda event: self.add())
        self.visible.bind("<Double-1>", lambda event: self.remove())
        self.bind("<Escape>", lambda event: self.destroy())
        self.refresh()
        self.grab_set()
        self.available.focus_set()

    def make_list(self, parent, title, column):
        frame = ttk.Frame(parent)
        frame.grid(row=0, column=column)
        ttk.Label(frame, text=title).pack(anchor="w", pady=(0, 4))
        field = tk.Listbox(frame, exportselection=False, width=25, height=13)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=field.yview)
        field.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        field.pack(side="left", fill="both")
        return field

    def refresh(self, index=None):
        self.other = [key for key in self.commands if key not in self.selected]
        for field, keys in ((self.available, self.other), (self.visible, self.selected)):
            field.delete(0, "end")
            for key in keys:
                field.insert("end", self.commands[key].label)
        if index is not None and self.selected:
            self.visible.selection_set(index)
            self.visible.see(index)

    def add(self):
        if indices := self.available.curselection():
            self.selected.append(self.other[indices[0]])
            self.refresh(len(self.selected) - 1)

    def remove(self):
        if indices := self.visible.curselection():
            self.selected.pop(indices[0])
            self.refresh(min(indices[0], len(self.selected) - 1))

    def move(self, step):
        if indices := self.visible.curselection():
            index = indices[0]
            target = index + step
            if 0 <= target < len(self.selected):
                self.selected[index], self.selected[target] = self.selected[target], self.selected[index]
                self.refresh(target)

    def reset(self):
        self.selected = list(self.defaults)
        self.refresh()

    def commit(self):
        try:
            self.save(tuple(self.selected))
        except OSError as error:
            messagebox.showerror("无法保存快捷栏", str(error), parent=self)
        else:
            self.destroy()

    def destroy(self):
        super().destroy()
        self.available = self.visible = None
