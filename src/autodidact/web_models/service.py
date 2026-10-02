# 文件职责：装载网页提供方配置，执行预算化提问并保存成功/失败的等级 0 观察。
from __future__ import annotations

import hashlib
from pathlib import Path

import yaml
from sqlalchemy.ext.asyncio import async_sessionmaker

from autodidact import models
from autodidact.config import runtime_settings
from autodidact.runtime import OperationBudget
from autodidact.web_models.playwright_adapter import PlaywrightWebModelAdapter, WebModelConfig


class WebModelService:
    # 功能：建立独立模型观察会话工厂与网页调用预算。
    def __init__(self, engine):
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)
        self.budget = OperationBudget(engine)

    # 功能：读取 YAML 中指定提供方并校验为 WebModelConfig，配置缺失明确报错。
    def adapter(self, provider):
        path = Path(runtime_settings().web_models_config)
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        entry = raw.get("providers", {}).get(provider)
        if entry is None:
            raise ValueError(f"网页提供方未配置：{provider}")
        return PlaywrightWebModelAdapter(WebModelConfig(provider=provider, **entry))

    # 功能：预留网页调用预算并请求适配器，保存原始回答/引用/错误；不把链接自动转换为事实证据。
    async def ask(self, provider, prompt, **context):
        adapter = self.adapter(provider)
        batch = await self.budget.reserve({"web_model_calls": 1}, {"provider": provider, **context})
        answer, error = None, None
        try:
            answer = await adapter.ask(prompt)
            return answer
        except Exception as exc:
            error = type(exc).__name__
            raise
        finally:
            await self.budget.finish(batch, error=error)
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
                        },
                    )
                )
