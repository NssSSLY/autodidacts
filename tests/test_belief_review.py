# 文件职责：以有界内存会话/模型/研究替身验证旧信念复核、立场幂等和失败不覆盖；不连接真实提供方。
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_claim_scope import _MemorySession, _Result

from autodidact import models
from autodidact.knowledge.belief_review import (
    BeliefReviewer,
    queue_belief_review,
    queue_stale_reviews,
)
from autodidact.knowledge.claim_support import ClaimSupportValidator
from autodidact.knowledge.decomposition import DecompositionProposal, DecompositionReview
from autodidact.knowledge.support_assessment import ClaimSupportAssessor, SupportVerdict
from autodidact.repository import Repository
from autodidact.schemas import (
    ClaimCitation,
    ClaimDraft,
    ClaimScope,
    ContradictionResult,
    LearningResult,
    SearchQueryPlan,
    SourceDocument,
)

URL = "https://independent.example/research"
QUOTE = "Observed drift grows over time even under the stated operating conditions."
STATEMENT = "The system has no drift under the stated operating conditions."


class Rows(_Result):
    # 功能：提供SQLAlchemy标量结果的all接口。
    def all(self):
        return self.rows


class Session(_MemorySession):
    # 功能：提供无真实事务的savepoint替身，竞争语义另由隔离PostgreSQL测试验证。
    @asynccontextmanager
    async def begin_nested(self):
        yield

    # 功能：按绑定等值/集合/日期/主键筛选模型记录，模拟复核查询而非真实事务。
    async def execute(self, statement, *args):
        if not getattr(statement, "is_select", False):
            return Rows([])
        model = statement.column_descriptions[0]["entity"]
        params = statement.compile().params
        rows = [r for r in self.rows if isinstance(r, model)]
        for key, value in params.items():
            field = key.rsplit("_", 1)[0]
            if not hasattr(model, field):
                continue
            if field == "created_at" and isinstance(value, datetime):
                rows = [r for r in rows if r.created_at < value]
            elif isinstance(value, list):
                rows = [r for r in rows if getattr(r, field) in value]
            else:
                rows = [r for r in rows if getattr(r, field) == value]
        return Rows(rows)

    # 功能：返回有all接口的标量查询结果。
    async def scalars(self, statement):
        return await self.execute(statement)

    # 功能：返回查询第一实体，供审计去重使用。
    async def scalar(self, statement):
        return (await self.execute(statement)).scalar_one_or_none()


class Judge:
    provider_name, model_name = "fake", "review"

    # 功能：设置确定性立场观察或服务失败，不生成事实。
    def __init__(self, relation="contradicts", coverage="complete", fail=False):
        self.relation, self.coverage, self.fail = relation, coverage, fail

    # 功能：按协议返回计划、原文引用和范围判断；故障时抛出超时。
    async def structured(self, system, user, schema):
        if self.fail:
            raise TimeoutError("test")
        if schema is SupportVerdict:
            return SupportVerdict(
                relation=self.relation, reason="原文否定或限制原结论", scope_coverage=self.coverage
            )
        if schema is SearchQueryPlan:
            return SearchQueryPlan(queries=["independent counterexample"])
        if schema is LearningResult:
            return LearningResult(
                claims=[
                    ClaimDraft(
                        statement=STATEMENT,
                        topic="drift",
                        confidence=0.5,
                        citations=[ClaimCitation(source_url=URL, excerpt=QUOTE)],
                    )
                ]
            )
        raise AssertionError(schema)


