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
    def __init__(self, engine):
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)
        self.budget = OperationBudget(engine)

    def adapter(self, provider):
        path = Path(runtime_settings().web_models_config)
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        entry = raw.get("providers", {}).get(provider)
        if entry is None:
            raise ValueError(f"网页提供方未配置：{provider}")
        return PlaywrightWebModelAdapter(WebModelConfig(provider=provider, **entry))

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
