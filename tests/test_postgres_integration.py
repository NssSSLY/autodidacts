# 文件职责：仅在显式 *_test 数据库内临时 schema 验证升级、并发唯一约束和向量召回。
"""在线集成验证：只对显式指定的、可丢弃的 *_test 数据库运行。"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from autodidact import models
from autodidact.enums import BeliefStatus, GoalStatus
from autodidact.knowledge.support_assessment import SUPPORT_PROTOCOL
from autodidact.migrations import BASELINE_REVISION, HEAD_REVISION, alembic_config, upgrade_database
from autodidact.repository import Repository
from autodidact.schemas import ClaimDraft, ClaimScope


# 功能：无测试库配置时跳过；有配置时要求 *_test 名称并创建/清理隔离 schema，不使用日常学习库。
@pytest.fixture
async def postgres_engine():
    url = os.environ.get("AUTODIDACT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("设置 AUTODIDACT_TEST_DATABASE_URL 后运行在线数据库测试")
    parsed = make_url(url)
    if not parsed.database or not parsed.database.endswith("_test"):
        pytest.fail("在线测试只能连接名称以 _test 结尾的可丢弃数据库")

    schema = f"autodidact_it_{uuid4().hex}"
    admin = create_async_engine(url)
    engine = None
    try:
        async with admin.begin() as connection:
            await connection.execute(
                text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
            )
            await connection.execute(text(f"CREATE SCHEMA {schema}"))
        engine = create_async_engine(
            url, connect_args={"server_settings": {"search_path": f"{schema},public"}}
        )
        yield engine
    finally:
        if engine is not None:
            await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        await admin.dispose()


# 功能：在隔离库验证无版本的初始结构升级后保留智能体身份。
@pytest.mark.asyncio
async def test_unversioned_0001_upgrade_preserves_agent(postgres_engine):
    config = alembic_config()

    # 功能：在测试连接创建 0001 基线，准备历史升级场景。
    def build_baseline(connection):
        config.attributes["connection"] = connection
        command.upgrade(config, BASELINE_REVISION)

    async with postgres_engine.begin() as connection:
        await connection.run_sync(build_baseline)
        await connection.execute(text("DROP TABLE alembic_version"))

    maker = async_sessionmaker(postgres_engine, expire_on_commit=False)
    async with maker() as session:
        agent = models.Agent(name="migration-survivor", mission="保留状态", current_focus="测试")
        session.add(agent)
        await session.commit()
        original_id = agent.id

    await upgrade_database(postgres_engine)
    async with postgres_engine.connect() as connection:
        actual = await connection.scalar(
            text("SELECT id FROM agents WHERE name = 'migration-survivor'")
        )
        revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    assert actual == original_id
    assert revision == HEAD_REVISION


# 功能：在隔离库并发写同主张并验证向量召回，检查真实数据库约束。
@pytest.mark.asyncio
async def test_concurrent_claim_uniqueness_and_pgvector_recall(postgres_engine):
    await upgrade_database(postgres_engine)
    maker = async_sessionmaker(postgres_engine, expire_on_commit=False)
    async with maker() as session:
        goal = models.Goal(
            title="并发测试",
            status=GoalStatus.DISCOVERED,
            source="human",
        )
        session.add(goal)
        await session.commit()
        learning = models.LearningSession(goal_id=goal.id)
        session.add(learning)
        await session.commit()
        learning_id = learning.id

    # 功能：使用独立测试会话写相同主张，以制造并发唯一键竞争。
    async def write_claim():
        async with maker() as session:
            return await Repository(session).add_claim(
                ClaimDraft(statement="IMU 提供短期约束", topic="VIO", confidence=0.8),
                learning_id,
                [],
            )

    first, second = await asyncio.gather(write_claim(), write_claim())
    assert first.id == second.id

    async with maker() as session:
        belief = models.Belief(
            topic="VIO",
            statement="IMU 提供短期约束",
            status=BeliefStatus.SUPPORTED,
            embedding=[1.0] + [0.0] * 1535,
        )
        session.add(belief)
        await session.commit()
        recalled = await Repository(session).semantic_beliefs([1.0] + [0.0] * 1535)
        assert [item.id for item in recalled] == [belief.id]


# 功能：在显式隔离库验证0006主张原文/范围/置信保留，0007旧结构审计默认为未知空对象。
@pytest.mark.asyncio
async def test_scope_upgrade_preserves_claim_and_defaults_structure(postgres_engine):
    config = alembic_config()

    # 功能：在测试连接创建0006旧版本，不触及日常学习库。
    def build_scope(connection):
        config.attributes["connection"] = connection
        command.upgrade(config, "20261003_0006")

    claim_id = uuid4()
    async with postgres_engine.begin() as connection:
        await connection.run_sync(build_scope)
        await connection.execute(
            text(
                "INSERT INTO claims (id, statement, confidence, source_ids, scope) VALUES (:id, '保留的旧主张', 0.8, '[]'::jsonb, '{}'::jsonb)"
            ),
            {"id": claim_id},
        )
    await upgrade_database(postgres_engine)
    async with postgres_engine.connect() as connection:
        row = (
            await connection.execute(
                text("SELECT statement, confidence, scope, structure FROM claims WHERE id=:id"),
                {"id": claim_id},
            )
        ).one()
    assert tuple(row) == ("保留的旧主张", 0.8, {}, {})


# 功能：在隔离库验证0007升级后旧信念评分/状态及支持证据保持原样，新审计默认未知。
@pytest.mark.asyncio
async def test_review_upgrade_preserves_old_belief_and_evidence(postgres_engine):
    config = alembic_config()

    # 功能：只在随机测试schema创建0007历史结构。
    def build_structure(connection):
        config.attributes["connection"] = connection
        command.upgrade(config, "20261003_0007")

    belief_id, evidence_id = uuid4(), uuid4()
    async with postgres_engine.begin() as connection:
        await connection.run_sync(build_structure)
        await connection.execute(
            text(
                "INSERT INTO beliefs (id, topic, statement, status, confidence, metadata_json) VALUES (:id, '历史', '保留原结论', 'verified', 0.9, '{}'::jsonb)"
            ),
            {"id": belief_id},
        )
        await connection.execute(
            text(
                "INSERT INTO evidence (id, belief_id, stance, excerpt) VALUES (:id, :belief, 'support', '保留历史摘录')"
            ),
            {"id": evidence_id, "belief": belief_id},
        )
    await upgrade_database(postgres_engine)
    async with postgres_engine.connect() as connection:
        belief = (
            await connection.execute(
                text("SELECT statement, status, confidence FROM beliefs WHERE id=:id"),
                {"id": belief_id},
            )
        ).one()
        evidence = (
            await connection.execute(
                text(
                    "SELECT stance, excerpt, assessment, claim_evidence_id FROM evidence WHERE id=:id"
                ),
                {"id": evidence_id},
            )
        ).one()
    assert tuple(belief) == ("保留原结论", "verified", 0.9)
    assert tuple(evidence) == ("support", "保留历史摘录", {}, None)


# 功能：在隔离库并发写同一反对锚点，验证已有唯一约束/savepoint幂等且旧信念不变。
@pytest.mark.asyncio
async def test_concurrent_negative_evidence_is_unique(postgres_engine):
    await upgrade_database(postgres_engine)
    maker = async_sessionmaker(postgres_engine, expire_on_commit=False)
    scope = ClaimScope().model_dump(mode="json")
    quote = "An independently observed counterexample contradicts the old assertion."
    async with maker() as session:
        belief = models.Belief(
            topic="test",
            statement="Old assertion",
            status="verified",
            confidence=0.9,
            metadata_json={},
        )
        source = models.Source(
            url="https://test.example/source",
            content_hash=uuid4().hex,
            extracted_text=quote + " background" * 30,
            source_type="web",
            evidence_level=1,
            credibility_score=0.3,
            metadata_json={},
        )
        claim = models.Claim(statement=belief.statement, scope=scope, source_ids=[])
        session.add_all([belief, source, claim])
        await session.flush()
        anchor = models.ClaimEvidence(
            claim_id=claim.id,
            source_id=source.id,
            excerpt=quote,
            excerpt_hash=uuid4().hex,
            status="contradicts",
            reason="counterexample",
            assessment={"protocol": SUPPORT_PROTOCOL, "scope": scope},
        )
        session.add(anchor)
        await session.commit()
        belief_id, claim_id = belief.id, claim.id

    # 功能：通过不同连接竞争同一反对Evidence，不操作控制器或生产学习库。
    async def write_evidence():
        async with maker() as session:
            belief = await session.get(models.Belief, belief_id)
            claim = await session.get(models.Claim, claim_id)
            return [e.id for e in await Repository(session).record_belief_evidence(belief, claim)]

    first, second = await asyncio.gather(write_evidence(), write_evidence())
    assert first == second and len(first) == 1
    async with maker() as session:
        belief = await session.get(models.Belief, belief_id)
        assert belief.status == "verified" and belief.confidence == 0.9