# 功能：建立未修改的高置信旧信念、真实摘录与原支持边，供端到端替身测试。
def fixture():
    session = Session()
    source = models.Source(
        id=uuid4(),
        url=URL,
        extracted_text=QUOTE + " Background context." * 30,
        evidence_level=1,
        credibility_score=0.3,
        source_type="web",
        metadata_json={},
    )
    belief = models.Belief(
        id=uuid4(),
        topic="drift",
        statement=STATEMENT,
        status="verified",
        confidence=0.9,
        metadata_json={},
        created_at=datetime.now(UTC) - timedelta(days=90),
    )
    old = models.Evidence(
        id=uuid4(),
        belief_id=belief.id,
        source_id=source.id,
        kind="web_source",
        stance="support",
        excerpt="历史摘录",
        assessment={},
    )
    session.rows.extend([source, belief, old])
    repo = Repository(session)

    # 功能：为替身提供确定每日目标额度，不操作真实预算。
    async def quota():
        return 10

    repo.goal_quota_remaining = quota
    return session, repo, source, belief, old


# 功能：验证反对/条件立场关联原文并幂等保存，复核排队而不改变原信念和历史支持。
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "relation,stance",
    [("contradicts", "attack"), ("conditional", "context"), ("supports", "support")],
)
async def test_observation_persists_stance_without_overwriting(relation, stance):
    session, repo, source, belief, old = fixture()
    reviewer = BeliefReviewer(repo, Judge(relation))
    attempt = SimpleNamespace(id=uuid4())
    citations = [ClaimCitation(source_url=URL, excerpt=QUOTE)]
    first = await reviewer.observe(belief, {URL: source}, citations, attempt)
    second = await reviewer.observe(belief, {URL: source}, citations, attempt)
    assert first["evidence_ids"] == second["evidence_ids"] and first["outcome"] == "observed"
    records = [r for r in session.rows if isinstance(r, models.Evidence) and r is not old]
    assert len(records) == 1 and records[0].stance == stance
    assert records[0].excerpt == QUOTE and records[0].claim_evidence_id is not None
    assert records[0].assessment["scope"] == ClaimScope().model_dump()
    assert (
        belief.statement == STATEMENT and belief.status == "verified" and belief.confidence == 0.9
    )
    assert old.excerpt == "历史摘录" and old.assessment == {} and old.stance == "support"
    goals = [r for r in session.rows if isinstance(r, models.Goal)]
    assert len(goals) == (0 if stance == "support" else 1)
    if goals:
        assert goals[0].metadata_json["belief_review_id"] == str(belief.id)
    audit = await repo.belief_audit(belief.id)
    assert any(e["stance"] == stance and e["excerpt"] == QUOTE for e in audit["evidence"])


# 功能：验证未锚定引文、模型失败、零等级观察均不产生反对事实证据或撤回。
@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["unanchored", "failure", "level_zero"])
async def test_unknown_observation_preserves_old_state(case):
    session, repo, source, belief, old = fixture()
    if case == "level_zero":
        source.source_type, source.evidence_level = "model", 0
    reviewer = BeliefReviewer(repo, Judge(fail=case == "failure"))
    excerpt = (
        "Invented counterexample not in the original document." if case == "unanchored" else QUOTE
    )
    result = await reviewer.observe(
        belief,
        {URL: source},
        [ClaimCitation(source_url=URL, excerpt=excerpt)],
        SimpleNamespace(id=uuid4()),
    )
    assert result["outcome"] == "unknown" and result["evidence_ids"] == []
    assert [r for r in session.rows if isinstance(r, models.Evidence)] == [old]
    assert belief.status == "verified" and belief.confidence == 0.9


# 功能：验证显式范围未确认相容的反例不作为旧结论反对证据。
@pytest.mark.asyncio
async def test_disjoint_or_unknown_scope_is_not_a_counterexample():
    draft = ClaimDraft(
        statement=STATEMENT,
        topic="drift",
        confidence=0.5,
        scope=ClaimScope(conditions=["under the stated operating conditions"]),
    )
    _session, _repo, source, _belief, _old = fixture()
    draft.citations = [ClaimCitation(source_url=URL, excerpt=QUOTE)]
    checked = await ClaimSupportAssessor(Judge(coverage="unknown")).assess(
        draft, ClaimSupportValidator().validate(draft, {URL: source}), {URL: source}
    )
    assert checked.records[0].status == "unclear"
    assert "counter_scope_not_complete" in checked.records[0].reason


