# 文件职责：封装认知数据库读写及事务：目标、来源、主张锚点、信念、争议、历史和评估。
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from autodidact import models
from autodidact.config import agent_config
from autodidact.enums import BeliefStatus, DisputeStatus, GoalStatus
from autodidact.knowledge.claim_support import ClaimEvidenceVerification
from autodidact.knowledge.lineage import SourceLineage
from autodidact.knowledge.resolution_claim import conditional_claim
from autodidact.knowledge.sources import (
    EvidenceSource,
    independent_source_representatives,
    normalize_url,
    publisher_key,
)
from autodidact.learning_state import LearningState
from autodidact.normalization import claim_statement_key, normalize_text_key, stable_key
from autodidact.retrieval import RetrievalIndex
from autodidact.schemas import CandidateGoal, ClaimDraft, SourceDocument

_ACTIVE_GOAL_STATUSES = (
    GoalStatus.DISCOVERED,
    GoalStatus.PLANNED,
    GoalStatus.RESEARCHING,
    GoalStatus.SYNTHESIZING,
    GoalStatus.TESTING,
    GoalStatus.REFLECTING,
    GoalStatus.FAILED,
)
_OPEN_DISPUTE_STATUSES = (
    DisputeStatus.OPEN,
    DisputeStatus.INVESTIGATING,
    DisputeStatus.UNRESOLVED,
)


# 功能：仅识别指定约束的 PostgreSQL 唯一键竞争，其他完整性错误不得当作成功去重。
def _expected_unique_violation(exc: IntegrityError, constraint: str) -> bool:
    """Only a known unique race is safe to treat as an existing row."""
    original = getattr(exc, "orig", None)
    # SQLAlchemy's asyncpg adapter wraps the server error and leaves the
    # constraint name on the underlying asyncpg exception.
    server_error = getattr(original, "__cause__", None)
    return (
        getattr(original, "sqlstate", None) == "23505"
        and (
            getattr(original, "constraint_name", None)
            or getattr(server_error, "constraint_name", None)
        )
        == constraint
    )


