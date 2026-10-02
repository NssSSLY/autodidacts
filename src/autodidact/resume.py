"""Durable replay of successful work items within one immutable learning attempt."""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from autodidact import models
from autodidact.brain.llm import LLM
from autodidact.schemas import SourceDocument

PROTOCOL = "durable_replay_v1"


class DurableSteps:
    def __init__(self, engine):
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)
        self.attempt_id = None

    def bind(self, attempt_id):
        self.attempt_id = UUID(str(attempt_id)) if attempt_id else None

    async def run(self, kind, key, invoke):
        if not self.attempt_id:
            return await invoke()
        digest = hashlib.sha256(
            json.dumps(key, ensure_ascii=False, sort_keys=True, default=str).encode()
        ).hexdigest()
        async with self.sessions() as session:
            found = await session.scalar(
                select(models.LearningStep).where(
                    models.LearningStep.learning_session_id == self.attempt_id,
                    models.LearningStep.kind == kind,
                    models.LearningStep.step_key == digest,
                )
            )
            if found:
                return found.payload["value"]
        value = await invoke()
        async with self.sessions() as session, session.begin():
            await session.execute(
                insert(models.LearningStep)
                .values(
                    learning_session_id=self.attempt_id,
                    kind=kind,
                    step_key=digest,
                    payload={"value": value},
                )
                .on_conflict_do_nothing(constraint="uq_learning_steps_item")
            )
        return value


class ResumableLLM(LLM):
    def __init__(self, inner, steps, role):
        self.inner, self.steps, self.role = inner, steps, role
        self.provider_name, self.model_name = inner.provider_name, inner.model_name
        self.context = {}

    def bind(self, **context):
        self.context = context
        self.inner.bind(**context)

    async def text(self, system, user, *, temperature=None):
        return await self.steps.run(
            "llm",
            [
                self.role,
                self.provider_name,
                self.model_name,
                system,
                user,
                temperature,
                self.context.get("phase"),
            ],
            lambda: self.inner.text(system, user, temperature=temperature),
        )

    async def structured(self, system, user, schema):
        async def invoke():
            return (await self.inner.structured(system, user, schema)).model_dump(mode="json")

        value = await self.steps.run(
            "llm",
            [
                self.role,
                self.provider_name,
                self.model_name,
                system,
                user,
                schema.model_json_schema(),
                self.context.get("phase"),
            ],
            invoke,
        )
        return schema.model_validate(value)


class ResumableResearch:
    def __init__(self, inner, steps):
        self.inner, self.steps = inner, steps

    async def fetch(self, query, *, exclude=(), limit=3):
        async def invoke():
            return [
                d.model_dump(mode="json")
                for d in await self.inner.fetch(query, exclude=exclude, limit=limit)
            ]

        value = await self.steps.run(
            "research",
            [
                query,
                limit,
                [hashlib.sha256(d.model_dump_json().encode()).hexdigest() for d in exclude],
            ],
            invoke,
        )
        docs = [SourceDocument.model_validate(d) for d in value]
        return await self.inner.filter_independent(docs, exclude)


class ResumableSearch:
    def __init__(self, inner, steps):
        self.inner, self.steps = inner, steps

    async def search(self, query, limit=5):
        from dataclasses import asdict

        from autodidact.tools.search import SearchHit

        async def invoke():
            return [asdict(hit) for hit in await self.inner.search(query, limit=limit)]

        value = await self.steps.run("search", [query, limit], invoke)
        return [SearchHit(**hit) for hit in value]


class ResumableReader:
    def __init__(self, inner, steps):
        self.inner, self.steps = inner, steps

    async def read(self, url):
        async def invoke():
            return (await self.inner.read(url)).model_dump(mode="json")

        return SourceDocument.model_validate(await self.steps.run("read", [url], invoke))

    @staticmethod
    def hash_text(value):
        from autodidact.tools.reader import WebReader

        return WebReader.hash_text(value)
