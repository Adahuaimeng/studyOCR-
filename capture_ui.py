"""Desktop controls for camera and phone photo sources."""

from threading import Event, Thread
from time import monotonic
import tkinter as tk
from tkinter import ttk

import cv2
from PIL import Image, ImageTk

from phone_capture import PhotoServer, lan_addresses


class CameraFeed:
    """Keep OpenCV operations on a worker so a slow camera cannot freeze Tk."""

    def __init__(self, index):
        self.latest = None
        self.error = ""
        self.stop = Event()
        self.worker = Thread(target=self.run, args=(index,), daemon=True)
        self.worker.start()

    def run(self, index):
        for backend in (cv2.CAP_DSHOW, cv2.CAP_MSMF):
            if self.stop.is_set():
                return
            try:
                if self.read_camera(index, backend):
                    return
            except cv2.error:
                continue
        self.error = "无法打开摄像头。请检查连接、Windows 相机权限，或切换设备重试。"

    def read_camera(self, index, backend):
        camera = cv2.VideoCapture(index, backend)
        try:
            if not camera.isOpened():
                return False
            if self.stop.is_set():
                return True
            camera.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
            camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
            missed = 0
            while not self.stop.is_set():
                ok, frame = camera.read()
                if ok and frame is not None and frame.size:
                    self.latest = frame
                    missed = 0
                else:
                    missed += 1
                    if missed >= 5:
                        return False
                    self.stop.wait(0.1)
            return True
        finally:
            self.latest = None
            camera.release()

    def close(self):
        self.stop.set()


def frame_image(frame):
    return Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))


class CameraWindow:
    def __init__(self, root, import_photo, can_import, previous_feed=None):
        self.import_photo = import_photo
        self.can_import = can_import
        self.window = tk.Toplevel(root)
        self.window.title("USB / 内置摄像头拍照")
        self.window.geometry("720x565")
        self.window.minsize(520, 380)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.feed = previous_feed
        self.pending_device = None
        self.snapshot = None
        self.last_frame = None
        self.photo = None
        self.timer = None
        self.status = tk.StringVar()
        self.build()
        self.connect()
        self.poll()

    def build(self):
        body = ttk.Frame(self.window, padding=12)
        body.pack(fill="both", expand=True)
        controls = ttk.Frame(body)
        controls.pack(fill="x", pady=(0, 8))
        self.devices = ttk.Combobox(
            controls, state="readonly", width=13, values=tuple(f"摄像头 {i + 1}" for i in range(8)),
        )
        self.devices.current(0)
        self.devices.bind("<<ComboboxSelected>>", lambda event: self.connect())
        self.devices.pack(side="left", padx=(0, 6))
        ttk.Button(controls, text="重新连接", command=self.connect).pack(side="left")
        ttk.Label(body, textvariable=self.status, wraplength=480).pack(anchor="w", pady=(0, 8))
        self.take_button = ttk.Button(controls, text="拍照", command=self.take)
        self.take_button.pack(side="right")
        self.retry_button = ttk.Button(controls, text="重拍", command=self.resume)
        self.retry_button.pack(side="right", padx=6)
        self.add_button = ttk.Button(controls, text="加入工作稿", command=self.add)
        self.add_button.pack(side="right")
        self.preview = tk.Canvas(body, background="#253442", highlightthickness=0)
        self.preview.pack(fill="both", expand=True)
        self.preview.bind("<Configure>", self.resize)

    def connect(self):
        self.pending_device = self.devices.current()
        self.connecting_since = monotonic()
        self.resume()
        self.status.set("正在打开摄像头，请稍候…")
        self.preview.delete("all")
        if self.feed:
            self.feed.close()

    def poll(self):
        if self.pending_device is not None and (self.feed is None or not self.feed.worker.is_alive()):
            self.feed = CameraFeed(self.pending_device)
            self.pending_device = None
        frame = self.feed.latest if self.feed and self.pending_device is None else None
        if self.snapshot is None:
            if frame is not None:
                if frame is not self.last_frame:
                    self.draw(frame_image(frame))
                    self.last_frame = frame
                self.status.set("正在取景。把纸张放平后点「拍照」。")
            elif self.feed and self.feed.error and self.pending_device is None:
                self.preview.delete("all")
                self.status.set(self.feed.error)
            elif monotonic() - self.connecting_since >= 8:
                self.status.set("摄像头仍在连接。请稍候，并确认其他应用没有占用摄像头。")
        self.take_button.configure(state="normal" if frame is not None and self.snapshot is None else "disabled")
        self.retry_button.configure(state="normal" if self.snapshot is not None else "disabled")
        self.add_button.configure(state="normal" if self.snapshot is not None and self.can_import() else "disabled")
        self.timer = self.window.after(65, self.poll)

    def draw(self, image):
        thumbnail = image.copy()
        thumbnail.thumbnail((self.preview.winfo_width(), self.preview.winfo_height()), Image.Resampling.BILINEAR)
        self.photo = ImageTk.PhotoImage(thumbnail)
        self.preview.delete("all")
        self.preview.create_image(self.preview.winfo_width() // 2, self.preview.winfo_height() // 2,
                                  image=self.photo)

    def resize(self, _event):
        if self.snapshot is not None:
            self.draw(self.snapshot)
        else:
            self.last_frame = None

    def take(self):
        frame = self.feed.latest if self.feed else None
        if frame is not None and self.pending_device is None:
            self.snapshot = frame_image(frame)
            self.draw(self.snapshot)
            self.status.set(f"已拍摄 {self.snapshot.width} × {self.snapshot.height}。可加入工作稿或重拍。")

    def resume(self):
        self.snapshot = None
        self.last_frame = None

    def add(self):
        if self.snapshot is not None and self.can_import() and self.import_photo(self.snapshot):
            self.resume()

    def close(self):
        if self.timer is not None:
            self.window.after_cancel(self.timer)
        if self.feed:
            self.feed.close()
            self.feed.worker.join(timeout=0.25)
        self.window.destroy()


