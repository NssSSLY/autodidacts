"""辅助来源工作台动作：人工登录、无发送检查、显式有预算提问和人工0级观察留档。"""

from __future__ import annotations

import asyncio
import hashlib
from urllib.parse import urlsplit
from uuid import UUID

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from autodidact import models
from autodidact.learning.ground_truth import digest
from autodidact.provider_runtime import reject_challenge
from autodidact.web_models.auxiliary_config import (
    PRESETS,
    SaveAuxiliary,
    SourceID,
    read_registry,
    save_source,
)
from autodidact.web_models.profile_lock import profile_lease
from autodidact.web_models.service import WebModelService


class SourceAction(BaseModel):
    model_config = {"extra": "forbid"}
    id: SourceID
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class AskSource(SourceAction):
    prompt: str = Field(min_length=1, max_length=2000)
    consent: bool = Field(strict=True)

    @model_validator(mode="after")
    def approved(self):
        """每次请求需显式确认向网站发送数据，空白问题不能提交。"""
        if not self.consent or not self.prompt.strip():
            raise ValueError("请明确允许发送这次问题，并填写非空问题")
        return self


class ImportSource(AskSource):
    observation_id: UUID
    response: str = Field(min_length=1, max_length=8000)
    conversation_ref: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def valid_import(self):
        """人工出处只接收对应站点 HTTPS 无凭据链接；不抓取链接、不认证文字来源。"""
        if not self.response.strip():
            raise ValueError("请粘贴非空回答原文")
        if self.conversation_ref:
            url = urlsplit(self.conversation_ref)
            expected = urlsplit(PRESETS[self.id][1]).hostname
            if (
                url.scheme != "https"
                or url.hostname != expected
                or url.username
                or url.password
                or url.port not in (None, 443)
                or url.query
                or url.fragment
            ):
                raise ValueError("出处须为对应网站的HTTPS会话链接，不含凭据、查询参数或片段")
        return self


async def import_observation(engine, request):
    """幂等存储人工声明的观察；不消耗模型预算、不写Evidence/Claim/Belief或假装自动采集。"""
    values = {
        "id": request.observation_id,
        "provider": request.id,
        "access_type": "web_manual",
        "prompt": request.prompt,
        "response": request.response,
        "response_hash": hashlib.sha256(request.response.encode()).hexdigest(),
        "conversation_ref": request.conversation_ref or None,
        "metadata_json": {
            "evidence_level": 0,
            "collection_method": "manual_paste",
            "provenance_verified": False,
            "role": "auxiliary_source",
            "config_revision": request.revision,
            "error": None,
        },
    }
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await session.execute(
            insert(models.ModelObservation)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[models.ModelObservation.id],
            )
        )
        row = await session.get(models.ModelObservation, request.observation_id)
        if row is None or any(
            getattr(row, key) != values[key]
            for key in (
                "provider",
                "access_type",
                "prompt",
                "response",
                "conversation_ref",
            )
        ):
            raise ValueError("观察ID已有不同内容，请刷新记录核对，未覆盖旧观察")
    return {
        "observation_id": str(request.observation_id),
        "evidence_level": 0,
        "collection_method": "manual_paste",
        "provenance_verified": False,
        "note": "仅保存人工粘贴观察；未调用网站、未核验出处、未晋升信念。",
    }


