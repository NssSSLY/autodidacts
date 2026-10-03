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
