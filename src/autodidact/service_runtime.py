# 文件职责：校验单用户有限运行计划、只读就绪检查、数据库计划互斥与持久进度，输出有界安全生命周期日志。
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from autodidact import models
from autodidact.delivery import DeliveryError
from autodidact.migrations import HEAD_REVISION, expected_schema_columns
from autodidact.runtime import ControllerBusy

SERVICE_LOCK = 718204615


class RunPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    protocol: Literal["single_user_plan_v1"] = "single_user_plan_v1"
    max_cycles: int = Field(default=1, ge=1, le=1000)
    interval_seconds: int = Field(default=60, ge=0, le=86400)
    max_runtime_seconds: int = Field(default=3600, ge=1, le=86400)


# 功能：有界读取不含命令/密钥的计划；只接受有限循环参数，不解释自然语言或shell。
def read_run_plan(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(16385)
    if len(raw) > 16384:
        raise ValueError("运行计划不得超过16KiB")
    return RunPlan.model_validate_json(raw)


# 功能：检查当前schema版本/表存在，不迁移、不写预算或探测模型；调用者持事务时复用连接。
async def database_readiness(connection):
    tables = set(
        (
            await connection.scalars(
                text("SELECT tablename FROM pg_tables WHERE schemaname=current_schema()")
            )
        ).all()
    )
    versions = (
        (await connection.scalars(text("SELECT version_num FROM alembic_version"))).all()
        if "alembic_version" in tables
        else []
    )
    revision = versions[0] if len(versions) == 1 else None
    missing = sorted(set(expected_schema_columns()) - tables)
    return {
        "ready": revision == HEAD_REVISION and not missing,
        "revision": revision,
        "expected_revision": HEAD_REVISION,
        "missing_tables": missing,
        "note": "就绪只检查版本和表存在，不认证数据/索引质量或外部服务。",
    }


# 功能：最多5秒只读数据库诊断，错误只给类型；不因健康查看自动升级或发起付费请求。
async def service_health(engine):
    try:
        async with asyncio.timeout(5), engine.connect() as connection, connection.begin():
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            await connection.execute(text("SET LOCAL statement_timeout='3000ms'"))
            return await database_readiness(connection)
    except Exception as exc:  # noqa: BLE001 - 本地诊断保留可用，不暴露数据库URL/错误正文。
        return {"ready": False, "expected_revision": HEAD_REVISION, "reason": type(exc).__name__}


# 功能：独占一个计划/维护操作，数据库掉线后锁释放；每轮前assert_service_lease再次核对真实锁。
@asynccontextmanager
async def service_lease(engine):
    async with engine.connect() as connection:
        acquired = await connection.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": SERVICE_LOCK}
        )
        await connection.commit()
        if not acquired:
            raise ControllerBusy("已有运行计划或备份恢复占用此数据库")
        try:
            yield connection
        finally:
            try:
                await connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": SERVICE_LOCK}
                )
                await connection.commit()
            except Exception:  # noqa: BLE001 - 连接断开即释放服务端锁，不重新取锁掩盖原错误。
                await connection.invalidate()


# 功能：核对当前连接仍持真实计划锁，防数据库重启/连接重建后旧计划继续收费。
async def assert_service_lease(connection):
    held = await connection.scalar(
        text(
            "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND pid=pg_backend_pid() "
            "AND classid=0 AND objid=:key AND objsubid=1 AND granted)"
        ),
        {"key": SERVICE_LOCK},
    )
    await connection.commit()
    if not held:
        raise ControllerBusy("计划所有权已丢失，请手动检查后重新启动")


# 功能：建立仅写安全服务事件的JSON轮转日志，不接收模型提示/URL/异常正文；不改全局第三方日志。
def service_logger(directory="logs"):
    folder = Path(directory).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"autodidact.service.{folder}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = RotatingFileHandler(
            folder / "service.jsonl", maxBytes=1048576, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    return logger


# 功能：仅序列化白名单事件、UUID、轮次和异常类型，不把结果正文或配置写进服务日志。
def log_event(logger, event, run_id, cycles, reason=None, session_id=None):
    if logger:
        logger.info(
            json.dumps(
                {
                    "at": datetime.now(UTC).isoformat(),
                    "event": event,
                    "run_id": str(run_id),
                    "cycles": cycles,
                    "reason": reason,
                    "session_id": session_id,
                },
                ensure_ascii=False,
            )
        )


