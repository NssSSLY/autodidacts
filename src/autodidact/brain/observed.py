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
    def __init__(self, inner: LLM, engine, *, role: str = "learner"):
        self.inner = inner
        self.provider_name, self.model_name = inner.provider_name, inner.model_name
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)
        self.budget = OperationBudget(engine)
        self.role = role
        self.context: dict = {}

    def bind(self, **context):
        self.context = context

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

    async def text(self, system, user, *, temperature=None):
        return await self._call(
            system, user, lambda: self.inner.text(system, user, temperature=temperature)
        )

    async def structured(self, system, user, schema):
        import json

        # Keep Mock's schema fixtures while accounting for the schema added to real prompts.
        audit_user = (
            user + "\nJSON schema:\n" + json.dumps(schema.model_json_schema(), ensure_ascii=False)
        )
        return await self._call(
            system, audit_user, lambda: self.inner.structured(system, user, schema), schema.__name__
        )
