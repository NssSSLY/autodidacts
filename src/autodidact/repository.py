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
from autodidact.knowledge.sources import (
    EvidenceSource,
    independent_source_representatives,
    normalize_url,
    publisher_key,
)
from autodidact.normalization import claim_statement_key, normalize_text_key, stable_key
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

    async def recent_beliefs(self, limit: int = 30) -> list[models.Belief]:
        q = await self.s.execute(
            select(models.Belief)
            .where(models.Belief.status != BeliefStatus.RETRACTED)
            .order_by(desc(models.Belief.updated_at))
            .limit(limit)
        )
        return list(q.scalars())

    async def semantic_beliefs(
        self, embedding: list[float], limit: int = 30
    ) -> list[models.Belief]:
        distance = models.Belief.embedding.cosine_distance(embedding)
        q = await self.s.execute(
            select(models.Belief)
            .where(models.Belief.status != BeliefStatus.RETRACTED)
            .where(models.Belief.embedding.is_not(None))
            .order_by(distance)
            .limit(limit)
        )
        return list(q.scalars())

    async def set_belief_embedding(self, belief: models.Belief, embedding: list[float]) -> None:
        belief.embedding = embedding
        await self.s.commit()

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

    async def qualified_resolution_sources(
        self,
        dispute: models.Dispute,
        belief: models.Belief,
        claim: models.Claim,
        outcome: str,
        requested_source_ids: list[str],
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
            claim_ids = [claim.id]
            linked_ids = None
            accepted_status = "supported"

        result = await self.s.execute(
            select(models.ClaimEvidence).where(
                models.ClaimEvidence.claim_id.in_(claim_ids),
                models.ClaimEvidence.status == accepted_status,
            )
        )
        found: dict[str, EvidenceSource] = {}
        for record in result.scalars():
            source_id = str(record.source_id)
            if source_id not in requested or (
                linked_ids is not None and record.source_id not in linked_ids
            ):
                continue
            source = await self.s.get(models.Source, record.source_id)
            if source is None or source.evidence_level <= 0:
                continue
            found[source_id] = EvidenceSource(
                source_id=source_id,
                normalized_url=source.normalized_url or "",
                publisher_key=source.publisher_key or "",
                content_hash=source.content_hash or "",
                evidence_level=source.evidence_level,
                credibility_score=source.credibility_score,
            )
        return list(found.values())

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
    ) -> models.Belief:
        if outcome not in {"keep_old", "adopt_new", "conditional", "unresolved"}:
            raise ValueError(f"unsupported dispute outcome: {outcome}")

        try:
            locked = await self.s.execute(
                select(models.Dispute).where(models.Dispute.id == dispute.id).with_for_update()
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

            if outcome == "conditional" and (not conditional_statement.strip() or not conditions):
                raise ValueError("conditional resolution requires a statement and conditions")
            if outcome == "conditional" and (
                normalize_text_key(conditional_statement) != normalize_text_key(claim.statement)
                or any(
                    normalize_text_key(condition) not in normalize_text_key(claim.statement)
                    for condition in conditions
                )
            ):
                raise ValueError("conditional conclusion must be the supported conditional claim")
            qualified = await self.qualified_resolution_sources(
                dispute, belief, claim, outcome, evidence_source_ids
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
                belief.status = BeliefStatus(prior_status)
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
                    topic=claim.topic or belief.topic,
                    statement=claim.statement if outcome == "adopt_new" else conditional_statement,
                    explanation=claim.reasoning,
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
                            excerpt="",
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
                "outcome": outcome,
                "conditions": conditions,
                "evidence_source_ids": [source.source_id for source in independent],
                "resolved_belief_id": str(resolved_belief.id),
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
            await self.s.commit()
            return resolved_belief
        except Exception:
            await self.s.rollback()
            raise

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
