# 文件职责：持久化嵌入重建任务/候选版本，执行有界后台批次、暂停/恢复/重试、完整性检查和原子启用。
from __future__ import annotations

import asyncio
import hashlib
import math
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, literal, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from autodidact import models
from autodidact.provider_runtime import classify_failure
from autodidact.retrieval import ENTITIES, RetrievalIndex, entity_text
from autodidact.runtime import BudgetExceeded, controller_lock

PROTOCOL = "retrieval_job_v1"
ACTIVE_PROTOCOL = "retrieval_active_v1"


# 功能：计算与实时关键词索引一致的内容摘要，内容改变后旧候选向量不可继续使用。
def content_hash(kind, row):
    return hashlib.sha256(entity_text(kind, row).encode()).hexdigest()


# 功能：拒绝非1536维、非有限float32数值、布尔和零向量，避免无效向量被视为完整索引。
def valid_vector(vector):
    if hasattr(vector, "tolist"):
        vector = vector.tolist()
    if not isinstance(vector, (list, tuple)) or len(vector) != 1536:
        return False
    return all(
        isinstance(v, (int, float))
        and not isinstance(v, bool)
        and -float.fromhex("0x1.fffffep+127") <= v <= float.fromhex("0x1.fffffep+127")
        and math.isfinite(v)
        for v in vector
    ) and any(abs(v) >= 1e-38 for v in vector)


# 功能：校验协议与不可变提供方指纹，禁止用换过模型的进程盲续跑旧任务。
def check_identity(payload, embedding):
    if payload.get("protocol") != PROTOCOL:
        raise ValueError("未知索引任务协议")
    if embedding is None or payload["fingerprint"] != embedding.fingerprint:
        raise ValueError("嵌入指纹不一致，请恢复任务配置或创建新版本")


