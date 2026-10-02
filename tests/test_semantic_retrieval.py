# 文件职责：使用查询替身检查兼容信念向量召回的 SQL 排序和空向量排除。
import pytest
from sqlalchemy.dialects import postgresql

from autodidact.embeddings import EMBEDDING_DIMENSION
from autodidact.repository import Repository


class _Result:
    # 功能：返回空结果用于检视构建的 SQL，而非测试真实数据库数据。
    def scalars(self):
        return []


class _Session:
    # 功能：初始化保存最近 SQL 的模拟会话。
    def __init__(self):
        self.statement = None

    # 功能：记录传入 SQL 并返回空标量结果，隔离 pgvector 在线操作。
    async def execute(self, statement):
        self.statement = statement
        return _Result()


# 功能：验证构造余弦距离排序并排除空向量；不证明 ANN 召回质量。
@pytest.mark.asyncio
async def test_semantic_recall_uses_pgvector_cosine_distance_and_skips_null_vectors():
    session = _Session()
    repo = Repository(session)  # type: ignore[arg-type]

    result = await repo.semantic_beliefs([0.0] * EMBEDDING_DIMENSION, limit=7)
    sql = str(session.statement.compile(dialect=postgresql.dialect()))

    assert result == []
    assert "<=>" in sql
    assert "embedding IS NOT NULL" in sql
    assert "LIMIT" in sql
