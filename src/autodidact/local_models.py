# 文件职责：管理本机对话模型配置，Windows密钥使用当前用户DPAPI加密，页面不回显密钥。
from __future__ import annotations

import ctypes
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from autodidact.brain.llm import OpenAICompatibleLLM, build_llm
from autodidact.config import RuntimeSettings, runtime_settings
from autodidact.local_tools import LocalError


class ModelConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str = Field(min_length=1, max_length=1000)
    model: str = Field(min_length=1, max_length=128)
    api_key: str = Field(default="", max_length=4000, repr=False)


# 功能：调用当前Windows用户的DPAPI，不使用机器范围标记或明文密钥文件。
def protect_key(data: bytes, *, decrypt=False) -> bytes:
    if os.name != "nt":
        raise LocalError("此平台只支持本次运行保存密钥，重启后需要重新输入")
    from ctypes import wintypes as w

    class Blob(ctypes.Structure):
        _fields_ = [("size", w.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, target = Blob(len(data), buffer), Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [w.HLOCAL]
    kernel.LocalFree.restype = w.HLOCAL
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                        ctypes.c_void_p, ctypes.c_void_p, w.DWORD, ctypes.POINTER(Blob)]
    function.restype = w.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise LocalError("无法保护或读取本机密钥，请在模型配置中重新输入")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


class LocalModels:
    # 功能：载入本机独立配置，损坏的密钥不阻止启动或读取已有示范。
    def __init__(self, directory: Path):
        self.path = directory / "model.json"
        self.configuration = None
        self.error = ""
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                key = protect_key(bytes.fromhex(data["protected_key"]), decrypt=True).decode()
                self.configuration = ModelConfiguration(base_url=data["base_url"], model=data["model"], api_key=key)
            except Exception:  # noqa: BLE001 - 错误信息不得包含密钥或密文。
                self.error = "本机模型配置暂不可用，请重新填写；已有示范和对话保留。"

    # 功能：仅返回模型名、地址和是否配置，不显示任何密钥片段或声称连接已验证。
    def info(self):
        if self.configuration:
            config = self.configuration
            return {"ready": True, "provider": "openai_compatible", "model": config.model,
                    "base_url": config.base_url, "key_saved": os.name == "nt",
                    "message": "已配置；首次发送任务时才会请求模型。"}
        settings = runtime_settings()
        ready = settings.llm_provider == "openai_compatible" and bool(settings.llm_api_key and settings.llm_model)
        # 旧环境可能把账号或密钥写入URL；页面只显示去除认证/查询参数后的地址。
        try:
            parsed = urlsplit(settings.llm_base_url)
            endpoint = parsed._replace(netloc=parsed.netloc.rsplit("@", 1)[-1], query="", fragment="").geturl()
        except ValueError:
            endpoint = ""
        return {"ready": ready, "provider": settings.llm_provider, "model": settings.llm_model,
                "base_url": endpoint, "key_saved": False,
                "message": self.error or ("使用已有运行配置。" if ready else "先保存示范；连接模型后可发送新任务。")}

    # 功能：用户显式保存配置；换端点必须重新提供密钥，避免将旧密钥发往新地址。
    def save(self, request: ModelConfiguration):
        endpoint = request.base_url.strip().rstrip("/")
        parsed = urlsplit(endpoint)
        if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment
                or (parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"})):
            raise LocalError("请填写无账号/查询参数的HTTPS API地址；本地服务可用回环HTTP")
        key = request.api_key.strip()
        if not key:
            if not self.configuration or endpoint != self.configuration.base_url:
                raise LocalError("首次配置或更换API地址时，请填写对应密钥")
            key = self.configuration.api_key
        config = ModelConfiguration(base_url=endpoint, model=request.model.strip(), api_key=key)
        if os.name == "nt":
            payload = {"base_url": endpoint, "model": config.model,
                       "protected_key": protect_key(key.encode()).hex()}
            fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".model-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(payload, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        self.configuration, self.error = config, ""
        return self.info()

    # 功能：仅在用户发送/修订时构造原有兼容API模型，Mock不会伪造示范学习输出。
    def build(self):
        if self.configuration:
            config = self.configuration
            settings = RuntimeSettings(_env_file=None, llm_provider="openai_compatible",
                llm_base_url=config.base_url, llm_model=config.model, llm_api_key=config.api_key,
                llm_max_output_tokens=4096)
            return OpenAICompatibleLLM(settings)
        llm = build_llm()
        if llm.provider_name == "mock":
            raise LocalError("当前尚未连接真实模型。可以先保存示范，再在右上角配置模型。")
        if hasattr(llm, "max_output_tokens"):
            llm.max_output_tokens = min(llm.max_output_tokens, 4096)
        return llm
