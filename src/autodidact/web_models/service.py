# 文件职责：装载网页提供方配置，执行预算化提问并保存成功/失败的等级 0 观察。
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import yaml
from sqlalchemy.ext.asyncio import async_sessionmaker

from autodidact import models
from autodidact.config import runtime_settings
from autodidact.learning.ground_truth import digest
from autodidact.provider_runtime import ProviderIdentity, ProviderRuntime, classify_failure
from autodidact.runtime import OperationBudget
from autodidact.web_models.playwright_adapter import PlaywrightWebModelAdapter, WebModelConfig


class WebModelService:
    # 功能：建立独立模型观察会话工厂与网页调用预算。
    def __init__(self, engine):
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)
        self.budget = OperationBudget(engine)
        self.runtime = ProviderRuntime(engine)

    # 功能：读取 YAML 中指定提供方并校验为 WebModelConfig，配置缺失明确报错。
    def adapter(self, provider):
        from autodidact.web_models.auxiliary_config import PRESETS, managed_config

        if provider in PRESETS:
            return PlaywrightWebModelAdapter(managed_config(provider))
        path = Path(runtime_settings().web_models_config)
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        entry = raw.get("providers", {}).get(provider)
        if entry is None:
            raise ValueError(f"网页提供方未配置：{provider}")
        return PlaywrightWebModelAdapter(WebModelConfig(provider=provider, **entry))

    # 功能：按完整配置隔离健康且不自动重发；F11显式fresh_chat强制新会话，沿预算和0级审计。
    async def ask(
        self, provider, prompt, *, fresh_chat=False, expected_config_sha256=None, **context
    ):
        adapter = self.adapter(provider)
        if (
            expected_config_sha256 is not None
            and digest(adapter.config.model_dump(mode="json")) != expected_config_sha256
        ):
            raise ValueError("网页来源配置已改变，拒绝使用旧冻结身份提交")
        if fresh_chat:
            adapter.config = adapter.config.model_copy(update={"require_new_chat": True})
        identity = ProviderIdentity(
            "web_model", provider, adapter.config.url, credential=adapter.config.model_dump_json()
        )

        # 功能：关联单次网页发送上下文，保持无自动重复提交。
        async def attempt(operation_context):
            return await self._attempt(adapter, provider, prompt, {**context, **operation_context})

        return await self.runtime.run(identity, attempt, retry_safe=False)

    # 功能：预留一次网页提交预算，记录回答/错误/取消和失败分类；输出保持等级0。
    async def _attempt(self, adapter, provider, prompt, context):
        batch = await self.budget.reserve({"web_model_calls": 1}, {"provider": provider, **context})
        answer, error = None, None
        try:
            answer = await adapter.ask(prompt)
            return answer
        except (Exception, asyncio.CancelledError) as exc:
            error = type(exc).__name__
            failure_category = classify_failure(exc).category
            raise
        finally:
            await self.budget.finish(
                batch,
                error=error,
                details={"failure_category": failure_category if error else None},
            )
            response = answer.response if answer else ""
            async with self.sessions() as session, session.begin():
                session.add(
                    models.ModelObservation(
                        provider=provider,
                        access_type="web",
                        displayed_model=answer.displayed_model if answer else None,
                        prompt=prompt,
                        response=response,
                        response_hash=hashlib.sha256(response.encode()).hexdigest(),
                        conversation_ref=answer.conversation_ref if answer else None,
                        metadata_json={
                            "evidence_level": 0,
                            "error": error,
                            "citations": answer.citations if answer else [],
                            **context,
                            "failure_category": failure_category if error else None,
                        },
                    )
                )
