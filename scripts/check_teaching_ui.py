# 文件职责：使用独立SQLite和替身模型，实际点击示范学习网页并保存流程证据；不使用个人资料或付费API。
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "build/demonstration_20261025"
os.environ.update(LLM_PROVIDER="mock", LLM_API_KEY="", LLM_MODEL="", EMBEDDING_PROVIDER="disabled")
from autodidact.config import RuntimeSettings

RuntimeSettings.model_config["env_file"] = None
from playwright.async_api import async_playwright, expect

from autodidact.local_client import LocalWorkbench
from autodidact.local_tasks import LocalAgent, TaskJournal


class Model:
    provider_name, model_name = "offline_test", "界面流程替身"

    # 功能：预置输出用于检查网页反馈与数据流，不冒充真实模型质量测量。
    def __init__(self):
        self.prompts = []
        self.answers = ["完成登录联调，准备测试。", "已完成：登录联调\n问题：缺少测试账号\n下一步：回归测试",
                        "已完成：导出功能\n问题：无\n下一步：整理文档", "已完成：报告\n下一步：发布"]
        self.last_usage = {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130}

    # 功能：保存模型实际收到的任务上下文，按顺序返回合成结果。
    async def text(self, system, prompt):
        self.prompts.append(json.loads(prompt))
        await asyncio.sleep(.05)
        return self.answers.pop(0)


