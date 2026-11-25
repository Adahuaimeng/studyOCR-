"""Short-lived local pairing for mobile camera and gallery uploads."""

from concurrent.futures import Future
from dataclasses import dataclass, field
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from ipaddress import IPv4Address, IPv4Network
from queue import Full
from secrets import token_urlsafe
import socket
from threading import BoundedSemaphore, Thread
from time import monotonic

from PIL import Image

from ocr_core import open_image


MAX_UPLOAD = 24 * 1024 * 1024
MAX_PHOTO_PIXELS = 32_000_000
PAIR_SECONDS = 15 * 60
LAN_NETWORKS = tuple(IPv4Network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))

PHONE_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>发送照片到电脑</title>
<style>
:root{color-scheme:light;font:17px system-ui,sans-serif;color:#142b42;background:#f0f5fa}
main{margin:8vh auto;padding:24px;max-width:390px}
h1{font-size:1.55rem}
label{display:block;padding:14px;margin:12px 0;background:#fff;border:1px solid #8499ae;
border-radius:8px;text-align:center;cursor:pointer}
label:first-of-type{background:#174c7d;color:white;border-color:#174c7d}
input{position:absolute;width:1px;height:1px;opacity:0}
label:focus-within{outline:3px solid #316bb0;outline-offset:3px}
p{line-height:1.55}
</style>
<script src="phone.js" defer></script>
</head>
<body><main>
<h1>发送照片到电脑</h1>
<p>拍照或选一张照片，传入电脑当前的 OCR 工作稿。</p>
<label>拍照<input type="file" accept="image/*" capture="environment"></label>
<label>从相册选图<input type="file" accept="image/*"></label>
<button id="retry" type="button" hidden>重试发送</button>
<p id="status" role="status"></p>
</main></body>
</html>
"""

PHONE_SCRIPT = """
const inputs = [...document.querySelectorAll('input[type=file]')];
const statusText = document.getElementById('status');
const retry = document.getElementById('retry');
let pendingFile;
async function sendPhoto() {
    statusText.textContent = '正在发送照片…';
    retry.hidden = true;
    for (const input of inputs) input.disabled = true;
    try {
        const response = await fetch('upload', {
            method: 'POST',
            headers: {'Content-Type': 'application/octet-stream'},
            body: pendingFile
        });
        statusText.textContent = await response.text();
        if (response.ok) {
            pendingFile = null;
            for (const input of inputs) input.value = '';
        }
    } catch {
        statusText.textContent = '发送失败。请检查 Wi-Fi 和电脑端的手机拍照窗口。';
    } finally {
        retry.hidden = !pendingFile;
        for (const input of inputs) input.disabled = false;
    }
}
for (const input of inputs) {
    input.addEventListener('change', () => {
        if (input.files.length) {
            pendingFile = input.files[0];
            sendPhoto();
        }
    });
}
retry.addEventListener('click', sendPhoto);
"""


@dataclass
class PhotoUpload:
    image: Image.Image
    page: Future[int | None] = field(default_factory=Future)


def lan_addresses():
    try:
        records = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return ()
    addresses = {IPv4Address(record[4][0]) for record in records}
    return tuple(str(address) for address in sorted(addresses)
                 if any(address in network for network in LAN_NETWORKS))


class PhotoServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, inbox):
        self.inbox = inbox
        self.path = f"/{token_urlsafe(24)}/"
        self.deadline = monotonic() + PAIR_SECONDS
        self.requests = BoundedSemaphore(4)
        self.active = True
        self.activity = "等待手机打开配对网页"
        super().__init__((address, 0), PhotoHandler)
        self.authority = f"{address}:{self.server_port}"
        self.url = f"http://{self.authority}{self.path}"
        self.worker = Thread(target=self.serve_forever, args=(0.1,), daemon=True)
        self.worker.start()

    @property
    def remaining(self):
        return max(0, self.deadline - monotonic())

    def close(self):
        self.active = False
        self.shutdown()
        self.server_close()

    def get_request(self):
        client, address = super().get_request()
        client.settimeout(5)
        return client, address

    def process_request(self, request, client_address):
        if not self.requests.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.requests.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.requests.release()


class PhotoHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, code, body, media="text/plain"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{media}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (OSError, TimeoutError):
            pass

    def match(self, suffix=""):
        return (self.headers.get("Host") == self.server.authority
                and self.path.isascii()
                and compare_digest(self.path, self.server.path + suffix))

    def available(self):
        if self.server.active and self.server.remaining:
            return True
        self.reply(410, "配对已结束，请在电脑端刷新二维码后重新扫码。")
        return False

    def do_GET(self):
        if self.match():
            if self.available():
                if self.server.activity == "等待手机打开配对网页":
                    self.server.activity = "配对网页已打开，等待照片"
                self.reply(200, PHONE_PAGE, "text/html")
        elif self.match("phone.js"):
            if self.available():
                self.reply(200, PHONE_SCRIPT, "text/javascript")
        else:
            self.reply(404, "配对地址无效，请使用电脑上的二维码。")

    def do_POST(self):
        if not self.match("upload"):
            self.reply(404, "配对地址无效，请使用电脑上的二维码。")
            return
        if not self.available():
            return
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{self.server.authority}":
            self.reply(403, "请从扫码打开的页面发送照片。")
            return
        self.server.activity = "正在接收手机照片"
        lengths = self.headers.get_all("Content-Length", ())
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit() or len(lengths[0]) > 12:
            self.upload_reply(411, "无法读取照片大小，请重新选择照片。")
            return
        length = int(lengths[0])
        if not 0 < length <= MAX_UPLOAD:
            self.upload_reply(413, "单张照片最多 24 MiB，请选一张较小的照片。")
            return
        try:
            data = self.rfile.read(length)
            if len(data) != length:
                self.upload_reply(400, "照片未传完，请重新发送。")
                return
            image = open_image(BytesIO(data), max_pixels=MAX_PHOTO_PIXELS)
        except (OSError, ValueError, Image.DecompressionBombError):
            self.upload_reply(415, "无法读取这张照片。请尝试 JPG、PNG 或 HEIC，或降低拍摄分辨率。")
            return
        if not self.available():
            return
        upload = PhotoUpload(image)
        self.server.activity = "照片已收到，等待加入工作稿"
        try:
            self.server.inbox.put_nowait(upload)
        except Full:
            self.upload_reply(429, "电脑还有照片等待导入，请稍后重试。")
            return
        try:
            page = upload.page.result(timeout=1.5)
        except TimeoutError:
            self.reply(202, "照片已收到，正在等待加入工作稿。可在电脑端查看进度。")
        else:
            if page is None:
                self.reply(507, "电脑工作稿已满，请更换工作稿后重试发送。")
            else:
                self.reply(200, f"照片已加入电脑工作稿第 {page} 页。")

    def upload_reply(self, code, message):
        self.server.activity = message
        self.reply(code, message)