class JobStore:
    # 功能：绑定独立短事务会话，任务控制不占用学习会话，外部调用时不持行锁。
    def __init__(self, engine):
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)

    # 功能：串行修改专用任务JSONB并校验协议；不修改冻结研究/评估报告。
    @asynccontextmanager
    async def edit(self, job_id):
        async with self.sessions() as session, session.begin():
            row = await session.scalar(
                select(models.ResearchReport)
                .where(
                    models.ResearchReport.id == job_id,
                    models.ResearchReport.kind == "retrieval_job",
                )
                .with_for_update()
            )
            if row is None or row.payload.get("protocol") != PROTOCOL:
                raise ValueError("索引任务不存在或协议未知")
            payload = dict(row.payload)
            yield payload
            payload["updated_at"] = datetime.now(UTC).isoformat()
            row.payload = payload

    # 功能：读取任务合同，不调用提供方；不存在或未知协议明确拒绝。
    async def load(self, job_id):
        async with self.sessions() as session:
            row = await session.get(models.ResearchReport, job_id)
            if (
                row is None
                or row.kind != "retrieval_job"
                or row.payload.get("protocol") != PROTOCOL
            ):
                raise ValueError("索引任务不存在或协议未知")
            return dict(row.payload)

    # 功能：创建唯一候选版本报告，创建本身不生成向量、不写认知状态。
    async def create(self, fingerprint, max_attempts):
        job_id = uuid4()
        async with self.sessions() as session, session.begin():
            session.add(
                models.ResearchReport(
                    id=job_id,
                    kind="retrieval_job",
                    report_key=str(job_id),
                    payload={
                        "protocol": PROTOCOL,
                        "fingerprint": fingerprint,
                        "dimension": 1536,
                        "max_attempts": max_attempts,
                        "status": "queued",
                        "reason": None,
                        "created_at": datetime.now(UTC).isoformat(),
                    },
                )
            )
        return job_id

    # 功能：用数据库集合插入当前三类实体的缺失工作项，幂等补齐新增实体，不把全库ID塞进JSON。
    async def populate(self, job_id):
        async with self.sessions() as session, session.begin():
            for kind, model in ENTITIES.items():
                statement = (
                    insert(models.RetrievalVector)
                    .from_select(
                        ["id", "generation_id", "entity_kind", "entity_id"],
                        select(func.gen_random_uuid(), literal(job_id), literal(kind), model.id),
                    )
                    .on_conflict_do_nothing(constraint="uq_retrieval_vector_item")
                )
                await session.execute(statement)

    # 功能：按主键每页250项检查候选/当前实体，支持大库且不持有跨网络事务。
    async def items(self, job_id):
        for kind, model in ENTITIES.items():
            cursor = None
            while True:
                async with self.sessions() as session:
                    rows = (
                        await session.execute(
                            select(models.RetrievalVector, model)
                            .outerjoin(
                                model,
                                model.id == models.RetrievalVector.entity_id,
                            )
                            .where(
                                models.RetrievalVector.generation_id == job_id,
                                models.RetrievalVector.entity_kind == kind,
                                models.RetrievalVector.id > cursor if cursor else True,
                            )
                            .order_by(models.RetrievalVector.id)
                            .limit(250)
                        )
                    ).all()
                if not rows:
                    break
                for item, row in rows:
                    yield item, row
                cursor = rows[-1][0].id

    # 功能：更新单工作项并可同步已有关键词索引；完整候选写入和关键词摘要同事务提交。
    async def mark(self, item_id, *, row=None, **values):
        async with self.sessions() as session, session.begin():
            item = await session.get(models.RetrievalVector, item_id)
            if item is None:
                raise ValueError("索引工作项不存在")
            if row is not None:
                await RetrievalIndex(session).sync(item.entity_kind, row)
            for key, value in values.items():
                setattr(item, key, value)

    # 功能：只取下一个待处理工作项与当前原实体，完成项不会再次消耗嵌入预算。
    async def next_item(self, job_id):
        async with self.sessions() as session:
            item = await session.scalar(
                select(models.RetrievalVector)
                .where(
                    models.RetrievalVector.generation_id == job_id,
                    models.RetrievalVector.status == "pending",
                )
                .order_by(models.RetrievalVector.entity_kind, models.RetrievalVector.entity_id)
                .limit(1)
            )
            if item is None:
                return None
            return item, await session.get(ENTITIES[item.entity_kind], item.entity_id)

    # 功能：统计未纳入候选版本的当前新实体；已启用版本只读检测，不原地补写新工作项。
    async def missing_entities(self, job_id):
        total = 0
        async with self.sessions() as session:
            for kind, model in ENTITIES.items():
                total += await session.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(
                        ~select(models.RetrievalVector.id)
                        .where(
                            models.RetrievalVector.generation_id == job_id,
                            models.RetrievalVector.entity_kind == kind,
                            models.RetrievalVector.entity_id == model.id,
                        )
                        .exists()
                    )
                )
        return total

    # 功能：在独占工作者已取得控制器锁后恢复崩溃中项；保守保留次数，到上限不自动再发。
    async def recover(self, job_id, max_attempts):
        async with self.sessions() as session, session.begin():
            await session.execute(
                update(models.RetrievalVector)
                .where(
                    models.RetrievalVector.generation_id == job_id,
                    models.RetrievalVector.status == "running",
                )
                .values(status="failed", error_category="interrupted")
            )
            await session.execute(
                update(models.RetrievalVector)
                .where(
                    models.RetrievalVector.generation_id == job_id,
                    models.RetrievalVector.status == "failed",
                    models.RetrievalVector.error_category == "interrupted",
                    models.RetrievalVector.attempts < max_attempts,
                )
                .values(status="pending")
            )

    # 功能：用户明确重试时只重排未超限的失败项，不重复完成项、不强制清零调用次数。
    async def retry(self, job_id, max_attempts):
        async with self.sessions() as session, session.begin():
            await session.execute(
                update(models.RetrievalVector)
                .where(
                    models.RetrievalVector.generation_id == job_id,
                    models.RetrievalVector.status == "failed",
                    models.RetrievalVector.attempts < max_attempts,
                )
                .values(status="pending")
            )

    # 功能：聚合持久工作项进度/尝试数并有界返回失败摘要，不触发模型或修改任务。
    async def stats(self, job_id, limit=20):
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(
                        models.RetrievalVector.status,
                        func.count(),
                        func.sum(models.RetrievalVector.attempts),
                    )
                    .where(models.RetrievalVector.generation_id == job_id)
                    .group_by(models.RetrievalVector.status)
                )
            ).all()
            failures = (
                await session.scalars(
                    select(models.RetrievalVector)
                    .where(
                        models.RetrievalVector.generation_id == job_id,
                        models.RetrievalVector.status == "failed",
                    )
                    .order_by(models.RetrievalVector.id)
                    .limit(limit)
                )
            ).all()
        counts = {"pending": 0, "running": 0, "completed": 0, "failed": 0, "skipped": 0}
        counts.update({state: count for state, count, _ in rows})
        return {
            "counts": counts,
            "total": sum(counts.values()),
            "attempts": sum(attempts or 0 for _, _, attempts in rows),
            "failures": [
                {
                    "kind": i.entity_kind,
                    "entity_id": str(i.entity_id),
                    "attempts": i.attempts,
                    "category": i.error_category,
                }
                for i in failures
            ],
            "failures_truncated": counts["failed"] > limit,
        }

    # 功能：核对实际候选HNSW与关键词GIN索引存在；不以模型“完成”声明替代物理结构检查。
    async def indexes_present(self):
        async with self.sessions() as session:
            names = (
                await session.scalars(
                    text(
                        "SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace JOIN pg_am a ON a.oid = c.relam "
                        "WHERE n.nspname = current_schema() AND i.indisvalid AND i.indisready AND "
                        "((c.relname = 'ix_retrieval_vector_cosine' AND a.amname = 'hnsw') OR "
                        "(c.relname = 'ix_retrieval_lexemes' AND a.amname = 'gin'))"
                    )
                )
            ).all()
        return set(names) == {"ix_retrieval_vector_cosine", "ix_retrieval_lexemes"}

    # 功能：核对完整候选与关键词入口的摘要一致，缺行或旧关键词不能当完整索引启用。
    async def keyword_gaps(self, job_id):
        total = 0
        async with self.sessions() as session:
            for kind, model in ENTITIES.items():
                vector, entry = models.RetrievalVector, models.RetrievalEntry
                total += await session.scalar(
                    select(func.count())
                    .select_from(vector)
                    .join(
                        model,
                        model.id == vector.entity_id,
                    )
                    .outerjoin(
                        entry, (entry.entity_id == vector.entity_id) & (entry.entity_kind == kind)
                    )
                    .where(
                        vector.generation_id == job_id,
                        vector.entity_kind == kind,
                        vector.status == "completed",
                        (entry.id.is_(None))
                        | entry.content_hash.is_distinct_from(vector.content_hash),
                    )
                )
        return total

    # 功能：原子替换唯一活动版本指针或切回旧索引，保留所有候选/旧向量与原认知。
    async def activate(self, job_id, fingerprint):
        async with self.sessions() as session, session.begin():
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 718204614})
            row = await session.scalar(
                select(models.ResearchReport)
                .where(
                    models.ResearchReport.kind == "retrieval_active",
                    models.ResearchReport.report_key == "vectors",
                )
                .with_for_update()
            )
            payload = {
                "protocol": ACTIVE_PROTOCOL,
                "generation_id": str(job_id) if job_id else None,
                "fingerprint": fingerprint,
                "activated_at": datetime.now(UTC).isoformat(),
                "previous_generation_id": (row.payload or {}).get("generation_id") if row else None,
            }
            if row:
                row.payload = payload
            else:
                session.add(
                    models.ResearchReport(
                        kind="retrieval_active", report_key="vectors", payload=payload
                    )
                )
            if job_id:
                job = await session.scalar(
                    select(models.ResearchReport)
                    .where(
                        models.ResearchReport.id == job_id,
                    )
                    .with_for_update()
                )
                job.payload = {**job.payload, "sealed": True}

    # 功能：只读列出最新任务及唯一活动版本，列表有界且不返回完整实体正文或密钥。
    async def status(self, job_id=None, limit=20):
        if not 1 <= limit <= 50:
            raise ValueError("limit需为1—50")
        async with self.sessions() as session:
            active = await session.scalar(
                select(models.ResearchReport).where(
                    models.ResearchReport.kind == "retrieval_active",
                    models.ResearchReport.report_key == "vectors",
                )
            )
            query = select(models.ResearchReport).where(
                models.ResearchReport.kind == "retrieval_job"
            )
            if job_id:
                query = query.where(models.ResearchReport.id == job_id)
            rows = (
                await session.scalars(
                    query.order_by(
                        models.ResearchReport.created_at.desc(), models.ResearchReport.id
                    ).limit(limit + 1)
                )
            ).all()
        if job_id and not rows:
            raise ValueError("索引任务不存在")
        jobs = []
        for row in rows[:limit]:
            jobs.append({"id": str(row.id), **row.payload, **(await self.stats(row.id, limit))})
        return {
            "active": active.payload if active else None,
            "jobs": jobs,
            "truncated": len(rows) > limit,
        }


