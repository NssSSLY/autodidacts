# 文件职责：提供控制器互斥和 UTC 每日预算预留/结算，避免无界外部调用。
"""Durable admission budgets and database-wide single-writer ownership."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from autodidact import models
from autodidact.config import agent_config

# All controllers sharing one learning database use the same lock, including web/CLI.
CONTROLLER_LOCK = 718204611
BUDGET_LOCK = 718204612


class BudgetExceeded(RuntimeError):
    pass


class ControllerBusy(RuntimeError):
    pass


# 功能：获取数据库级非阻塞控制器锁，在退出时释放；占用时抛出 ControllerBusy。
@asynccontextmanager
async def controller_lock(engine):
    async with engine.connect() as connection:
        acquired = await connection.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": CONTROLLER_LOCK}
        )
        await connection.commit()
        if not acquired:
            raise ControllerBusy("已有学习控制器正在操作此数据库，请等待当前操作结束")
        try:
            yield
        finally:
            await connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": CONTROLLER_LOCK}
            )
            await connection.commit()


class OperationBudget:
    """Reservations survive process interruption; day boundaries are UTC."""

    # 功能：建立独立账本会话工厂，从学习配置读取每日资源上限。
    def __init__(self, engine):
        self.engine = engine
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)
        cfg = agent_config().learning
        self.limits = {
            "llm_calls": cfg.max_daily_model_calls,
            "tokens": cfg.max_daily_tokens,
            "searches": cfg.max_daily_searches,
            "web_reads": cfg.max_daily_web_reads,
            "web_model_calls": cfg.max_daily_web_model_calls,
            "embedding_calls": cfg.max_daily_embedding_calls,
            "estimated_usd": cfg.max_daily_estimated_cost_usd,
        }

    # 功能：在预算事务锁下检查 UTC 当天用量并保存调用预留，超过上限则拒绝调用。
    async def reserve(self, charges: dict[str, float], details: dict | None = None):
        batch_id = uuid4()
        now = datetime.now(UTC)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        async with self.sessions() as session, session.begin():
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": BUDGET_LOCK})
            for resource, units in charges.items():
                if units < 0:
                    raise ValueError("预算预留不能为负数")
                limit = self.limits.get(resource, 0)
                if limit:
                    used = await session.scalar(
                        select(
                            func.coalesce(
                                func.sum(
                                    func.coalesce(
                                        models.OperationEvent.actual, models.OperationEvent.reserved
                                    )
                                ),
                                0,
                            )
                        ).where(
                            models.OperationEvent.resource == resource,
                            models.OperationEvent.created_at >= start,
                        )
                    )
                    if used + units > limit:
                        raise BudgetExceeded(
                            f"{resource} 今日预算不足（已用 {used:g}，上限 {limit:g}）"
                        )
                session.add(
                    models.OperationEvent(
                        batch_id=batch_id,
                        resource=resource,
                        reserved=units,
                        status="reserved",
                        details=details or {},
                    )
                )
        return batch_id

    # 功能：按批次更新完成/失败、实耗与详情；未知消耗保留预留估算以免低估费用。
    async def finish(
        self,
        batch_id,
        *,
        actual: dict[str, float] | None = None,
        error: str | None = None,
        details: dict | None = None,
    ):
        async with self.sessions() as session, session.begin():
            rows = (
                await session.scalars(
                    select(models.OperationEvent).where(models.OperationEvent.batch_id == batch_id)
                )
            ).all()
            for row in rows:
                row.status = "failed" if error else "completed"
                # Unknown usage is kept charged at the reserved upper estimate.
                if actual and row.resource in actual:
                    row.actual = max(0, actual[row.resource])
                row.details = {
                    **row.details,
                    **(details or {}),
                    **({"error": error} if error else {}),
                }


class BudgetedSearch:
    # 功能：逐叶搜索提供方附加预算和持久健康控制，降级链不重复预留总费用。
    def __init__(self, inner, budget: OperationBudget):
        from autodidact.provider_runtime import ProviderIdentity, ProviderRuntime
        from autodidact.tools.search import FallbackSearchProvider

        self.inner, self.budget = inner, budget
        self.provider_name = inner.provider_name
        if isinstance(inner, FallbackSearchProvider):
            self.inner = FallbackSearchProvider(
                [BudgetedSearch(p, budget) for p in inner.providers]
            )
            self.runtime = None
        else:
            self.runtime = ProviderRuntime(getattr(budget, "engine", None))
            self.identity = ProviderIdentity(
                "search",
                inner.provider_name,
                getattr(inner, "base_url", "https://html.duckduckgo.com/html/"),
                credential=getattr(inner, "api_key", ""),
            )

    # 功能：每次物理尝试先熔断准入再预留搜索预算；降级后的请求和重试分别入账。
    async def search(self, query: str, limit: int = 5):
        if self.runtime is None:
            return await self.inner.search(query, limit)

        # 功能：绑定单次受预算搜索与统一尝试上下文。
        async def invoke(context):
            return await self._attempt(query, limit, context)

        return await self.runtime.run(self.identity, invoke)

    # 功能：预留单提供方一次请求并记录结果或取消，错误仅存类型及安全分类。
    async def _attempt(self, query, limit, context):
        from autodidact.provider_runtime import classify_failure

        batch = await self.budget.reserve(
            {"searches": 1}, {"query": query[:500], "provider": self.provider_name, **context}
        )
        try:
            hits = await self.inner.search(query, limit)
        except (Exception, asyncio.CancelledError) as exc:
            await self.budget.finish(
                batch,
                error=type(exc).__name__,
                details={"failure_category": classify_failure(exc).category},
            )
            raise
        await self.budget.finish(batch, details={"result_count": len(hits)})
        return hits
