# 文件职责：启动无需研究数据库的回环本机任务工作台，保护请求来源及逐项执行确认。
from __future__ import annotations

import asyncio
import json
import secrets
import socket
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import typer
from pydantic import ValidationError

from autodidact.local_models import LocalModels
from autodidact.local_page import PAGE
from autodidact.local_tasks import LocalAgent, NewTask, TaskJournal, default_state_dir
from autodidact.local_tools import LocalError, ToolRequest, Workspace
from autodidact.teaching import TeachingService
from autodidact.teaching_page import PAGE as TEACHING_PAGE


class LocalWorkbench:
    # 功能：为本进程生成浏览器Token和CSP nonce，不在启动时读取文件正文或调用模型。
    def __init__(self, agent: LocalAgent, workspace: str, port: int):
        self.agent, self.workspace, self.port = agent, workspace, port
        self.token, self.nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        self.models = LocalModels(agent.journal.directory)
        self.teaching = TeachingService(agent.journal, self.models)
        if agent.llm_factory is None:
            agent.llm_factory = self.models.build

    # 功能：只路由本机任务，模型/命令由后台任务执行，未知路径与重复操作拒绝。
    async def dispatch(self, path, method, body):
        if path == "/api/local/teaching" or path.startswith("/api/local/teaching/"):
            return await self.teaching.dispatch(path, method, body)
        if path == "/api/local/info" and method == "GET":
            model = self.models.info()
            return {"workspace": self.workspace, "device": socket.gethostname(),
                    "provider": model["provider"], "model": model["model"]}
        if path == "/api/local/tasks":
            if method == "GET":
                return {"tasks": self.agent.journal.listing()}
            if method == "POST":
                return self.agent.create(NewTask.model_validate(body))
        parts = path.split("/")
        if len(parts) not in {5, 6} or parts[1:4] != ["api", "local", "tasks"]:
            raise LocalError("未知本机操作")
        try:
            task_id = str(UUID(parts[4]))
        except ValueError:
            raise LocalError("任务ID无效") from None
        if len(parts) == 5 and method == "GET":
            return self.agent.journal.get(task_id)
        if len(parts) != 6 or method != "POST":
            raise LocalError("操作方法无效")
        action = parts[5]
        job = self.agent.jobs.get(task_id)
        if action not in {"stop"} and job and not job.done():
            raise LocalError("此任务有运行中的步骤，请等待或暂停")
        if action == "propose":
            return await self.agent.propose(task_id, ToolRequest.model_validate(body))
        if action == "advance" and not body:
            self.agent.journal.get(task_id)
            self.agent.start(task_id, self.agent.advance(task_id))
            return {"started": True}
        if action == "approve" and set(body) == {"digest"} and isinstance(body["digest"], str):
            task = self.agent.journal.get(task_id)
            if (task["status"] != "awaiting_approval" or not task["pending"]
                    or body["digest"] != task["pending"]["digest"]):
                raise LocalError("确认已过期或内容不一致，请刷新预览")
            self.agent.start(task_id, self.agent.approve(task_id, body["digest"]))
            return {"started": True}
        if action in {"stop", "reject", "complete"} and not body:
            return await getattr(self.agent, action)(task_id)
        raise LocalError("未知操作或参数不完整")

    # 功能：严格校验回环Host、来源、Token和有界JSON，输出无缓存/CSP保护页面。
    async def connection(self, reader, writer):
        status, content_type = "200 OK", "application/json; charset=utf-8"
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            if len(header) > 12000:
                raise LocalError("请求头过大")
            lines = header.decode("ascii").split("\r\n")
            method, target, version = lines[0].split()
            if version != "HTTP/1.1" or method not in {"GET", "POST"} or not target.startswith("/"):
                raise LocalError("请求格式无效")
            headers = {}
            for line in lines[1:]:
                if not line:
                    continue
                key, value = line.split(":", 1)
                key = key.lower()
                if key in headers:
                    raise LocalError("请求头不得重复")
                headers[key] = value.strip()
            host = f"127.0.0.1:{self.port}"
            if headers.get("host") != host or "transfer-encoding" in headers:
                raise LocalError("请使用127.0.0.1访问本机工作台")
            if headers.get("origin") and headers["origin"] != f"http://{host}":
                raise LocalError("请求来源无效")
            length = int(headers.get("content-length", "0"))
            if not 0 <= length <= 200_000 or (method == "GET" and length):
                raise LocalError("请求内容过大或GET携带请求体")
            parsed = urlsplit(target)
            if parsed.query or parsed.netloc or parsed.fragment:
                raise LocalError("本机接口不接受附加URL参数")
            if parsed.path in {"/", "/tools"} and method == "GET":
                content_type = "text/html; charset=utf-8"
                page = TEACHING_PAGE if parsed.path == "/" else PAGE
                data = page.replace("TOKEN", self.token).replace("NONCE", self.nonce).encode()
            elif parsed.path == "/healthz" and method == "GET":
                data = b'{"alive":true,"mode":"local"}'
            else:
                if not secrets.compare_digest(headers.get("x-autodidact-token", ""), self.token):
                    raise LocalError("访问令牌无效，请刷新页面")
                if method == "POST" and (headers.get("origin") != f"http://{host}"
                                         or headers.get("content-type") != "application/json"):
                    raise LocalError("写请求需要同源JSON")
                raw = await asyncio.wait_for(reader.readexactly(length), 10)
                body = json.loads(raw) if raw else {}
                if not isinstance(body, dict):
                    raise LocalError("请求内容必须为JSON对象")
                result = await self.dispatch(parsed.path, method, body)
                data = json.dumps(result, ensure_ascii=False).encode()
        except Exception as exc:  # noqa: BLE001 - HTTP边界只展示受控错误，避免配置/模型正文泄漏。
            status = "400 Bad Request"
            error = (str(exc) if isinstance(exc, LocalError) else
                     "填写内容不符合要求，请检查必填项和长度。" if isinstance(exc, ValidationError)
                     else type(exc).__name__)
            data = json.dumps({"error": error}, ensure_ascii=False).encode()
        csp = (f"default-src 'none'; script-src 'nonce-{self.nonce}'; style-src 'unsafe-inline'; "
               "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        writer.write((f"HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\n"
                      f"Content-Length: {len(data)}\r\nConnection: close\r\nCache-Control: no-store\r\n"
                      "X-Content-Type-Options: nosniff\r\nReferrer-Policy: no-referrer\r\n"
                      f"Content-Security-Policy: {csp}\r\n\r\n").encode() + data)
        try:
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


