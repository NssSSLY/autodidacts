# 文件职责：在显式项目目录内读写文本并运行少量受控命令；命令执行不是操作系统沙箱。
from __future__ import annotations

import asyncio
import ctypes
import difflib
import hashlib
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_FILE_BYTES = 128_000
MAX_OUTPUT_BYTES = 32_000
PRIVATE_DIRS = {".git", ".ssh", ".aws", ".azure", ".codex", ".browser"}
SKIP_DIRS = PRIVATE_DIRS | {".venv", "node_modules", "__pycache__", "dist", "build"}
RUNTIME = Path(__file__).resolve().parent


class LocalError(ValueError):
    """可向本机用户展示的受控错误。"""


class ToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: Literal["list", "read", "write", "command", "finish"]
    path: str = Field(default=".", max_length=500)
    content: str = Field(default="", max_length=MAX_FILE_BYTES)
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    command: Literal["git_status", "git_diff", "python_check", "pytest"] = "git_status"
    paths: list[str] = Field(default_factory=list, max_length=8)
    timeout: int = Field(default=60, ge=1, le=120)
    message: str = Field(default="", max_length=4000)


# 功能：计算字节摘要，用于确认前后文件变化，不把摘要当语义正确性证明。
def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# 功能：拒绝私密配置、版本库内部、密钥文件及常见依赖目录。
def private_path(parts) -> bool:
    return any(
        p.casefold() in SKIP_DIRS
        or (p.casefold().startswith(".env") and p.casefold() != ".env.example")
        or p.casefold().endswith((".pem", ".key", ".pfx", ".p12", ".ppk"))
        or p.casefold() in {"id_rsa", "id_ed25519", "credentials", "credentials.json"}
        for p in parts
    )


