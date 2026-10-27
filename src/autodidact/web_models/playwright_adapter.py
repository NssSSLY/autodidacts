# 文件职责：使用人工已登录的浏览器资料与站点选择器读取网页模型回答，不自动登录或绕过验证。
from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright
from pydantic import BaseModel

from autodidact.schemas import ModelAnswer
from autodidact.web_models.base import ExternalCognitiveSource
from autodidact.web_models.profile_lock import profile_lease


class WebModelConfig(BaseModel):
    provider: str
    url: str
    persistent_profile_dir: str
    input_selector: str
    send_selector: str
    assistant_message_selector: str
    new_chat_selector: str | None = None
    require_new_chat: bool = False
    done_selector: str | None = None
    headless: bool = False
    response_timeout_seconds: int = 120


class PlaywrightWebModelAdapter(ExternalCognitiveSource):
    """Generic UI adapter. It does not log in, bypass CAPTCHA, or evade anti-bot controls."""

    # 功能：保存网页 URL、选择器、浏览器资料路径与超时配置。
    def __init__(self, config: WebModelConfig):
        self.config = config

    # 功能：打开配置页面检查输入框是否存在；浏览器/网站失败返回 False，不绕过障碍。
    async def health_check(self) -> bool:
        """与登录/提问互斥检查，资料被占用时明确返回不可用。"""
        try:
            with profile_lease(self.config.persistent_profile_dir):
                return await self._health_check_unlocked()
        except ValueError:
            return False

    async def _health_check_unlocked(self) -> bool:
        """检查页面输入框，不发送内容；导航或验证失败也关闭本次浏览器。"""
        try:
            async with async_playwright() as p:
                ctx = await p.chromium.launch_persistent_context(
                    user_data_dir=str(Path(self.config.persistent_profile_dir)),
                    headless=self.config.headless,
                )
                try:
                    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                    await page.goto(self.config.url, wait_until="domcontentloaded")
                    from autodidact.provider_runtime import reject_challenge

                    reject_challenge(await page.content())
                    return await page.locator(self.config.input_selector).count() > 0
                finally:
                    await ctx.close()
        # Health checks intentionally collapse provider/UI failures into a degraded state.
        except Exception:  # noqa: BLE001
            return False

    # 功能：通过合法配置UI取得等级0回答；F11强制新会话且旧回答清空才发送，不自动登录/重发。
    async def ask(self, prompt: str) -> ModelAnswer:
        """锁定专用资料后发送一次；忙碌时拒绝，不自动重发。"""
        with profile_lease(self.config.persistent_profile_dir):
            return await self._ask_unlocked(prompt)

    async def _ask_unlocked(self, prompt: str) -> ModelAnswer:
        """沿既有新会话及回答完成检测执行，不复用其他正在操作的浏览器窗口。"""
        async with async_playwright() as p:
            ctx = await p.chromium.launch_persistent_context(
                user_data_dir=str(Path(self.config.persistent_profile_dir)),
                headless=self.config.headless,
            )
            try:
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.goto(self.config.url, wait_until="domcontentloaded")
                from autodidact.provider_runtime import reject_challenge

                reject_challenge(await page.content())
                if self.config.require_new_chat and not self.config.new_chat_selector:
                    raise ValueError("独立盲答须配置并执行新会话动作")
                if self.config.new_chat_selector:
                    locator = page.locator(self.config.new_chat_selector)
                    if self.config.require_new_chat:
                        await locator.first.wait_for(state="visible", timeout=30000)
                        if await locator.count() != 1:
                            raise ValueError("新会话选择器必须唯一，拒绝点击不确定目标")
                        await locator.first.click()
                        cleared_deadline = asyncio.get_running_loop().time() + 30
                        while await page.locator(self.config.assistant_message_selector).count():
                            if asyncio.get_running_loop().time() >= cleared_deadline:
                                raise ValueError("新会话旧回答未清空，拒绝发送独立问题")
                            await asyncio.sleep(0.2)
                    elif await locator.count():
                        await locator.first.click()
                old_count = await page.locator(self.config.assistant_message_selector).count()
                box = page.locator(self.config.input_selector).first
                await box.wait_for(state="visible", timeout=30000)
                if self.config.require_new_chat and (
                    await page.locator(self.config.input_selector).count() != 1
                    or await page.locator(self.config.send_selector).count() != 1
                ):
                    raise ValueError("独立问题输入框/发送按钮必须唯一，拒绝不确定提交")
                await box.fill(prompt)
                await page.locator(self.config.send_selector).first.click()
                deadline = asyncio.get_running_loop().time() + self.config.response_timeout_seconds
                last, stable, completed = "", 0, False
                while asyncio.get_running_loop().time() < deadline:
                    nodes = page.locator(self.config.assistant_message_selector)
                    count = await nodes.count()
                    if self.config.require_new_chat and count > 1:
                        raise ValueError("独立会话匹配多个回答节点，需核对助手最终回答选择器")
                    current = await nodes.last.inner_text() if count > old_count else ""
                    if (
                        self.config.done_selector
                        and current
                        and await page.locator(self.config.done_selector).is_visible()
                    ):
                        completed = True
                        break
                    stable = stable + 1 if current and current == last else 0
                    last = current
                    if not self.config.done_selector and stable >= 5:
                        completed = True
                        break
                    await asyncio.sleep(0.5)
                if not completed:
                    raise TimeoutError("网页模型回答未在限时内完成")
                node = page.locator(self.config.assistant_message_selector).last
                text = (await node.inner_text())[:24000]
                links = await node.locator("a[href]").evaluate_all(
                    "(nodes) => nodes.map(n => n.href)"
                )
                citations = list(
                    dict.fromkeys(u for u in links if u.startswith(("https://", "http://")))
                )
                return ModelAnswer(
                    provider=self.config.provider,
                    access_type="web",
                    prompt=prompt,
                    response=text,
                    citations=citations,
                    conversation_ref=page.url,
                    metadata={"url": self.config.url, "evidence_level": 0},
                )
            finally:
                await ctx.close()