# 功能：验证同周期终态目标复用，不循环重建绕过blocked与每日配额。
@pytest.mark.asyncio
async def test_review_queue_reuses_terminal_goal_and_age_is_only_a_signal():
    _session, repo, _source, belief, _old = fixture()
    first = await queue_belief_review(repo, belief, "manual")
    first.status = "blocked"
    assert await queue_belief_review(repo, belief, "age_due") is first
    assert await queue_stale_reviews(repo) == []
    assert belief.status == "verified" and belief.confidence == 0.9


# 功能：验证没有新来源时保存unknown报告并可重放，不伪造复核时间或改旧状态。
@pytest.mark.asyncio
async def test_empty_independent_review_is_unknown_and_replayed():
    _session, repo, _source, belief, _old = fixture()

    class Collector:
        calls = 0

        # 功能：检查旧支持来源被排除并模拟搜索提供方无可读资料。
        async def fetch(self, query, exclude, limit):
            self.calls += 1
            assert [d.url for d in exclude] == [URL]
            return []

    collector = Collector()
    attempt = models.LearningSession(id=uuid4(), result={})
    reviewer = BeliefReviewer(repo, Judge(), collector)
    result = await reviewer.investigate(belief.id, attempt)
    assert result["outcome"] == "unknown" and result["sources"] == 0
    assert await reviewer.investigate(belief.id, attempt) == result and collector.calls == 1
    assert "latest_evidence_review" not in belief.metadata_json
    assert belief.status == "verified" and belief.confidence == 0.9


# 功能：验证新独立来源反对原句，合格替代句才能排争议；缺替代/支持保持原状态并留复核审计。
@pytest.mark.asyncio
@pytest.mark.parametrize("counter_supported", [True, False, None])
async def test_fresh_review_opens_only_evidenced_dispute(counter_supported):
    session, repo, old_source, belief, _old = fixture()
    old_source.url = "https://old.example/support"
    fresh = models.Source(
        id=uuid4(),
        url=URL,
        extracted_text=QUOTE + " Context." * 40,
        evidence_level=1,
        credibility_score=0.3,
        source_type="web",
        metadata_json={},
    )
    session.rows.append(fresh)
    alternative = "Observed drift grows over time under the stated operating conditions."

    class ReviewJudge(Judge):
        # 功能：分别核验原句/替代句，返回单句结构和冲突观察；无替代不凭空造相反结论。
        async def structured(self, system, user, schema):
            if schema is LearningResult:
                return LearningResult(
                    claims=[
                        ClaimDraft(
                            statement=STATEMENT if counter_supported is None else alternative,
                            topic="drift",
                            confidence=0.5,
                            citations=[ClaimCitation(source_url=URL, excerpt=QUOTE)],
                        )
                    ]
                )
            if schema is SupportVerdict and user.startswith(f"Claim: {alternative}"):
                return SupportVerdict(
                    relation="supports" if counter_supported else "unclear", reason="替代结论核验"
                )
            if schema is DecompositionProposal:
                return DecompositionProposal(kind="atomic", reason="单句")
            if schema is DecompositionReview:
                return DecompositionReview(
                    equivalent=True,
                    all_atomic=True,
                    scope_preserved=True,
                    no_added_facts=True,
                    reason="结构观察",
                )
            if schema is ContradictionResult:
                return ContradictionResult(
                    relation="contradicts", score=0.95, explanation="有原文支持的反例"
                )
            return await super().structured(system, user, schema)

    class Collector:
        # 功能：断言旧支持来源被排除，模拟返回另一出版方独立原文。
        async def fetch(self, query, exclude, limit):
            assert [d.url for d in exclude] == [old_source.url]
            return [SourceDocument(url=URL, text=fresh.extracted_text)]

    # 功能：使用已预置新原文，隔离本测试于书目/内容分类入库的独立测试。
    async def upsert(doc, digest):
        assert doc.url == URL
        return fresh

    repo.upsert_source = upsert
    attempt = models.LearningSession(id=uuid4(), result={})
    reviewer = BeliefReviewer(repo, ReviewJudge(), Collector())
    result = await reviewer.investigate(belief.id, attempt)
    assert result["outcome"] == ("observed" if counter_supported else "needs_follow_up")
    assert result["stances"] == ["attack"]
    disputes = [r for r in session.rows if isinstance(r, models.Dispute)]
    assert len(disputes) == (1 if counter_supported else 0)
    assert belief.statement == STATEMENT and belief.confidence == 0.9
    assert belief.status == ("disputed" if counter_supported else "verified")
    if disputes:
        assert disputes[0].previous_belief_state["status"] == "verified"
        assert any(
            isinstance(r, models.Goal) and r.metadata_json.get("dispute_id") == str(disputes[0].id)
            for r in session.rows
        )
    assert ("latest_evidence_review" in belief.metadata_json) == bool(counter_supported)
    assert await reviewer.investigate(belief.id, attempt) == result


