# 文件职责：隔离无账户浏览器验证审计列表/详情/返回/分页及恶意HTML纯文本展示，所有网页请求本地替身响应。
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from autodidact.workbench import PAGE


# 功能：优先显式测试浏览器或已有Edge；不存在时明确跳过，不下载浏览器或复用用户资料。
def browser_path():
    candidates = [
        os.environ.get("AUTODIDACT_TEST_BROWSER_PATH"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    return next((p for p in candidates if p and Path(p).is_file()), None)


async def test_auxiliary_preview_manual_flow_and_layout():
    """六站界面真实渲染；演示人工留档、确认、XSS和移动布局，不调用外网或数据库。"""
    from autodidact.ui import render_page

    playwright = pytest.importorskip("playwright.async_api")
    executable = browser_path()
    if not executable:
        pytest.skip("无现有测试浏览器")
    errors, requests = [], []
    hostile = '<img src="https://evil.test/x" onerror="window.compromised=1">观点'
    async with playwright.async_playwright() as runtime:
        browser = await runtime.chromium.launch(executable_path=executable, headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 1000}, service_workers="block")
            page = await context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))

            async def route(handler):
                """仅主页面有响应；任何真实网络请求都视为失败并阻断。"""
                requests.append(handler.request.url)
                if handler.request.url == "http://127.0.0.1:8766/":
                    await handler.fulfill(body=render_page(preview=True).replace("TOKEN", "test").replace("NONCE", "test"), content_type="text/html")
                else:
                    await handler.abort()

            await context.route("**/*", route)
            await page.goto("http://127.0.0.1:8766/#sources")
            await playwright.expect(page.locator(".source-choice")).to_have_count(6)
            await playwright.expect(page.locator("#aux-name")).to_have_text("DeepSeek")
            await page.locator("#aux-enabled").check()
            await page.locator("#aux-save").click()
            await playwright.expect(page.locator("#aux-result")).to_contain_text('"revision"')
            await page.locator("#aux-prompt").fill("辅助研究问题")
            await page.locator("#aux-response").fill(hostile)
            await page.locator("#aux-import").click()
            await playwright.expect(page.locator("#aux-result")).to_contain_text("先勾选")
            await page.locator("#aux-consent").check()
            await page.locator("#aux-import").click()
            await page.locator('#confirm-dialog button[value="cancel"]').click()
            await page.locator("#aux-history-load").click()
            await playwright.expect(page.locator("#aux-history")).to_contain_text("暂无观察")
            await page.locator("#aux-import").click()
            await page.locator('#confirm-dialog button[value="confirm"]').click()
            await playwright.expect(page.locator("#aux-result")).to_contain_text("仅内存演示")
            await page.locator("#aux-history-load").click()
            await page.locator("#aux-history summary").click()
            await playwright.expect(page.locator("#aux-history")).to_contain_text(hostile)
            assert await page.locator("#aux-history img").count() == 0
            assert await page.evaluate("window.compromised") is None
            output = Path("build/ui_preview_20261023")
            output.mkdir(parents=True, exist_ok=True)
            await page.evaluate("window.scrollTo(0, 0)")
            await page.screenshot(path=str(output / "auxiliary-desktop.png"), full_page=True)
            await page.get_by_role("button", name="Claude Code 网页任务 · 未启用", exact=True).click()
            await page.locator('#confirm-dialog button[value="confirm"]').click()
            await playwright.expect(page.locator('#aux-mode option[value="browser"]')).to_be_disabled()
            await playwright.expect(page.locator("#aux-boundary")).to_contain_text("仅人工采集")
            assert await page.locator("#aux-url").get_attribute("href") is None
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            await page.screenshot(path=str(output / "auxiliary-mobile.png"), full_page=True)
            assert errors == []
            assert requests == ["http://127.0.0.1:8766/"]
        finally:
            await browser.close()


