# 文件职责：验证本地执行增量，默认封禁业务网络/子进程；显式开关只放行合成项目和回环HTTP。
from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMP = ROOT / "build" / "local-validation" / "tmp"
LOCAL = "--local-runtime" in sys.argv
if LOCAL:
    sys.argv.remove("--local-runtime")
sys.path.insert(0, str(ROOT))
os.environ.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", LLM_PROVIDER="mock", LLM_API_KEY="",
                  JUDGE_LLM_API_KEY="", EMBEDDING_API_KEY="", BRAVE_SEARCH_API_KEY="",
                  EMBEDDING_PROVIDER="disabled",
                  DATABASE_URL="postgresql+asyncpg://offline:offline@127.0.0.1:1/offline_test",
                  AUTODIDACT_TEST_LOCAL_EXECUTION="1" if LOCAL else "0")
os.environ.pop("AUTODIDACT_TEST_DATABASE_URL", None)
os.environ.pop("AUTODIDACT_TEST_BROWSER_PATH", None)
platform.platform = lambda *args, **kwargs: "Windows (isolated local validation)"
platform.processor = lambda: "local-validation"
COUNTS = {"blocked": 0, "local_subprocess": 0, "loopback": 0}


# 功能：只允许本轮显式合成子进程/回环连接；其它业务网络、DNS和系统命令一律失败。
def audit(event, args):
    if (event == "open" and isinstance(args[0], (str, os.PathLike))
            and Path(args[0]).resolve() == ROOT / ".env"):
        COUNTS["blocked"] += 1
        raise RuntimeError("Local validation blocked: real dotenv file")
    if event not in {"socket.bind", "socket.connect", "socket.getaddrinfo", "socket.gethostbyname",
                     "socket.gethostbyaddr", "socket.sendto", "subprocess.Popen", "os.system",
                     "os.posix_spawn"}:
        return
    frame = sys._getframe(1)
    while frame:
        if frame.f_globals.get("__name__") == "socket" and frame.f_code.co_name in {
            "socketpair", "_fallback_socketpair"}:
            return
        frame = frame.f_back
    if LOCAL and event in {"socket.bind", "socket.connect"}:
        address = args[1]
        if isinstance(address, tuple) and address[0] == "127.0.0.1":
            COUNTS["loopback"] += 1
            return
    if LOCAL and event == "subprocess.Popen":
        executable, command, cwd, _ = args
        # Windows审计事件中的executable可为None，argv已转为CreateProcess命令行。
        python_command = executable == sys.executable or (
            executable is None and isinstance(command, str)
            and command.startswith(subprocess.list2cmdline([sys.executable]) + " "))
        if python_command and cwd and Path(cwd).resolve().is_relative_to(TEMP):
            COUNTS["local_subprocess"] += 1
            return
    COUNTS["blocked"] += 1
    raise RuntimeError(f"Local validation blocked: {event}")


sys.addaudithook(audit)
import pytest
import pytest_asyncio.plugin

from autodidact.config import RuntimeSettings

RuntimeSettings.model_config["env_file"] = None


class Scope:
    # 功能：保留旧研究HTTP/浏览器待验收边界；新增本机测试由独立开关控制。
    def pytest_collection_modifyitems(self, items):
        for item in items:
            if item.path.name == "test_workbench_browser.py" or item.name in {
                             "test_http_protection_and_safe_errors", "test_http_health_protection"}:
                item.add_marker(pytest.mark.skip(reason="研究工作台HTTP/浏览器不在本机执行增量验收内"))


if __name__ == "__main__":
    arguments = sys.argv[1:] or ["-q", "tests"]
    if LOCAL:
        TEMP.parent.mkdir(parents=True, exist_ok=True)
        arguments = [*arguments, f"--basetemp={TEMP}"]
    code = pytest.main(arguments, plugins=[pytest_asyncio.plugin, Scope()])
    print("Local validation guard:", COUNTS)
    raise SystemExit(1 if COUNTS["blocked"] else code)
