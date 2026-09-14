from autodidact.web_models.playwright_adapter import WebModelConfig


def test_web_model_config():
    cfg = WebModelConfig(
        provider="example", url="https://example.com", persistent_profile_dir=".browser/x",
        input_selector="textarea", send_selector="button", assistant_message_selector=".msg"
    )
    assert cfg.provider == "example"
