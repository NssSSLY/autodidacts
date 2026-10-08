# 文件职责：分类提供方失败，执行有界退避/熔断与单探针恢复，持久化脱敏健康快照并提供只读状态查询。
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from autodidact import models
from autodidact.config import agent_config

PROTOCOL = "provider_health_v1"


class ProviderFailure(RuntimeError):
    # 功能：携带安全的失败分类/重试信息，异常文字不包含请求地址、密钥或外部响应正文。
    def __init__(self, category, *, retryable=False, status_code=None, retry_after=None):
        self.category = category
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after = retry_after
        super().__init__(f"提供方失败：{category}")


class CircuitOpen(ProviderFailure):
    # 功能：明确熔断/探针占用阻断，不实际调用提供方或消耗调用预算。
    def __init__(self, retry_after=None):
        super().__init__("circuit_open", retry_after=retry_after)


# 功能：读取Retry-After秒数或HTTP日期，非法值未知，有效等待最多七天且不执行响应指令。
def retry_after_seconds(value, now=None):
    if not value or len(value) > 100:
        return None
    now = time.time() if now is None else now
    try:
        if value.strip().isdigit():
            seconds = int(value)
        else:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                return None
            seconds = date.timestamp() - now
        return max(0, min(float(seconds), 604800))
    except (ValueError, TypeError, OverflowError):
        return None


# 功能：把HTTP/超时/连接/格式/预算/取消等归入有限类别，鉴权/验证码/安全拒绝不自动重试。
def classify_failure(exc):
    from playwright.async_api import TimeoutError as BrowserTimeout

    from autodidact.runtime import BudgetExceeded

    if isinstance(exc, ProviderFailure):
        return exc
    if isinstance(exc, BudgetExceeded):
        return ProviderFailure("budget")
    if isinstance(exc, asyncio.CancelledError):
        return ProviderFailure("cancelled")
    if getattr(exc, "category", None) in {"unsafe_source", "invalid_response"}:
        return ProviderFailure(exc.category)
    if isinstance(exc, (TimeoutError, httpx.TimeoutException, BrowserTimeout)):
        return ProviderFailure("timeout", retryable=True)
    if isinstance(exc, (httpx.NetworkError, httpx.RemoteProtocolError, OSError)):
        return ProviderFailure("connection", retryable=True)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        category = {
            401: "authentication",
            403: "forbidden",
            404: "not_found",
            408: "timeout",
            429: "rate_limit",
        }.get(status)
        return ProviderFailure(
            category or ("upstream" if status >= 500 else "request_rejected"),
            retryable=status in {408, 425, 429, 500, 502, 503, 504},
            status_code=status,
            retry_after=retry_after_seconds(exc.response.headers.get("retry-after")),
        )
    if isinstance(exc, (ValueError, KeyError, IndexError, TypeError, AttributeError)):
        return ProviderFailure("invalid_response")
    return ProviderFailure("unexpected")


# 功能：检测明确的验证页面标题/挑战标记并停止读取，不猜测正文中的普通验证码讨论。
def reject_challenge(body):
    head = body[:65536].lower()
    match = re.search(r"<title[^>]*>(.*?)</title>", head, flags=re.DOTALL)
    title = " ".join(match.group(1).split()) if match else ""
    if title in {"captcha", "verify you are human", "人机验证", "安全验证"} or (
        title in {"just a moment...", "attention required! | cloudflare"}
        and ("cf-chl-" in head or "challenge-form" in head)
    ):
        raise ProviderFailure("access_challenge")


