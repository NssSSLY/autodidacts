# 文件职责：使用 HTTP 替身检查嵌入响应形状和关闭模式降级。
import httpx
import pytest

from autodidact.config import RuntimeSettings
from autodidact.embeddings import (
    EMBEDDING_DIMENSION,
    DisabledEmbeddingProvider,
    EmbeddingUnavailable,
    OpenAICompatibleEmbeddingProvider,
)


# 功能：验证兼容嵌入服务返回固定维度向量，并检查请求内容。
@pytest.mark.asyncio
async def test_openai_compatible_embedding_provider_validates_vector_shape():
    # 功能：模拟 embeddings HTTP 响应，不向真实模型发请求。
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret"
        assert request.url.path == "/v1/embeddings"
        return httpx.Response(200, json={"data": [{"embedding": [0.1] * EMBEDDING_DIMENSION}]})

    settings = RuntimeSettings(
        embedding_provider="openai_compatible",
        embedding_base_url="https://embedding.example/v1",
        embedding_api_key="secret",
        embedding_model="text-embedding-3-small",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleEmbeddingProvider(settings, client)
        vector = await provider.embed("semantic recall")

    assert len(vector) == EMBEDDING_DIMENSION


# 功能：验证关闭嵌入时给出明确不可用异常供检索降级。
@pytest.mark.asyncio
async def test_disabled_embedding_provider_is_a_graceful_fallback():
    with pytest.raises(EmbeddingUnavailable, match="disabled"):
        await DisabledEmbeddingProvider().embed("semantic recall")
