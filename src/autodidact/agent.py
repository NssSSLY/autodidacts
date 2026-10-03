# 文件职责：组织持久学习主循环：召回、研究、核证、争议、评估、晋升和续跑，模型不直接决定真相。
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from uuid import UUID

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
from autodidact.knowledge.belief_review import REVIEW_PROTOCOL, BeliefReviewer, queue_stale_reviews
from autodidact.knowledge.bibliography import BIBLIOGRAPHY_PROTOCOL
from autodidact.knowledge.claim_support import ClaimSupportValidator
from autodidact.knowledge.conflicts import ConflictDetector
from autodidact.knowledge.content_quality import (
    CONTENT_QUALITY_PROTOCOL,
    effective_source_assessment,
)
from autodidact.knowledge.decomposition import (
    DECOMPOSITION_PROTOCOL,
    ClaimDecomposer,
    eligible_claim,
)
from autodidact.knowledge.investigation import DisputeInvestigator
from autodidact.knowledge.promotion import BeliefPromotionPolicy
from autodidact.knowledge.sources import normalize_url
from autodidact.knowledge.support_assessment import SUPPORT_PROTOCOL, ClaimSupportAssessor
from autodidact.learning.evaluator import Evaluator
from autodidact.learning.ground_truth import CLOSED_BOOK_SCORING, CLOSED_BOOK_WEIGHTS, digest
from autodidact.learning.planner import Planner
from autodidact.learning.reflection import Reflector
from autodidact.learning.synthesizer import Synthesizer
from autodidact.memory import MemoryConsolidator
from autodidact.normalization import normalize_text_key
from autodidact.repository import Repository
from autodidact.research import ResearchCollector
from autodidact.resume import (
    PROTOCOL,
    DurableSteps,
    ResumableLLM,
    ResumableReader,
    ResumableResearch,
    ResumableSearch,
)
from autodidact.retrieval import HybridRetriever
from autodidact.runtime import (
    BudgetedSearch,
    BudgetExceeded,
    ControllerBusy,
    OperationBudget,
    controller_lock,
)
from autodidact.schemas import CandidateGoal, SearchQueryPlan, SourceDocument
from autodidact.tools.reader import WebReader
from autodidact.tools.search import SearchProvider, build_search_provider

log = logging.getLogger(__name__)


