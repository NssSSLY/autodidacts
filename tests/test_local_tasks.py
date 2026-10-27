# 文件职责：验证本机目录边界、精确确认、任务恢复和模型工具循环，不调用真实模型或研究库。
import asyncio
import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from autodidact.local_client import LocalWorkbench
from autodidact.local_tasks import LocalAgent, NewTask, TaskJournal
from autodidact.local_tools import LocalError, ToolRequest, Workspace


# 功能：每个用例使用独立项目和日志，退出时释放本机目录锁。
@pytest.fixture
def local(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    journal = TaskJournal(tmp_path / "state")
    agent = LocalAgent(journal)
    yield root, journal, agent
    journal.close()


# 功能：构造默认手动任务，不触发自动文件读取或模型调用。
def create(local, **kwargs):
    root, _, agent = local
    return agent.create(NewTask(workspace=str(root), objective="处理项目文件", **kwargs))


# 功能：越界、盘符、秘密和版本库路径在读取前被拒绝。
@pytest.mark.parametrize("path", ["../outside", "a/../../x", "/tmp/a", "C:\\data\\x",
    "a:stream", ".env", ".env.production", ".git/config", "x/.ssh/id_rsa", "private.pem",
    "node_modules/a", "a\x00b", "CON", ".git./config", "x. /y"])
def test_workspace_boundaries(local, path):
    with pytest.raises(LocalError):
        Workspace(local[0]).path(path)


# 功能：磁盘根、任务日志目录及包含日志的项目拒绝使用。
def test_root_and_state_are_not_workspaces(local):
    root, journal, _ = local
    for candidate in [Path(root.anchor), journal.directory, root.parent]:
        with pytest.raises(LocalError):
            Workspace(candidate, journal.directory)


# 功能：普通文本可读、秘密和依赖不进入文件列表，二进制和大文件拒绝。
def test_read_listing_and_limits(local):
    root, _, _ = local
    (root / "hello.md").write_text("你好", encoding="utf-8")
    (root / ".env").write_text("SYNTHETIC_SECRET=yes")
    (root / "node_modules").mkdir()
    (root / "node_modules/x").write_text("hidden")
    workspace = Workspace(root)
    assert workspace.listing()["files"] == ["hello.md"]
    assert workspace.read("hello.md")["content"] == "你好"
    (root / "bad").write_bytes(b"\x00binary")
    (root / "large").write_bytes(b"x" * 128001)
    for name in ["bad", "large"]:
        with pytest.raises(LocalError):
            workspace.read(name)


# 功能：多硬链接不会绕过项目边界读写另一位置的文件。
def test_hardlink_rejected(local, tmp_path):
    original = tmp_path / "original.txt"
    original.write_text("keep")
    os.link(original, local[0] / "linked.txt")
    with pytest.raises(LocalError):
        Workspace(local[0]).read("linked.txt")


# 功能：链接目录和链接文件在可用平台真实创建，不能逃逸工作目录。
def test_symlink_rejected(local, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    try:
        (local[0] / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("本机未授予创建符号链接权限")
    with pytest.raises(LocalError):
        Workspace(local[0]).read("link/keep.txt")


# 功能：模型不能要求任意argv/shell，已有命令参数按项目相对路径解析。
def test_command_protocol_is_fixed(local):
    workspace = Workspace(local[0])
    (local[0] / "x.py").write_text("x = 1\n")
    with pytest.raises(ValidationError):
        ToolRequest(tool="command", command="powershell")
    with pytest.raises(ValidationError):
        ToolRequest(tool="command", argv=["remove", "everything"])
    with pytest.raises(LocalError):
        workspace.command(ToolRequest(tool="command", command="python_check"))
    argv = workspace.command(ToolRequest(tool="command", command="python_check", paths=["x.py"]))
    assert "-I" in argv and argv[-1] == str(local[0] / "x.py")
    assert "--no-ext-diff" in workspace.command(ToolRequest(tool="command", command="git_diff"))


# 功能：文件提议先冻结差异；错误摘要、拒绝、重放都不会产生写入。
async def test_exact_approval_and_replay(local):
    root, _, agent = local
    task = create(local)
    request = ToolRequest(tool="write", path="report.md", content="new")
    pending = await agent.propose(task["id"], request)
    assert not (root / "report.md").exists()
    with pytest.raises(LocalError):
        await agent.approve(task["id"], "wrong")
    await agent.reject(task["id"])
    assert not (root / "report.md").exists()
    pending = await agent.propose(task["id"], request)
    approved = await agent.approve(task["id"], pending["pending"]["digest"])
    assert approved["status"] == "ready"
    assert (root / "report.md").read_text() == "new"
    with pytest.raises(LocalError):
        await agent.approve(task["id"], pending["pending"]["digest"])


# 功能：预览后其它进程修改文件时拒绝覆盖，保留用户的新内容。
async def test_stale_file_confirmation_preserves_edit(local):
    root, _, agent = local
    path = root / "file.txt"
    path.write_text("original")
    sha = Workspace(root).read("file.txt")["sha256"]
    task = create(local)
    pending = await agent.propose(task["id"], ToolRequest(
        tool="write", path="file.txt", content="agent", expected_sha256=sha))
    path.write_text("user changed it")
    result = await agent.approve(task["id"], pending["pending"]["digest"])
    assert result["status"] == "failed"
    assert path.read_text() == "user changed it"
    assert not list(root.glob(".autodidact-*"))


# 功能：已有文件缺少读取摘要时拒绝提议，正常写入保留旧正文以便核对。
async def test_existing_write_needs_snapshot(local):
    root, _, agent = local
    (root / "file.txt").write_text("before")
    task = create(local)
    with pytest.raises(LocalError):
        await agent.propose(task["id"], ToolRequest(tool="write", path="file.txt", content="after"))
    sha = Workspace(root).read("file.txt")["sha256"]
    pending = await agent.propose(task["id"], ToolRequest(
        tool="write", path="file.txt", content="after", expected_sha256=sha))
    result = await agent.approve(task["id"], pending["pending"]["digest"])
    receipt = result["events"][-1]["data"]["result"]
    assert receipt["before_content"] == "before" and "-before" in receipt["diff"]


# 功能：替身模型逐次返回真实工具协议，用于验证规划循环而非声称真实模型效果。
class ScriptedLLM:
    provider_name, model_name = "offline_test", "scripted"

    # 功能：保存预置动作和收到的有界模型上下文。
    def __init__(self, requests):
        self.requests, self.prompts = list(requests), []
        self.last_usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    # 功能：提供确定的下一步动作，绝不进行网络调用。
    async def structured(self, system, prompt, schema):
        self.prompts.append((system, prompt))
        return schema.model_validate(self.requests.pop(0))


# 功能：读取→提议修改→等待确认→实际写入→总结→人工关闭完整闭环。
async def test_model_tool_loop(local):
    root, journal, agent = local
    (root / "notes.md").write_text("old")
    sha = Workspace(root).read("notes.md")["sha256"]
    llm = ScriptedLLM([
        {"tool": "read", "path": "notes.md"},
        {"tool": "write", "path": "notes.md", "content": "new", "expected_sha256": sha},
        {"tool": "finish", "message": "修改完成；尚未运行测试。"},
    ])
    agent.llm_factory = lambda: llm
    task = create(local, mode="agent")
    task = await agent.advance(task["id"])
    assert task["status"] == "awaiting_approval" and task["model_calls"] == 2
    assert (root / "notes.md").read_text() == "old"
    await agent.approve(task["id"], task["pending"]["digest"])
    task = await agent.advance(task["id"])
    assert task["status"] == "review" and (root / "notes.md").read_text() == "new"
    assert task["events"][-1]["kind"] == "summary"
    assert (await agent.complete(task["id"]))["status"] == "completed"
    assert journal.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 3


# 功能：发送次数先持久预留，达到配额后不会再次调用模型。
async def test_model_budget(local):
    _, _, agent = local
    llm = ScriptedLLM([{"tool": "list"}])
    agent.llm_factory = lambda: llm
    task = create(local, mode="agent", max_calls=1)
    with pytest.raises(LocalError):
        await agent.advance(task["id"])
    assert len(llm.prompts) == 1


# 功能：中断在途规划后持久保留未知发送，不自动重试或收费。
async def test_stop_model_call(local):
    _, journal, agent = local
    entered = asyncio.Event()

    # 功能：模拟尚未返回的模型请求，等待取消。
    class WaitingLLM(ScriptedLLM):
        # 功能：声明发送已经开始但不返回结果。
        async def structured(self, *args):
            entered.set()
            await asyncio.Event().wait()

    agent.llm_factory = lambda: WaitingLLM([])
    task = create(local, mode="agent")
    agent.start(task["id"], agent.advance(task["id"]))
    await entered.wait()
    result = await agent.stop(task["id"])
    assert result["status"] == "paused" and result["model_calls"] == 1
    assert journal.db.execute("SELECT status FROM model_calls").fetchone()[0] == "interrupted"


# 功能：重启仅标记运行中任务为未知中断；已等待确认的预览保持但不自动执行。
def test_restart_does_not_replay(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    journal = TaskJournal(tmp_path / "state")
    agent = LocalAgent(journal)
    task = agent.create(NewTask(workspace=str(root), objective="test"))
    task["status"] = "executing"
    journal.save(task)
    journal.close()
    restored = TaskJournal(tmp_path / "state")
    try:
        assert restored.get(task["id"])["status"] == "interrupted"
        assert list(root.iterdir()) == []
    finally:
        restored.close()


# 功能：同一任务目录不能开启第二个运行器，避免并发恢复或重复命令。
def test_single_runner_lock(local):
    with pytest.raises(LocalError):
        TaskJournal(local[1].directory)


# 功能：用内存HTTP收发器核验实际解析代码，不建立网络连接。
async def http(app, path="/", method="GET", headers=None, body=b""):
    reader = asyncio.StreamReader()
    request = f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{app.port}\r\n"
    for key, value in (headers or {}).items():
        request += f"{key}: {value}\r\n"
    reader.feed_data(request.encode() + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    reader.feed_eof()

    class Writer:
        # 功能：捕获原始HTTP响应以核对安全头和正文。
        def write(self, data):
            self.data = data

        # 功能：模拟已完成写入，不进行网络操作。
        async def drain(self):
            pass

        # 功能：模拟关闭连接。
        def close(self):
            pass

        # 功能：模拟关闭完成。
        async def wait_closed(self):
            pass

    writer = Writer()
    await app.connection(reader, writer)
    return writer.data


# 功能：页面提供本机工具且启用CSP，接口缺Token/错误Origin不会读取任务。
async def test_local_http_protection(local):
    app = LocalWorkbench(local[2], str(local[0]), 8766)
    response = await http(app)
    assert b"200 OK" in response and b"frame-ancestors 'none'" in response
    assert b"textContent" in response and b"innerHTML" not in response
    for headers in [{}, {"X-Autodidact-Token": app.token, "Origin": "https://evil.example"}]:
        assert b"400 Bad Request" in await http(app, "/api/local/tasks", headers=headers)
    ok = await http(app, "/api/local/tasks", headers={"X-Autodidact-Token": app.token})
    assert json.loads(ok.split(b"\r\n\r\n", 1)[1]) == {"tasks": []}


# 功能：停止和确认接口严格校验任务ID，手动工具不能与同任务后台步骤竞争。
async def test_local_routes_and_private_state(local):
    app = LocalWorkbench(local[2], str(local[0]), 8766)
    task = await app.dispatch("/api/local/tasks", "POST", {
        "workspace": str(local[0]), "objective": "safe"})
    with pytest.raises(LocalError):
        await app.dispatch("/api/local/tasks/not-a-uuid", "GET", {})
    result = await app.dispatch(f"/api/local/tasks/{task['id']}/propose", "POST", {"tool": "list"})
    assert result["events"][-1]["kind"] == "list"
