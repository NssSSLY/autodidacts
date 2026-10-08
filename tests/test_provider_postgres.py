# 文件职责：在显式*_test随机schema验证F06健康状态跨实例、并发单探针和旧认知保留，缺配置跳过。
import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_postgres_integration import (
    postgres_engine as postgres_engine,  # noqa: PLC0414 - 共享隔离fixture
)

from autodidact import models
from autodidact.migrations import upgrade_database
from autodidact.provider_runtime import (
    CircuitOpen,
    ProviderFailure,
    ProviderIdentity,
    ProviderRuntime,
)


# 功能：真实事务唯一键/锁保持一个健康快照，另实例读熔断，并发恢复仅一探针且不改旧信念。
async def test_health_persistence_single_probe_and_old_belief(postgres_engine):
    await upgrade_database(postgres_engine)
    maker = async_sessionmaker(postgres_engine, expire_on_commit=False)
    async with maker() as session:
        belief = models.Belief(
            topic="legacy", statement="旧信念", status="verified", confidence=0.9
        )
        session.add(belief)
        await session.commit()
        belief_id = belief.id
    first, second = ProviderRuntime(postgres_engine), ProviderRuntime(postgres_engine)
    identity = ProviderIdentity("search", "primary", "https://example.com")
    token = await first.acquire(identity)
    await first.finish(identity, token, ProviderFailure("forbidden"), 0.1)
    with pytest.raises(CircuitOpen):
        await second.acquire(identity)
    async with first.store.edit(identity) as state:
        state["open_until"] = 0  # 仅测试fixture模拟冷却，不修改真实学习库时间。
    results = await asyncio.gather(
        first.acquire(identity), second.acquire(identity), return_exceptions=True
    )
    assert sum(isinstance(result, CircuitOpen) for result in results) == 1
    token = next(result for result in results if isinstance(result, tuple))
    await first.finish(identity, token, None, 0.1)
    async with maker() as session:
        old = await session.get(models.Belief, belief_id)
        assert (old.statement, old.status, old.confidence) == ("旧信念", "verified", 0.9)
