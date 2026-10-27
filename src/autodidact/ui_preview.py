"""隔离界面演示入口：仅标准库与本地资源，不导入配置、数据库或模型。"""

import argparse
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from autodidact.ui import render_page


class PreviewHandler(BaseHTTPRequestHandler):
    """只提供回环演示首页，拒绝 API 和其他文件访问。"""

    def do_GET(self):
        """校验 Host 并发送无缓存、带 nonce 的自包含演示页。"""
        port = self.server.server_port
        if self.headers.get("Host") != f"127.0.0.1:{port}" or self.path != "/":
            self.send_error(404, "Preview page only")
            return
        nonce = secrets.token_urlsafe(24)
        data = render_page(preview=True).replace("TOKEN", "preview-only").replace("NONCE", nonce).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; "
            "connect-src 'none'; frame-ancestors 'none'; form-action 'none'",
        )
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        """预览不输出用户提交文本；仅提供固定首页，没有业务请求日志。"""


def main():
    """启动可手动停止的本机演示服务；不启动学习或创建持久状态。"""
    parser = argparse.ArgumentParser(description="Autodidact 界面演示，无需数据库或模型")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口须在 1–65535")
    with ThreadingHTTPServer(("127.0.0.1", args.port), PreviewHandler) as server:
        print(f"演示工作台：http://127.0.0.1:{args.port} （虚构数据，Ctrl+C 停止）", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