@dataclass(frozen=True)
class ProviderIdentity:
    kind: str
    provider: str
    endpoint: str = field(default="", repr=False)
    model: str = ""
    credential: str = field(default="", repr=False)

    # 功能：将完整配置身份转为摘要；配置/凭据变更使用新健康键，状态中不保存秘密原值。
    def key(self):
        return hashlib.sha256(
            json.dumps(
                [self.kind, self.provider, self.endpoint, self.model, self.credential]
            ).encode()
        ).hexdigest()

    # 功能：导出安全的提供方标签和端点主机，不输出路径、查询参数、用户名、密码或密钥。
    def labels(self):
        try:
            host = urlsplit(self.endpoint).hostname or ""
        except ValueError:
            host = "invalid_endpoint"
        return {
            "kind": self.kind,
            "provider": self.provider[:100],
            "model": self.model[:100],
            "endpoint_host": host[:250],
        }


class HealthStore:
    # 功能：绑定独立数据库会话；无数据库的调用仅用该Store的内存状态，避免跨库混用。
    def __init__(self, engine=None):
        self.sessions = (
            async_sessionmaker(engine, expire_on_commit=False) if engine is not None else None
        )
        self.rows = {}
        self.lock = asyncio.Lock()

    # 功能：按身份事务锁串行更新既有JSONB健康快照；不占用学习事务或改写认知/冻结报告。
    @asynccontextmanager
    async def edit(self, identity):
        key = identity.key()
        if self.sessions is None:
            async with self.lock:
                row = self.rows.setdefault(key, {"protocol": PROTOCOL, **identity.labels()})
                yield row
            return
        async with self.sessions() as session, session.begin():
            lock = int(key[:15], 16)
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
            report = await session.scalar(
                select(models.ResearchReport).where(
                    models.ResearchReport.kind == "provider_health",
                    models.ResearchReport.report_key == key,
                )
            )
            if report is None:
                report = models.ResearchReport(
                    kind="provider_health",
                    report_key=key,
                    payload={"protocol": PROTOCOL, **identity.labels()},
                )
                session.add(report)
            if report.payload.get("protocol") != PROTOCOL:
                raise ValueError("未知提供方健康协议")
            payload = dict(report.payload)
            yield payload
            report.payload = payload


