from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from autodidact import models
from autodidact.enums import BeliefStatus, DisputeStatus, GoalStatus
from autodidact.knowledge.sources import normalize_url, publisher_key
from autodidact.normalization import normalize_text_key
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


class Repository:
    def __init__(self, session: AsyncSession):
        self.s = session

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

    async def add_goal(
        self, g: CandidateGoal, score: float, parent_goal_id: UUID | None = None
    ) -> models.Goal:
        goal, _ = await self.add_goal_if_absent(g, score, parent_goal_id)
        return goal

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
        await self.s.commit()
        await self.s.refresh(goal)
        return goal, True

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

    async def update_goal_status(
        self,
        goal: models.Goal,
        status: str,
        confidence_after: float | None = None,
        *,
        max_retry: int | None = None,
    ) -> None:
        if goal.started_at is None:
            goal.started_at = datetime.now(UTC)
        if status == GoalStatus.FAILED:
            goal.retry_count += 1
            if max_retry is not None and goal.retry_count >= max_retry:
                status = GoalStatus.BLOCKED
        goal.status = status
        if status in (GoalStatus.PASSED, GoalStatus.BLOCKED):
            goal.completed_at = datetime.now(UTC)
        if confidence_after is not None:
            goal.confidence_after = confidence_after
        await self.s.commit()

    async def create_learning_session(self, goal_id: UUID, plan: dict) -> models.LearningSession:
        item = models.LearningSession(goal_id=goal_id, plan=plan)
        self.s.add(item)
        await self.s.commit()
        await self.s.refresh(item)
        return item

    async def finish_learning_session(
        self, item: models.LearningSession, result: dict, reflection: dict, success: bool
    ) -> None:
        item.result = result
        item.reflection = reflection
        item.success = success
        item.completed_at = datetime.now(UTC)
        await self.s.commit()

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
            if changed:
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
        )
        self.s.add(item)
        await self.s.commit()
        await self.s.refresh(item)
        return item

    async def add_claim(
        self, draft: ClaimDraft, learning_session_id: UUID, source_ids: list[str]
    ) -> models.Claim:
        q = await self.s.execute(
            select(models.Claim).where(models.Claim.learning_session_id == learning_session_id)
        )
        statement_key = normalize_text_key(draft.statement)
        for existing in q.scalars():
            if normalize_text_key(existing.statement) == statement_key:
                return existing

        item = models.Claim(
            learning_session_id=learning_session_id,
            statement=draft.statement,
            topic=draft.topic,
            reasoning=draft.reasoning,
            confidence=draft.confidence,
            source_ids=list(dict.fromkeys(source_ids)),
        )
        self.s.add(item)
        await self.s.commit()
        await self.s.refresh(item)
        return item

    async def recent_beliefs(self, limit: int = 30) -> list[models.Belief]:
        q = await self.s.execute(
            select(models.Belief)
            .where(models.Belief.status != BeliefStatus.RETRACTED)
            .order_by(desc(models.Belief.updated_at))
            .limit(limit)
        )
        return list(q.scalars())

    async def beliefs_for_topic(self, topic: str, limit: int = 10) -> list[models.Belief]:
        q = await self.s.execute(
            select(models.Belief)
            .where(models.Belief.topic.ilike(f"%{topic}%"))
            .where(models.Belief.status != BeliefStatus.RETRACTED)
            .order_by(desc(models.Belief.confidence))
            .limit(limit)
        )
        return list(q.scalars())

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
            source_count=(
                len(claim.source_ids)
                if independent_source_count is None
                else independent_source_count
            ),
        )
        self.s.add(belief)
        await self.s.commit()
        await self.s.refresh(belief)
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

    async def add_evidence(
        self, belief_id: UUID, source_id: UUID, level: int, strength: float, excerpt: str = ""
    ) -> None:
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
                evidence_level=level,
                strength=strength,
                excerpt=excerpt[:1500],
            )
        )
        await self.s.commit()

    async def create_dispute(
        self, belief: models.Belief, claim: models.Claim, score: float, explanation: str
    ) -> models.Dispute:
        item, _ = await self.get_or_create_dispute(belief, claim, score, explanation)
        return item

    async def get_or_create_dispute(
        self,
        belief: models.Belief,
        claim: models.Claim,
        score: float,
        explanation: str,
    ) -> tuple[models.Dispute, bool]:
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

        previous_status = belief.status
        belief.status = BeliefStatus.DISPUTED
        item = models.Dispute(
            belief_id=belief.id,
            incoming_claim_id=claim.id,
            status=DisputeStatus.UNRESOLVED,
            contradiction_score=score,
            resolution_notes=explanation,
        )
        self.s.add(item)
        self.s.add(
            models.BeliefHistory(
                belief_id=belief.id,
                action="disputed",
                previous_state={"status": previous_status, "confidence": belief.confidence},
                new_state={"status": BeliefStatus.DISPUTED},
                reason=explanation,
            )
        )
        await self.s.commit()
        await self.s.refresh(item)
        return item, True

    async def save_evaluation(
        self, goal_id: UUID, learning_session_id: UUID, result
    ) -> models.Evaluation:
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
            },
            questions=result.questions,
            answers=result.answers,
        )
        self.s.add(item)
        await self.s.commit()
        await self.s.refresh(item)
        return item

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
