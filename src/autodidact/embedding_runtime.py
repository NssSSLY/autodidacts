# 文件职责：为嵌入调用添加提供方指纹、预算和观察记录，不把向量当证据。
from __future__ import annotations

import hashlib
import json

from sqlalchemy.ext.asyncio import async_sessionmaker

from autodidact import models
from autodidact.embeddings import DisabledEmbeddingProvider, EmbeddingUnavailable
from autodidact.runtime import OperationBudget


class RecordedEmbedding:
    # 功能：包装嵌入提供方并计算类别、服务地址、模型和维度的稳定指纹。
    def __init__(self, inner, engine):
        self.inner = inner
        self.budget = OperationBudget(engine)
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)
        identity = f"{type(inner).__name__}:{getattr(inner, 'base_url', '')}:{getattr(inner, 'model', '')}:{inner.dimension}"
        self.fingerprint = hashlib.sha256(identity.encode()).hexdigest()

    # 功能：预留嵌入调用预算、执行向量计算并记录输入摘要/错误，不把完整向量当模型事实输出。
    async def embed(self, text):
        if isinstance(self.inner, DisabledEmbeddingProvider):
            raise EmbeddingUnavailable("embedding provider is disabled")
        batch = await self.budget.reserve(
            {"embedding_calls": 1, "tokens": len(text.encode()) + 256},
            {"embedding_fingerprint": self.fingerprint},
        )
        error, summary = None, {}
        try:
            vector = await self.inner.embed(text)
            summary = {
                "dimension": len(vector),
                "vector_sha256": hashlib.sha256(json.dumps(vector).encode()).hexdigest(),
            }
            return vector
        except Exception as exc:
            error = type(exc).__name__
            raise
        finally:
            await self.budget.finish(batch, error=error)
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
                        },
                    )
                )
