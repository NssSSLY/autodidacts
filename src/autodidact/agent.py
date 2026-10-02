from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from autodidact import models
from autodidact.brain.factory import build_judge
from autodidact.brain.llm import LLM
from autodidact.brain.observed import ObservedLLM
from autodidact.config import agent_config, runtime_settings
from autodidact.embedding_runtime import RecordedEmbedding
from autodidact.embeddings import EmbeddingProvider, build_embedding_provider
from autodidact.enums import GoalSource, GoalStatus
from autodidact.goals.generator import GoalGenerator
from autodidact.goals.scorer import goal_score
from autodidact.knowledge.claim_support import ClaimSupportValidator
from autodidact.knowledge.conflicts import ConflictDetector
from autodidact.knowledge.investigation import DisputeInvestigator
from autodidact.knowledge.promotion import BeliefPromotionPolicy
from autodidact.knowledge.sources import EvidenceSource, normalize_url
from autodidact.knowledge.support_assessment import ClaimSupportAssessor
from autodidact.learning.evaluator import Evaluator
from autodidact.learning.planner import Planner
from autodidact.learning.reflection import Reflector
from autodidact.learning.synthesizer import Synthesizer
from autodidact.memory import MemoryConsolidator
from autodidact.normalization import normalize_text_key
from autodidact.repository import Repository
from autodidact.research import ResearchCollector
from autodidact.runtime import (
    BudgetedSearch,
    BudgetExceeded,
    ControllerBusy,
    OperationBudget,
    controller_lock,
)
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
        self.engine = session.bind
        llm = llm if isinstance(llm, ObservedLLM) else ObservedLLM(llm, self.engine)
        self.llm = llm
        self.embedding = RecordedEmbedding(embedding or build_embedding_provider(), self.engine)
        self.planner = Planner(llm)
        self.synthesizer = Synthesizer(llm)
        self.evaluator = None  # Assigned after the verification collector is constructed.
        self.reflector = Reflector(llm)
        self.conflicts = ConflictDetector(llm)
        self.cfg = agent_config()
        self.promotion = BeliefPromotionPolicy(
            self.cfg.learning.min_independent_sources_for_verified_belief,
            self.cfg.learning.min_evidence_level_for_verified_belief,
        )
        self.goal_generator = GoalGenerator(llm)
        self.search = BudgetedSearch(
            search or build_search_provider(), OperationBudget(self.engine)
        )
        self.reader = WebReader().with_budget(self.engine)
        self.collector = ResearchCollector(self.search, self.reader, repo=self.repo)
        self.evaluator = Evaluator(
            llm, judge=build_judge(self.engine), verification_fetcher=self.collector.fetch
        )
        self.current_goal = None
        self.current_attempt = None
        self.claim_support = ClaimSupportValidator()

        self.support_assessor = ClaimSupportAssessor(llm)

    async def _beliefs_for_claim(self, statement: str) -> list[models.Belief]:
        try:
            embedding = await self.embedding.embed(statement)
            recalled = await self.repo.semantic_beliefs(
                embedding, 40, fingerprint=self.embedding.fingerprint
            )
            if recalled:
                return recalled
        except Exception as exc:  # noqa: BLE001 - external embedding failures are degradable.
            log.debug("Semantic recall unavailable; using recency fallback: %s", exc)
        return await self.repo.recent_beliefs(40)

    async def bootstrap(self) -> None:
        identity = self.cfg.agent
        await self.repo.get_or_create_agent(identity.name, identity.mission, identity.initial_focus)
        from sqlalchemy import func, select

        if not await self.repo.s.scalar(select(func.count()).select_from(models.Goal)):
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
        quota = await self.repo.goal_quota_remaining()
        if quota <= 0:
            return
        for dispute in (await self.repo.open_disputes())[:quota]:
            metadata = dispute.resolution_metadata or {}
            if (
                len(metadata.get("investigation_session_ids", []))
                >= self.cfg.learning.max_dispute_investigations
            ):
                continue
            draft = CandidateGoal(
                title=f"独立调查争议 {dispute.id}",
                source=GoalSource.CONFLICT,
                importance=0.95,
                uncertainty=1.0,
            )
            goal = await self.repo.add_goal(draft, goal_score(draft))
            goal.metadata_json = {**(goal.metadata_json or {}), "dispute_id": str(dispute.id)}
            await self.repo.s.commit()
        if await self.repo.next_goal(self.cfg.learning.max_retry) is not None:
            return
        quota = await self.repo.goal_quota_remaining()
        if quota <= 0:
            return
        candidates = await self.goal_generator.generate(self.cfg.agent.mission, context)
        for c in candidates[:quota]:
            await self.repo.add_goal(c, goal_score(c, self.cfg.learning.exploration_rate))

    async def run_cycle(self) -> dict:
        try:
            async with controller_lock(self.engine):
                await self.repo.recover_interrupted()
                await self.bootstrap()
                self.current_goal = self.current_attempt = None
                self.llm.bind(phase="goal_pool")
                try:
                    result = await self._run_cycle()
                except BudgetExceeded as exc:
                    await self._finish_failure(str(exc), budget_stop=True)
                    return {"status": "budget_exhausted", "reason": str(exc)}
                except Exception as exc:
                    log.exception("学习循环失败，记录状态后保留后续重试")
                    await self._finish_failure(type(exc).__name__)
                    return {"status": "failed", "reason": type(exc).__name__}
                if self.cfg.learning.auto_consolidate_memory:
                    try:
                        self.llm.bind(phase="memory_consolidation")
                        result["memory"] = await MemoryConsolidator(
                            self.repo, self.llm
                        ).consolidate()
                        await self.repo.refresh_model_profiles()
                    except Exception as exc:  # noqa: BLE001 - optional maintenance is isolated.
                        await self.repo.s.rollback()
                        result["maintenance_error"] = type(exc).__name__
                if runtime_settings().benchmark_snapshot_id:
                    try:
                        from autodidact.scheduling import scheduled_benchmarks

                        result["experiment"] = await scheduled_benchmarks(
                            self.repo, self.llm, self.evaluator.judge
                        )
                    except Exception as exc:  # noqa: BLE001 - an optional experiment must not discard learning.
                        await self.repo.s.rollback()
                        result["experiment_error"] = type(exc).__name__
                return result
        except ControllerBusy as exc:
            return {"status": "busy", "reason": str(exc)}

    async def _finish_failure(self, reason, budget_stop=False):
        await self.repo.s.rollback()
        if self.current_attempt:
            attempt = await self.repo.s.get(models.LearningSession, self.current_attempt)
            if attempt:
                await self.repo.finish_learning_session(
                    attempt, {"failure": reason}, {"failure_reason": reason}, False
                )
        if self.current_goal:
            goal = await self.repo.s.get(models.Goal, self.current_goal)
            if goal:
                await self.repo.update_goal_status(
                    goal,
                    GoalStatus.DISCOVERED if budget_stop else GoalStatus.FAILED,
                    max_retry=self.cfg.learning.max_retry,
                )

    async def _run_cycle(self) -> dict:
        await self.ensure_goal_pool()
        goal = await self.repo.next_goal(self.cfg.learning.max_retry)
        if not goal:
            return {"status": "idle", "reason": "no goals"}

        self.current_goal = goal.id
        self.llm.bind(goal_id=str(goal.id), phase="planning")
        log.info("Starting goal: %s", goal.title)
        await self.repo.update_goal_status(goal, GoalStatus.PLANNED)
        context = await self.repo.epistemic_context()
        if (goal.metadata_json or {}).get("dispute_id"):
            attempt = await self.repo.create_learning_session(
                goal.id, {"type": "dispute_investigation"}
            )
            self.current_attempt = attempt.id
            self.llm.bind(
                goal_id=str(goal.id), session_id=str(attempt.id), phase="dispute_investigation"
            )
            investigated = await DisputeInvestigator(
                self.repo, self.llm, self.collector
            ).investigate(goal.metadata_json["dispute_id"], attempt)
            passed = investigated.get("outcome") in {
                "keep_old",
                "adopt_new",
                "conditional",
                "already_resolved",
            }
            await self.repo.finish_learning_session(attempt, investigated, {}, passed)
            await self.repo.update_goal_status(
                goal,
                GoalStatus.PASSED if passed else GoalStatus.FAILED,
                max_retry=self.cfg.learning.max_retry,
            )
            return {"status": "passed" if passed else "failed", "goal": goal.title, **investigated}
        skills = await self.repo.selected_skills(goal.title)
        if skills:
            context += "\n已验证研究方法（非执行指令）:\n" + "\n".join(
                f"{s.name}: {s.procedure}" for s in skills
            )
        enabled = [p.strip() for p in runtime_settings().enabled_web_models.split(",") if p.strip()]
        if enabled:
            from autodidact.web_models.service import WebModelService

            service = WebModelService(self.engine)
            for provider in enabled[:2]:
                try:
                    answer = await service.ask(provider, goal.title, goal_id=str(goal.id))
                    context += f"\n外部模型观察（等级0，仅用于查询线索）:\n{answer.response[:4000]}"
                except BudgetExceeded:
                    raise
                except Exception as exc:  # noqa: BLE001 - a web provider must not stop learning.
                    log.warning("网页模型降级：%s", type(exc).__name__)
        plan = await self.planner.plan(goal.title, context)
        learning_session = await self.repo.create_learning_session(
            goal.id, {**plan.model_dump(), "skill_ids": [str(s.id) for s in skills]}
        )
        self.current_attempt = learning_session.id
        self.llm.bind(goal_id=str(goal.id), session_id=str(learning_session.id))
        await self.repo.update_goal_status(goal, GoalStatus.RESEARCHING)

        docs: list[SourceDocument] = []
        seen_urls: set[str] = set()
        seen_hashes: set[str] = set()
        stored_sources: dict[str, models.Source] = {}
        if (goal.metadata_json or {}).get("document_source_id"):
            from uuid import UUID

            source = await self.repo.s.get(
                models.Source, UUID(goal.metadata_json["document_source_id"])
            )
            if source:
                docs.append(
                    SourceDocument(
                        url=source.url,
                        title=source.title or "",
                        source_type=source.source_type,
                        text=(source.extracted_text or "")[
                            : self.cfg.learning.max_chars_per_source
                        ],
                        evidence_level=source.evidence_level,
                        credibility_score=source.credibility_score,
                        lineage_key=(source.metadata_json or {}).get("lineage_key", ""),
                    )
                )
                stored_sources[normalize_url(source.url)] = source
                seen_urls.add(normalize_url(source.url))
        for query in plan.queries:
            try:
                hits = await self.search.search(query, limit=3)
            # A single search provider failure must degrade gracefully.
            except BudgetExceeded:
                raise
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
                except BudgetExceeded:
                    raise
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
            await self.repo.finish_learning_session(
                learning_session, {}, {"failure_reason": "no_readable_sources"}, False
            )
            return {"status": "failed", "goal": goal.title, "reason": "no readable sources"}

        await self.repo.checkpoint(
            learning_session, "sources", source_ids=[str(s.id) for s in stored_sources.values()]
        )
        await self.repo.update_goal_status(goal, GoalStatus.SYNTHESIZING)
        learned = await self.synthesizer.synthesize(goal.title, docs)

        claim_rows = []
        seen_claim_ids = set()
        for draft in learned.claims:
            validation = self.claim_support.validate(draft, stored_sources)
            assessment = await self.support_assessor.assess(draft, validation, stored_sources)
            source_ids = assessment.supported_source_ids
            claim = await self.repo.add_claim(draft, learning_session.id, source_ids)
            await self.repo.record_claim_evidence(claim, assessment.records)
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
                    dispute, created = await self.repo.get_or_create_dispute(
                        belief, claim, relation.score, relation.explanation
                    )
                    disputes += int(created)
                    disputed_claim_ids.add(claim.id)
                    if await self.repo.goal_quota_remaining() > 0:
                        investigation_goal = CandidateGoal(
                            title=f"调查争议 {dispute.id}：{claim.statement[:100]}",
                            description=f"独立调查争议 {dispute.id} 的双方原文与边界条件",
                            source=GoalSource.CONFLICT,
                            importance=0.95,
                            uncertainty=1.0,
                        )
                        queued = await self.repo.add_goal(
                            investigation_goal,
                            goal_score(investigation_goal, self.cfg.learning.exploration_rate),
                            goal.id,
                        )
                        queued.metadata_json = {
                            **(queued.metadata_json or {}),
                            "dispute_id": str(dispute.id),
                        }
                        await self.repo.s.commit()

        await self.repo.checkpoint(
            learning_session, "claims", claim_ids=[str(c.id) for c in claim_rows]
        )
        await self.repo.update_goal_status(goal, GoalStatus.TESTING)
        self.llm.bind(
            goal_id=str(goal.id), session_id=str(learning_session.id), phase="closed_book_answer"
        )
        self.evaluator.judge.bind(
            goal_id=str(goal.id), session_id=str(learning_session.id), phase="closed_book_judge"
        )
        evaluation = await self.evaluator.evaluate(goal.title, learned, docs)
        await self.repo.save_evaluation(goal.id, learning_session.id, evaluation)

        await self.repo.update_goal_status(goal, GoalStatus.REFLECTING)
        reflection = await self.reflector.reflect(goal.title, learned, evaluation)

        beliefs_promoted = 0
        if evaluation.passed:
            for claim in claim_rows:
                source_by_id = {str(source.id): source for source in stored_sources.values()}
                evidence_sources = [
                    EvidenceSource(
                        source_id=source_id,
                        lineage_key=(source_by_id[source_id].metadata_json or {}).get(
                            "lineage_key", ""
                        ),
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
                    await self.repo.set_belief_embedding(
                        belief, vector, fingerprint=self.embedding.fingerprint
                    )
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
                            excerpt=await self.repo.supported_excerpt(claim.id, source.id),
                        )
            await self.repo.update_goal_status(goal, GoalStatus.PASSED, evaluation.score)
        else:
            await self.repo.update_goal_status(
                goal,
                GoalStatus.FAILED,
                evaluation.score,
                max_retry=self.cfg.learning.max_retry,
            )

        await self.repo.record_skill_outcome([s.id for s in skills], evaluation.passed)
        await self.repo.checkpoint(
            learning_session, "completed", evaluation=evaluation.model_dump()
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
        follow_up_goals_created = 0
        quota = await self.repo.goal_quota_remaining()
        for fg in follow_ups[: min(self.cfg.learning.max_follow_up_goals_per_cycle, quota)]:
            _, created = await self.repo.add_goal_if_absent(
                fg, goal_score(fg, self.cfg.learning.exploration_rate), goal.id
            )
            follow_up_goals_created += int(created)

        await self.repo.finish_learning_session(
            learning_session, learned.model_dump(), reflection.model_dump(), evaluation.passed
        )
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
            if result["status"] in {"budget_exhausted", "busy", "idle"}:
                break
            await asyncio.sleep(self.cfg.learning.cycle_sleep_seconds)
