# 文件职责：检查网页模型适配器配置字段和默认值，不启动浏览器。
from autodidact.web_models.playwright_adapter import WebModelConfig


# 功能：构造网页模型配置并检查关键字段，确认 schema 可接受示例输入。
def test_web_model_config():
    cfg = WebModelConfig(
        provider="example", url="https://example.com", persistent_profile_dir=".browser/x",
        input_selector="textarea", send_selector="button", assistant_message_selector=".msg"
    )
    assert cfg.provider == "example"
