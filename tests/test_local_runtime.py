# 文件职责：仅在显式本机验收开关下，使用合成项目验证真实子进程及回环HTTP；无业务数据库/模型。
import asyncio
import ctypes
import json
import os
import time

import pytest

from autodidact.local_client import LocalWorkbench
from autodidact.local_tasks import LocalAgent, NewTask, TaskJournal
from autodidact.local_tools import MAX_OUTPUT_BYTES, ToolRequest, Workspace, execute_command

pytestmark = pytest.mark.skipif(
    os.environ.get("AUTODIDACT_TEST_LOCAL_EXECUTION") != "1",
    reason="真实本机子进程/回环HTTP需要显式验收开关；仅操作合成项目",
)


# 功能：创建专用临时项目，不使用真实用户项目或学习数据库。
@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "synthetic-project"
    root.mkdir()
    return Workspace(root)


# 功能：实际启动固定Python语法驱动，确认中文/空格路径和非零退出被如实记录。
async def test_real_python_check(workspace):
    (workspace.root / "示例 文件.py").write_text("value = 1\n", encoding="utf-8")
    good = await execute_command(workspace, ToolRequest(
        tool="command", command="python_check", paths=["示例 文件.py"]))
    assert good["passed"] and "OK" in good["output"]
    (workspace.root / "示例 文件.py").write_text("def broken(\n", encoding="utf-8")
    bad = await execute_command(workspace, ToolRequest(
        tool="command", command="python_check", paths=["示例 文件.py"]))
    assert not bad["passed"] and bad["exit_code"] != 0


# 功能：真实pytest失败、大输出和未知环境变量不会被当作成功或无限积累。
async def test_real_pytest_failure_output_and_environment(workspace, monkeypatch):
    monkeypatch.setenv("AUTODIDACT_SYNTHETIC_SECRET", "must-not-inherit")
    (workspace.root / "test_sample.py").write_text(
        "import os\ndef test_failure():\n"
        "    assert 'AUTODIDACT_SYNTHETIC_SECRET' not in os.environ\n"
        "    print('x' * 50000)\n    assert False, 'synthetic failure'\n", encoding="utf-8")
    result = await execute_command(workspace, ToolRequest(
        tool="command", command="pytest", paths=["test_sample.py"]))
    assert result["exit_code"] == 1 and not result["passed"]
    assert result["output_truncated"] and len(result["output"].encode()) <= MAX_OUTPUT_BYTES


# 功能：查询本验收创建的子进程是否仍活着，不枚举或操作其它进程。
def process_alive(pid):
    if os.name == "nt":
        from ctypes import wintypes as w

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        kernel.OpenProcess.restype = w.HANDLE
        kernel.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
        kernel.CloseHandle.argtypes = [w.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = w.DWORD()
        kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel.CloseHandle(handle)
        return code.value == 259
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


# 功能：超时/人工取消都清理合成pytest及其后代，不遗留后台任务。
@pytest.mark.parametrize("cancel", [False, True])
async def test_real_timeout_and_process_tree(workspace, cancel):
    (workspace.root / "test_slow.py").write_text(
        "import subprocess,sys,time\nfrom pathlib import Path\ndef test_slow():\n"
        "    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])\n"
        "    Path('child.pid').write_text(str(child.pid))\n    time.sleep(30)\n", encoding="utf-8")
    before = time.monotonic()
    task = asyncio.create_task(execute_command(workspace, ToolRequest(
        tool="command", command="pytest", paths=["test_slow.py"], timeout=3)))
    if cancel:
        for _ in range(100):
            if (workspace.root / "child.pid").exists():
                break
            await asyncio.sleep(.03)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await task
        assert result["timed_out"] and not result["passed"]
    assert time.monotonic() - before < 8
    pid = int((workspace.root / "child.pid").read_text())
    assert not process_alive(pid)


# 功能：真实回环HTTP完成建任务、读取、预览、确认写入；默认模型不参与。
async def test_real_http_task_flow(workspace, tmp_path):
    journal = TaskJournal(tmp_path / "state")
    agent = LocalAgent(journal)
    app = LocalWorkbench(agent, str(workspace.root), 0)
    server = await asyncio.start_server(app.connection, "127.0.0.1", 0)
    app.port = server.sockets[0].getsockname()[1]

    # 功能：只连接测试服务器，使用同源Token发送有界JSON请求。
    async def request(path, data=None, token=True):
        reader, writer = await asyncio.open_connection("127.0.0.1", app.port)
        raw = json.dumps(data).encode() if data is not None else b""
        method = "POST" if data is not None else "GET"
        headers = (f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{app.port}\r\n"
                   f"Content-Length: {len(raw)}\r\nContent-Type: application/json\r\n"
                   f"Origin: http://127.0.0.1:{app.port}\r\n")
        if token:
            headers += f"X-Autodidact-Token: {app.token}\r\n"
        writer.write(headers.encode() + b"\r\n" + raw)
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()
        head, body = response.split(b"\r\n\r\n", 1)
        return head, json.loads(body)

    try:
        head, _ = await request("/api/local/tasks", token=False)
        assert b"400 Bad Request" in head
        _, task = await request("/api/local/tasks", NewTask(
            workspace=str(workspace.root), objective="真实本机验收").model_dump())
        path = "/api/local/tasks/" + task["id"]
        _, pending = await request(path + "/propose", {
            "tool": "write", "path": "result.md", "content": "本机执行成功"})
        assert not (workspace.root / "result.md").exists()
        await request(path + "/approve", {"digest": pending["pending"]["digest"]})
        for _ in range(40):
            _, task = await request(path)
            if task["status"] == "ready":
                break
            await asyncio.sleep(.02)
        assert task["status"] == "ready"
        assert (workspace.root / "result.md").read_text(encoding="utf-8") == "本机执行成功"
        assert task["model_calls"] == 0
    finally:
        server.close()
        await server.wait_closed()
        await agent.close()


# 功能：替身模型规划，真实文件修改和真实pytest验证，完成后仍等待人工审阅。
async def test_real_agent_write_verify_loop(workspace, tmp_path):
    (workspace.root / "example.py").write_text("value = 1\n")
    (workspace.root / "test_example.py").write_text(
        "from example import value\ndef test_value():\n    assert value == 2\n")
    sha = workspace.read("example.py")["sha256"]
    requests = [
        {"tool": "read", "path": "example.py"},
        {"tool": "write", "path": "example.py", "content": "value = 2\n", "expected_sha256": sha},
        {"tool": "command", "command": "pytest", "paths": ["test_example.py"]},
        {"tool": "finish", "message": "已修改并运行指定测试，请审阅。"},
    ]

    class Model:
        provider_name, model_name = "offline_test", "scripted"

        # 功能：返回预置动作，真实执行与判定由工具完成，不进行付费调用。
        async def structured(self, system, prompt, schema):
            return schema.model_validate(requests.pop(0))

    journal = TaskJournal(tmp_path / "state")
    agent = LocalAgent(journal, Model)
    try:
        task = agent.create(NewTask(workspace=str(workspace.root), objective="修复value并测试", mode="agent"))
        for expected in ("write", "command"):
            task = await agent.advance(task["id"])
            assert task["pending"]["request"]["tool"] == expected
            task = await agent.approve(task["id"], task["pending"]["digest"])
        task = await agent.advance(task["id"])
        commands = [e["data"]["result"] for e in task["events"]
                    if e["kind"] == "result" and e["data"]["tool"] == "command"]
        assert task["status"] == "review" and task["model_calls"] == 4
        assert commands[0]["passed"] and "1 passed" in commands[0]["output"]
    finally:
        await agent.close()
