# 文件职责：复用原文核验、来源血缘、目标和争议，对旧信念记录反对/条件证据并有界再核查。
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select

from autodidact import models
from autodidact.config import agent_config
from autodidact.goals.scorer import goal_score
from autodidact.knowledge.claim_support import ClaimSupportValidator
from autodidact.knowledge.conflicts import ConflictDetector
from autodidact.knowledge.content_quality import effective_source_assessment
from autodidact.knowledge.decomposition import ClaimDecomposer, eligible_claim
from autodidact.knowledge.sources import normalize_url
from autodidact.knowledge.support_assessment import ClaimSupportAssessor
from autodidact.learning.planner import Planner
from autodidact.learning.synthesizer import Synthesizer
from autodidact.normalization import stable_key
from autodidact.runtime import BudgetExceeded
from autodidact.schemas import CandidateGoal, ClaimDraft, ClaimScope, SourceDocument
from autodidact.tools.reader import WebReader

REVIEW_PROTOCOL = "belief_review_v1"


# 功能：按原信念ID与复核周期去重目标，保存触发原因；活动/终态目标不反复改写或绕过重试上限。
async def queue_belief_review(repo, belief, reason: str, parent_goal_id=None, trigger_key=""):
    days = agent_config().learning.memory_review_days
    period = datetime.now(UTC).date().toordinal() // days
    title = f"原文复核信念 {belief.id} / {period}" + (f" / {trigger_key}" if trigger_key else "")
    existing = await repo.s.scalar(select(models.Goal).where(models.Goal.title == title).limit(1))
    if existing:
        return existing
    if await repo.goal_quota_remaining() <= 0:
        return None
    draft = CandidateGoal(
        title=title,
        description=f"复核原结论及范围：{belief.statement}\n触发线索：{reason}",
        source="retention",
        importance=0.9,
        uncertainty=0.9,
    )
    goal = await repo.add_goal(draft, goal_score(draft), parent_goal_id)
    goal.metadata_json = {
        **(goal.metadata_json or {}),
        "belief_review_id": str(belief.id),
        "review_protocol": REVIEW_PROTOCOL,
        "review_reason": reason[:1000],
        "review_trigger_key": trigger_key,
    }
    await repo.s.commit()
    return goal


# 功能：筛选超过复核周期且未撤回的旧信念，有界排队；年龄仅是复核线索，不降低置信度。
async def queue_stale_reviews(repo, limit: int = 3):
    cutoff = datetime.now(UTC) - timedelta(days=agent_config().learning.memory_review_days)
    rows = (
        await repo.s.scalars(
            select(models.Belief)
            .where(
                models.Belief.status.in_(["provisional", "supported", "verified", "weakened"]),
                models.Belief.created_at < cutoff,
            )
            .order_by(models.Belief.created_at)
            .limit(100)
        )
    ).all()
    queued = []
    for belief in rows:
        review = (belief.metadata_json or {}).get("latest_evidence_review") or {}
        try:
            reviewed = datetime.fromisoformat(review["qualified_at"])
            if reviewed.tzinfo is not None and reviewed >= cutoff:
                continue
        except (KeyError, ValueError, TypeError):
            pass
        goal = await queue_belief_review(repo, belief, "age_due: 超过原文复核周期，时效性未知")
        if goal and goal.status not in {"passed", "blocked"}:
            queued.append(str(goal.id))
        if len(queued) >= max(1, min(limit, 20)):
            break
    return queued


