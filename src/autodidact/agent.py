from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from autodidact import models
from autodidact.brain.llm import LLM
from autodidact.config import agent_config
from autodidact.embeddings import EmbeddingProvider, build_embedding_provider
from autodidact.enums import GoalSource, GoalStatus
from autodidact.goals.generator import GoalGenerator
from autodidact.goals.scorer import goal_score
from autodidact.knowledge.claim_support import ClaimSupportValidator
from autodidact.knowledge.conflicts import ConflictDetector
from autodidact.knowledge.promotion import BeliefPromotionPolicy
from autodidact.knowledge.sources import EvidenceSource, normalize_url
from autodidact.learning.evaluator import Evaluator
from autodidact.learning.planner import Planner
from autodidact.learning.reflection import Reflector
from autodidact.learning.synthesizer import Synthesizer
from autodidact.normalization import normalize_text_key
from autodidact.repository import Repository
from autodidact.schemas import CandidateGoal, SourceDocument
from autodidact.tools.reader import WebReader
from autodidact.tools.search import SearchProvider, build_search_provider

log = logging.getLogger(__name__)


class AutonomousLearner:
    def __init__(
        self,
        session: AsyncSession,
        llm: LLM,
        search: SearchProvider | None = None,
        embedding: EmbeddingProvider | None = None,
    ):
        self.repo = Repository(session)
        self.llm = llm
        self.embedding = embedding or build_embedding_provider()
        self.planner = Planner(llm)
        self.synthesizer = Synthesizer(llm)
        self.evaluator = Evaluator(llm)
        self.reflector = Reflector(llm)
        self.conflicts = ConflictDetector(llm)
        self.cfg = agent_config()
        self.promotion = BeliefPromotionPolicy(
            self.cfg.learning.min_independent_sources_for_verified_belief,
            self.cfg.learning.min_evidence_level_for_verified_belief,
        )
        self.goal_generator = GoalGenerator(llm)
        self.search = search or build_search_provider()
        self.reader = WebReader()
        self.claim_support = ClaimSupportValidator()

    async def _beliefs_for_claim(self, statement: str) -> list[models.Belief]:
        try:
            embedding = await self.embedding.embed(statement)
            recalled = await self.repo.semantic_beliefs(embedding, 40)
            if recalled:
                return recalled
        except Exception as exc:  # noqa: BLE001 - external embedding failures are degradable.
            log.debug("Semantic recall unavailable; using recency fallback: %s", exc)
        return await self.repo.recent_beliefs(40)

    async def bootstrap(self) -> None:
        identity = self.cfg.agent
        await self.repo.get_or_create_agent(identity.name, identity.mission, identity.initial_focus)
        if await self.repo.next_goal(self.cfg.learning.max_retry) is None:
            seed = CandidateGoal(
                title=f"建立 {identity.initial_focus} 的基础系统地图",
                description="识别核心子系统、关键概念、依赖关系、常见限制和后续学习路径。",
                source=GoalSource.HUMAN,
                importance=1.0,
                uncertainty=0.9,
                novelty=0.9,
                utility=1.0,
                prerequisite_score=0.9,
                estimated_cost=0.4,
            )
            await self.repo.add_goal(seed, goal_score(seed, self.cfg.learning.exploration_rate))

    async def ensure_goal_pool(self) -> None:
        if await self.repo.next_goal(self.cfg.learning.max_retry) is not None:
            return
        context = await self.repo.epistemic_context()
        candidates = await self.goal_generator.generate(self.cfg.agent.mission, context)
        for c in candidates[: self.cfg.learning.daily_goal_limit]:
            await self.repo.add_goal(c, goal_score(c, self.cfg.learning.exploration_rate))

    async def run_cycle(self) -> dict:
        await self.ensure_goal_pool()
        goal = await self.repo.next_goal(self.cfg.learning.max_retry)
        if not goal:
            return {"status": "idle", "reason": "no goals"}

        log.info("Starting goal: %s", goal.title)
        await self.repo.update_goal_status(goal, GoalStatus.PLANNED)
        context = await self.repo.epistemic_context()
        plan = await self.planner.plan(goal.title, context)
        learning_session = await self.repo.create_learning_session(goal.id, plan.model_dump())
        await self.repo.update_goal_status(goal, GoalStatus.RESEARCHING)

        docs: list[SourceDocument] = []
        seen_urls: set[str] = set()
        seen_hashes: set[str] = set()
        stored_sources: dict[str, models.Source] = {}
        for query in plan.queries:
            try:
                hits = await self.search.search(query, limit=3)
            # A single search provider failure must degrade gracefully.
            except Exception as exc:  # noqa: BLE001
                log.warning("Search failed for %r: %s", query, exc)
                continue
            for hit in hits:
                hit_key = normalize_url(hit.url)
                if hit_key in seen_urls or len(docs) >= self.cfg.learning.max_sources_per_goal:
                    continue
                seen_urls.add(hit_key)
                try:
                    doc = await self.reader.read(hit.url)
                    h = self.reader.hash_text(doc.text)
                    if h in seen_hashes:
                        continue
                    seen_hashes.add(h)
                    docs.append(doc)
                    db_source = await self.repo.upsert_source(doc, h)
                    stored_sources[normalize_url(doc.url)] = db_source
                # A single unreadable source must not terminate the learning cycle.
                except Exception as exc:  # noqa: BLE001
                    log.debug("Read failed %s: %s", hit.url, exc)
            if len(docs) >= self.cfg.learning.max_sources_per_goal:
                break

        if not docs:
            await self.repo.update_goal_status(
                goal,
                GoalStatus.FAILED,
                max_retry=self.cfg.learning.max_retry,
            )
            return {"status": "failed", "goal": goal.title, "reason": "no readable sources"}

        await self.repo.update_goal_status(goal, GoalStatus.SYNTHESIZING)
        learned = await self.synthesizer.synthesize(goal.title, docs)

        claim_rows = []
        seen_claim_ids = set()
        for draft in learned.claims:
            validation = self.claim_support.validate(draft, stored_sources)
            source_ids = validation.anchored_source_ids
            claim = await self.repo.add_claim(draft, learning_session.id, source_ids)
            await self.repo.record_claim_evidence(claim, validation.records)
            if claim.id not in seen_claim_ids:
                claim_rows.append(claim)
                seen_claim_ids.add(claim.id)

        # Conflict detection happens before belief promotion. New model output cannot overwrite old beliefs.
        disputes = 0
        disputed_claim_ids = set()
        for claim in claim_rows:
            old_beliefs = await self._beliefs_for_claim(claim.statement)
            for belief in old_beliefs:
                if normalize_text_key(belief.statement) == normalize_text_key(claim.statement):
                    continue
                relation = await self.conflicts.compare(belief.statement, claim.statement)
                if (
                    relation.relation == "contradicts"
                    and relation.score >= self.cfg.learning.contradiction_threshold
                ):
                    _, created = await self.repo.get_or_create_dispute(
                        belief, claim, relation.score, relation.explanation
                    )
                    disputes += int(created)
                    disputed_claim_ids.add(claim.id)

        await self.repo.update_goal_status(goal, GoalStatus.TESTING)
        evaluation = await self.evaluator.evaluate(goal.title, learned, docs)
        await self.repo.save_evaluation(goal.id, learning_session.id, evaluation)

        await self.repo.update_goal_status(goal, GoalStatus.REFLECTING)
        reflection = await self.reflector.reflect(goal.title, learned, evaluation)
        await self.repo.finish_learning_session(
            learning_session, learned.model_dump(), reflection.model_dump(), evaluation.passed
        )

        beliefs_promoted = 0
        if evaluation.passed:
            for claim in claim_rows:
                source_by_id = {str(source.id): source for source in stored_sources.values()}
                evidence_sources = [
                    EvidenceSource(
                        source_id=source_id,
                        normalized_url=source_by_id[source_id].normalized_url or "",
                        publisher_key=source_by_id[source_id].publisher_key or "",
                        content_hash=source_by_id[source_id].content_hash or "",
                        evidence_level=source_by_id[source_id].evidence_level,
                        credibility_score=source_by_id[source_id].credibility_score,
                    )
                    for source_id in claim.source_ids
                    if source_id in source_by_id
                ]
                decision = self.promotion.decide(
                    evaluation_passed=True,
                    has_open_dispute=claim.id in disputed_claim_ids,
                    sources=evidence_sources,
                )
                if not decision.promote:
                    log.info(
                        "Claim %s was not promoted: %s",
                        claim.id,
                        decision.reason,
                    )
                    continue
                belief = await self.repo.create_belief_from_claim(
                    claim,
                    evaluation.score,
                    decision.verified,
                    evidence_score=decision.evidence_score,
                    independent_source_count=decision.independent_source_count,
                )
                try:
                    vector = await self.embedding.embed(belief.statement)
                    await self.repo.set_belief_embedding(belief, vector)
                except Exception as exc:  # noqa: BLE001 - promotion must not depend on embeddings.
                    log.debug("Belief embedding was not persisted: %s", exc)
                beliefs_promoted += 1
                for source_id in dict.fromkeys(claim.source_ids):
                    source = source_by_id.get(source_id)
                    if source:
                        await self.repo.add_evidence(
                            belief.id,
                            source.id,
                            source.evidence_level,
                            strength=min(1.0, claim.confidence),
                            excerpt=source.extracted_text or "",
                        )
            await self.repo.update_goal_status(goal, GoalStatus.PASSED, evaluation.score)
        else:
            await self.repo.update_goal_status(
                goal,
                GoalStatus.FAILED,
                evaluation.score,
                max_retry=self.cfg.learning.max_retry,
            )

        follow_ups: list[CandidateGoal] = []
        for dep in learned.discovered_dependencies[:2]:
            follow_ups.append(
                CandidateGoal(
                    title=f"补齐前置知识：{dep}",
                    description=f"由目标“{goal.title}”发现的前置知识缺口。",
                    source=GoalSource.PREREQUISITE,
                    importance=0.8,
                    uncertainty=0.8,
                    novelty=0.7,
                    utility=0.9,
                    prerequisite_score=1.0,
                    estimated_cost=0.45,
                )
            )
        for q in learned.unanswered_questions[:2]:
            follow_ups.append(
                CandidateGoal(
                    title=q,
                    description=f"由目标“{goal.title}”产生的未解决问题。",
                    source=GoalSource.FOLLOW_UP,
                    importance=0.65,
                    uncertainty=0.9,
                    novelty=0.7,
                    utility=0.7,
                    prerequisite_score=0.6,
                    estimated_cost=0.4,
                )
            )
        for claim in claim_rows:
            if claim.id not in disputed_claim_ids:
                continue
            follow_ups.append(
                CandidateGoal(
                    title=f"调查争议主张：{claim.statement[:120]}",
                    description=f"主张在目标“{goal.title}”中与已有信念发生实质冲突，需要独立证据调查。",
                    source=GoalSource.CONFLICT,
                    importance=0.9,
                    uncertainty=1.0,
                    novelty=0.6,
                    utility=0.9,
                    prerequisite_score=0.8,
                    estimated_cost=0.6,
                )
            )
        follow_up_goals_created = 0
        for fg in follow_ups[: self.cfg.learning.max_follow_up_goals_per_cycle]:
            _, created = await self.repo.add_goal_if_absent(
                fg, goal_score(fg, self.cfg.learning.exploration_rate), goal.id
            )
            follow_up_goals_created += int(created)

        return {
            "status": "passed" if evaluation.passed else "failed",
            "goal": goal.title,
            "score": evaluation.score,
            "sources": len(docs),
            "claims": len(claim_rows),
            "beliefs_promoted": beliefs_promoted,
            "claims_with_anchored_evidence": sum(bool(claim.source_ids) for claim in claim_rows),
            "disputes_created": disputes,
            "follow_up_goals": follow_up_goals_created,
        }

    async def run_daemon(self, max_cycles: int | None = None) -> None:
        cycles = max_cycles or self.cfg.learning.max_cycles_per_process
        for _ in range(cycles):
            result = await self.run_cycle()
            log.info("Cycle result: %s", result)
            await asyncio.sleep(self.cfg.learning.cycle_sleep_seconds)