class RunStore:
    # 功能：用独立短事务保存专用可变service_run报告，不回写冻结认知/评估报告。
    def __init__(self, engine):
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)

    # 功能：在计划锁已取得后创建一次运行，重启不自动重放未结束计划。
    async def create(self, plan):
        key = uuid4()
        async with self.sessions() as session, session.begin():
            session.add(
                models.ResearchReport(
                    id=key,
                    kind="service_run",
                    report_key=str(key),
                    payload={
                        "protocol": "single_user_run_v1",
                        "plan": plan.model_dump(),
                        "status": "running",
                        "cycles": 0,
                        "started_at": datetime.now(UTC).isoformat(),
                    },
                )
            )
        return key

    # 功能：行锁合并有限进度字段，服务事件另存轮转日志而非无限增长JSON列表。
    async def update(self, run_id, **fields):
        async with self.sessions() as session, session.begin():
            row = await session.scalar(
                select(models.ResearchReport)
                .where(
                    models.ResearchReport.id == run_id, models.ResearchReport.kind == "service_run"
                )
                .with_for_update()
            )
            if row is None or row.payload.get("protocol") != "single_user_run_v1":
                raise ValueError("服务运行记录不存在或协议不一致")
            row.payload = {**row.payload, **fields, "updated_at": datetime.now(UTC).isoformat()}

    # 功能：只读返回最近20次持久快照；running只表示上次记录，不冒充进程仍存活。
    async def status(self):
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(models.ResearchReport)
                    .where(models.ResearchReport.kind == "service_run")
                    .order_by(models.ResearchReport.created_at.desc(), models.ResearchReport.id)
                    .limit(20)
                )
            ).all()
        return {
            "runs": [{"id": str(r.id), **r.payload} for r in rows],
            "note": "这是最后持久快照；硬崩溃可能仍显示running，重启后不自动收费恢复。",
        }


class PlanRunner:
    # 功能：绑定现有单轮学习回调及状态Store；计划互斥与每轮控制器锁分开，模型/认知规则不变。
    def __init__(self, engine, cycle, store=None, logger=None):
        self.engine, self.cycle, self.logger = engine, cycle, logger
        self.store = store or RunStore(engine)
        self.stop = asyncio.Event()

    # 功能：记录轮间停止请求，当前在途轮正常收尾；强制中断仍由调用者取消task。
    def request_stop(self):
        self.stop.set()

    # 功能：显式运行有界计划、逐轮落进度，失败/预算/空闲停止；退出不自动重启或清零费用。
    async def run(self, plan):
        health = await service_health(self.engine)
        if not health["ready"]:
            raise DeliveryError("数据库未就绪，请先检查service-health并显式init-db")
        async with service_lease(self.engine) as owner:
            run_id = await self.store.create(plan)
            start, cycles, status, reason = time.monotonic(), 0, "completed", "cycle_limit"
            log_event(self.logger, "started", run_id, 0)
            deadline = asyncio.timeout(plan.max_runtime_seconds)
            try:
                async with deadline:
                    for _ in range(plan.max_cycles):
                        if self.stop.is_set():
                            status, reason = "stopped", "requested"
                            break
                        await assert_service_lease(owner)
                        await self.store.update(run_id, phase="running")
                        result = await self.cycle()
                        cycles += 1
                        outcome = result.get("status", "unknown")
                        if not isinstance(outcome, str) or outcome not in {
                            "passed",
                            "reviewed",
                            "budget_exhausted",
                            "busy",
                            "idle",
                            "resume_incompatible",
                            "failed",
                        }:
                            outcome = "unknown"
                        try:
                            session_id = str(UUID(str(result.get("session_id"))))
                        except (ValueError, TypeError):
                            session_id = None
                        await self.store.update(
                            run_id, cycles=cycles, last_outcome=outcome, last_session_id=session_id
                        )
                        log_event(self.logger, "cycle", run_id, cycles, outcome, session_id)
                        if outcome in {
                            "budget_exhausted",
                            "busy",
                            "idle",
                            "resume_incompatible",
                            "failed",
                            "unknown",
                        }:
                            status, reason = "stopped", outcome
                            break
                        if cycles < plan.max_cycles:
                            await self.store.update(run_id, phase="waiting")
                            try:
                                await asyncio.wait_for(self.stop.wait(), plan.interval_seconds)
                            except TimeoutError:
                                pass
            except TimeoutError:
                status, reason = (
                    ("interrupted", "runtime_limit")
                    if deadline.expired()
                    else ("failed", "TimeoutError")
                )
            except asyncio.CancelledError:
                await self.store.update(
                    run_id, status="interrupted", reason="cancelled", cycles=cycles
                )
                log_event(self.logger, "interrupted", run_id, cycles, "cancelled")
                raise
            except Exception as exc:  # noqa: BLE001 - 计划停止，不无限重试或保存异常正文。
                status, reason = "failed", type(exc).__name__
            await self.store.update(
                run_id,
                status=status,
                reason=reason,
                cycles=cycles,
                elapsed_seconds=round(time.monotonic() - start, 3),
                phase="finished",
            )
            log_event(self.logger, status, run_id, cycles, reason)
            return {"run_id": str(run_id), "status": status, "cycles": cycles, "reason": reason}


# 功能：按现有预算/续跑和全库控制器锁执行一轮；只由明确运行计划或网页操作调用。
async def learning_cycle():
    from autodidact.agent import AutonomousLearner
    from autodidact.brain.llm import build_llm
    from autodidact.db import SessionLocal

    async with SessionLocal() as session:
        return await AutonomousLearner(session, build_llm()).run_cycle()