class Workspace:
    # 功能：绑定用户明确选择的非根目录，拒绝链接、整个用户目录和运行器数据目录。
    def __init__(self, root: str | Path, state_dir: Path | None = None):
        candidate = Path(root).absolute()
        self._no_links(candidate)
        self.root = candidate.resolve(strict=True)
        if not self.root.is_dir() or self.root == Path(self.root.anchor) or self.root == Path.home():
            raise LocalError("请选择具体项目文件夹，不要选择磁盘根目录或整个用户目录")
        if state_dir and (
            self.root.is_relative_to(state_dir.resolve())
            or state_dir.resolve().is_relative_to(self.root)
        ):
            raise LocalError("项目目录不能包含运行器的任务记录目录")

    # 功能：逐层拒绝符号链接、Windows目录联接和其它reparse point。
    @staticmethod
    def _no_links(path: Path):
        for entry in (path, *path.parents):
            if entry.is_symlink() or entry.is_junction():
                raise LocalError("项目和文件路径不能经过链接或目录联接")
            if entry.exists() and getattr(entry.lstat(), "st_file_attributes", 0) & 0x400:
                raise LocalError("不支持重解析点路径")

    # 功能：解析相对项目路径并再次检查边界；目录限制只适用于文件工具。
    def path(self, relative: str, *, write=False) -> Path:
        text = relative.replace("\\", "/")
        if (
            not text or "\x00" in text or ":" in text or PureWindowsPath(text).drive
            or text.startswith("/") or ".." in text.split("/")
        ):
            raise LocalError("只接受项目内相对路径，禁止上级目录、盘符和绝对路径")
        parts = [p for p in text.split("/") if p not in {"", "."}]
        if any(p.endswith((".", " ")) or PureWindowsPath(p).is_reserved() for p in parts):
            raise LocalError("不支持Windows设备名或带尾部点/空格的路径")
        if private_path(parts):
            raise LocalError("该路径属于私密配置、版本库内部或排除目录")
        candidate = self.root.joinpath(*parts)
        self._no_links(candidate)
        if not candidate.resolve().is_relative_to(self.root):
            raise LocalError("文件超出项目目录")
        if write and candidate.resolve().is_relative_to(RUNTIME):
            raise LocalError("本地任务不能修改正在运行的Agent核心模块")
        if candidate.exists() and candidate.is_file() and candidate.stat().st_nlink > 1:
            raise LocalError("不支持多硬链接文件")
        return candidate

    # 功能：有界读取UTF-8文本和原始字节摘要，拒绝设备、二进制及大文件。
    def read(self, relative: str) -> dict:
        path = self.path(relative)
        if not path.exists() or not stat.S_ISREG(path.stat().st_mode):
            raise LocalError("请选择存在的普通文本文件")
        with path.open("rb") as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES or b"\x00" in raw:
            raise LocalError("仅支持不超过128KB的UTF-8文本文件")
        try:
            content = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise LocalError("文件不是UTF-8文本") from None
        return {"path": path.relative_to(self.root).as_posix(), "sha256": digest(raw),
                "content": content, "bytes": len(raw)}

    # 功能：最多遍历2000项、返回200项文件名；不跟随链接或读取文件正文。
    def listing(self, relative=".") -> dict:
        directory = self.path(relative)
        if not directory.is_dir():
            raise LocalError("请选择存在的文件夹")
        files, visited = [], 0
        for parent, dirs, names in os.walk(directory, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not private_path([d])
                             and not (Path(parent) / d).is_symlink()
                             and not (Path(parent) / d).is_junction())
            for name in sorted(names):
                visited += 1
                if not private_path([name]):
                    path = Path(parent) / name
                    if not path.is_symlink():
                        files.append(path.relative_to(self.root).as_posix())
                if visited >= 2000 or len(files) >= 200:
                    return {"files": files, "truncated": True}
            visited += len(dirs)
            if visited >= 2000:
                return {"files": files, "truncated": True}
        return {"files": files, "truncated": False}

    # 功能：准备精确文件差异和旧摘要，已有文件必须先提供读取时的摘要。
    def preview_write(self, request: ToolRequest) -> dict:
        path = self.path(request.path, write=True)
        raw = request.content.encode("utf-8")
        if len(raw) > MAX_FILE_BYTES or b"\x00" in raw:
            raise LocalError("写入内容须为不超过128KB的文本")
        before = self.read(request.path) if path.exists() else None
        if (before["sha256"] if before else None) != request.expected_sha256:
            raise LocalError("文件版本已变化，或未提供旧摘要；请重新读取后生成修改")
        old = before["content"] if before else ""
        diff = "".join(difflib.unified_diff(old.splitlines(True), request.content.splitlines(True),
                                        fromfile=request.path, tofile=request.path))
        return {"path": request.path, "before_sha256": request.expected_sha256,
                "after_sha256": digest(raw), "diff": diff[:MAX_FILE_BYTES],
                "before_content": old if before else None, "creates_file": before is None}

    # 功能：确认后重验版本、在同目录写临时文件并原子替换；不提供删除操作。
    def write(self, request: ToolRequest) -> dict:
        receipt = self.preview_write(request)
        path = self.path(request.path, write=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path(request.path, write=True)
        fd, temporary = tempfile.mkstemp(prefix=".autodidact-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(request.content.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            if path.exists():
                os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
            self.preview_write(request)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return receipt

    # 功能：构造固定argv，不接受shell、额外参数、任意脚本或模型提供的可执行文件。
    def command(self, request: ToolRequest) -> list[str]:
        paths = [str(self.path(p)) for p in request.paths]
        if request.command in {"git_status", "git_diff"}:
            executable = shutil.which("git")
            if not executable or Path(executable).resolve().is_relative_to(self.root):
                raise LocalError("未找到项目目录之外的已安装Git，请安装并配置系统Git")
            # Git配置、外部diff和pager均不由项目控制；这里只提供只读状态和差异。
            base = [str(Path(executable).resolve()), "--no-pager", "-c", "core.fsmonitor=false",
                    "-c", "diff.external=", "-c", "core.pager=cat"]
            if request.command == "git_status":
                return base + ["status", "--short", "--untracked-files=no"]
            return base + ["diff", "--no-ext-diff", "--no-textconv", "--", *paths]
        if request.command == "python_check":
            if not paths or any(not p.endswith(".py") for p in paths):
                raise LocalError("语法检查需要显式选择项目内Python文件")
            for path in request.paths:
                self.read(path)
            # 固定驱动仅AST解析，不导入目标代码，也不生成pyc。
            driver = "import ast,sys,tokenize;[(ast.parse(tokenize.open(p).read(),p),print(p+' OK')) for p in sys.argv[1:]]"
            return [sys.executable, "-I", "-c", driver, *paths]
        return [sys.executable, "-I", "-m", "pytest", "-q", "--maxfail=1", "--", *paths]


class ProcessTree:
    # 功能：Windows用关闭即终止的Job Object收纳进程树；其它系统用独立进程组。
    def __init__(self, pid):
        self.pid, self.handle = pid, None
        if os.name != "nt":
            return
        from ctypes import wintypes as w

        class Basic(ctypes.Structure):
            _fields_ = [("user", ctypes.c_int64), ("job", ctypes.c_int64),
                        ("flags", w.DWORD), ("min", ctypes.c_size_t), ("max", ctypes.c_size_t),
                        ("active", w.DWORD), ("affinity", ctypes.c_size_t),
                        ("priority", w.DWORD), ("scheduling", w.DWORD)]

        class Info(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", ctypes.c_uint64 * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [w.LPVOID, w.LPCWSTR]
        kernel.CreateJobObjectW.restype = w.HANDLE
        kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        kernel.OpenProcess.restype = w.HANDLE
        kernel.CloseHandle.argtypes = [w.HANDLE]
        handle = kernel.CreateJobObjectW(None, None)
        info = Info()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        process = kernel.OpenProcess(0x0101, False, pid)
        try:
            if not handle or not process or not kernel.SetInformationJobObject(
                handle, 9, ctypes.byref(info), ctypes.sizeof(info)
            ) or not kernel.AssignProcessToJobObject(handle, process):
                if handle:
                    kernel.CloseHandle(handle)
                raise LocalError("无法建立本机子进程生命周期控制，命令已停止")
            self.handle, self.kernel = handle, kernel
        finally:
            if process:
                kernel.CloseHandle(process)

    # 功能：正常结束、超时及取消均清理整个受管进程组，拒绝后台遗留任务。
    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
        elif os.name != "nt":
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


# 功能：显式批准后运行argv，清理继承环境并有界排空输出；不承诺隔离项目代码权限。
async def execute_command(workspace: Workspace, request: ToolRequest) -> dict:
    argv = workspace.command(request)
    env = {k: v for k, v in os.environ.items() if k.upper() in {
        "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE",
        "LOCALAPPDATA", "APPDATA", "COMSPEC", "PATHEXT", "LANG", "LC_ALL",
    }}
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", GIT_TERMINAL_PROMPT="0",
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS="0")
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {
        "start_new_session": True}
    process = await asyncio.create_subprocess_exec(
        *argv, cwd=workspace.root, env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **options)
    tree = None
    output, total = bytearray(), 0

    # 功能：持续读取避免管道阻塞，超出保存上限后仅排空不累计内存。
    async def drain():
        nonlocal total
        while chunk := await process.stdout.read(4096):
            total += len(chunk)
            output.extend(chunk[:max(0, MAX_OUTPUT_BYTES - len(output))])

    reader = asyncio.create_task(drain())
    timed_out = False
    try:
        tree = ProcessTree(process.pid)
        try:
            await asyncio.wait_for(asyncio.shield(process.wait()), request.timeout)
        except TimeoutError:
            timed_out = True
    finally:
        if tree:
            tree.close()
        if process.returncode is None:
            process.kill()
        await process.wait()
        await asyncio.wait_for(reader, 5)
    return {"command": request.command, "argv": argv, "exit_code": process.returncode,
            "timed_out": timed_out, "output": output.decode("utf-8", errors="replace"),
            "output_truncated": total > MAX_OUTPUT_BYTES, "output_bytes": total,
            "passed": process.returncode == 0 and not timed_out}
