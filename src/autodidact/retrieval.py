# 文件职责：维护 Goal/Claim/Belief 派生索引并融合关键词、向量和有界降级召回。
"""Belief / Claim / Goal 混合召回。索引只用于定位，不改变认识论状态。"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import case, exists, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert

from autodidact import models
from autodidact.runtime import BudgetExceeded

log = logging.getLogger(__name__)
ENTITIES = {"belief": models.Belief, "claim": models.Claim, "goal": models.Goal}


# 功能：提取英文/数字词和中文二三字片段，生成关键词检索词而非语言理解结论。
def tokens(value):
    result = set(re.findall(r"[a-z0-9_]+", value.casefold()))
    for run in re.findall(r"[\u3400-\u9fff]+", value):
        if len(run) == 1:
            result.add(run)
        for size in (2, 3):
            result.update(run[i : i + size] for i in range(len(run) - size + 1))
    return sorted(result)


# 功能：按实体类型组织目标标题/描述或主张/信念正文/主题，作为索引文本。
def entity_text(kind, row):
    if kind == "goal":
        return f"{row.title}\n{row.description or ''}"
    return f"{row.statement}\n{row.topic or ''}\n{getattr(row, 'reasoning', None) or getattr(row, 'explanation', None) or ''}"


class RetrievalIndex:
    # 功能：绑定数据库会话，用于可重建的检索索引写入。
    def __init__(self, session):
        self.s = session

    # 功能：同步实体文本摘要与关键词，内容变化时使旧向量失效；不改变实体事实状态。
    async def sync(self, kind, row):
        value = entity_text(kind, row)
        digest = hashlib.sha256(value.encode()).hexdigest()
        stmt = insert(models.RetrievalEntry).values(
            entity_kind=kind,
            entity_id=row.id,
            content_hash=digest,
            lexemes=" ".join(tokens(value)),
            updated_at=datetime.now(UTC),
        )
        await self.s.execute(
            stmt.on_conflict_do_update(
                constraint="uq_retrieval_entity",
                set_={
                    "content_hash": digest,
                    "lexemes": stmt.excluded.lexemes,
                    "updated_at": stmt.excluded.updated_at,
                    "embedding": case(
                        (
                            models.RetrievalEntry.content_hash == digest,
                            models.RetrievalEntry.embedding,
                        ),
                        else_=None,
                    ),
                    "fingerprint": case(
                        (
                            models.RetrievalEntry.content_hash == digest,
                            models.RetrievalEntry.fingerprint,
                        ),
                        else_=None,
                    ),
                },
            )
        )

    # 功能：为实体计算并保存当前指纹向量，失败保留关键词索引以便降级。
    async def embed_entity(self, kind, row, embedding):
        async with self.s.begin_nested():
            await self.sync(kind, row)
        await self.s.commit()
        async with self.s.begin_nested():
            entry = await self.s.scalar(
                select(models.RetrievalEntry).where(
                    models.RetrievalEntry.entity_kind == kind,
                    models.RetrievalEntry.entity_id == row.id,
                )
            )
        if entry.embedding is not None and entry.fingerprint == embedding.fingerprint:
            return
        vector = await embedding.embed(entity_text(kind, row))
        async with self.s.begin_nested():
            entry.embedding, entry.fingerprint = vector, embedding.fingerprint
            await self.s.flush()
        await self.s.commit()

    # 功能：分批回填三类实体的缺失/过期文本与可选向量，返回重建统计。
    async def rebuild(self, embedding=None, limit=200):
        updated, embedded, failures = 0, 0, 0
        for kind, model in ENTITIES.items():
            cursor = None
            while updated < limit:
                rows = (
                    await self.s.scalars(
                        select(model)
                        .where(model.id > cursor if cursor else True)
                        .order_by(model.id)
                        .limit(100)
                    )
                ).all()
                if not rows:
                    break
                for row in rows:
                    cursor = row.id
                    digest = hashlib.sha256(entity_text(kind, row).encode()).hexdigest()
                    existing = await self.s.scalar(
                        select(models.RetrievalEntry).where(
                            models.RetrievalEntry.entity_kind == kind,
                            models.RetrievalEntry.entity_id == row.id,
                        )
                    )
                    needs_text = not existing or existing.content_hash != digest
                    needs_vector = embedding and (
                        needs_text
                        or existing.embedding is None
                        or existing.fingerprint != embedding.fingerprint
                    )
                    if not needs_text and not needs_vector:
                        continue
                    await self.sync(kind, row)
                    await self.s.commit()
                    updated += 1
                    if needs_vector:
                        try:
                            vector = await embedding.embed(entity_text(kind, row))
                            entry = await self.s.scalar(
                                select(models.RetrievalEntry).where(
                                    models.RetrievalEntry.entity_kind == kind,
                                    models.RetrievalEntry.entity_id == row.id,
                                )
                            )
                            async with self.s.begin_nested():
                                entry.embedding, entry.fingerprint = vector, embedding.fingerprint
                                await self.s.flush()
                            await self.s.commit()
                            embedded += 1
                        except BudgetExceeded as exc:
                            return {
                                "updated": updated,
                                "embedded": embedded,
                                "status": "budget_exhausted",
                                "reason": str(exc),
                            }
                        except Exception as exc:  # noqa: BLE001 - keyword channel remains usable.
                            failures += 1
                            log.warning("索引嵌入降级：%s", type(exc).__name__)
                    if updated >= limit:
                        break
        return {
            "updated": updated,
            "embedded": embedded,
            "failures": failures,
            "status": "batch_complete",
            "limit": limit,
        }


@dataclass
class RetrievalHit:
    kind: str
    row: object
    score: float
    ranks: dict

    # 功能：展示召回实体、排序通道及主张范围；范围空值为未知，相关性不代表事实可信度。
    def as_dict(self):
        return {
            "kind": self.kind,
            "id": str(self.row.id),
            "score": self.score,
            "ranks": self.ranks,
            "text": entity_text(self.kind, self.row),
            "status": getattr(self.row, "status", "candidate_claim"),
            "confidence": getattr(self.row, "confidence", None),
            "claim_scope": getattr(self.row, "scope", None)
            or (getattr(self.row, "metadata_json", None) or {}).get("claim_scope", {}),
        }


class HybridRetriever:
    # 功能：绑定仓库和可选嵌入提供方，复用当前数据库会话。
    def __init__(self, repo, embedding=None):
        self.repo, self.s, self.embedding = repo, repo.s, embedding

    # 功能：构造实体过滤条件；接纳记忆仅含无开放争议的 supported/verified 信念。
    def eligible(self, kind, accepted_only):
        if kind != "belief":
            return True
        belief = models.Belief
        if not accepted_only:
            return belief.status != "retracted"
        disputed = exists(
            select(models.Dispute.id).where(
                models.Dispute.belief_id == belief.id,
                models.Dispute.status.in_(["open", "investigating", "unresolved"]),
            )
        )
        return (belief.status.in_(["supported", "verified"])) & ~disputed

    # 功能：结合关键词和同指纹向量结果进行 RRF 排序；向量失败降级，相关性分数不代表事实可信度。
    async def retrieve(
        self, query, kinds=("belief", "claim", "goal"), limit=20, accepted_only=False
    ):
        if not query.strip() or not 1 <= limit <= 100:
            return []
        if accepted_only:
            kinds = ("belief",)
        if any(k not in ENTITIES for k in kinds):
            raise ValueError("检索类型只能为 belief / claim / goal")
        vector, fingerprint = None, None
        if self.embedding:
            try:
                vector = await self.embedding.embed(query)
                fingerprint = self.embedding.fingerprint
            except Exception as exc:  # noqa: BLE001 - preserve keyword recall on provider/budget failures.
                log.debug("混合召回使用关键词降级：%s", type(exc).__name__)
        terms = tokens(query)[:128]
        candidates = {}
        pool = min(200, max(40, limit * 4))

        # 功能：合并同实体的各检索通道名次，保留每通道最优排名。
        def collect(kind, channel, rows):
            for rank, row in enumerate(rows, 1):
                key = (kind, row.id)
                hit = candidates.setdefault(key, RetrievalHit(kind, row, 0.0, {}))
                hit.ranks[channel] = min(rank, hit.ranks.get(channel, rank))

        for kind in kinds:
            model = ENTITIES[kind]
            eligibility = self.eligible(kind, accepted_only)
            entry = models.RetrievalEntry
            joined = (
                select(model)
                .join(entry, (entry.entity_id == model.id) & (entry.entity_kind == kind))
                .where(eligibility)
            )
            if terms:
                lex = func.to_tsvector(text("'simple'::regconfig"), entry.lexemes)
                tsquery = func.to_tsquery(text("'simple'::regconfig"), " | ".join(terms))
                rows = (
                    await self.s.scalars(
                        joined.where(lex.op("@@")(tsquery))
                        .order_by(func.ts_rank_cd(lex, tsquery).desc(), model.id)
                        .limit(pool)
                    )
                ).all()
                collect(kind, "keyword", rows)
                # Legacy records are searchable even before index backfill, with bounded results.
                field = model.title if kind == "goal" else model.statement
                topic = model.description if kind == "goal" else model.topic
                predicates = []
                for term in terms[:32]:
                    pattern = "%" + term.replace("_", "\\_") + "%"
                    predicates.append(
                        or_(field.ilike(pattern, escape="\\"), topic.ilike(pattern, escape="\\"))
                    )
                match_count = sum(case((condition, 1), else_=0) for condition in predicates)
                rows = (
                    await self.s.scalars(
                        select(model)
                        .where(eligibility, or_(*predicates))
                        .order_by(match_count.desc(), model.created_at.desc(), model.id)
                        .limit(pool)
                    )
                ).all()
                rows = sorted(
                    rows,
                    key=lambda r: (-len(set(terms) & set(tokens(entity_text(kind, r)))), str(r.id)),
                )
                collect(kind, "keyword", rows)
            if vector is not None:
                try:
                    async with self.s.begin_nested():
                        rows = (
                            await self.s.scalars(
                                joined.where(
                                    entry.embedding.is_not(None),
                                    entry.fingerprint == fingerprint,
                                )
                                .order_by(entry.embedding.cosine_distance(vector), model.id)
                                .limit(pool)
                            )
                        ).all()
                    collect(kind, "semantic", rows)
                    if kind == "belief":
                        async with self.s.begin_nested():
                            rows = (
                                await self.s.scalars(
                                    select(model)
                                    .where(
                                        eligibility,
                                        model.embedding.is_not(None),
                                        model.metadata_json["embedding_fingerprint"].astext
                                        == fingerprint,
                                    )
                                    .order_by(model.embedding.cosine_distance(vector), model.id)
                                    .limit(pool)
                                )
                            ).all()
                        collect(kind, "semantic", rows)
                except Exception as exc:  # noqa: BLE001 - savepoint protects later repository writes.
                    log.warning("向量查询降级：%s", type(exc).__name__)
            # Recency is a fallback only; unrelated recent memories cannot crowd out actual hits.
            if not any(h.kind == kind for h in candidates.values()):
                rows = (
                    await self.s.scalars(
                        select(model)
                        .where(eligibility)
                        .order_by(model.created_at.desc(), model.id)
                        .limit(limit)
                    )
                ).all()
                collect(kind, "recency", rows)
        for hit in candidates.values():
            hit.score = sum(
                (0.15 if channel == "recency" else 1.0) / (60 + rank)
                for channel, rank in hit.ranks.items()
            )
            if query.casefold().strip() in entity_text(hit.kind, hit.row).casefold():
                hit.score += 0.01
        return sorted(candidates.values(), key=lambda h: (-h.score, h.kind, str(h.row.id)))[:limit]
