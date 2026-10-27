"""六个固定网页辅助来源的个人配置；单文件原子保存，不改主模型/旧 YAML/登录凭据。"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from autodidact.config import runtime_settings
from autodidact.web_models.playwright_adapter import WebModelConfig
from autodidact.web_models.profile_lock import file_lease

PRESETS = {
    "aux_deepseek": ("DeepSeek", "https://chat.deepseek.com/"),
    "aux_zhipu": ("智谱清言", "https://chatglm.cn/"),
    "aux_kimi": ("Kimi", "https://www.kimi.com/"),
    "aux_chatgpt": ("ChatGPT", "https://chatgpt.com/"),
    "aux_claude": ("Claude 网页聊天", "https://claude.ai/new"),
    "aux_claude_code": ("Claude Code 网页任务", "https://claude.ai/code"),
}
SourceID = Literal[
    "aux_deepseek", "aux_zhipu", "aux_kimi", "aux_chatgpt", "aux_claude", "aux_claude_code"
]


class AuxiliaryEntry(BaseModel):
    """只接收已知站点的操作配置，不允许网页提交任意 URL、文件路径或秘密。"""

    model_config = {"extra": "forbid", "str_strip_whitespace": True}
    id: SourceID
    enabled: bool = Field(default=False, strict=True)
    mode: Literal["manual", "browser"] = "manual"
    input_selector: str = Field(default="", max_length=500)
    send_selector: str = Field(default="", max_length=500)
    assistant_message_selector: str = Field(default="", max_length=500)
    new_chat_selector: str = Field(default="", max_length=500)
    done_selector: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def check_mode(self):
        """任务型 Claude Code 禁止自动提交；自动通道必须具备新会话及三个基本定位器。"""
        if self.id == "aux_claude_code" and self.mode != "manual":
            raise ValueError("Claude Code 只开放人工采集，不自动创建或执行代码任务")
        if (
            self.enabled
            and self.mode == "browser"
            and not all(
                (
                    self.input_selector,
                    self.send_selector,
                    self.assistant_message_selector,
                    self.new_chat_selector,
                )
            )
        ):
            raise ValueError("网页提问需配置输入框、发送按钮、助手回答和新会话四个选择器")
        return self

    def browser_config(self):
        """为专用资料构造适配器配置；与日常浏览器及另一个 Claude 来源分离。"""
        return WebModelConfig(
            provider=self.id,
            url=PRESETS[self.id][1],
            persistent_profile_dir=str(Path(".browser/auxiliary") / self.id),
            input_selector=self.input_selector,
            send_selector=self.send_selector,
            assistant_message_selector=self.assistant_message_selector,
            new_chat_selector=self.new_chat_selector or None,
            done_selector=self.done_selector or None,
            require_new_chat=True,
            headless=False,
        )


class AuxiliaryRegistry(BaseModel):
    model_config = {"extra": "forbid"}
    protocol: Literal["auxiliary_web_v1"] = "auxiliary_web_v1"
    sources: list[AuxiliaryEntry] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def unique(self):
        """重复 ID 拒绝载入，避免隐藏覆盖。"""
        if len({entry.id for entry in self.sources}) != len(self.sources):
            raise ValueError("网页辅助来源 ID 重复")
        return self

    def get(self, provider):
        """返回显式条目或未启用默认条目，不自动创建配置文件。"""
        return next((s for s in self.sources if s.id == provider), AuxiliaryEntry(id=provider))


class SaveAuxiliary(BaseModel):
    model_config = {"extra": "forbid"}
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    source: AuxiliaryEntry


def registry_path():
    """路径仅由本机配置确定，不从 HTTP 请求获取。"""
    return Path(runtime_settings().web_auxiliary_config)


def read_registry():
    """有界读取个人清单，缺文件表示尚未配置；内容错误不覆盖为默认值。"""
    path = registry_path()
    raw = b""
    if path.exists():
        with path.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("辅助来源配置超过64KiB")
    registry = AuxiliaryRegistry.model_validate_json(raw) if raw else AuxiliaryRegistry()
    return registry, hashlib.sha256(raw).hexdigest()


def save_source(request):
    """锁内比较版本并原子替换单个条目；保留其他来源，失败不破坏原文件。"""
    path = registry_path()
    with file_lease(str(path.resolve()) + ".lock"):
        registry, revision = read_registry()
        if request.revision != revision:
            raise ValueError("配置已改变，请重新加载后再保存；当前输入尚未写入")
        values = {s.id: s for s in registry.sources}
        values[request.source.id] = request.source
        registry.sources = list(values.values())
        # 仅启用的自动来源进入 F11；保存前验证合并后的清单，避免隐藏同名来源。
        from autodidact.cognitive_sources import read_catalog

        if any(s.enabled and s.mode == "browser" for s in registry.sources):
            read_catalog(auxiliary=registry)
        data = registry.model_dump_json(indent=2).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return hashlib.sha256(data).hexdigest()


def managed_config(provider):
    """仅已启用自动来源可由网页服务调用；人工采集绝不能被后台当成模型发送。"""
    entry = read_registry()[0].get(provider)
    if not entry.enabled or entry.mode != "browser":
        raise ValueError("该辅助来源未启用网页自动提问")
    return entry.browser_config()