class RetrievalJobs:
    # 功能：绑定持久任务Store和可选受预算嵌入工具；只读/暂停无需可用模型配置。
    def __init__(self, engine, embedding=None, store=None):
        self.engine, self.embedding = engine, embedding
        self.store = store or JobStore(engine)

    # 功能：在控制器互斥下创建候选版本并补齐实体，不发实际嵌入请求。
    async def create(self, max_attempts=3):
        if not 1 <= max_attempts <= 5 or self.embedding is None:
            raise ValueError("需配置嵌入工具，max_attempts需为1—5")
        async with controller_lock(self.engine):
            job_id = await self.store.create(self.embedding.fingerprint, max_attempts)
            await self.store.populate(job_id)
        return {"job_id": str(job_id), "status": "queued"}

    # 功能：暂停可在工作者持锁时记录；只在途项收尾，恢复仅重排任务，不自动启动请求。
    async def control(self, job_id, action):
        if action not in {"pause", "resume", "retry"}:
            raise ValueError("未知任务控制操作")
        if (await self.store.load(job_id)).get("sealed"):
            raise ValueError("已启用版本不可修改，请创建新版本")
        if action == "retry":
            async with controller_lock(self.engine):
                payload = await self.store.load(job_id)
                if payload.get("sealed"):
                    raise ValueError("已启用版本不可修改，请创建新版本")
                await self.store.retry(job_id, payload["max_attempts"])
                async with self.store.edit(job_id) as state:
                    state.update(status="queued", reason="manual_retry")
        else:
            async with self.store.edit(job_id) as state:
                if state.get("sealed"):
                    raise ValueError("已启用版本不可修改，请创建新版本")
                if state["status"] != "ready":
                    state.update(
                        status="paused" if action == "pause" else "queued",
                        reason=f"manual_{action}",
                    )
        return await self.store.status(job_id)

    # 功能：逐项核对内容、维数/数值/指纹和物理索引；修复仅重排失效候选，不发模型或改认知。
    async def _verify(self, job_id, payload, repair=False):
        if repair:
            if payload.get("sealed"):
                raise ValueError("已启用版本不可修改，请创建新版本")
            await self.store.populate(job_id)
        complete, missing, stale, invalid, skipped = (
            0,
            await self.store.missing_entities(job_id),
            0,
            0,
            0,
        )
        async for item, row in self.store.items(job_id):
            if row is None:
                skipped += 1
                if repair:
                    await self.store.mark(
                        item.id, status="skipped", embedding=None, error_category="entity_deleted"
                    )
                continue
            changed = item.content_hash is not None and item.content_hash != content_hash(
                item.entity_kind, row
            )
            malformed = item.status == "completed" and (
                not valid_vector(item.embedding)
                or item.fingerprint != payload["fingerprint"]
                or item.content_hash is None
            )
            if changed:
                stale += 1
            elif malformed:
                invalid += 1
            elif item.status != "completed":
                missing += 1
            else:
                complete += 1
                if repair:
                    await self.store.mark(item.id, row=row)
            if repair and (changed or malformed or item.status == "skipped"):
                await self.store.mark(
                    item.id,
                    status="pending",
                    content_hash=None,
                    embedding=None,
                    fingerprint=None,
                    attempts=0,
                    error_category="content_changed" if changed else "invalid_index",
                )
        indexes = await self.store.indexes_present()
        keyword_gaps = await self.store.keyword_gaps(job_id)
        result = {
            "complete": complete,
            "missing": missing,
            "stale": stale,
            "invalid": invalid,
            "skipped_deleted": skipped,
            "indexes_present": indexes,
            "keyword_gaps": keyword_gaps,
            "valid": complete > 0 and not (missing or stale or invalid or keyword_gaps) and indexes,
        }
        async with self.store.edit(job_id) as state:
            state["integrity"] = {**result, "checked_at": datetime.now(UTC).isoformat()}
        return result

    # 功能：用户显式校验候选完整性，无外部调用；检查覆盖当前全库，不仅创建时快照。
    async def verify(self, job_id):
        async with controller_lock(self.engine):
            return await self._verify(job_id, await self.store.load(job_id))

    # 功能：配置一致且完整性通过后原子启用；旧版本保持可回退，未完成/空版本禁止切换。
    async def activate(self, job_id):
        async with controller_lock(self.engine):
            payload = await self.store.load(job_id)
            check_identity(payload, self.embedding)
            result = await self._verify(job_id, payload)
            if not result["valid"]:
                raise ValueError("候选索引不完整或已过期，请运行重建/重试后再启用")
            await self.store.activate(job_id, payload["fingerprint"])
        return {"active_generation_id": str(job_id), "integrity": result}

    # 功能：显式切回旧检索入口，不删除新版本或认知；需用户恢复旧提供方配置才有匹配向量。
    async def legacy(self):
        async with controller_lock(self.engine):
            await self.store.activate(None, None)
        return {"status": "legacy", "note": "向量仍按当前配置指纹过滤，不混用旧模型"}

    # 功能：每个有界批次独占控制器，逐项持久化且遵守暂停/预算/取消，已成功项不重新计费。
    async def run(self, job_id, max_items=200):
        if not 1 <= max_items <= 10000:
            raise ValueError("max_items需为1—10000")
        async with controller_lock(self.engine):
            payload = await self.store.load(job_id)
            check_identity(payload, self.embedding)
            if payload.get("sealed"):
                raise ValueError("已启用版本不可重建，请创建新版本")
            if payload["status"] == "paused":
                return await self.store.status(job_id)
            await self.store.recover(job_id, payload["max_attempts"])
            await self._verify(job_id, payload, repair=True)
            async with self.store.edit(job_id) as state:
                if state["status"] == "paused":
                    return await self.store.status(job_id)
                state.update(status="running", reason=None)
            processed = 0
            try:
                while processed < max_items:
                    if (await self.store.load(job_id))["status"] == "paused":
                        break
                    selected = await self.store.next_item(job_id)
                    if selected is None:
                        break
                    item, row = selected
                    if row is None:
                        await self.store.mark(
                            item.id, status="skipped", error_category="entity_deleted"
                        )
                        processed += 1
                        continue
                    if item.attempts >= payload["max_attempts"]:
                        await self.store.mark(
                            item.id, status="failed", error_category="attempt_limit"
                        )
                        processed += 1
                        continue
                    await self.store.mark(
                        item.id,
                        status="running",
                        attempts=item.attempts + 1,
                        content_hash=content_hash(item.entity_kind, row),
                    )
                    try:
                        if hasattr(self.embedding, "bind"):
                            self.embedding.bind(
                                retrieval_job_id=str(job_id),
                                entity_kind=item.entity_kind,
                                entity_id=str(item.entity_id),
                            )
                        vector = await self.embedding.embed(entity_text(item.entity_kind, row))
                        if not valid_vector(vector):
                            raise ValueError("嵌入向量无效")
                        await self.store.mark(
                            item.id,
                            row=row,
                            status="completed",
                            embedding=vector,
                            fingerprint=payload["fingerprint"],
                            error_category=None,
                        )
                    except BudgetExceeded:
                        # 内部已发出物理尝试后才遇预算拒绝，不能退还整个逻辑次数。
                        attempts = item.attempts + int(
                            getattr(self.embedding, "attempts_started", 0) > 0
                        )
                        await self.store.mark(
                            item.id,
                            status="pending",
                            attempts=attempts,
                            error_category="budget",
                        )
                        async with self.store.edit(job_id) as state:
                            state.update(status="paused", reason="budget_exhausted")
                        break
                    except asyncio.CancelledError:
                        await self.store.mark(
                            item.id, status="failed", error_category="interrupted"
                        )
                        raise
                    except Exception as exc:  # noqa: BLE001 - 单项故障保留进度/失败类别，继续有限批次。
                        await self.store.mark(
                            item.id, status="failed", error_category=classify_failure(exc).category
                        )
                    processed += 1
                stats = await self.store.stats(job_id)
                async with self.store.edit(job_id) as state:
                    if state["status"] != "paused":
                        state.update(
                            status="paused"
                            if stats["counts"]["pending"]
                            else ("failed" if stats["counts"]["failed"] else "ready"),
                            reason="batch_limit" if stats["counts"]["pending"] else None,
                        )
                if not stats["counts"]["pending"] and not stats["counts"]["failed"]:
                    integrity = await self._verify(job_id, payload)
                    if not integrity["valid"]:
                        async with self.store.edit(job_id) as state:
                            if state["status"] != "paused":
                                state.update(status="failed", reason="integrity_failed")
            except asyncio.CancelledError:
                async with self.store.edit(job_id) as state:
                    state.update(status="paused", reason="interrupted")
                raise
            except Exception:
                async with self.store.edit(job_id) as state:
                    state.update(status="failed", reason="worker_failed")
                raise
        return await self.store.status(job_id)