async def test_web_adapter_fresh_answer_with_intercepted_browser(tmp_path, monkeypatch):
    """实际浏览器执行新会话/一次发送/最终回答；全请求拦截的合成页不代表网站验收。"""
    from types import SimpleNamespace

    from autodidact.web_models import playwright_adapter

    playwright = pytest.importorskip("playwright.async_api")
    executable = browser_path()
    if not executable:
        pytest.skip("无现有测试浏览器")
    calls = []
    async with playwright.async_playwright() as runtime:
        async def launch(**kwargs):
            """使用临时无账户资料和已有Edge，所有页面请求都替换为合成HTML。"""
            context = await runtime.chromium.launch_persistent_context(user_data_dir=kwargs["user_data_dir"], executable_path=executable, headless=True)
            await context.expose_function("recordSubmit", lambda: calls.append("sent"))
            await context.route("**/*", lambda route: route.fulfill(content_type="text/html; charset=utf-8", body='''<button id="new" onclick="document.querySelectorAll('.answer').forEach(n=>n.remove())">新对话</button><textarea id="input"></textarea><button id="send" onclick="recordSubmit();const n=document.createElement('div');n.className='answer';n.textContent='待核验回答';document.body.append(n);document.querySelector('#done').hidden=false">发送</button><div class="answer">旧答案</div><span id="done" hidden>完成</span>'''))
            return context

        class PlaywrightFixture:
            """仅提供上下文协议，浏览器实际生命周期仍由适配器控制。"""
            async def __aenter__(self):
                return SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))

            async def __aexit__(self, *args):
                pass

        monkeypatch.setattr(playwright_adapter, "async_playwright", PlaywrightFixture)
        config = playwright_adapter.WebModelConfig(provider="fixture", url="https://fixture.invalid/", persistent_profile_dir=str(tmp_path / "profile"), input_selector="#input", send_selector="#send", assistant_message_selector=".answer", new_chat_selector="#new", done_selector="#done", require_new_chat=True)
        result = await playwright_adapter.PlaywrightWebModelAdapter(config).ask("问题")
        assert calls == ["sent"]
        assert result.response == "待核验回答" and result.metadata["evidence_level"] == 0


# 功能：实际浏览器验证GET、筛选、详情关联/返回、原文分页、未知/空页及XSS；无数据库或真实模型。
async def test_read_only_audit_ui_with_hostile_text():
    playwright = pytest.importorskip("playwright.async_api")
    executable = browser_path()
    if not executable:
        pytest.skip("无现有测试浏览器；可设置 AUTODIDACT_TEST_BROWSER_PATH，不自动下载")
    belief_id, source_id = str(uuid4()), str(uuid4())
    hostile = '<img src="https://evil.test/x" onerror="window.compromised=1"><script>window.compromised=1</script>'
    requests, errors = [], []
    async with playwright.async_playwright() as runtime:
        browser = await runtime.chromium.launch(executable_path=executable, headless=True)
        try:
            context = await browser.new_context(service_workers="block")
            page = await context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))

            # 功能：拦截所有网络；只响应本地页面/审计GET，其他请求拒绝，不访问恶意URL。
            async def route(handler):
                request = handler.request
                if request.url == "http://127.0.0.1:8765/":
                    await handler.fulfill(
                        body=PAGE.replace("TOKEN", "test-token").replace("NONCE", "test-nonce"),
                        content_type="text/html",
                    )
                    return
                if not request.url.startswith("http://127.0.0.1:8765/api/"):
                    await handler.abort()
                    return
                requests.append(request.url)
                assert request.method == "GET"
                assert request.headers["x-autodidact-token"] == "test-token"
                if "/api/state" in request.url:
                    response = {
                        "goals": [],
                        "beliefs": [],
                        "disputes": [],
                        "reports": [],
                        "metrics": {},
                        "job": {},
                    }
                else:
                    view = {"items": [], "pagination": {"next_offset": None}}
                    if f"/beliefs/{belief_id}" in request.url:
                        view.update(
                            part="evidence",
                            parts=["evidence", "history"],
                            data={"statement": hostile, "scope": {}, "status": "disputed"},
                            items=[{"stance": "attack", "excerpt": hostile}],
                            references=[{"kind": "sources", "id": source_id, "label": "引文来源"}],
                        )
                    elif f"/sources/{source_id}" in request.url:
                        is_text = "part=text" in request.url
                        second = "text_offset=12000" in request.url
                        view.update(
                            part="text" if is_text else "lineage",
                            parts=["lineage", "text"],
                            data={
                                "original_text": "末段原文" if second else hostile,
                                "bibliography": {},
                            },
                            references=[],
                            pagination={"next_offset": 12000 if is_text and not second else None},
                        )
                    elif (
                        "/beliefs?" in request.url
                        and "offset=20" not in request.url
                        and "q=empty" not in request.url
                    ):
                        view.update(
                            items=[{"id": belief_id, "statement": hostile, "status": "retracted"}],
                            pagination={"next_offset": 20},
                        )
                    response = {
                        "view": view,
                        "note": "只读：未知不等于已验证",
                        "display_truncated": False,
                    }
                await handler.fulfill(
                    body=json.dumps(response, ensure_ascii=False), content_type="application/json"
                )

            await context.route("**/*", route)
            await page.goto("http://127.0.0.1:8765/")
            await page.get_by_role("button", name="知识与证据", exact=False).first.click()
            await playwright.expect(page.locator("#audit-list")).to_contain_text("retracted")
            await playwright.expect(page.locator("#audit-list")).to_contain_text(hostile)
            assert await page.locator("#audit img, #audit script").count() == 0
            await page.get_by_role("button", name="查看信念", exact=True).click()
            await playwright.expect(page.locator("#audit-items")).to_contain_text("attack")
            await playwright.expect(page.locator("#audit-data")).to_contain_text("未知")
            await page.locator("#audit-references button").click()
            await playwright.expect(page.locator("#audit-detail-title")).to_contain_text(source_id)
            await page.get_by_role("button", name="保存原文", exact=True).click()
            await playwright.expect(
                page.locator("#audit-detail-pages button").nth(1)
            ).to_be_enabled()
            await page.locator("#audit-detail-pages button").nth(1).click()
            await playwright.expect(page.locator("#audit-data")).to_contain_text("末段原文")
            await page.locator("#audit-back").click()
            await playwright.expect(page.locator("#audit-detail-title")).to_contain_text(belief_id)
            await page.locator("#audit-next").click()
            await playwright.expect(page.locator("#audit-message")).to_contain_text("暂无匹配记录")
            await playwright.expect(page.locator("#audit-next")).to_be_disabled()
            assert await page.evaluate("window.compromised") is None
            assert errors == []
            assert not any("evil.test" in r for r in requests)
        finally:
            await browser.close()