class PhoneWindow:
    def __init__(self, root, inbox):
        self.inbox = inbox
        self.server = None
        self.qr_photo = None
        self.timer = None
        self.window = tk.Toplevel(root)
        self.window.title("手机拍照")
        self.window.geometry("515x570")
        self.window.minsize(460, 530)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.status = tk.StringVar()
        self.url = tk.StringVar()
        self.last_received = tk.StringVar()
        self.build()
        self.start()
        self.poll()

    def build(self):
        body = ttk.Frame(self.window, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="手机和电脑连接同一 Wi-Fi，扫码后拍照或从相册选图。",
                  wraplength=430).pack(anchor="w")
        controls = ttk.Frame(body)
        controls.pack(fill="x", pady=8)
        ttk.Label(controls, text="电脑地址").pack(side="left")
        addresses = lan_addresses()
        self.addresses = ttk.Combobox(controls, values=addresses, state="readonly", width=17)
        if addresses:
            self.addresses.current(0)
        self.addresses.bind("<<ComboboxSelected>>", lambda event: self.start())
        self.addresses.pack(side="left", padx=6)
        ttk.Button(controls, text="刷新二维码", command=self.start).pack(side="left")
        self.qr = ttk.Label(body, anchor="center")
        self.qr.pack(fill="both", expand=True)
        address_bar = ttk.Frame(body)
        address_bar.pack(fill="x", pady=(8, 4))
        ttk.Entry(address_bar, textvariable=self.url, state="readonly").pack(side="left", fill="x", expand=True)
        self.copy_button = ttk.Button(address_bar, text="复制地址", command=self.copy, state="disabled")
        self.copy_button.pack(side="left", padx=(6, 0))
        ttk.Label(body, textvariable=self.status, wraplength=440).pack(fill="x", pady=(2, 8))
        ttk.Label(body, textvariable=self.last_received, wraplength=440).pack(fill="x", pady=(0, 8))
        ttk.Label(body, text="扫码无响应时，可用手机浏览器打开上面的地址。关闭此窗口即结束配对。",
                  wraplength=440).pack(fill="x")

    def start(self):
        self.stop()
        address = self.addresses.get()
        addresses = lan_addresses()
        self.addresses.configure(values=addresses)
        if not addresses:
            self.addresses.set("")
            self.status.set("没有找到局域网地址。请让这台电脑连上 Wi-Fi，再刷新二维码。")
            return
        if address not in addresses:
            self.addresses.current(0)
            address = self.addresses.get()
        try:
            import qrcode
        except ImportError:
            self.status.set("缺少二维码组件。请在当前 Python 环境安装 requirements.txt 后重启软件。")
            return
        try:
            self.server = PhotoServer(address, self.inbox)
        except OSError:
            self.status.set("无法在该地址上启动配对。请检查网络连接或切换电脑地址。")
            return
        self.url.set(self.server.url)
        qr = qrcode.QRCode(box_size=6, border=4)
        qr.add_data(self.server.url)
        self.qr_photo = ImageTk.PhotoImage(qr.make_image().get_image().convert("RGB"))
        self.qr.configure(image=self.qr_photo)
        self.copy_button.configure(state="normal")
        self.update_status()

    def update_status(self):
        seconds = int(self.server.remaining)
        minutes, seconds = divmod(seconds, 60)
        self.status.set(f"{self.server.activity} · 配对还有 {minutes:02}:{seconds:02}")

    def poll(self):
        if self.server:
            if self.server.remaining:
                self.update_status()
            else:
                self.stop()
                self.status.set("配对已过期。需要继续拍照时，请刷新二维码。")
        self.timer = self.window.after(700, self.poll)

    def received(self, number):
        self.last_received.set(f"已加入工作稿第 {number} 页，可在主窗口点「查看手机照片」。")
        if self.server and self.server.activity == "照片已收到，等待加入工作稿" and self.inbox.empty():
            self.server.activity = "照片已加入工作稿"

    def rejected(self):
        self.last_received.set("工作稿已满。请保存并更换工作稿后，重新发送手机照片。")
        if self.server:
            self.server.activity = "照片入稿失败"

    def clear_receipt(self):
        self.last_received.set("")

    def copy(self):
        self.window.clipboard_clear()
        self.window.clipboard_append(self.url.get())

    def stop(self):
        if self.server:
            self.server.close()
            self.server = None
        self.qr.configure(image="")
        self.qr_photo = None
        self.url.set("")
        self.copy_button.configure(state="disabled")

    def close(self):
        if self.timer is not None:
            self.window.after_cancel(self.timer)
        self.stop()
        self.window.destroy()