# 功能：共用CLI/本地网页的有限操作入口，只有创建/运行/启用需嵌入配置，实际付费只在run发生。
async def job_action(engine, action, job_id=None, *, max_items=200, max_attempts=3, limit=20):
    embedding = None
    if action in {"create", "run", "activate"}:
        from autodidact.embedding_runtime import RecordedEmbedding
        from autodidact.embeddings import DisabledEmbeddingProvider, build_embedding_provider

        inner = build_embedding_provider()
        if isinstance(inner, DisabledEmbeddingProvider):
            raise ValueError("请先启用并配置1536维嵌入服务，当前为disabled")
        embedding = RecordedEmbedding(inner, engine)
    jobs = RetrievalJobs(engine, embedding)
    if action == "create":
        return await jobs.create(max_attempts)
    if action == "status":
        return await jobs.store.status(job_id, limit)
    if action == "legacy":
        return await jobs.legacy()
    if job_id is None:
        raise ValueError("此操作需要索引任务UUID")
    if action in {"pause", "resume", "retry"}:
        return await jobs.control(job_id, action)
    if action == "run":
        return await jobs.run(job_id, max_items)
    if action == "verify":
        return await jobs.verify(job_id)
    if action == "activate":
        return await jobs.activate(job_id)
    raise ValueError("未知索引任务操作")