# 功能：用真实 Chromium 和全部网络拦截验证新预览的目标/对话/引用/导航及窄屏布局。
async def test_workspace_preview_interaction_and_layout():
    from autodidact.ui import render_page

    playwright = pytest.importorskip("playwright.async_api")
    executable = browser_path()
    if not executable:
        pytest.skip("未配置已有浏览器")
    errors, external = [], []
    async with playwright.async_playwright() as runtime:
        browser = await runtime.chromium.launch(executable_path=executable, headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 1000})
            page = await context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))

            # 功能：仅初始 HTML 替身；演示内的 API 不得真正发出网络。
            async def route(handler):
                if handler.request.url == "http://127.0.0.1:8766/":
                    await handler.fulfill(
                        body=render_page(preview=True).replace("TOKEN", "demo").replace("NONCE", "test"),
                        content_type="text/html",
                        headers={"Content-Security-Policy": "default-src 'none'; script-src 'nonce-test'; style-src 'unsafe-inline'; connect-src 'none'; form-action 'none'"},
                    )
                else:
                    external.append(handler.request.url)
                    await handler.abort()

            await context.route("**/*", route)
            await page.goto("http://127.0.0.1:8766/")
            await playwright.expect(page.locator("#metric-goals")).to_have_text("3")
            assert await page.locator(".workspace-panel:visible").count() == 1
            assert await page.locator(".nav-item[aria-current=page]").count() == 1
            await page.goto("http://127.0.0.1:8766/#constructor")
            await playwright.expect(page.locator("#view-overview")).to_be_visible()
            screenshots = Path("build/ui_preview_20261023")
            screenshots.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(screenshots / "overview-desktop.png"), full_page=False)
            await page.locator(".topbar [data-action=new-goal]").click()
            await page.locator("#goal").fill("界面回归：检查低照度证据")
            await page.locator("#add").click()
            await playwright.expect(page.locator("#goal-dialog")).not_to_be_visible()
            await playwright.expect(page.locator("#goals")).to_contain_text("界面回归")
            await playwright.expect(page.locator("#activity-list")).to_contain_text("/api/goals")
            await page.locator(".nav-item[data-go=research]").click()
            await page.locator("#question").fill("适用范围是什么？")
            await page.locator("#question").press("Enter")
            await playwright.expect(page.locator("#chat-messages")).to_contain_text("这是界面演示回答")
            await playwright.expect(page.locator("#chat-messages")).to_contain_text("仍待研究")
            await page.screenshot(path=str(screenshots / "conversation-desktop.png"), full_page=True)
            await page.get_by_role("button", name="核对信念与原文 →").click()
            await playwright.expect(page.locator("#audit-detail")).to_be_visible()
            await playwright.expect(page.locator("#audit-data")).to_contain_text("视觉定位")
            await page.locator("#audit-references button").click()
            await playwright.expect(page.locator("#audit-data")).to_contain_text("示例来源")
            await page.locator(".nav-item[data-go=run]").click()
            await page.locator("#run").click()
            await playwright.expect(page.locator("#confirm-dialog")).to_be_visible()
            await page.locator("#confirm-dialog button[value=cancel]").click()
            await playwright.expect(page.locator("#run-summary")).to_contain_text("等待研究")
            for view in ["review", "experiments", "tools"]:
                await page.locator(".nav-item[data-go=" + view + "]").click()
                await playwright.expect(page.locator("#view-" + view)).to_be_visible()
            await page.locator(".nav-item[data-go=overview]").click()
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True
            await page.screenshot(path=str(screenshots / "overview-mobile.png"), full_page=True)
            assert errors == []
            assert external == []
        finally:
            await browser.close()


