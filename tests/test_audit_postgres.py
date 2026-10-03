# 文件职责：仅在显式隔离测试库验证审计真实查询和数据库READ ONLY强制拒写；不访问日常认知库。
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_postgres_integration import (
    postgres_engine as postgres_engine,  # noqa: PLC0414 - pytest共享隔离fixture
)

from autodidact import models
from autodidact.audit import AuditQuery, CognitiveAudit
from autodidact.migrations import upgrade_database
from autodidact.repository import Repository


# 功能：*_test临时schema验证Source排序、撤回信念/字面筛选、重复快照只读及数据库拒绝写入。
async def test_audit_database_enforces_read_only(postgres_engine):
    await upgrade_database(postgres_engine)
    maker = async_sessionmaker(postgres_engine, expire_on_commit=False)
    async with maker() as session:
        source = models.Source(url="https://example.test/audit", title="只读来源")
        belief = models.Belief(topic="审计", statement="literal %_ candidate", status="retracted")
        session.add_all([source, belief])
        await session.commit()
        source_id, belief_id = source.id, belief.id
    async with maker() as session:
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        await session.execute(text("SET LOCAL statement_timeout = '5000ms'"))
        audit = CognitiveAudit(Repository(session))
        listing = (await audit.listing("sources", AuditQuery()))["view"]
        assert listing["items"][0]["id"] == str(source_id)
        listing = (await audit.listing("beliefs", AuditQuery(q="%_")))["view"]
        assert listing["items"][0]["id"] == str(belief_id)
        assert listing["items"][0]["status"] == "retracted"
        detail = (await audit.detail("beliefs", belief_id, AuditQuery(part="history")))["view"]
        assert detail["items"] == []
        with pytest.raises(DBAPIError):
            await session.execute(text("UPDATE beliefs SET status='verified'"))
        await session.rollback()
    async with maker() as session:
        assert (await session.get(models.Belief, belief_id)).status == "retracted"
