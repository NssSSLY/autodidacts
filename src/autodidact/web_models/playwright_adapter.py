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

    def __init__(self, config: WebModelConfig):
        self.config = config

    async def health_check(self) -> bool:
        try:
            async with async_playwright() as p:
                ctx = await p.chromium.launch_persistent_context(
                    user_data_dir=str(Path(self.config.persistent_profile_dir)),
                    headless=self.config.headless,
                )
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.goto(self.config.url, wait_until="domcontentloaded")
                ok = await page.locator(self.config.input_selector).count() > 0
                await ctx.close()
                return ok
        # Health checks intentionally collapse provider/UI failures into a degraded state.
        except Exception:  # noqa: BLE001
            return False

    async def ask(self, prompt: str) -> ModelAnswer:
        async with async_playwright() as p:
            ctx = await p.chromium.launch_persistent_context(
                user_data_dir=str(Path(self.config.persistent_profile_dir)),
                headless=self.config.headless,
            )
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(self.config.url, wait_until="domcontentloaded")
            if self.config.new_chat_selector:
                locator = page.locator(self.config.new_chat_selector)
                if await locator.count():
                    await locator.first.click()
            input_box = page.locator(self.config.input_selector).first
            await input_box.wait_for(state="visible", timeout=30_000)
            await input_box.fill(prompt)
            await page.locator(self.config.send_selector).first.click()

            timeout_ms = self.config.response_timeout_seconds * 1000
            if self.config.done_selector:
                await page.locator(self.config.done_selector).wait_for(state="visible", timeout=timeout_ms)
            else:
                # Poll until the last assistant message stops changing for 2.5 seconds.
                deadline = asyncio.get_event_loop().time() + self.config.response_timeout_seconds
                last = ""
                stable = 0
                while asyncio.get_event_loop().time() < deadline:
                    nodes = page.locator(self.config.assistant_message_selector)
                    count = await nodes.count()
                    current = await nodes.nth(count - 1).inner_text() if count else ""
                    if current and current == last:
                        stable += 1
                    else:
                        stable = 0
                    last = current
                    if stable >= 5:
                        break
                    await asyncio.sleep(0.5)
            nodes = page.locator(self.config.assistant_message_selector)
            count = await nodes.count()
            if not count:
                await ctx.close()
                raise RuntimeError("No assistant response found")
            text = await nodes.nth(count - 1).inner_text()
            await ctx.close()
            return ModelAnswer(
                provider=self.config.provider,
                access_type="web",
                displayed_model=None,
                prompt=prompt,
                response=text,
                citations=[],
                metadata={"url": self.config.url},
            )