# 功能：验证规划服务故障保存unknown审计，不修改旧信念或假造成功复核时间。
@pytest.mark.asyncio
async def test_planning_failure_is_audited_as_unknown():
    _session, repo, _source, belief, _old = fixture()
    result = await BeliefReviewer(repo, Judge(fail=True)).investigate(
        belief.id, models.LearningSession(id=uuid4(), result={})
    )
    assert (
        result["outcome"] == "unknown" and result["reason"] == "planning_unavailable:TimeoutError"
    )
    assert belief.status == "verified" and "latest_evidence_review" not in belief.metadata_json
    assert belief.metadata_json["latest_evidence_review_attempt"]["outcome"] == "unknown"


# 功能：验证旧支持来源降级为零仍排复核，但模型/撤稿线索不凭空变成正等级反对证据。
@pytest.mark.asyncio
async def test_support_source_degradation_queues_review_without_truth_edit():
    _session, repo, source, belief, old = fixture()
    old.evidence_level = 3
    source.evidence_level, source.source_type = 0, "model"
    result = await BeliefReviewer(repo, Judge()).observe(
        belief,
        {URL: source},
        [ClaimCitation(source_url=URL, excerpt=QUOTE)],
        SimpleNamespace(id=uuid4()),
    )
    assert result["outcome"] == "unknown" and result["review_goal_id"]
    assert result["degraded_source_ids"] == [str(source.id)]
    assert belief.status == "verified" and belief.confidence == 0.9


# 功能：验证同周期已经复核的旧反例不会压制新的原文线索，同时同线索重放不重复排队。
@pytest.mark.asyncio
async def test_new_negative_passage_can_queue_after_completed_review():
    session, repo, source, belief, _old = fixture()
    reviewer = BeliefReviewer(repo, Judge())
    attempt = SimpleNamespace(id=uuid4())
    first = await reviewer.observe(
        belief, {URL: source}, [ClaimCitation(source_url=URL, excerpt=QUOTE)], attempt
    )
    goal = next(r for r in session.rows if isinstance(r, models.Goal))
    goal.status = "passed"
    second_quote = "A separate counterexample was observed in an additional operating trial."
    source.extracted_text += second_quote
    second = await reviewer.observe(
        belief, {URL: source}, [ClaimCitation(source_url=URL, excerpt=second_quote)], attempt
    )
    replay = await reviewer.observe(
        belief, {URL: source}, [ClaimCitation(source_url=URL, excerpt=second_quote)], attempt
    )
    assert first["review_goal_id"] != second["review_goal_id"]
    assert second["review_goal_id"] == replay["review_goal_id"]
    assert len([r for r in session.rows if isinstance(r, models.Goal)]) == 2
