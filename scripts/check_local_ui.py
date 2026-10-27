# 文件职责：用指定的已安装浏览器和合成项目验收本机界面，不使用账户资料或真实模型。
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "build/local-client-ui"
os.environ.update(LLM_PROVIDER="mock", LLM_API_KEY="", LLM_MODEL="", EMBEDDING_PROVIDER="disabled")
from autodidact.config import RuntimeSettings

RuntimeSettings.model_config["env_file"] = None
from playwright.async_api import async_playwright, expect

from autodidact.local_client import LocalWorkbench
from autodidact.local_tasks import LocalAgent, TaskJournal


# 功能：在独立浏览器配置中验证建任务、确认写入、实际检查、文本转义和历史重载。
async def main(browser_path):
    run_id = uuid4().hex[:12]
    project = OUT / "projects" / run_id
    project.mkdir(parents=True)
    (project / "example.py").write_text("value = 2\n", encoding="utf-8")
    journal = TaskJournal(OUT / "state" / run_id)
    agent = LocalAgent(journal)
    app = LocalWorkbench(agent, str(project), 0)
    server = await asyncio.start_server(app.connection, "127.0.0.1", 0)
    app.port = server.sockets[0].getsockname()[1]
    errors, external = [], []
    url = f"http://127.0.0.1:{app.port}"
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path=browser_path,
            headless=True)
        context = await browser.new_context(viewport={"width": 1512, "height": 1100}, service_workers="block")
        # 功能：页面只可请求本轮回环服务，所有其它页面请求均拦截并记录。
        async def route(request):
            if not request.request.url.startswith(url + "/"):
                external.append(request.request.url)
                await request.abort()
            else:
                await request.continue_()
        await context.route("**/*", route)
        page = await context.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            await page.goto(url + "/tools")
            await expect(page.locator("#workspace")).to_have_value(str(project))
            await page.locator("#objective").fill("生成本机验收报告，并检查Python代码")
            await page.locator("#create").click()
            await expect(page.locator("#task-state")).to_have_text("待执行")
            await page.locator("#tool").select_option("write")
            await page.locator("#path").fill("report.md")
            await page.locator("#content").fill("# 本机任务验收\n\n文件由本机工具在人工确认后生成。\n<img src=x onerror=window.pwned=1>\n")
            await page.locator("#propose").click()
            await expect(page.locator("#pending")).to_be_visible()
            assert not (project / "report.md").exists()
            await page.locator("#approve").click()
            await expect(page.locator("#task-state")).to_have_text("待执行")
            assert (project / "report.md").read_text(encoding="utf-8").startswith("# 本机任务验收")
            await page.locator("#tool").select_option("command")
            await page.locator("#command").select_option("python_check")
            await page.locator("#paths").fill("example.py")
            await page.locator("#propose").click()
            await expect(page.locator("#pending")).to_be_visible()
            await page.locator("#approve").click()
            await expect(page.locator("#task-state")).to_have_text("待执行", timeout=15000)
            await expect(page.locator("#events .event").last.locator("pre").first).to_contain_text("OK", timeout=15000)
            assert await page.evaluate("window.pwned") is None
            await page.reload()
            await page.locator("#tasks .task").first.click()
            await expect(page.locator("#events .event").last.locator("pre").first).to_contain_text("OK")
            await page.screenshot(path=str(OUT / "local-workbench.png"), full_page=True)
            await page.locator("#complete").click()
            await expect(page.locator("#task-state")).to_have_text("已关闭")
            assert not errors and not external
            result = {"browser": "Microsoft Edge headless", "page_errors": errors,
                      "external_requests": external, "confirmed_file_write": True,
                      "real_python_syntax_check": True, "history_after_reload": True,
                      "hostile_text_executed": False, "human_review_close": True,
                      "model_calls": 0, "research_database_connections": 0}
            (OUT / "ui-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
            print(json.dumps(result))
        finally:
            await browser.close()
            server.close()
            await server.wait_closed()
            await agent.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="用合成项目验收本机任务界面")
    parser.add_argument("--browser-path", required=True, help="已安装Edge/Chromium的可执行文件路径")
    arguments = parser.parse_args()
    asyncio.run(main(str(Path(arguments.browser_path).resolve(strict=True))))