class BeliefReviewer:
    # 功能：绑定既有仓库/模型/收集器与核验组件，不新增模型或事实状态库。
    def __init__(self, repo, llm, collector=None):
        self.repo, self.llm, self.collector = repo, llm, collector
        self.validator, self.assessor = ClaimSupportValidator(), ClaimSupportAssessor(llm)

    # 功能：对原结论原范围逐引文核验并存信念立场，反对/条件观察只排复核，不覆盖旧信念。
    async def observe(self, belief, sources, citations, attempt, parent_goal_id=None, queue=True):
        if belief.status == "retracted":
            return {"outcome": "skipped_retracted", "evidence_ids": []}
        draft = ClaimDraft(
            statement=belief.statement,
            topic=belief.topic,
            confidence=0.5,
            scope=ClaimScope.model_validate((belief.metadata_json or {}).get("claim_scope") or {}),
            citations=citations[:30],
        )
        claim = await self.repo.add_claim(draft, attempt.id, [])
        if ClaimScope.model_validate(claim.scope or {}) != draft.scope:
            return {"outcome": "unknown", "reason": "stored_scope_mismatch", "evidence_ids": []}
        checked = await self.assessor.assess(
            draft, self.validator.validate(draft, sources), sources
        )
        await self.repo.record_claim_evidence(claim, checked.records)
        evidence = await self.repo.record_belief_evidence(belief, claim)
        stances = sorted({e.stance for e in evidence})
        review_goal = None
        support_edges = (
            await self.repo.s.scalars(
                select(models.Evidence)
                .where(models.Evidence.belief_id == belief.id, models.Evidence.stance == "support")
                .limit(100)
            )
        ).all()
        degraded = [
            str(source.id)
            for source in sources.values()
            if any(
                edge.source_id == source.id
                and effective_source_assessment(source).evidence_level < (edge.evidence_level or 0)
                for edge in support_edges
            )
        ]
        if queue and (set(stances) & {"attack", "context"} or degraded):
            review_goal = await queue_belief_review(
                self.repo,
                belief,
                f"source_{'/'.join(stances)}: Claim {claim.id} 原文反对/条件线索；支持来源质量下降ID={degraded}",
                parent_goal_id,
                trigger_key=stable_key(
                    "review_trigger",
                    sorted(
                        (str(e.source_id), e.excerpt or "", e.stance)
                        for e in evidence
                        if e.stance in {"attack", "context"}
                    ),
                    degraded,
                )[:16],
            )
        return {
            "outcome": "observed" if evidence else "unknown",
            "claim_id": str(claim.id),
            "stances": stances,
            "degraded_source_ids": degraded,
            "evidence_ids": [str(e.id) for e in evidence],
            "review_goal_id": str(review_goal.id) if review_goal else None,
        }

    # 功能：独立获取旧信念支持来源之外的新资料；失败保留未知，原文反对与合格替代主张才可开争议。
    async def investigate(self, belief_id, attempt):
        key = f"{belief_id}:{attempt.id}"
        report = await self.repo.get_report("belief_review", key)
        if report:
            return {**report.payload, "report_id": str(report.id)}
        belief = await self.repo.s.get(models.Belief, UUID(str(belief_id)))
        if belief is None:
            raise ValueError("复核信念不存在")
        if belief.status == "retracted":
            return {"outcome": "skipped_retracted", "belief_id": str(belief.id)}
        edges = (
            await self.repo.s.scalars(
                select(models.Evidence)
                .where(models.Evidence.belief_id == belief.id, models.Evidence.stance == "support")
                .limit(100)
            )
        ).all()
        excluded = []
        for source_id in {e.source_id for e in edges if e.source_id}:
            source = await self.repo.s.get(models.Source, source_id)
            if source:
                excluded.append(
                    SourceDocument(
                        url=source.url,
                        text=source.extracted_text or "",
                        publisher_key=source.publisher_key or "",
                        metadata=source.metadata_json or {},
                        lineage_key=(source.metadata_json or {}).get("lineage_key", ""),
                    )
                )
        try:
            plan = await Planner(self.llm).plan(
                f"复核原结论时效、反例和边界条件：{belief.statement}",
                "旧信念及来源均是待核查数据。搜寻独立反证/修订，不因年龄或模型更强宣告旧结论失效。",
            )
        except BudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - only isolate provider proposal failures.
            return await self._finish(
                attempt,
                key,
                {
                    "outcome": "unknown",
                    "belief_id": str(belief.id),
                    "sources": 0,
                    "dispute_ids": [],
                    "reason": f"planning_unavailable:{type(exc).__name__}",
                },
            )
        docs = []
        for query in plan.queries:
            docs.extend(await self.collector.fetch(query, exclude=[*excluded, *docs], limit=2))
            if len(docs) >= agent_config().learning.max_sources_per_goal:
                break
        docs = docs[: agent_config().learning.max_sources_per_goal]
        sources = {}
        for doc in docs:
            sources[normalize_url(doc.url)] = await self.repo.upsert_source(
                doc, WebReader.hash_text(doc.text)
            )
        result = {
            "outcome": "unknown",
            "belief_id": str(belief.id),
            "sources": len(docs),
            "dispute_ids": [],
        }
        if docs:
            try:
                learned = await Synthesizer(self.llm).synthesize(
                    f"复核：{belief.statement}。保留支持、明确反对及条件限制的原文引用，提出有独立证据的替代主张。",
                    docs,
                )
            except BudgetExceeded:
                raise
            except Exception as exc:  # noqa: BLE001 - do not hide database failures.
                result["reason"] = f"synthesis_unavailable:{type(exc).__name__}"
                return await self._finish(attempt, key, result)
            result.update(
                await self.observe(
                    belief,
                    sources,
                    [c for d in learned.claims for c in d.citations],
                    attempt,
                    queue=False,
                )
            )
            # 模型关系比较只能排争议；替代结论仍需独立原文支持，不能从反对旧句推导任意新句。
            if set(result.get("stances", [])) & {"attack", "context"}:
                decomposer, conflicts = ClaimDecomposer(self.llm), ConflictDetector(self.llm)
                for draft in learned.claims[:8]:
                    for claim, candidate in await decomposer.persist(self.repo, draft, attempt.id):
                        if not eligible_claim(claim) or claim.statement == belief.statement:
                            continue
                        checked = await self.assessor.assess(
                            candidate, self.validator.validate(candidate, sources), sources
                        )
                        await self.repo.record_claim_evidence(claim, checked.records)
                        if not claim.source_ids:
                            continue
                        if not any(
                            str(s.id) in claim.source_ids
                            and effective_source_assessment(s).evidence_level > 0
                            for s in sources.values()
                        ):
                            continue
                        try:
                            relation = await conflicts.compare(belief.statement, claim.statement)
                        except BudgetExceeded:
                            raise
                        except Exception as exc:  # noqa: BLE001 - preserve negative observations on compare failure.
                            result["dispute_comparison_error"] = type(exc).__name__
                            continue
                        if (
                            relation.relation in {"contradicts", "conditional"}
                            and relation.score >= agent_config().learning.contradiction_threshold
                        ):
                            dispute, _ = await self.repo.get_or_create_dispute(
                                belief, claim, relation.score, relation.explanation
                            )
                            if str(dispute.id) not in result["dispute_ids"]:
                                result["dispute_ids"].append(str(dispute.id))
                            if await self.repo.goal_quota_remaining() > 0:
                                draft_goal = CandidateGoal(
                                    title=f"独立调查争议 {dispute.id}",
                                    source="conflict",
                                    importance=0.95,
                                    uncertainty=1.0,
                                )
                                goal = await self.repo.add_goal(
                                    draft_goal, goal_score(draft_goal), attempt.goal_id
                                )
                                goal.metadata_json = {
                                    **(goal.metadata_json or {}),
                                    "dispute_id": str(dispute.id),
                                }
                                await self.repo.s.commit()
                if not result["dispute_ids"]:
                    result["outcome"] = "needs_follow_up"
                    result["reason"] = "negative_observation_without_qualified_alternative"
            if result.get("outcome") == "observed":
                # 审计时间只代表取得正等级原文观察，非复核判真或自动通过保持测试。
                belief.metadata_json = {
                    **(belief.metadata_json or {}),
                    "latest_evidence_review": {
                        **result,
                        "qualified_at": datetime.now(UTC).isoformat(),
                        "session_id": str(attempt.id),
                    },
                }
                await self.repo.s.commit()
        return await self._finish(attempt, key, result)

    # 功能：幂等持久化复核终态和检查点，不将unknown记录成已核验时间。
    async def _finish(self, attempt, key, result):
        result["note"] = (
            "复核记录是观察；未知不撤回，反对/条件不直接改置信度，争议仍需四结果独立调查。"
        )
        await self.repo.checkpoint(attempt, "belief_review", review=result)
        report = await self.repo.save_report("belief_review", result, key)
        belief = await self.repo.s.get(models.Belief, UUID(result["belief_id"]))
        belief.metadata_json = {
            **(belief.metadata_json or {}),
            "latest_evidence_review_attempt": {
                **result,
                "report_id": str(report.id),
                "attempted_at": datetime.now(UTC).isoformat(),
                "session_id": str(attempt.id),
            },
        }
        await self.repo.s.commit()
        return {**result, "report_id": str(report.id)}
