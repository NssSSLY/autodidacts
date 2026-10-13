# 文件职责：仅显式*_test随机schema验收0008升级保留、持久续跑、索引检查/切换及数据库控制器互斥。
import asyncio
from uuid import UUID

import pytest
from alembic import command
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_postgres_integration import postgres_engine as postgres_engine  # noqa: PLC0414

from autodidact import models
from autodidact.migrations import alembic_config, upgrade_database
from autodidact.retrieval import HybridRetriever, RetrievalIndex
from autodidact.retrieval_jobs import RetrievalJobs
from autodidact.runtime import ControllerBusy

VECTOR = [1.0] + [0.0] * 1535


class TestEmbedding:
    __test__ = False
    fingerprint = "a" * 64

    # 功能：模拟嵌入并统计逻辑次数，不发HTTP或付费请求。
    def __init__(self):
        self.calls = 0

    # 功能：返回有效1536维向量，隔离测试只验收DB工作流。
    async def embed(self, text):
        self.calls += 1
        return VECTOR.copy()


# 功能：真实升级后旧信念/向量保留，新工作项跨实例续跑并核验指针召回及旧入口回退。
async def test_generation_upgrade_resume_switch_preserves_legacy(postgres_engine):
    config = alembic_config()

    # 功能：准备0008旧库，后续只expand到0009。
    def old_schema(connection):
        config.attributes["connection"] = connection
        command.upgrade(config, "20261003_0008")

    async with postgres_engine.begin() as connection:
        await connection.run_sync(old_schema)
    maker = async_sessionmaker(postgres_engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        belief = models.Belief(
            topic="legacy", statement="旧结论", confidence=0.9, status="supported", embedding=VECTOR
        )
        goal = models.Goal(title="测试目标", description="续跑", source="human")
        session.add_all([belief, goal])
        await session.flush()
        belief_id = belief.id
        await RetrievalIndex(session).sync("belief", belief)
        entry = await session.scalar(
            select(models.RetrievalEntry).where(models.RetrievalEntry.entity_id == belief_id)
        )
        entry.embedding, entry.fingerprint = VECTOR, "legacy"
    await upgrade_database(postgres_engine)
    embedding = TestEmbedding()
    jobs = RetrievalJobs(postgres_engine, embedding)
    key = UUID((await jobs.create())["job_id"])
    await jobs.run(key, 1)
    assert (await jobs.store.load(key))["status"] == "paused"
    other = RetrievalJobs(postgres_engine, embedding)
    await other.control(key, "resume")
    await other.run(key)
    assert embedding.calls == 2 and (await other.verify(key))["valid"]
    await other.activate(key)
    async with maker() as session:
        old = await session.get(models.Belief, belief_id)
        assert old.statement == "旧结论" and old.confidence == 0.9 and list(old.embedding) == VECTOR
        entry = await session.scalar(
            select(models.RetrievalEntry).where(models.RetrievalEntry.entity_id == belief_id)
        )
        assert list(entry.embedding) == VECTOR and entry.fingerprint == "legacy"
        hits = await HybridRetriever(type("Repo", (), {"s": session})(), embedding).retrieve(
            "不匹配关键词", accepted_only=True
        )
        assert hits[0].row.id == belief_id and "semantic" in hits[0].ranks
        await session.execute(text("DELETE FROM retrieval_entries WHERE entity_kind='belief'"))
        await session.commit()
    assert (await other.verify(key))["keyword_gaps"] == 1
    with pytest.raises(ValueError, match="不完整"):
        await other.activate(key)
    await other.legacy()
    assert (await other.store.status())["active"]["generation_id"] is None


# 功能：工作者持真实控制器锁时第二实例拒绝写入，但暂停可立即持久化且在途结果仍保存。
async def test_generation_controller_lock_and_pause(postgres_engine):
    await upgrade_database(postgres_engine)
    maker = async_sessionmaker(postgres_engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        session.add(models.Belief(topic="测试", statement="候选", status="supported"))
    started, release = asyncio.Event(), asyncio.Event()
    embedding = TestEmbedding()

    # 功能：阻塞在途请求以复现另一个连接的暂停和锁竞争。
    async def blocking(text):
        started.set()
        await release.wait()
        return VECTOR.copy()

    embedding.embed = blocking
    jobs = RetrievalJobs(postgres_engine, embedding)
    key = UUID((await jobs.create())["job_id"])
    task = asyncio.create_task(jobs.run(key))
    try:
        await asyncio.wait_for(started.wait(), 5)
        with pytest.raises(ControllerBusy):
            await RetrievalJobs(postgres_engine, embedding).create()
        await RetrievalJobs(postgres_engine).control(key, "pause")
    finally:
        release.set()
        await asyncio.wait_for(task, 5)
    result = await jobs.store.status(key)
    assert result["jobs"][0]["status"] == "paused"
    assert result["jobs"][0]["counts"]["completed"] == 1
