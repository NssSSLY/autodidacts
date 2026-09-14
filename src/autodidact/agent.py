from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from autodidact.brain.llm import LLM
from autodidact.config import agent_config
from autodidact.enums import GoalSource, GoalStatus
from autodidact.goals.generator import GoalGenerator
from autodidact.goals.scorer import goal_score
from autodidact.knowledge.conflicts import ConflictDetector
from autodidact.knowledge.promotion import BeliefPromotionPolicy
from autodidact.learning.evaluator import Evaluator
from autodidact.learning.planner import Planner
from autodidact.learning.reflection import Reflector
from autodidact.learning.synthesizer import Synthesizer
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
    ):
        self.repo = Repository(session)
        self.llm = llm
        self.planner = Planner(llm)
        self.synthesizer = Synthesizer(llm)
        self.evaluator = Evaluator(llm)
        self.reflector = Reflector(llm)
        self.conflicts = ConflictDetector(llm)
        self.cfg = agent_config()
        self.promotion = BeliefPromotionPolicy(
            self.cfg.learning.min_independent_sources_for_verified_belief
        )
        self.goal_generator = GoalGenerator(llm)
        self.search = search or build_search_provider()
        self.reader = WebReader()

    async def bootstrap(self) -> None:
        identity = self.cfg.agent
        await self.repo.get_or_create_agent(identity.name, identity.mission, identity.initial_focus)
        if await self.repo.next_goal(self.cfg.learning.max_retry) is None:
            seed = CandidateGoal(
                title=f"建立 {identity.initial_focus} 的基础系统地图",
                description="识别核心子系统、关键概念、依赖关系、常见限制和后续学习路径。",
                source=GoalSource.HUMAN,
                importance=1.0, uncertainty=0.9, novelty=0.9, utility=1.0,
                prerequisite_score=0.9, estimated_cost=0.4,
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
        stored_sources = {}
        for query in plan.queries:
            try:
                hits = await self.search.search(query, limit=3)
            # A single search provider failure must degrade gracefully.
            except Exception as exc:  # noqa: BLE001
                log.warning("Search failed for %r: %s", query, exc)
                continue
            for hit in hits:
                if hit.url in seen_urls or len(docs) >= self.cfg.learning.max_sources_per_goal:
                    continue
                seen_urls.add(hit.url)
                try:
                    doc = await self.reader.read(hit.url)
                    docs.append(doc)
                    h = self.reader.hash_text(doc.text)
                    db_source = await self.repo.upsert_source(doc, h)
                    stored_sources[doc.url] = db_source
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
        for draft in learned.claims:
            source_ids = [str(stored_sources[u].id) for u in draft.source_urls if u in stored_sources]
            claim_rows.append(await self.repo.add_claim(draft, learning_session.id, source_ids))

        # Conflict detection happens before belief promotion. New model output cannot overwrite old beliefs.
        old_beliefs = await self.repo.recent_beliefs(40)
        disputes = 0
        disputed_claim_ids = set()
        for claim in claim_rows:
            for belief in old_beliefs:
                relation = await self.conflicts.compare(belief.statement, claim.statement)
                if relation.relation == "contradicts" and relation.score >= self.cfg.learning.contradiction_threshold:
                    await self.repo.create_dispute(belief, claim, relation.score, relation.explanation)
                    disputes += 1
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
                decision = self.promotion.decide(
                    evaluation_passed=True,
                    source_ids=claim.source_ids,
                    has_open_dispute=claim.id in disputed_claim_ids,
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
                )
                beliefs_promoted += 1
                for source_id in claim.source_ids:
                    for source in stored_sources.values():
                        if str(source.id) == source_id:
                            await self.repo.add_evidence(
                                belief.id, source.id, source.evidence_level,
                                strength=min(1.0, claim.confidence), excerpt=source.extracted_text or ""
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
            follow_ups.append(CandidateGoal(
                title=f"补齐前置知识：{dep}", description=f"由目标“{goal.title}”发现的前置知识缺口。",
                source=GoalSource.PREREQUISITE, importance=0.8, uncertainty=0.8,
                novelty=0.7, utility=0.9, prerequisite_score=1.0, estimated_cost=0.45,
            ))
        for q in learned.unanswered_questions[:2]:
            follow_ups.append(CandidateGoal(
                title=q, description=f"由目标“{goal.title}”产生的未解决问题。",
                source=GoalSource.FOLLOW_UP, importance=0.65, uncertainty=0.9,
                novelty=0.7, utility=0.7, prerequisite_score=0.6, estimated_cost=0.4,
            ))
        for claim in claim_rows:
            if claim.id not in disputed_claim_ids:
                continue
            follow_ups.append(CandidateGoal(
                title=f"调查争议主张：{claim.statement[:120]}",
                description=f"主张在目标“{goal.title}”中与已有信念发生实质冲突，需要独立证据调查。",
                source=GoalSource.CONFLICT, importance=0.9, uncertainty=1.0,
                novelty=0.6, utility=0.9, prerequisite_score=0.8, estimated_cost=0.6,
            ))
        for fg in follow_ups[: self.cfg.learning.max_follow_up_goals_per_cycle]:
            await self.repo.add_goal(fg, goal_score(fg, self.cfg.learning.exploration_rate), goal.id)

        return {
            "status": "passed" if evaluation.passed else "failed",
            "goal": goal.title,
            "score": evaluation.score,
            "sources": len(docs),
            "claims": len(claim_rows),
            "beliefs_promoted": beliefs_promoted,
            "disputes_created": disputes,
            "follow_up_goals": len(follow_ups),
        }

    async def run_daemon(self, max_cycles: int | None = None) -> None:
        cycles = max_cycles or self.cfg.learning.max_cycles_per_process
        for _ in range(cycles):
            result = await self.run_cycle()
            log.info("Cycle result: %s", result)
            await asyncio.sleep(self.cfg.learning.cycle_sleep_seconds)
