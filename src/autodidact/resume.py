# 文件职责：按不可变学习尝试缓存成功工作项；恢复外部结果，不恢复模型内部思考。
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
    # 功能：建立独立工作项数据库会话工厂，初始化未绑定尝试的状态。
    def __init__(self, engine):
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)
        self.attempt_id = None

    # 功能：设置当前学习会话 UUID；未绑定时外部操作直接执行且不缓存。
    def bind(self, attempt_id):
        self.attempt_id = UUID(str(attempt_id)) if attempt_id else None

    # 功能：按尝试、类别和输入摘要复用已存结果，否则执行并保存成功值；调用与入库间仍可能重复计费。
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
    # 功能：包装被审计模型与工作项缓存，记录角色和提供方身份。
    def __init__(self, inner, steps, role):
        self.inner, self.steps, self.role = inner, steps, role
        self.provider_name, self.model_name = inner.provider_name, inner.model_name
        self.context = {}

    # 功能：更新当前调用上下文并传给内部审计模型。
    def bind(self, **context):
        self.context = context
        self.inner.bind(**context)

    # 功能：以角色、模型、提示和阶段为缓存键，执行或复用文本模型结果。
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

    # 功能：以 JSON schema 等输入作为缓存键，重放后重新校验结构化结果。
    async def structured(self, system, user, schema):
        # 功能：调用内部结构化模型并转为可写入 JSONB 的数据。
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
    # 功能：将独立研究收集器与学习工作项缓存绑定。
    def __init__(self, inner, steps):
        self.inner, self.steps = inner, steps

    # 功能：重放研究文档集合，并按当前来源依赖再次过滤独立性。
    async def fetch(self, query, *, exclude=(), limit=3):
        # 功能：执行独立研究收集并序列化成功取得的来源文档。
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
    # 功能：包装搜索提供方和持久工作项存储。
    def __init__(self, inner, steps):
        self.inner, self.steps = inner, steps

    # 功能：按查询及数量复用或执行搜索，恢复为 SearchHit 列表。
    async def search(self, query, limit=5):
        from dataclasses import asdict

        from autodidact.tools.search import SearchHit

        # 功能：调用内部搜索并将结果数据类转成可缓存字典。
        async def invoke():
            return [asdict(hit) for hit in await self.inner.search(query, limit=limit)]

        value = await self.steps.run("search", [query, limit], invoke)
        return [SearchHit(**hit) for hit in value]


class ResumableReader:
    # 功能：包装网页阅读器和工作项存储以复用成功原文。
    def __init__(self, inner, steps):
        self.inner, self.steps = inner, steps

    # 功能：按 URL 读取或重放文档，并校验为 SourceDocument。
    async def read(self, url):
        # 功能：读取网页原文并序列化文档，供持久缓存保存。
        async def invoke():
            return (await self.inner.read(url)).model_dump(mode="json")

        return SourceDocument.model_validate(await self.steps.run("read", [url], invoke))

    # 功能：复用 WebReader 的文本摘要算法，保证内容去重键一致。
    @staticmethod
    def hash_text(value):
        from autodidact.tools.reader import WebReader

        return WebReader.hash_text(value)
