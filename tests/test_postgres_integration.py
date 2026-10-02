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
from autodidact.migrations import BASELINE_REVISION, HEAD_REVISION, alembic_config, upgrade_database
from autodidact.repository import Repository
from autodidact.schemas import ClaimDraft


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


@pytest.mark.asyncio
async def test_unversioned_0001_upgrade_preserves_agent(postgres_engine):
    config = alembic_config()

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
