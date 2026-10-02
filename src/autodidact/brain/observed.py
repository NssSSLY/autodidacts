# 文件职责：为模型调用保存等级 0 观察、错误、时延、usage 及预算账本。
"""Record model output as level-zero observations, including failed calls."""

from __future__ import annotations

import hashlib
import time

from sqlalchemy.ext.asyncio import async_sessionmaker

from autodidact import models
from autodidact.brain.llm import LLM
from autodidact.config import runtime_settings
from autodidact.runtime import OperationBudget


class ObservedLLM(LLM):
    # 功能：包装模型并建立独立审计会话与预算，记录学习/裁判等调用角色。
    def __init__(self, inner: LLM, engine, *, role: str = "learner"):
        self.inner = inner
        self.provider_name, self.model_name = inner.provider_name, inner.model_name
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)
        self.budget = OperationBudget(engine)
        self.role = role
        self.context: dict = {}

    # 功能：绑定目标、会话、阶段或实验组上下文以关联后续审计记录。
    def bind(self, **context):
        self.context = context

    # 功能：预留预算、执行模型、在成功/失败后记 usage 与原始输出；保存不等于承认输出为真。
    async def _call(self, system, user, invoke, schema_name=None):
        settings = runtime_settings()
        # UTF-8 bytes are a conservative admission estimate, not reported token usage.
        input_bound = len((system + user).encode("utf-8")) + 256
        output_bound = settings.llm_max_output_tokens
        estimate = (
            input_bound * settings.llm_input_usd_per_million
            + output_bound * settings.llm_output_usd_per_million
        ) / 1_000_000
        batch = await self.budget.reserve(
            {"llm_calls": 1, "tokens": input_bound + output_bound, "estimated_usd": estimate},
            {
                "provider": self.provider_name,
                "model": self.model_name,
                "role": self.role,
                **self.context,
            },
        )
        started = time.monotonic()
        response, error = "", None
        self.inner.last_response = ""
        self.inner.last_usage = {}
        try:
            result = await invoke()
            response = result.model_dump_json() if schema_name else result
        except Exception as exc:
            error = type(exc).__name__
            raise
        finally:
            if error and not response:
                response = getattr(self.inner, "last_response", "") or ""
            usage = getattr(self.inner, "last_usage", {}) or {}
            actual = {"llm_calls": 1}
            if usage.get("total_tokens") is not None:
                actual["tokens"] = float(usage["total_tokens"])
            if (
                usage.get("prompt_tokens") is not None
                and usage.get("completion_tokens") is not None
            ):
                actual["estimated_usd"] = (
                    usage["prompt_tokens"] * settings.llm_input_usd_per_million
                    + usage["completion_tokens"] * settings.llm_output_usd_per_million
                ) / 1_000_000
            metadata = {
                "evidence_level": 0,
                "role": self.role,
                "schema": schema_name,
                "usage": usage,
                "error": error,
                "latency_seconds": time.monotonic() - started,
                "operation_batch_id": str(batch),
                **self.context,
            }
            await self.budget.finish(
                batch,
                actual=actual,
                error=error,
                details={"latency_seconds": metadata["latency_seconds"]},
            )
            async with self.sessions() as session, session.begin():
                session.add(
                    models.ModelObservation(
                        provider=self.provider_name,
                        access_type="api" if self.provider_name != "mock" else "mock",
                        displayed_model=self.model_name,
                        prompt=f"SYSTEM:\n{system}\nUSER:\n{user}",
                        response=response,
                        response_hash=hashlib.sha256(response.encode()).hexdigest(),
                        metadata_json=metadata,
                    )
                )
        return result

    # 功能：经统一审计入口执行文本请求，保留温度参数。
    async def text(self, system, user, *, temperature=None):
        return await self._call(
            system, user, lambda: self.inner.text(system, user, temperature=temperature)
        )

    # 功能：将 schema 纳入预算/审计提示，保持内部结构化校验和 Mock 协议兼容。
    async def structured(self, system, user, schema):
        import json

        # Keep Mock's schema fixtures while accounting for the schema added to real prompts.
        audit_user = (
            user + "\nJSON schema:\n" + json.dumps(schema.model_json_schema(), ensure_ascii=False)
        )
        return await self._call(
            system, audit_user, lambda: self.inner.structured(system, user, schema), schema.__name__
        )
