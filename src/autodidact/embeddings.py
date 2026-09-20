from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import httpx

from autodidact.config import RuntimeSettings, runtime_settings

EMBEDDING_DIMENSION = 1536


class EmbeddingUnavailable(RuntimeError):
    """Raised when semantic retrieval is intentionally unavailable or fails."""


class EmbeddingProvider(ABC):
    dimension: int = EMBEDDING_DIMENSION

    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        raise NotImplementedError


class DisabledEmbeddingProvider(EmbeddingProvider):
    async def embed(self, text: str) -> list[float]:
        del text
        raise EmbeddingUnavailable("embedding provider is disabled")


class OpenAICompatibleEmbeddingProvider(EmbeddingProvider):
    """Embedding adapter for the common OpenAI-compatible endpoint shape."""

    def __init__(self, settings: RuntimeSettings, client: httpx.AsyncClient | None = None):
        if not settings.embedding_api_key or not settings.embedding_model:
            raise ValueError("EMBEDDING_API_KEY and EMBEDDING_MODEL are required")
        self.base_url = settings.embedding_base_url.rstrip("/")
        self.api_key = settings.embedding_api_key
        self.model = settings.embedding_model
        self.client = client

    async def embed(self, text: str) -> list[float]:
        payload: dict[str, Any] = {"model": self.model, "input": text}
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        if self.client is not None:
            response = await self.client.post(
                f"{self.base_url}/embeddings", json=payload, headers=headers
            )
        else:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    f"{self.base_url}/embeddings", json=payload, headers=headers
                )
        response.raise_for_status()
        try:
            vector = response.json()["data"][0]["embedding"]
        except (IndexError, KeyError, TypeError) as exc:
            raise EmbeddingUnavailable("embedding response has no vector") from exc
        if not isinstance(vector, list) or len(vector) != self.dimension:
            raise EmbeddingUnavailable(
                f"embedding dimension must be {self.dimension}, got {len(vector) if isinstance(vector, list) else 'invalid'}"
            )
        if not all(isinstance(value, int | float) for value in vector):
            raise EmbeddingUnavailable("embedding vector contains non-numeric values")
        return [float(value) for value in vector]


def build_embedding_provider(settings: RuntimeSettings | None = None) -> EmbeddingProvider:
    settings = settings or runtime_settings()
    provider = settings.embedding_provider.strip().lower()
    if provider == "disabled":
        return DisabledEmbeddingProvider()
    if provider == "openai_compatible":
        return OpenAICompatibleEmbeddingProvider(settings)
    raise ValueError(f"Unknown EMBEDDING_PROVIDER={settings.embedding_provider}")
