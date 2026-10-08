# 文件职责：为嵌入调用添加提供方指纹、预算和观察记录，不把向量当证据。
from __future__ import annotations

import asyncio
import hashlib
import json

from sqlalchemy.ext.asyncio import async_sessionmaker

from autodidact import models
from autodidact.embeddings import DisabledEmbeddingProvider, EmbeddingUnavailable
from autodidact.provider_runtime import ProviderIdentity, ProviderRuntime, classify_failure
from autodidact.runtime import OperationBudget


class RecordedEmbedding:
    # 功能：包装嵌入提供方并计算类别、服务地址、模型和维度的稳定指纹。
    def __init__(self, inner, engine):
        self.inner = inner
        self.budget = OperationBudget(engine)
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)
        identity = f"{type(inner).__name__}:{getattr(inner, 'base_url', '')}:{getattr(inner, 'model', '')}:{inner.dimension}"
        self.fingerprint = hashlib.sha256(identity.encode()).hexdigest()
        self.runtime = ProviderRuntime(engine)
        self.identity = ProviderIdentity(
            "embedding",
            type(inner).__name__,
            getattr(inner, "base_url", ""),
            getattr(inner, "model", ""),
            getattr(inner, "api_key", ""),
        )

    # 功能：关闭嵌入保持原关键词降级；启用时经持久熔断和有限重试，每次尝试单独预算/审计。
    async def embed(self, text):
        if isinstance(self.inner, DisabledEmbeddingProvider):
            raise EmbeddingUnavailable("embedding provider is disabled")

        # 功能：绑定嵌入单次尝试上下文，防止重试漏计调用/Token。
        async def attempt(context):
            return await self._attempt(text, context)

        return await self.runtime.run(self.identity, attempt)

    # 功能：预留并记录一次嵌入请求/取消/失败类别，未知Token保留预留，向量只记摘要。
    async def _attempt(self, text, context):
        batch = await self.budget.reserve(
            {"embedding_calls": 1, "tokens": len(text.encode()) + 256},
            {"embedding_fingerprint": self.fingerprint, **context},
        )
        error, summary = None, {}
        try:
            vector = await self.inner.embed(text)
            summary = {
                "dimension": len(vector),
                "vector_sha256": hashlib.sha256(json.dumps(vector).encode()).hexdigest(),
            }
            return vector
        except (Exception, asyncio.CancelledError) as exc:
            error = type(exc).__name__
            failure_category = classify_failure(exc).category
            raise
        finally:
            await self.budget.finish(
                batch,
                error=error,
                details={"failure_category": failure_category if error else None},
            )
            async with self.sessions() as session, session.begin():
                session.add(
                    models.ModelObservation(
                        provider="embedding",
                        access_type="api",
                        displayed_model=getattr(self.inner, "model", "unknown"),
                        prompt=text,
                        response=json.dumps(summary),
                        metadata_json={
                            "evidence_level": 0,
                            "error": error,
                            "fingerprint": self.fingerprint,
                            "failure_category": failure_category if error else None,
                            **context,
                        },
                    )
                )