# 功能：实际浏览器点击教学闭环，并校验原始日志中的示范/要求选择与刷新保留。
async def main(browser_path):
    run_id = uuid4().hex[:12]
    project = OUT / "ui-projects" / run_id
    project.mkdir(parents=True)
    journal = TaskJournal(OUT / "ui-state" / run_id)
    agent = LocalAgent(journal)
    app = LocalWorkbench(agent, str(project), 0)
    server = await asyncio.start_server(app.connection, "127.0.0.1", 0)
    app.port = server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{app.port}"
    errors, external = [], []
    model = Model()
    hostile = '<img src=x onerror="window.pwned=1">'
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=browser_path, headless=True)
        context = await browser.new_context(viewport={"width": 1512, "height": 1100}, service_workers="block")

        # 功能：只允许页面访问本轮回环服务器，不向外部站点传送合成资料。
        async def route(request):
            if not request.request.url.startswith(url + "/"):
                external.append(request.request.url)
                await request.abort()
            else:
                await request.continue_()

        await context.route("**/*", route)
        page = await context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            await page.goto(url)
            await expect(page.locator("#model-status")).to_have_text("尚未连接模型")
            await page.locator("#course-name").fill("项目周报")
            await page.locator("#course-instructions").fill("根据材料输出已完成、问题、下一步；不编造进度。")
            await page.locator("#create-course").click()
            await expect(page.locator("#task-name")).to_have_text("项目周报")
            await page.locator("#sample-input").fill("完成页面；接口未通；明天联调。" + hostile)
            await page.locator("#sample-output").fill("已完成：页面\n问题：接口未通\n下一步：联调")
            await page.locator("#save-sample").click()
            await expect(page.locator("#sample-count")).to_have_text("1")
            await page.locator("#new-input").fill("完成登录联调，缺少测试账号，明天回归测试。")
            await page.locator("#send").click()
            await expect(page.locator("#model-dialog")).to_be_visible()
            assert journal.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
            await page.locator("#close-model").click()
            # 只为本轮隔离应用注入替身；正式应用的Mock继续拒绝生成。
            app.teaching.llm_factory = lambda: model
            app.models.info = lambda: {"ready": True, "provider": model.provider_name, "model": model.model_name,
                                       "base_url": "", "key_saved": False, "message": "仅离线流程测试"}
            await page.reload()
            await page.locator("#courses button").first.click()
            await expect(page.locator("#sample-count")).to_have_text("1")
            await page.locator("#new-input").fill("完成登录联调，缺少测试账号，明天回归测试。")
            await page.locator("#send").click()
            await expect(page.get_by_role("button", name="需要调整", exact=True)).to_be_visible(timeout=15000)
            await page.get_by_role("button", name="需要调整", exact=True).click()
            await page.get_by_label("修改反馈", exact=True).fill("用三个标题分行，不能漏掉材料里的问题")
            await page.get_by_label("理想修订结果", exact=True).fill(model.answers[0])
            await page.get_by_role("checkbox").check()
            await page.evaluate("refresh(true)")
            await expect(page.get_by_label("修改反馈", exact=True)).to_have_value("用三个标题分行，不能漏掉材料里的问题")
            await page.get_by_role("button", name="按反馈修订", exact=True).click()
            await expect(page.locator("#thread .assistant pre").filter(has_text="已完成：登录联调").first).to_be_visible(timeout=15000)
            await expect(page.locator("#rule-count")).to_have_text("1")
            await page.get_by_role("button", name="认可，加入示范", exact=True).click()
            await expect(page.locator("#sample-count")).to_have_text("2")
            await page.locator("#new-input").fill("完成导出功能，无问题，下步整理文档。")
            await page.locator("#send").click()
            await expect(page.locator("#thread .assistant pre").filter(has_text="已完成：导出功能")).to_be_visible(timeout=15000)
            await expect(page.locator("#message")).to_have_text("回答已生成，可以认可或提出修改。")
            course = app.teaching.store.courses()[0]
            detail = app.teaching.store.detail(course["id"])
            accepted = next(r for r in detail["runs"] if r["status"] == "accepted")
            assert len(accepted["versions"]) == 2
            assert accepted["accepted_sample_id"] in {
                s["id"] for s in model.prompts[2]["teaching_context"]["demonstrations"]}
            assert model.prompts[2]["teaching_context"]["user_requirements"][0]["text"] == "用三个标题分行，不能漏掉材料里的问题"
            await page.screenshot(path=str(OUT / "teaching-workbench.png"), full_page=True)
            await page.locator("#samples .record").filter(has_text="完成页面；接口未通").locator("summary").click()
            await page.locator("#samples .record").filter(has_text="完成页面；接口未通").get_by_role("button", name="停用", exact=True).click()
            await expect(page.locator("#sample-count")).to_have_text("1")
            await page.locator("#rules .record summary").click()
            await page.locator("#rules").get_by_role("button", name="停用", exact=True).click()
            await expect(page.locator("#rule-count")).to_have_text("0")
            await page.locator("#new-input").fill("完成报告，下步发布。")
            await page.locator("#send").click()
            await expect(page.locator("#thread .assistant pre").filter(has_text="已完成：报告")).to_be_visible(timeout=15000)
            assert len(model.prompts[3]["teaching_context"]["demonstrations"]) == 1
            assert model.prompts[3]["teaching_context"]["user_requirements"] == []
            await page.reload()
            await page.locator("#courses button").first.click()
            await expect(page.locator("#thread .assistant")).to_have_count(4)
            assert await page.evaluate("window.pwned") is None
            assert not errors and not external
            result = {"browser": "Microsoft Edge headless", "page_errors": errors, "external_requests": external,
                      "save_demonstration_without_model": True, "mock_generation_blocked": True,
                      "new_input_used_demonstration": True, "feedback_revision_versions": 2,
                      "feedback_draft_survived_refresh": True, "accepted_answer_reused": True,
                      "explicit_rule_reused": True, "deactivated_items_excluded": True,
                      "history_after_reload": True, "hostile_text_executed": False,
                      "synthetic_model_calls": len(model.prompts), "real_model_calls": 0,
                      "research_database_connections": 0, "state_directory": str(journal.directory)}
            (OUT / "teaching-ui-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(result, ensure_ascii=True))
        finally:
            await browser.close()
            server.close()
            await server.wait_closed()
            await app.teaching.stop()
            await agent.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="在隔离数据中验收示范学习界面")
    parser.add_argument("--browser-path", required=True)
    args = parser.parse_args()
    asyncio.run(main(str(Path(args.browser_path).resolve(strict=True))))
