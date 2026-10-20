# 文件职责：仅显式*_test随机schema验证计划互斥/真实锁丢失、持久进度/只读健康和流式认知摘要，缺配置跳过。
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_postgres_integration import postgres_engine as postgres_engine  # noqa: PLC0414

from autodidact import models
from autodidact.backups import database_inventory
from autodidact.migrations import upgrade_database
from autodidact.runtime import ControllerBusy
from autodidact.service_runtime import (
    PlanRunner,
    RunPlan,
    RunStore,
    assert_service_lease,
    service_health,
    service_lease,
)


# 功能：真实连接服务锁互斥，手动释放后旧所有者检测失效；有限计划落库但不改旧信念。
async def test_plan_single_owner_and_durable_state(postgres_engine):
    await upgrade_database(postgres_engine)
    maker = async_sessionmaker(postgres_engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        belief = models.Belief(statement="旧结论", topic="运营", status="verified", confidence=0.9)
        session.add(belief)
    async with service_lease(postgres_engine) as owner:
        await assert_service_lease(owner)
        with pytest.raises(ControllerBusy):
            async with service_lease(postgres_engine):
                pass
        await owner.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": 718204615})
        await owner.commit()
        with pytest.raises(ControllerBusy):
            await assert_service_lease(owner)

    # 功能：无付费模型的固定单轮替身，持久层仍使用真实PostgreSQL。
    async def cycle():
        return {"status": "passed"}

    result = await PlanRunner(postgres_engine, cycle).run(RunPlan(max_cycles=2, interval_seconds=0))
    assert result["cycles"] == 2 and result["status"] == "completed"
    assert (await RunStore(postgres_engine).status())["runs"][0]["cycles"] == 2
    async with maker() as session:
        old = await session.get(models.Belief, belief.id)
        assert old.statement == "旧结论" and old.confidence == 0.9 and old.status == "verified"


# 功能：READ ONLY健康与规范JSON流式摘要使用真实表/数据；相同行数内容变更会改变信念摘要。
async def test_health_and_inventory_preserve_and_detect_data(postgres_engine):
    await upgrade_database(postgres_engine)
    assert (await service_health(postgres_engine))["ready"]
    maker = async_sessionmaker(postgres_engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        session.add(
            models.Belief(statement="旧结论", topic="运营", status="verified", confidence=0.9)
        )
    async with postgres_engine.connect() as connection, connection.begin():
        await connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        await connection.execute(text("SET LOCAL timezone='UTC'"))
        first = await database_inventory(connection)
    async with postgres_engine.begin() as connection:
        await connection.execute(text("UPDATE beliefs SET statement='新副本差异'"))
    async with postgres_engine.connect() as connection, connection.begin():
        second = await database_inventory(connection)
    assert first["beliefs"]["rows"] == second["beliefs"]["rows"] == 1
    assert first["beliefs"]["sha256"] != second["beliefs"]["sha256"]
