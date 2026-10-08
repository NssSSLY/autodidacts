# 文件职责：使用人工已登录的浏览器资料与站点选择器读取网页模型回答，不自动登录或绕过验证。
from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright
from pydantic import BaseModel

from autodidact.schemas import ModelAnswer
from autodidact.web_models.base import ExternalCognitiveSource


class WebModelConfig(BaseModel):
    provider: str
    url: str
    persistent_profile_dir: str
    input_selector: str
    send_selector: str
    assistant_message_selector: str
    new_chat_selector: str | None = None
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
        try:
            async with async_playwright() as p:
                ctx = await p.chromium.launch_persistent_context(
                    user_data_dir=str(Path(self.config.persistent_profile_dir)),
                    headless=self.config.headless,
                )
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.goto(self.config.url, wait_until="domcontentloaded")
                from autodidact.provider_runtime import reject_challenge

                reject_challenge(await page.content())
                ok = await page.locator(self.config.input_selector).count() > 0
                await ctx.close()
                return ok
        # Health checks intentionally collapse provider/UI failures into a degraded state.
        except Exception:  # noqa: BLE001
            return False

    # 功能：通过配置 UI 输入问题并等待回答完成，收集正文/引用作为等级 0 观察，最后关闭上下文。
    async def ask(self, prompt: str) -> ModelAnswer:
        async with async_playwright() as p:
            ctx = await p.chromium.launch_persistent_context(
                user_data_dir=str(Path(self.config.persistent_profile_dir)),
                headless=self.config.headless,
            )
            try:
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.goto(self.config.url, wait_until="domcontentloaded")
                if self.config.new_chat_selector:
                    locator = page.locator(self.config.new_chat_selector)
                    if await locator.count():
                        await locator.first.click()
                old_count = await page.locator(self.config.assistant_message_selector).count()
                box = page.locator(self.config.input_selector).first
                await box.wait_for(state="visible", timeout=30000)
                await box.fill(prompt)
                await page.locator(self.config.send_selector).first.click()
                deadline = asyncio.get_running_loop().time() + self.config.response_timeout_seconds
                last, stable, completed = "", 0, False
                while asyncio.get_running_loop().time() < deadline:
                    nodes = page.locator(self.config.assistant_message_selector)
                    count = await nodes.count()
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