class AuxiliaryWorkbench:
    """仅服务进程持有人工登录窗口，最长15分钟；不读取/返回Cookie，不管理用户日常浏览器。"""

    def __init__(self, engine):
        """保存数据库句柄及本进程窗口状态，构造时不联网、不读库。"""
        self.engine = engine
        self.context = self.playwright = self.lease = self.login_id = None
        self.timer = None
        self.busy = asyncio.Lock()

    def snapshot(self):
        """读取配置声明和本进程窗口状态；启用不等于网站登录或实测成功。"""
        registry, revision = read_registry()
        return {
            "revision": revision,
            "sources": [
                {
                    **registry.get(provider).model_dump(),
                    "name": name,
                    "url": url,
                    "login_open": self.login_id == provider,
                    "automatic_supported": provider != "aux_claude_code",
                    "verification": "待真实站点验收",
                }
                for provider, (name, url) in PRESETS.items()
            ],
            "note": "不更换主模型，不自动启用学习循环；人工采集为未核验的0级观察。",
        }

    async def close(self):
        """停止计时并释放本进程窗口及资料锁；服务退出也调用，保留登录资料文件。"""
        if self.timer and self.timer is not asyncio.current_task():
            self.timer.cancel()
        self.timer = None
        try:
            if self.context:
                await self.context.close()
        finally:
            self.context = self.login_id = None
            try:
                if self.playwright:
                    await self.playwright.stop()
            finally:
                self.playwright = None
                if self.lease:
                    self.lease.__exit__(None, None, None)
                    self.lease = None

    async def expire(self):
        """15分钟后关闭托管登录窗口，不后台续期、不重新登录。"""
        await asyncio.sleep(900)
        async with self.busy:
            await self.close()

    async def open_login(self, entry):
        """只打开固定官方入口，账号/验证码由用户在专用可见窗口自行输入。"""
        await self.close()
        config = entry.browser_config()
        lease = profile_lease(config.persistent_profile_dir)
        lease.__enter__()
        self.lease = lease
        try:
            self.playwright = await async_playwright().start()
            self.context = await self.playwright.chromium.launch_persistent_context(
                config.persistent_profile_dir,
                headless=False,
            )
            page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            await page.goto(config.url, wait_until="domcontentloaded", timeout=30000)
            self.login_id = entry.id
            self.timer = asyncio.create_task(self.expire())
        except BaseException:
            await self.close()
            raise
        return {
            "status": "login_open",
            "note": "请在新窗口人工登录，完成后点击关闭登录窗口；15分钟后自动关闭。未发送问题。",
        }

    async def check(self, entry):
        """只检查输入、新会话、发送元素的唯一可见性及选择器语法；不点击、不填充。"""
        if entry.mode != "browser":
            raise ValueError("人工采集无需自动检查；网页提问模式配置选择器后再检查")
        config = entry.browser_config()
        with profile_lease(config.persistent_profile_dir):
            async with async_playwright() as runtime:
                context = await runtime.chromium.launch_persistent_context(
                    config.persistent_profile_dir,
                    headless=False,
                )
                try:
                    page = context.pages[0] if context.pages else await context.new_page()
                    await page.goto(config.url, wait_until="domcontentloaded", timeout=30000)
                    reject_challenge(await page.content())
                    checks = {}
                    for key in ("input_selector", "send_selector", "new_chat_selector"):
                        selector = getattr(entry, key)
                        if not selector:
                            checks[key] = False
                            continue
                        locator = page.locator(selector)
                        checks[key] = await locator.count() == 1 and await locator.is_visible()
                    answer_count = (
                        await page.locator(entry.assistant_message_selector).count()
                        if entry.assistant_message_selector
                        else None
                    )
                    if entry.done_selector:
                        await page.locator(entry.done_selector).count()
                    return {
                        "status": "page_detected" if all(checks.values()) else "needs_setup",
                        "checks": checks,
                        "existing_answer_nodes": answer_count,
                        "note": "只检查页面元素；未测试发送、回答截取或新会话隔离，不代表接入已验收。",
                    }
                finally:
                    await context.close()

    async def history(self, provider):
        """只读最近20条0级观察，正文有界；不调用模型或提升任何知识状态。"""
        provider = SourceAction(id=provider, revision="0" * 64).id
        sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with sessions() as session:
            rows = (
                await session.scalars(
                    select(models.ModelObservation)
                    .where(
                        models.ModelObservation.provider == provider,
                    )
                    .order_by(models.ModelObservation.created_at.desc(), models.ModelObservation.id)
                    .limit(20)
                )
            ).all()
        return {
            "items": [
                {
                    "id": str(row.id),
                    "prompt": row.prompt[:2000],
                    "response": row.response[:8000],
                    "created_at": row.created_at,
                    "access_type": row.access_type,
                    "conversation_ref": row.conversation_ref,
                    "metadata": row.metadata_json,
                    "response_truncated": len(row.response) > 8000,
                    "evidence_level": 0,
                }
                for row in rows
            ]
        }

    async def action(self, name, body):
        """严格动作分发，版本及授权检查在浏览器/写库之前；同一工作台不重叠执行。"""
        if self.busy.locked():
            raise ValueError("辅助来源操作正在进行，请等待完成；不会自动重复发送")
        async with self.busy:
            if name == "close":
                if body:
                    raise ValueError("关闭窗口不接受额外字段")
                await self.close()
                return {"status": "closed"}
            if name == "save":
                if self.context:
                    raise ValueError("请先关闭登录窗口再修改配置")
                save_source(SaveAuxiliary.model_validate(body))
                return self.snapshot()
            schema = {
                "login": SourceAction,
                "check": SourceAction,
                "ask": AskSource,
                "import": ImportSource,
            }.get(name)
            if schema is None:
                raise ValueError("未知辅助来源操作")
            request = schema.model_validate(body)
            registry, revision = read_registry()
            if request.revision != revision:
                raise ValueError("配置已改变，请先重新加载")
            entry = registry.get(request.id)
            if name in {"login", "check"}:
                try:
                    return await (self.open_login(entry) if name == "login" else self.check(entry))
                except PlaywrightError as exc:
                    raise ValueError(
                        "浏览器或页面操作失败：请确认已用项目Python执行 python -m playwright "
                        "install chromium，并在有桌面的本机运行；关闭占用窗口后核查网络、登录与选择器。"
                        "本次没有自动提交问题。"
                    ) from exc
            if not entry.enabled:
                raise ValueError("请先启用该来源并保存配置")
            if name == "import":
                return await import_observation(self.engine, request)
            if entry.mode != "browser":
                raise ValueError("该来源处于人工采集模式，不自动发送")
            answer = await WebModelService(self.engine).ask(
                entry.id,
                request.prompt,
                fresh_chat=True,
                expected_config_sha256=digest(entry.browser_config().model_dump(mode="json")),
                role="auxiliary_source",
                config_revision=revision,
            )
            return {
                "answer": answer.model_dump(),
                "evidence_level": 0,
                "note": "已按网页回答留档；仍未独立核验，未晋升信念。",
            }