# 功能：真实模式替身检查只读启动、调用许可、失败保留、重复提交抑制及不可信回答不执行。
async def test_workspace_real_mode_request_boundaries():
    import asyncio

    playwright = pytest.importorskip("playwright.async_api")
    executable = browser_path()
    if not executable:
        pytest.skip("未配置已有浏览器")
    calls, errors = [], []
    ask_started, release_answer = asyncio.Event(), asyncio.Event()
    fail_goal, fail_state = True, False
    hostile = '<img src="https://evil.test/x" onerror="window.compromised=1">'
    async with playwright.async_playwright() as runtime:
        browser = await runtime.chromium.launch(executable_path=executable, headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1366, "height": 900}, service_workers="block")
            page = await context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))

            # 功能：拦截全部请求，使用可控延迟和失败；不存在数据库或真实模型调用。
            async def route(handler):
                request = handler.request
                if request.url == "http://127.0.0.1:8765/":
                    await handler.fulfill(body=PAGE.replace("TOKEN", "test-token").replace("NONCE", "test"), content_type="text/html")
                    return
                calls.append((request.method, request.url))
                assert request.url.startswith("http://127.0.0.1:8765/api/")
                assert request.headers["x-autodidact-token"] == "test-token"
                status, response = 200, {}
                if request.url.endswith("/api/state"):
                    response = {"goals": [], "beliefs": [], "disputes": [], "reports": [], "metrics": {}, "job": {"status": "idle"}}
                    if fail_state:
                        status, response = 503, {"error": "离线故障示例"}
                elif request.url.endswith("/api/goals"):
                    if fail_goal:
                        status, response = 400, {"error": "保存失败示例"}
                    else:
                        response = {"goal_id": str(uuid4()), "title": "保留输入"}
                elif request.url.endswith("/api/ask"):
                    ask_started.set()
                    await release_answer.wait()
                    response = {"model_explanation_pending_review": hostile, "saved_beliefs": [], "missing_knowledge": [], "open_disputes": 0}
                elif request.url.endswith("/api/run"):
                    response = {"status": "queued"}
                elif request.url.endswith("/api/service/runs"):
                    response = {"runs": []}
                await handler.fulfill(status=status, body=json.dumps(response), content_type="application/json")

            await context.route("**/*", route)
            await page.goto("http://127.0.0.1:8765/")
            await playwright.expect(page.locator("#connection-state")).to_contain_text("已连接")
            assert all(method == "GET" for method, _ in calls)
            await page.locator(".topbar [data-action=new-goal]").click()
            await page.locator("#goal").fill("保留输入")
            await page.locator("#add").click()
            await playwright.expect(page.locator("#added")).to_contain_text("保存失败")
            await playwright.expect(page.locator("#goal")).to_have_value("保留输入")
            fail_goal = False
            await page.locator("#add").click()
            await playwright.expect(page.locator("#goal-dialog")).not_to_be_visible()
            await page.locator(".nav-item[data-go=research]").click()
            await page.locator("#question").fill("问题")
            await page.locator("#ask").click()
            assert not any(url.endswith("/api/ask") for _, url in calls)
            await page.locator("#ask-consent").check()
            await page.locator("#ask").click()
            await asyncio.wait_for(ask_started.wait(), 5)
            await playwright.expect(page.locator("#ask")).to_be_disabled()
            await page.locator("#question").fill("第二条未发送问题")
            await page.locator("#question").press("Enter")
            assert sum(url.endswith("/api/ask") for _, url in calls) == 1
            release_answer.set()
            await playwright.expect(page.locator("#chat-messages")).to_contain_text(hostile)
            assert await page.locator("#chat-messages img").count() == 0
            assert await page.evaluate("window.compromised") is None
            await playwright.expect(page.locator("#question")).to_have_value("第二条未发送问题")
            fail_state = True
            await page.locator("#global-refresh").click()
            await playwright.expect(page.locator("#connection-error")).to_contain_text("离线故障示例")
            fail_state = False
            await page.locator("#global-refresh").click()
            await playwright.expect(page.locator("#connection-error")).not_to_be_visible()
            assert errors == []
        finally:
            release_answer.set()
            await browser.close()