class AutonomousLearner:
    # 功能：组装仓库、审计/续跑模型、预算化搜索阅读、评估与晋升策略，不在构造时执行学习。
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
        self.steps = DurableSteps(self.engine)
        llm = ResumableLLM(llm, self.steps, "learner")
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
        self.search = ResumableSearch(
            BudgetedSearch(search or build_search_provider(), OperationBudget(self.engine)),
            self.steps,
        )
        self.reader = ResumableReader(WebReader().with_budget(self.engine), self.steps)
        self.collector = ResumableResearch(
            ResearchCollector(self.search, self.reader, repo=self.repo), self.steps
        )
        self.evaluator = Evaluator(
            llm,
            judge=ResumableLLM(build_judge(self.engine), self.steps, "judge"),
            verification_fetcher=self.collector.fetch,
        )
        self.current_goal = None
        self.current_attempt = None
        self.claim_support = ClaimSupportValidator()

        self.support_assessor = ClaimSupportAssessor(llm)
        self.decomposer = ClaimDecomposer(llm)

    # 功能：混合召回与候选主张相关的非撤回旧信念，作为冲突比较对象，并缓存本尝试的召回结果。
    async def _beliefs_for_claim(self, statement: str) -> list[models.Belief]:
        # 功能：查询主张相关的信念实体，转换为可持久重放的信念 ID 列表。
        async def recall():
            hits = await HybridRetriever(self.repo, self.embedding).retrieve(
                statement, kinds=("belief",), limit=40
            )
            return [str(hit.row.id) for hit in hits]

        ids = await self.steps.run("belief_recall", [statement], recall)
        return [row for i in ids if (row := await self.repo.s.get(models.Belief, UUID(i)))]

    # 功能：计算模型、嵌入、主张核验协议、策略和目标签名，防止在不兼容配置下继续旧尝试。
    def _resume_signature(self, goal):
        settings = runtime_settings()
        value = {
            "protocol": PROTOCOL,
            "claim_support_protocol": SUPPORT_PROTOCOL,
            "decomposition_protocol": DECOMPOSITION_PROTOCOL,
            "content_quality_protocol": CONTENT_QUALITY_PROTOCOL,
            "bibliography_protocol": BIBLIOGRAPHY_PROTOCOL,
            "belief_review_protocol": REVIEW_PROTOCOL,
            "evaluation_scoring_protocol": CLOSED_BOOK_SCORING,
            "evaluation_formula_sha256": digest(CLOSED_BOOK_WEIGHTS),
            "config": self.cfg.model_dump(mode="json"),
            "learner": [self.llm.provider_name, self.llm.model_name, settings.llm_base_url],
            "judge": [
                self.evaluator.judge.provider_name,
                self.evaluator.judge.model_name,
                settings.judge_llm_base_url,
            ],
            "embedding": self.embedding.fingerprint,
            "sampling": [settings.llm_temperature, settings.llm_max_output_tokens],
            "research": [
                settings.search_provider,
                settings.brave_search_base_url,
                settings.enabled_web_models,
                settings.web_models_config,
            ],
            "goal": [goal.title, goal.description, goal.metadata_json],
        }
        return hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
        ).hexdigest()

    # 功能：收集认知上下文、已验证技能和可选网页模型建议，返回供本次规划冻结的输入。
    async def _planning_inputs(self, goal):
        context = await self.repo.epistemic_context()
        hits = await HybridRetriever(self.repo, self.embedding).retrieve(goal.title, limit=15)
        context += "\n相关认知条目（Claim是候选，Goal是意图，均不是真值）:\n" + json.dumps(
            [h.as_dict() for h in hits if h.row.id != goal.id], ensure_ascii=False
        )
        skills = await self.repo.selected_skills(goal.title)
        if skills:
            context += "\n已验证研究方法（非执行指令）:\n" + "\n".join(
                f"{skill.name}: {skill.procedure}" for skill in skills
            )
        enabled = [p.strip() for p in runtime_settings().enabled_web_models.split(",") if p.strip()]
        if enabled:
            from autodidact.web_models.service import WebModelService

            service = WebModelService(self.engine)
            for provider in enabled[:2]:
                try:
                    # 功能：请求一个已启用网页模型的规划建议；它仍是等级 0 观察而非事实证据。
                    async def ask(provider=provider):
                        answer = await service.ask(provider, goal.title, goal_id=str(goal.id))
                        return answer.response[:4000]

                    response = await self.steps.run("web_model", [provider, goal.title], ask)
                    context += f"\n外部模型观察（等级0，仅用于查询线索）:\n{response}"
                except BudgetExceeded:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.warning("网页模型降级：%s", type(exc).__name__)
        return {"context": context, "skill_ids": [str(skill.id) for skill in skills]}

    # 功能：尽力同步实体关键词和向量索引，索引/嵌入失败不使核心认知写入失效。
    async def _index_entity(self, kind, row):
        try:
            await self.repo.index.embed_entity(kind, row, self.embedding)
        except Exception as exc:  # noqa: BLE001 - optional index cannot block epistemic writes.
            log.debug("语义索引降级：%s", type(exc).__name__)

    # 功能：获取或创建持久智能体；目标池为空时添加种子目标，不重复初始化已有目标。
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

    # 功能：目标池为空时先排过旧信念复核/争议调查，再依据使命提出目标；均遵守每日配额。
    async def ensure_goal_pool(self) -> None:
        if await self.repo.next_goal(self.cfg.learning.max_retry) is not None:
            return
        await queue_stale_reviews(self.repo)
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

    # 功能：执行原文复核旁路并记录目标终态；观察成功不代表保持测试通过或旧结论判真。
    async def _review_goal(self, goal, attempt, resuming):
        self.llm.bind(goal_id=str(goal.id), session_id=str(attempt.id), phase="belief_review")
        result = await BeliefReviewer(self.repo, self.llm, self.collector).investigate(
            goal.metadata_json["belief_review_id"], attempt
        )
        completed = result["outcome"] in {"observed", "skipped_retracted"}
        await self.repo.update_goal_status(
            goal,
            GoalStatus.PASSED if completed else GoalStatus.FAILED,
            max_retry=self.cfg.learning.max_retry,
            attempt=attempt,
        )
        await self.repo.finish_learning_session(attempt, result, {}, completed)
        return {
            "status": "reviewed" if completed else "failed",
            "goal": goal.title,
            "session_id": str(attempt.id),
            "resumed": resuming,
            **result,
        }

    # 功能：获取控制器互斥后运行一轮学习，区分预算、取消和一般异常并保留恢复状态。
    async def run_cycle(self) -> dict:
        try:
            async with controller_lock(self.engine):
                await self.repo.recover_interrupted()
                await self.bootstrap()
                self.current_goal = self.current_attempt = None
                self.steps.bind(None)
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
                self.steps.bind(None)
                if result["status"] == "resume_incompatible":
                    return result
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

    # 功能：记录失败或预算暂停；支持续跑的会话保留工作项，旧会话沿有界重试路径收尾。
    async def _finish_failure(self, reason, budget_stop=False):
        await self.repo.s.rollback()
        if self.current_attempt:
            attempt = await self.repo.s.get(models.LearningSession, self.current_attempt)
            if attempt and (attempt.plan or {}).get("resume_protocol") == PROTOCOL:
                failures = (attempt.result or {}).get("resume_failures", 0) + int(not budget_stop)
                attempt.result = {
                    **(attempt.result or {}),
                    "resume_error": reason,
                    "resume_failures": failures,
                    "paused_for_budget": budget_stop,
                }
                if failures >= self.cfg.learning.max_retry:
                    goal = await self.repo.s.get(models.Goal, attempt.goal_id)
                    goal.status = GoalStatus.BLOCKED
                    goal.metadata_json = {
                        **(goal.metadata_json or {}),
                        "blocked_resume_session_id": str(attempt.id),
                    }
                await self.repo.s.commit()
                return
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

    # 功能：选取可恢复尝试或新目标，逐阶段研究、核证、评估和谨慎晋升，并保存结果与后续任务。
    async def _run_cycle(self) -> dict:
        learning_session = await self.repo.resumable_attempt()
        resuming = learning_session is not None
        if learning_session:
            goal = await self.repo.s.get(models.Goal, learning_session.goal_id)
            if learning_session.plan.get("resume_signature") != self._resume_signature(goal):
                return {
                    "status": "resume_incompatible",
                    "session_id": str(learning_session.id),
                    "reason": "模型、学习策略、主张核验协议或目标已改变；请恢复原版本/配置或人工重启，不能混用旧结果",
                }
        else:
            await self.ensure_goal_pool()
            goal = await self.repo.next_goal(self.cfg.learning.max_retry)
            if not goal:
                return {"status": "idle", "reason": "no goals"}
            learning_session = await self.repo.create_learning_session(
                goal.id,
                {"resume_protocol": PROTOCOL, "resume_signature": self._resume_signature(goal)},
            )
        self.current_goal, self.current_attempt = goal.id, learning_session.id
        self.steps.bind(learning_session.id)
        self.llm.bind(goal_id=str(goal.id), session_id=str(learning_session.id), phase="planning")
        self.evaluator.judge.bind(
            goal_id=str(goal.id), session_id=str(learning_session.id), phase="closed_book_judge"
        )
        log.info(
            "%s goal: %s / session %s",
            "Resuming" if resuming else "Starting",
            goal.title,
            learning_session.id,
        )
        if not resuming:
            await self.repo.update_goal_status(goal, GoalStatus.PLANNED)

        if (goal.metadata_json or {}).get("belief_review_id"):
            return await self._review_goal(goal, learning_session, resuming)
        if (goal.metadata_json or {}).get("dispute_id"):
            attempt = learning_session
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
            await self.repo.update_goal_status(
                goal,
                GoalStatus.PASSED if passed else GoalStatus.FAILED,
                max_retry=self.cfg.learning.max_retry,
                attempt=learning_session,
            )
            await self.repo.finish_learning_session(attempt, investigated, {}, passed)
            return {
                "status": "passed" if passed else "failed",
                "goal": goal.title,
                "session_id": str(attempt.id),
                "resumed": resuming,
                **investigated,
            }
        inputs = await self.steps.run(
            "planning_inputs", [goal.title], lambda: self._planning_inputs(goal)
        )
        skills = [
            skill
            for i in inputs["skill_ids"]
            if (skill := await self.repo.s.get(models.Skill, UUID(i)))
        ]
        if learning_session.plan.get("queries"):
            plan = SearchQueryPlan.model_validate(learning_session.plan)
        else:
            plan = await self.planner.plan(goal.title, inputs["context"])
            learning_session.plan = {
                **learning_session.plan,
                **plan.model_dump(),
                "skill_ids": inputs["skill_ids"],
            }
            await self.repo.s.commit()
        await self._index_entity("goal", goal)
        await self.repo.update_goal_status(goal, GoalStatus.RESEARCHING, attempt=learning_session)

        docs: list[SourceDocument] = []
        seen_urls: set[str] = set()
        seen_hashes: set[str] = set()
        stored_sources: dict[str, models.Source] = {}
        saved_docs = (learning_session.result or {}).get("source_documents")
        if saved_docs:
            docs = [SourceDocument.model_validate(d) for d in saved_docs]
            for doc in docs:
                source = await self.repo.upsert_source(doc, self.reader.hash_text(doc.text))
                stored_sources[normalize_url(doc.url)] = source
        else:
            if (goal.metadata_json or {}).get("document_source_id"):
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
                            metadata=source.metadata_json or {},
                            authors=(source.metadata_json or {})
                            .get("bibliography", {})
                            .get("authors", []),
                            published_date=(source.metadata_json or {})
                            .get("bibliography", {})
                            .get("published_date", ""),
                            research_identifiers=(source.metadata_json or {})
                            .get("bibliography", {})
                            .get("research_identifiers", []),
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
                attempt=learning_session,
            )
            await self.repo.finish_learning_session(
                learning_session, {}, {"failure_reason": "no_readable_sources"}, False
            )
            return {"status": "failed", "goal": goal.title, "reason": "no readable sources"}

        await self.repo.checkpoint(
            learning_session,
            "sources",
            source_ids=[str(s.id) for s in stored_sources.values()],
            source_documents=[doc.model_dump(mode="json") for doc in docs],
        )
        await self.repo.update_goal_status(goal, GoalStatus.SYNTHESIZING, attempt=learning_session)
        self.llm.bind(goal_id=str(goal.id), session_id=str(learning_session.id), phase="synthesis")
        learned = await self.synthesizer.synthesize(goal.title, docs)
        await self.repo.checkpoint(
            learning_session, "synthesis", learned=learned.model_dump(mode="json")
        )
        self.llm.bind(
            goal_id=str(goal.id), session_id=str(learning_session.id), phase="claim_support"
        )

        claim_rows = []
        atomic_drafts = []
        seen_claim_ids = set()
        for draft in learned.claims:
            for claim, candidate in await self.decomposer.persist(
                self.repo, draft, learning_session.id
            ):
                validation = self.claim_support.validate(candidate, stored_sources)
                assessment = await self.support_assessor.assess(
                    candidate, validation, stored_sources
                )
                await self.repo.record_claim_evidence(claim, assessment.records)
                await self._index_entity("claim", claim)
                if claim.id not in seen_claim_ids:
                    claim_rows.append(claim)
                    seen_claim_ids.add(claim.id)
                    if eligible_claim(claim):
                        atomic_drafts.append(candidate)

        # Conflict detection happens before belief promotion. New model output cannot overwrite old beliefs.
        self.llm.bind(
            goal_id=str(goal.id), session_id=str(learning_session.id), phase="conflict_detection"
        )
        disputes = 0
        disputed_claim_ids = set()
        reviewed_belief_ids = set()
        reviewer = BeliefReviewer(self.repo, self.llm)
        review_citations = [citation for draft in learned.claims for citation in draft.citations]
        for claim in claim_rows:
            old_beliefs = await self._beliefs_for_claim(claim.statement)
            for belief in old_beliefs:
                if belief.id not in reviewed_belief_ids and len(reviewed_belief_ids) < 10:
                    await reviewer.observe(
                        belief, stored_sources, review_citations, learning_session, goal.id
                    )
                    reviewed_belief_ids.add(belief.id)
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
        await self.repo.update_goal_status(goal, GoalStatus.TESTING, attempt=learning_session)
        self.llm.bind(
            goal_id=str(goal.id), session_id=str(learning_session.id), phase="closed_book_answer"
        )
        self.evaluator.judge.bind(
            goal_id=str(goal.id), session_id=str(learning_session.id), phase="closed_book_judge"
        )
        evaluation = await self.evaluator.evaluate(
            goal.title, learned.model_copy(update={"claims": atomic_drafts}), docs
        )
        await self.repo.save_evaluation(goal.id, learning_session.id, evaluation)

        await self.repo.update_goal_status(goal, GoalStatus.REFLECTING, attempt=learning_session)
        self.llm.bind(goal_id=str(goal.id), session_id=str(learning_session.id), phase="reflection")
        reflection = await self.reflector.reflect(goal.title, learned, evaluation)

        beliefs_promoted = 0
        if evaluation.passed:
            for claim in claim_rows:
                if not eligible_claim(claim):
                    continue
                source_by_id = {str(source.id): source for source in stored_sources.values()}
                evidence_sources = await self.repo.lineage.evidence_sources(
                    [
                        source_by_id[source_id]
                        for source_id in claim.source_ids
                        if source_id in source_by_id
                    ]
                )
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
                    if (
                        belief.embedding is None
                        or (belief.metadata_json or {}).get("embedding_fingerprint")
                        != self.embedding.fingerprint
                    ):
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
                        quality = effective_source_assessment(source)
                        if quality.evidence_level <= 0:
                            continue
                        await self.repo.add_evidence(
                            belief.id,
                            source.id,
                            quality.evidence_level,
                            strength=min(quality.credibility_score, claim.confidence),
                            excerpt=await self.repo.supported_excerpt(claim.id, source.id),
                        )
            await self.repo.update_goal_status(
                goal, GoalStatus.PASSED, evaluation.score, attempt=learning_session
            )
        else:
            await self.repo.update_goal_status(
                goal,
                GoalStatus.FAILED,
                evaluation.score,
                max_retry=self.cfg.learning.max_retry,
                attempt=learning_session,
            )

        await self.repo.record_skill_outcome(
            [s.id for s in skills], evaluation.passed, learning_session
        )
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
            "session_id": str(learning_session.id),
            "resumed": resuming,
            "score": evaluation.score,
            "sources": len(docs),
            "claims": len(claim_rows),
            "beliefs_promoted": beliefs_promoted,
            "claims_with_anchored_evidence": sum(bool(claim.source_ids) for claim in claim_rows),
            "disputes_created": disputes,
            "follow_up_goals": follow_up_goals_created,
        }

    # 功能：按配置或参数运行有限轮次并间隔等待；不是操作系统常驻服务或分布式任务队列。
    async def run_daemon(self, max_cycles: int | None = None) -> None:
        cycles = max_cycles or self.cfg.learning.max_cycles_per_process
        for _ in range(cycles):
            result = await self.run_cycle()
            log.info("Cycle result: %s", result)
            if result["status"] in {"budget_exhausted", "busy", "idle", "resume_incompatible"}:
                break
            await asyncio.sleep(self.cfg.learning.cycle_sleep_seconds)
