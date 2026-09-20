import pytest
from sqlalchemy.dialects import postgresql

from autodidact.embeddings import EMBEDDING_DIMENSION
from autodidact.repository import Repository


class _Result:
    def scalars(self):
        return []


class _Session:
    def __init__(self):
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return _Result()


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