class ProviderRuntime:
    # 功能：建立有界运营策略及可注入时间/等待器，供所有受预算提供方复用同一执行规则。
    def __init__(self, engine=None, *, policy=None, store=None, clock=None, sleep=None):
        self.policy = policy or agent_config().providers
        self.store = store or HealthStore(engine)
        self.clock = clock or time.time
        self.sleep = sleep or asyncio.sleep

    # 功能：检查熔断并领取过冷却后的唯一半开探针；健康闭合调用保留代号防止旧响应误关新熔断。
    async def acquire(self, identity):
        now = self.clock()
        denied = False
        token = None
        remaining = None
        async with self.store.edit(identity) as state:
            state.setdefault("state", "closed")
            remaining = max(state.get("open_until", 0), state.get("probe_until", 0)) - now
            if remaining > 0:
                state["blocked_calls"] = state.get("blocked_calls", 0) + 1
                denied = True
            elif state.get("state", "closed") != "closed":
                probe = str(uuid4())
                state.update(
                    state="half_open",
                    probe_id=probe,
                    probe_until=now + self.policy.attempt_timeout_seconds + 5,
                )
                token = (state.get("generation", 0), probe)
            else:
                token = (state.get("generation", 0), None)
        if denied:
            raise CircuitOpen(remaining)
        return token

    # 功能：记录已完成尝试、失败类别/时延；连续故障或明确封锁开启冷却，探针成功才关闭新熔断。
    async def finish(self, identity, token, failure, latency):
        now = self.clock()
        async with self.store.edit(identity) as state:
            state.update(
                last_attempt_at=datetime.fromtimestamp(now, UTC).isoformat(),
                last_latency_seconds=latency,
            )
            state["attempts"] = state.get("attempts", 0) + int(
                failure is None or failure.category != "budget"
            )
            state["successes" if failure is None else "failures"] = (
                state.get("successes" if failure is None else "failures", 0) + 1
            )
            state["last_error"] = (
                None
                if failure is None
                else {
                    "category": failure.category,
                    "status_code": failure.status_code,
                    "retryable": failure.retryable,
                    "retry_after_seconds": failure.retry_after,
                }
            )
            if token[0] != state.get("generation", 0) or token[1] != state.get("probe_id"):
                return
            if failure is None:
                state.update(
                    state="closed",
                    consecutive_failures=0,
                    open_until=0,
                    probe_until=0,
                    probe_id=None,
                    generation=token[0] + int(token[1] is not None),
                )
                return
            if (
                failure.category
                in {
                    "budget",
                    "cancelled",
                    "unsafe_source",
                    "unsupported_content",
                    "not_found",
                    "request_rejected",
                }
                and not token[1]
            ):
                return
            failures = state.get("consecutive_failures", 0) + 1
            state["consecutive_failures"] = failures
            blocked = failure.category in {"authentication", "forbidden", "access_challenge"}
            if (
                token[1]
                or blocked
                or failures >= self.policy.circuit_failure_threshold
                or failure.retry_after
            ):
                delay = max(
                    self.policy.circuit_cooldown_seconds if not failure.retry_after else 0,
                    failure.retry_after or 0,
                )
                state.update(
                    state="open",
                    open_until=now + delay,
                    probe_until=0,
                    probe_id=None,
                    generation=token[0] + 1,
                )

    # 功能：对可重试故障指数退避加抖动并遵守有界Retry-After；每次尝试重新准入/计费，取消与预算立即传播。
    async def run(self, identity, invoke, *, retry_safe=True):
        call_id = str(uuid4())
        for attempt in range(1, self.policy.max_attempts + 1):
            token = await self.acquire(identity)
            started = time.monotonic()
            context = {
                "provider_health_key": identity.key(),
                "provider_call_id": call_id,
                "provider_attempt": attempt,
            }
            try:
                async with asyncio.timeout(self.policy.attempt_timeout_seconds):
                    result = await invoke(context)
            except (Exception, asyncio.CancelledError) as exc:
                failure = classify_failure(exc)
                await self.finish(identity, token, failure, time.monotonic() - started)
                if failure.category in {"budget", "cancelled"}:
                    raise
                if not retry_safe or not failure.retryable or attempt == self.policy.max_attempts:
                    raise failure from None
                delay = min(
                    self.policy.retry_max_seconds,
                    self.policy.retry_base_seconds * 2 ** (attempt - 1),
                )
                delay = random.uniform(delay / 2, delay)
                if failure.retry_after is not None:
                    if failure.retry_after > self.policy.retry_max_seconds:
                        raise failure from None
                    delay = max(delay, failure.retry_after)
                await self.sleep(delay)
            else:
                await self.finish(identity, token, None, time.monotonic() - started)
                return result
        raise RuntimeError("提供方尝试上限已到达")


# 功能：有界只读查询已进入准入的持久健康快照，按冷却时间计算可探测状态，不触发模型/网络/恢复。
async def provider_health(session, limit=50):
    if not 1 <= limit <= 100:
        raise ValueError("limit必须为1—100")
    rows = (
        await session.scalars(
            select(models.ResearchReport)
            .where(models.ResearchReport.kind == "provider_health")
            .order_by(models.ResearchReport.created_at.desc(), models.ResearchReport.id)
            .limit(limit + 1)
        )
    ).all()
    now = time.time()
    items = []
    for row in rows[:limit]:
        payload = dict(row.payload)
        remaining = max(payload.get("open_until", 0), payload.get("probe_until", 0)) - now
        items.append(
            {
                "id": str(row.id),
                "health_key": row.report_key,
                **payload,
                "retry_in_seconds": max(0, remaining),
                "probe_available": payload.get("state") != "closed" and remaining <= 0,
            }
        )
    return {
        "providers": items,
        "truncated": len(rows) > limit,
        "note": "只显示已进入准入的身份；未记录不代表健康。冷却到期仅允许下一次实际调用探针，查看状态不发请求。",
    }