# 功能：运行本机任务服务，不初始化或连接研究数据库，退出时清理在途任务和命令树。
async def serve_local(workspace: str, port=8766, state_dir: Path | None = None, open_browser=False):
    root = Workspace(workspace).root
    journal = TaskJournal(state_dir or default_state_dir())
    agent = LocalAgent(journal)
    try:
        Workspace(root, journal.directory)
        app = LocalWorkbench(agent, str(root), port)
        server = await asyncio.start_server(app.connection, "127.0.0.1", port, limit=210_000)
        async with server:
            url = f"http://127.0.0.1:{port}"
            print(f"本机示范学习：{url}\n文件与命令工具：{url}/tools\n执行设备：{socket.gethostname()}\n默认项目：{root}")
            if open_browser:
                webbrowser.open(url)
            await server.serve_forever()
    finally:
        if "app" in locals():
            await app.teaching.stop()
        await agent.close()


# 功能：为现有CLI和独立入口注册同一启动函数，无需新增桌面或数据库依赖。
def register_local_commands(app):
    # 功能：显式启动本机网页，可指定项目、端口和独立任务记录目录。
    @app.command("local-serve")
    def local_serve(
        workspace: str = typer.Option(".", help="默认项目目录；也可在页面为新任务选择目录。"),
        port: int = typer.Option(8766, min=1024, max=65535),
        state_dir: str = typer.Option("", help="独立任务记录目录，默认个人Autodidact目录。"),
        open_browser: bool = typer.Option(False, help="启动后在本机浏览器打开页面。"),
    ):
        try:
            asyncio.run(serve_local(workspace, port, Path(state_dir) if state_dir else None, open_browser))
        except KeyboardInterrupt:
            print("本机运行器已停止，历史记录保留。")
        except Exception as exc:  # noqa: BLE001 - CLI不展开配置和宿主局部变量。
            print(str(exc) if isinstance(exc, LocalError) else type(exc).__name__)
            raise typer.Exit(1) from None


app = typer.Typer(pretty_exceptions_show_locals=False)
register_local_commands(app)

if __name__ == "__main__":
    app()