class Repository(LearningState):
    # 功能：绑定异步数据库会话、来源血缘和派生检索索引；运行状态方法由 LearningState 继承。
    def __init__(self, session: AsyncSession):
        self.s = session
        self.lineage = SourceLineage(session)
        self.index = RetrievalIndex(session)

    # 功能：按智能体名称读取身份，缺失时创建使命与当前焦点并提交。
    async def get_or_create_agent(self, name: str, mission: str, focus: str) -> models.Agent:
        q = await self.s.execute(select(models.Agent).where(models.Agent.name == name))
        agent = q.scalar_one_or_none()
        if agent:
            return agent
        agent = models.Agent(name=name, mission=mission, current_focus=focus)
        self.s.add(agent)
        await self.s.commit()
        await self.s.refresh(agent)
        return agent

    # 功能：复用去重入库方法，返回已有或新建目标，不向调用者暴露创建标记。
    async def add_goal(
        self, g: CandidateGoal, score: float, parent_goal_id: UUID | None = None
    ) -> models.Goal:
        goal, _ = await self.add_goal_if_absent(g, score, parent_goal_id)
        return goal

    # 功能：规范化标题去重活动目标，新增时保存父目标与评分并同步关键词索引，返回目标及是否新建。
    async def add_goal_if_absent(
        self,
        g: CandidateGoal,
        score: float,
        parent_goal_id: UUID | None = None,
    ) -> tuple[models.Goal, bool]:
        q = await self.s.execute(
            select(models.Goal).where(models.Goal.status.in_(_ACTIVE_GOAL_STATUSES))
        )
        goal_key = normalize_text_key(g.title)
        for existing in q.scalars():
            if normalize_text_key(existing.title) == goal_key:
                return existing, False

        goal = models.Goal(
            parent_goal_id=parent_goal_id,
            title=g.title,
            description=g.description,
            status=GoalStatus.DISCOVERED,
            source=g.source,
            importance=g.importance,
            uncertainty=g.uncertainty,
            novelty=g.novelty,
            utility=g.utility,
            prerequisite_score=g.prerequisite_score,
            estimated_cost=g.estimated_cost,
            priority_score=score,
        )
        self.s.add(goal)
        await self.s.flush()
        await self.index.sync("goal", goal)
        await self.s.commit()
        await self.s.refresh(goal)
        return goal, True

    # 功能：按优先级和创建时间选择未开始或未达重试上限的失败目标。
    async def next_goal(self, max_retry: int = 3) -> models.Goal | None:
        q = await self.s.execute(
            select(models.Goal)
            .where(
                (models.Goal.status == GoalStatus.DISCOVERED)
                | (
                    (models.Goal.status == GoalStatus.FAILED)
                    & (models.Goal.retry_count < max_retry)
                )
            )
            .order_by(desc(models.Goal.priority_score), models.Goal.created_at)
            .limit(1)
        )
        return q.scalar_one_or_none()

    # 功能：更新目标阶段、置信度和重试次数；终态写入会话结果以防续跑重复累计失败。
    async def update_goal_status(
        self,
        goal: models.Goal,
        status: str,
        confidence_after: float | None = None,
        *,
        max_retry: int | None = None,
        attempt: models.LearningSession | None = None,
    ) -> None:
        terminal = status in (GoalStatus.PASSED, GoalStatus.FAILED, GoalStatus.BLOCKED)
        if attempt and (attempt.result or {}).get("goal_outcome_finalized"):
            recorded = (attempt.result or {}).get("goal_outcome", {})
            if terminal and recorded:
                goal.status = recorded["status"]
                goal.retry_count = recorded["retry_count"]
                goal.confidence_after = recorded["confidence_after"]
                goal.completed_at = (
                    datetime.fromisoformat(recorded["completed_at"])
                    if recorded["completed_at"]
                    else None
                )
                await self.s.commit()
            return
        if goal.started_at is None:
            goal.started_at = datetime.now(UTC)
        if status == GoalStatus.FAILED and goal.status != GoalStatus.FAILED:
            goal.retry_count += 1
            if max_retry is not None and goal.retry_count >= max_retry:
                status = GoalStatus.BLOCKED
        goal.status = status
        if status in (GoalStatus.PASSED, GoalStatus.BLOCKED):
            goal.completed_at = datetime.now(UTC)
        if confidence_after is not None:
            goal.confidence_after = confidence_after
        if attempt and terminal:
            attempt.result = {
                **(attempt.result or {}),
                "goal_outcome_finalized": True,
                "goal_outcome": {
                    "status": status,
                    "retry_count": goal.retry_count,
                    "confidence_after": goal.confidence_after,
                    "completed_at": goal.completed_at.isoformat() if goal.completed_at else None,
                },
            }
        await self.s.commit()

    # 功能：创建并提交一个学习尝试，尽早保存计划以便后续阶段恢复。
    async def create_learning_session(self, goal_id: UUID, plan: dict) -> models.LearningSession:
        item = models.LearningSession(goal_id=goal_id, plan=plan)
        self.s.add(item)
        await self.s.commit()
        await self.s.refresh(item)
        return item

    # 功能：合并会话结果，保存反思、成功标记和完成时间；不删除历史工作项。
    async def finish_learning_session(
        self, item: models.LearningSession, result: dict, reflection: dict, success: bool
    ) -> None:
        item.result = {**(item.result or {}), **result}
        item.reflection = reflection
        item.success = success
        item.completed_at = datetime.now(UTC)
        await self.s.commit()

    # 功能：按内容 hash 复用来源，补缺失质量/血缘 metadata 并记录依赖边，保留原文。
    async def upsert_source(self, doc: SourceDocument, content_hash: str) -> models.Source:
        q = await self.s.execute(
            select(models.Source).where(models.Source.content_hash == content_hash)
        )
        found = q.scalar_one_or_none()
        if found:
            changed = False
            for field, value in (
                ("normalized_url", doc.normalized_url or normalize_url(doc.url)),
                ("publisher_key", doc.publisher_key or publisher_key(doc.url)),
                ("quality_class", doc.quality_class),
                ("quality_reason", doc.quality_reason),
            ):
                if not getattr(found, field, None) and value:
                    setattr(found, field, value)
                    changed = True
            if doc.metadata:
                changed = True
            if doc.lineage_key and not (found.metadata_json or {}).get("lineage_key"):
                found.metadata_json = {
                    **(found.metadata_json or {}),
                    **doc.metadata,
                    "lineage_key": doc.lineage_key,
                }
                changed = True
            await self.lineage.record(found, doc)
            if changed:
                found.metadata_json = {
                    **(found.metadata_json or {}),
                    **doc.metadata,
                    "lineage_key": doc.lineage_key
                    or (found.metadata_json or {}).get("lineage_key", ""),
                }
            await self.s.commit()
            return found
        item = models.Source(
            url=doc.url,
            normalized_url=doc.normalized_url or normalize_url(doc.url),
            publisher_key=doc.publisher_key or publisher_key(doc.url),
            title=doc.title,
            source_type=doc.source_type,
            quality_class=doc.quality_class,
            quality_reason=doc.quality_reason,
            evidence_level=doc.evidence_level,
            credibility_score=doc.credibility_score,
            content_hash=content_hash,
            extracted_text=doc.text,
            metadata_json={**doc.metadata, "lineage_key": doc.lineage_key},
        )
        self.s.add(item)
        await self.s.flush()
        await self.lineage.record(item, doc)
        await self.s.commit()
        await self.s.refresh(item)
        return item

    # 功能：按会话和规范主张去重候选 Claim；唯一约束竞争只在已知约束上恢复，不自动创建信念。
    async def add_claim(
        self, draft: ClaimDraft, learning_session_id: UUID, source_ids: list[str]
    ) -> models.Claim:
        q = await self.s.execute(
            select(models.Claim).where(models.Claim.learning_session_id == learning_session_id)
        )
        normalized_statement = normalize_text_key(draft.statement)
        for existing in q.scalars():
            if normalize_text_key(existing.statement) == normalized_statement:
                return existing

        statement_key = claim_statement_key(draft.statement)
        item = models.Claim(
            learning_session_id=learning_session_id,
            statement=draft.statement,
            topic=draft.topic,
            statement_key=statement_key,
            reasoning=draft.reasoning,
            confidence=draft.confidence,
            source_ids=list(dict.fromkeys(source_ids)),
        )
        self.s.add(item)
        try:
            await self.s.flush()
            await self.index.sync("claim", item)
            await self.s.commit()
        except IntegrityError as exc:
            await self.s.rollback()
            if not _expected_unique_violation(exc, "uq_claims_session_statement_key_current"):
                raise
            q = await self.s.execute(
                select(models.Claim).where(
                    models.Claim.learning_session_id == learning_session_id,
                    models.Claim.statement_key == statement_key,
                )
            )
            existing = q.scalar_one_or_none()
            if existing:
                return existing
            raise
        await self.s.refresh(item)
        return item

    # 功能：持久化引文锚点及语义判定，排除同源混合矛盾记录，避免旧锚点冒充已支持证据。
    async def record_claim_evidence(
        self, claim: models.Claim, records: list[ClaimEvidenceVerification]
    ) -> None:
        for record in records:
            if record.source_id is None:
                continue
            source_id = UUID(record.source_id)
            q = await self.s.execute(
                select(models.ClaimEvidence).where(
                    models.ClaimEvidence.claim_id == claim.id,
                    models.ClaimEvidence.source_id == source_id,
                    models.ClaimEvidence.excerpt_hash == record.excerpt_hash,
                )
            )
            existing = q.scalar_one_or_none()
            if existing:
                if existing.status != record.status or existing.reason != record.reason:
                    existing.status = record.status
                    existing.reason = record.reason
                    await self.s.commit()
                continue
            self.s.add(
                models.ClaimEvidence(
                    claim_id=claim.id,
                    source_id=source_id,
                    excerpt=record.excerpt,
                    excerpt_hash=record.excerpt_hash,
                    status=record.status,
                    reason=record.reason,
                )
            )
            try:
                await self.s.commit()
            except IntegrityError as exc:
                await self.s.rollback()
                if not _expected_unique_violation(exc, "uq_claim_evidence_anchor"):
                    raise
        q = await self.s.execute(
            select(models.ClaimEvidence).where(models.ClaimEvidence.claim_id == claim.id)
        )
        statuses: dict[str, set[str]] = {}
        for evidence in q.scalars():
            statuses.setdefault(str(evidence.source_id), set()).add(evidence.status)
        qualified = [source_id for source_id, values in statuses.items() if values == {"supported"}]
        if claim.source_ids != qualified:
            claim.source_ids = qualified
            await self.s.commit()

    # 功能：读取最新非撤回信念，作为无需向量的兼容记忆入口。
    async def recent_beliefs(self, limit: int = 30) -> list[models.Belief]:
        q = await self.s.execute(
            select(models.Belief)
            .where(models.Belief.status != BeliefStatus.RETRACTED)
            .order_by(desc(models.Belief.updated_at))
            .limit(limit)
        )
        return list(q.scalars())

    # 功能：按 pgvector 余弦距离召回有向量的非撤回信念，供兼容调用及检索使用。
    async def semantic_beliefs(
        self, embedding: list[float], limit: int = 30, *, fingerprint: str | None = None
    ) -> list[models.Belief]:
        distance = models.Belief.embedding.cosine_distance(embedding)
        q = await self.s.execute(
            select(models.Belief)
            .where(models.Belief.status != BeliefStatus.RETRACTED)
            .where(models.Belief.embedding.is_not(None))
            .where(
                models.Belief.metadata_json["embedding_fingerprint"].astext == fingerprint
                if fingerprint
                else True
            )
            .order_by(distance)
            .limit(limit)
        )
        return list(q.scalars())

    # 功能：保存信念向量与提供方指纹，并更新派生索引以避免跨模型向量混用。
    async def set_belief_embedding(
        self, belief: models.Belief, embedding: list[float], *, fingerprint: str | None = None
    ) -> None:
        belief.embedding = embedding
        if fingerprint:
            belief.metadata_json = {
                **(belief.metadata_json or {}),
                "embedding_fingerprint": fingerprint,
            }
        await self.s.commit()

    # 功能：按主题文本匹配读取非撤回信念，用于旧式主题召回。
    async def beliefs_for_topic(self, topic: str, limit: int = 10) -> list[models.Belief]:
        q = await self.s.execute(
            select(models.Belief)
            .where(models.Belief.topic.ilike(f"%{topic}%"))
            .where(models.Belief.status != BeliefStatus.RETRACTED)
            .order_by(desc(models.Belief.confidence))
            .limit(limit)
        )
        return list(q.scalars())

    # 功能：由已批准的 Claim 创建或更新信念，保存分数、来源数和历史，并防止重复重放变更。
    async def create_belief_from_claim(
        self,
        claim: models.Claim,
        test_score: float,
        verified: bool,
        *,
        evidence_score: float | None = None,
        independent_source_count: int | None = None,
    ) -> models.Belief:
        q = await self.s.execute(
            select(models.Belief).where(models.Belief.status != BeliefStatus.RETRACTED)
        )
        statement_key = normalize_text_key(claim.statement)
        for existing in q.scalars():
            if normalize_text_key(existing.statement) == statement_key:
                processed = list((existing.metadata_json or {}).get("promoted_claim_ids", []))
                if str(claim.id) in processed:
                    return existing
                existing.metadata_json = {
                    **(existing.metadata_json or {}),
                    "promoted_claim_ids": [*processed, str(claim.id)],
                }
                self.s.add(
                    models.BeliefHistory(
                        belief_id=existing.id,
                        action="reobserved",
                        previous_state={
                            "status": existing.status,
                            "confidence": existing.confidence,
                        },
                        new_state={"status": existing.status, "confidence": existing.confidence},
                        reason="等价主张再次通过评估；保留原信念状态，新增证据单独记录",
                    )
                )
                await self.s.commit()
                return existing

        status = BeliefStatus.VERIFIED if verified else BeliefStatus.PROVISIONAL
        belief = models.Belief(
            topic=claim.topic or "unknown",
            statement=claim.statement,
            explanation=claim.reasoning,
            status=status,
            confidence=min(0.99, (claim.confidence + test_score) / 2),
            evidence_score=claim.confidence if evidence_score is None else evidence_score,
            test_score=test_score,
            stability_score=0.3 if verified else 0.1,
            metadata_json={"promoted_claim_ids": [str(claim.id)]},
            source_count=(
                len(claim.source_ids)
                if independent_source_count is None
                else independent_source_count
            ),
        )
        self.s.add(belief)
        await self.s.flush()
        await self.index.sync("belief", belief)
        hist = models.BeliefHistory(
            belief_id=belief.id,
            action="created",
            previous_state={},
            new_state={"status": belief.status, "confidence": belief.confidence},
            reason="claim promoted after evaluation",
        )
        self.s.add(hist)
        await self.s.commit()
        return belief

    # 功能：按证据键去重保存信念与来源的关系、摘录、等级和强度。
    async def add_evidence(
        self, belief_id: UUID, source_id: UUID, level: int, strength: float, excerpt: str = ""
    ) -> None:
        dedup_key = stable_key("evidence", belief_id, source_id, "web_source", "support")
        q = await self.s.execute(
            select(models.Evidence).where(models.Evidence.dedup_key == dedup_key)
        )
        if q.scalar_one_or_none():
            return
        # Rows created before 0003 keep a NULL dedup_key; preserve their idempotency too.
        q = await self.s.execute(
            select(models.Evidence).where(
                models.Evidence.belief_id == belief_id,
                models.Evidence.source_id == source_id,
                models.Evidence.kind == "web_source",
                models.Evidence.stance == "support",
            )
        )
        if q.scalar_one_or_none():
            return
        self.s.add(
            models.Evidence(
                belief_id=belief_id,
                source_id=source_id,
                kind="web_source",
                stance="support",
                dedup_key=dedup_key,
                evidence_level=level,
                strength=strength,
                excerpt=excerpt[:1500],
            )
        )
        try:
            await self.s.commit()
        except IntegrityError as exc:
            await self.s.rollback()
            if not _expected_unique_violation(exc, "uq_evidence_dedup_key_current"):
                raise

    # 功能：提取信念结论、状态及评分字段，作为争议和历史中的变更前快照。
    @staticmethod
    def _belief_snapshot(belief: models.Belief) -> dict:
        return {
            "status": str(belief.status),
            "statement": belief.statement,
            "explanation": belief.explanation,
            "confidence": belief.confidence,
            "evidence_score": belief.evidence_score,
            "test_score": belief.test_score,
            "stability_score": belief.stability_score,
            "source_count": belief.source_count,
        }

    # 功能：调用幂等争议创建入口，只返回争议实体。
    async def create_dispute(
        self, belief: models.Belief, claim: models.Claim, score: float, explanation: str
    ) -> models.Dispute:
        item, _ = await self.get_or_create_dispute(belief, claim, score, explanation)
        return item

    # 功能：复用相同开放冲突，或创建争议、冻结旧信念状态并记录争议历史，返回创建标记。
    async def get_or_create_dispute(
        self,
        belief: models.Belief,
        claim: models.Claim,
        score: float,
        explanation: str,
    ) -> tuple[models.Dispute, bool]:
        dedup_key = stable_key("dispute", belief.id, claim.id)
        q = await self.s.execute(
            select(models.Dispute).where(models.Dispute.dedup_key == dedup_key)
        )
        existing = q.scalar_one_or_none()
        if existing:
            return existing, False
        # The migration deliberately does not backfill legacy rows, so retain the
        # former open-dispute lookup as a compatibility fallback.
        q = await self.s.execute(
            select(models.Dispute).where(
                models.Dispute.belief_id == belief.id,
                models.Dispute.incoming_claim_id == claim.id,
                models.Dispute.status.in_(_OPEN_DISPUTE_STATUSES),
            )
        )
        existing = q.scalar_one_or_none()
        if existing:
            return existing, False

        previous_state = self._belief_snapshot(belief)
        belief.status = BeliefStatus.DISPUTED
        item = models.Dispute(
            belief_id=belief.id,
            incoming_claim_id=claim.id,
            dedup_key=dedup_key,
            previous_belief_state=previous_state,
            status=DisputeStatus.UNRESOLVED,
            contradiction_score=score,
            resolution_notes=explanation,
        )
        self.s.add(item)
        self.s.add(
            models.BeliefHistory(
                belief_id=belief.id,
                action="disputed",
                previous_state=previous_state,
                new_state={"status": str(BeliefStatus.DISPUTED)},
                reason=explanation,
            )
        )
        try:
            await self.s.commit()
        except IntegrityError as exc:
            await self.s.rollback()
            if not _expected_unique_violation(exc, "uq_disputes_dedup_key_current"):
                raise
            q = await self.s.execute(
                select(models.Dispute).where(models.Dispute.dedup_key == dedup_key)
            )
            existing = q.scalar_one_or_none()
            if existing:
                return existing, False
            raise
        await self.s.refresh(item)
        return item, True

    # 功能：从库内支持记录筛选争议指定结论的合格来源，不能仅凭模型提供的 source_id 决议。
    async def qualified_resolution_sources(
        self,
        dispute: models.Dispute,
        belief: models.Belief,
        claim: models.Claim,
        outcome: str,
        requested_source_ids: list[str],
        conditional_claim_id: UUID | None = None,
    ) -> list[EvidenceSource]:
        """Fetch source links for the proposition the proposed outcome changes."""
        if dispute.belief_id != belief.id or dispute.incoming_claim_id != claim.id:
            raise ValueError("dispute, belief and claim do not belong together")
        if outcome == "unresolved":
            return []
        if outcome not in {"keep_old", "adopt_new", "conditional"}:
            raise ValueError(f"unsupported dispute outcome: {outcome}")
        requested = set(requested_source_ids)
        if not requested:
            return []

        if outcome == "keep_old":
            supported_claims = await self.s.execute(
                select(models.Claim.id).where(models.Claim.statement == belief.statement)
            )
            claim_ids = list(supported_claims.scalars())
            if not claim_ids:
                return []
            linked_evidence = await self.s.execute(
                select(models.Evidence.source_id).where(
                    models.Evidence.belief_id == belief.id,
                    models.Evidence.stance == "support",
                    models.Evidence.source_id.is_not(None),
                )
            )
            linked_ids = set(linked_evidence.scalars())
            accepted_status = "supported"
        else:
            conclusion = (
                await conditional_claim(self.s, dispute, claim, conditional_claim_id)
                if outcome == "conditional"
                else claim
            )
            claim_ids = [conclusion.id]
            linked_ids = None
            accepted_status = "supported"

        result = await self.s.execute(
            select(models.ClaimEvidence).where(
                models.ClaimEvidence.claim_id.in_(claim_ids),
                models.ClaimEvidence.status == accepted_status,
            )
        )
        found: dict[str, models.Source] = {}
        for record in result.scalars():
            source_id = str(record.source_id)
            if source_id not in requested or (
                linked_ids is not None and record.source_id not in linked_ids
            ):
                continue
            source = await self.s.get(models.Source, record.source_id)
            if source is None or source.evidence_level <= 0:
                continue
            found[source_id] = source
        return await self.lineage.evidence_sources(list(found.values()))

    # 功能：锁定并刷新争议缓存，在同一事务内应用四结果、证据和历史；重试使用最新决议，证据不足拒绝改写。
    async def apply_dispute_resolution(
        self,
        dispute: models.Dispute,
        belief: models.Belief,
        claim: models.Claim,
        *,
        outcome: str,
        rationale: str,
        conditional_statement: str,
        conditions: list[str],
        evidence_source_ids: list[str],
        conditional_claim_id: UUID | None = None,
    ) -> models.Belief:
        if outcome not in {"keep_old", "adopt_new", "conditional", "unresolved"}:
            raise ValueError(f"unsupported dispute outcome: {outcome}")

        try:
            locked = await self.s.execute(
                select(models.Dispute)
                .where(models.Dispute.id == dispute.id)
                .with_for_update()
                # 行锁不会自动覆盖身份映射中的旧属性，必须读取锁定后的最新决议。
                .execution_options(populate_existing=True)
            )
            dispute = locked.scalar_one()
            if dispute.belief_id != belief.id or dispute.incoming_claim_id != claim.id:
                raise ValueError("dispute, belief and claim do not belong together")
            prior_resolution = (dispute.resolution_metadata or {}).get("outcome")
            if prior_resolution and not (
                prior_resolution == "unresolved" and outcome != "unresolved"
            ):
                if prior_resolution != outcome:
                    raise ValueError("dispute has already been resolved differently")
                resolved_id = (dispute.resolution_metadata or {}).get("resolved_belief_id")
                result = (
                    await self.s.get(models.Belief, UUID(resolved_id)) if resolved_id else belief
                )
                await self.s.commit()
                return result
            if dispute.status in {
                DisputeStatus.RESOLVED_OLD,
                DisputeStatus.RESOLVED_NEW,
                DisputeStatus.RESOLVED_CONDITIONAL,
            }:
                raise ValueError("legacy resolved dispute cannot be reopened")

            conclusion_claim = (
                await conditional_claim(self.s, dispute, claim, conditional_claim_id)
                if outcome == "conditional"
                else claim
            )
            if outcome == "conditional" and (not conditional_statement.strip() or not conditions):
                raise ValueError("conditional resolution requires a statement and conditions")
            if outcome == "conditional" and (
                normalize_text_key(conditional_statement)
                != normalize_text_key(conclusion_claim.statement)
                or any(
                    normalize_text_key(condition)
                    not in normalize_text_key(conclusion_claim.statement)
                    for condition in conditions
                )
            ):
                raise ValueError("conditional conclusion must be the supported conditional claim")
            qualified = await self.qualified_resolution_sources(
                dispute, belief, claim, outcome, evidence_source_ids, conditional_claim_id
            )
            independent = independent_source_representatives(qualified)
            required = agent_config().learning.min_independent_sources_for_dispute_resolution
            if outcome != "unresolved" and (
                len(independent) < required
                or {source.source_id for source in independent} != set(evidence_source_ids)
            ):
                raise ValueError("dispute evidence is not independently supported")
            if outcome == "unresolved" and evidence_source_ids:
                raise ValueError("unresolved outcome cannot claim accepted evidence")

            previous_state = self._belief_snapshot(belief)
            resolved_belief = belief
            if outcome == "keep_old":
                prior_status = (dispute.previous_belief_state or {}).get("status")
                if not prior_status:
                    raise ValueError("cannot restore a belief without preserved prior state")
                another_open = await self.s.scalar(
                    select(models.Dispute.id)
                    .where(
                        models.Dispute.belief_id == belief.id,
                        models.Dispute.id != dispute.id,
                        models.Dispute.status.in_(["open", "investigating", "unresolved"]),
                    )
                    .limit(1)
                )
                belief.status = (
                    BeliefStatus.DISPUTED if another_open else BeliefStatus(prior_status)
                )
                dispute.status = DisputeStatus.RESOLVED_OLD
            elif outcome in {"adopt_new", "conditional"}:
                belief.status = (
                    BeliefStatus.RETRACTED if outcome == "adopt_new" else BeliefStatus.WEAKENED
                )
                dispute.status = (
                    DisputeStatus.RESOLVED_NEW
                    if outcome == "adopt_new"
                    else DisputeStatus.RESOLVED_CONDITIONAL
                )
                score = sum(
                    min(1.0, source.evidence_level / 5) * source.credibility_score
                    for source in independent
                ) / len(independent)
                resolved_belief = models.Belief(
                    topic=conclusion_claim.topic or belief.topic,
                    statement=claim.statement if outcome == "adopt_new" else conditional_statement,
                    explanation=conclusion_claim.reasoning,
                    status=BeliefStatus.SUPPORTED,
                    confidence=min(0.7, score),
                    evidence_score=score,
                    test_score=0.0,
                    stability_score=0.1,
                    source_count=len(independent),
                    metadata_json={
                        "conditions": conditions,
                        "resolved_from_dispute": str(dispute.id),
                    },
                )
                self.s.add(resolved_belief)
                await self.s.flush()
                for source_info in independent:
                    source_id = UUID(source_info.source_id)
                    self.s.add(
                        models.Evidence(
                            belief_id=resolved_belief.id,
                            source_id=source_id,
                            kind="web_source",
                            stance="support",
                            evidence_level=source_info.evidence_level,
                            dedup_key=stable_key(
                                "evidence", resolved_belief.id, source_id, "web_source", "support"
                            ),
                            strength=min(1.0, source_info.credibility_score),
                            excerpt=await self.supported_excerpt(conclusion_claim.id, source_id),
                        )
                    )
                self.s.add(
                    models.BeliefHistory(
                        belief_id=resolved_belief.id,
                        action="created_from_dispute_resolution",
                        previous_state={},
                        new_state=self._belief_snapshot(resolved_belief),
                        reason=rationale,
                    )
                )
            else:
                belief.status = BeliefStatus.UNRESOLVED
                dispute.status = DisputeStatus.UNRESOLVED

            dispute.resolution_notes = rationale
            dispute.resolution_metadata = {
                **(dispute.resolution_metadata or {}),
                "outcome": outcome,
                "conditions": conditions,
                "evidence_source_ids": [source.source_id for source in independent],
                "resolved_belief_id": str(resolved_belief.id),
                "conditional_claim_id": str(conditional_claim_id) if conditional_claim_id else None,
            }
            dispute.resolved_at = None if outcome == "unresolved" else datetime.now(UTC)
            self.s.add(
                models.BeliefHistory(
                    belief_id=belief.id,
                    action="dispute_resolved",
                    previous_state=previous_state,
                    new_state=self._belief_snapshot(belief),
                    reason=rationale,
                )
            )
            await self.s.flush()
            await self.index.sync("belief", resolved_belief)
            if resolved_belief.id != belief.id:
                await self.index.sync("belief", belief)
            await self.s.commit()
            return resolved_belief
        except Exception:
            await self.s.rollback()
            raise

    # 功能：保存按学习会话关联的分维度评估与审计信息，重放时避免重复保存同一结果。
    async def save_evaluation(
        self, goal_id: UUID, learning_session_id: UUID, result
    ) -> models.Evaluation:
        existing = await self.s.scalar(
            select(models.Evaluation)
            .where(models.Evaluation.learning_session_id == learning_session_id)
            .order_by(models.Evaluation.created_at.desc())
            .limit(1)
        )
        if (
            existing
            and existing.score == result.score
            and existing.passed == result.passed
            and (existing.components or {}).get("audit") == result.audit
        ):
            return existing
        item = models.Evaluation(
            goal_id=goal_id,
            learning_session_id=learning_session_id,
            score=result.score,
            passed=result.passed,
            components={
                "factual_accuracy": result.factual_accuracy,
                "reasoning": result.reasoning,
                "transfer": result.transfer,
                "completeness": result.completeness,
                "calibration": result.calibration,
                "feedback": result.feedback,
                "audit": result.audit,
            },
            questions=result.questions,
            answers=result.answers,
        )
        self.s.add(item)
        await self.s.commit()
        await self.s.refresh(item)
        return item

    # 功能：组织信念、开放争议等认知摘要供模型参考；摘要内容不是控制器指令。
    async def epistemic_context(self) -> str:
        beliefs = await self.recent_beliefs(20)
        open_disputes = (
            (
                await self.s.execute(
                    select(models.Dispute)
                    .where(models.Dispute.status.in_(["open", "investigating", "unresolved"]))
                    .limit(10)
                )
            )
            .scalars()
            .all()
        )
        lines = ["Recent beliefs:"]
        for b in beliefs:
            lines.append(f"- [{b.status} conf={b.confidence:.2f}] {b.topic}: {b.statement[:300]}")
        lines.append("Open disputes:")
        for d in open_disputes:
            lines.append(f"- dispute={d.id} contradiction={d.contradiction_score:.2f}")
        return "\n".join(lines)
