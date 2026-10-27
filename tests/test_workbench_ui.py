"""工作台资源和演示隔离契约；不启动数据库、模型或浏览器。"""

from bs4 import BeautifulSoup

from autodidact.ui import render_page


def test_page_assets_and_unique_controls():
    """正式页面完整内嵌资源、唯一控件ID，不携带演示脚本或外部资源。"""
    page = render_page()
    document = BeautifulSoup(page, "html.parser")
    ids = [element["id"] for element in document.select("[id]")]
    assert len(ids) == len(set(ids))
    assert len(document.select(".workspace-panel")) == 8
    assert document.select_one("#goal-dialog form") is not None
    assert document.select_one("#chat-form textarea[maxlength='2000']") is not None
    assert document.select("script[src],link[href],img[src]") == []
    assert "WORKSPACE_JS" not in page and "WORKSPACE_CSS" not in page
    assert "window.AUTODIDACT_PREVIEW = true" not in page
    assert len(document.select("script[nonce='NONCE']")) == 1


def test_preview_explicitly_separate_from_production():
    """演示页才注入本地替身，所有高级写入默认拒绝而不转发服务。"""
    preview = render_page(preview=True)
    assert "window.AUTODIDACT_PREVIEW = true" in preview
    assert "window.fetch=async" in preview
    assert "此高级写入不在演示中模拟" in preview
    assert "所有操作仅在本页模拟" in preview
