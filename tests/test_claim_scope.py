# 文件职责：验证主张范围、原文覆盖门控、持久审计、旧结构兼容和续跑协议身份。
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from autodidact import models
from autodidact.knowledge.claim_support import ClaimEvidenceVerification, ClaimSupportValidator
from autodidact.knowledge.sources import normalize_url
from autodidact.knowledge.support_assessment import (
    SUPPORT_PROTOCOL,
    ClaimSupportAssessor,
    SupportVerdict,
)
from autodidact.repository import Repository
from autodidact.retrieval import RetrievalHit
from autodidact.schemas import ClaimCitation, ClaimDraft, ClaimScope

SCOPE = {"conditions": ["低照度"], "time_scope": "2025年", "units": ["m/s"]}
STATEMENT = "2025年在低照度条件下，该方法的速度误差为0.2 m/s。"
URL = "https://example.org/research"


class _Judge:
    provider_name, model_name = "test", "scope-judge"

    # 功能：预置范围覆盖观察并记录调用，不访问真实模型。
    def __init__(self, coverage="complete", fail=False):
        self.coverage, self.fail, self.calls = coverage, fail, []

    # 功能：返回测试观察或模拟超时，用于验证失败不升格。
    async def structured(self, system, user, schema):
        self.calls.append((system, user))
        if self.fail:
            raise TimeoutError("offline")
        return schema(relation="supports", reason="fixture only", scope_coverage=self.coverage)


# 功能：构造含范围的主张和已读原文，复用真实锚点验证器。
def _inputs(statement=STATEMENT):
    draft = ClaimDraft(
        statement=statement,
        topic="VIO",
        confidence=0.8,
        scope=SCOPE,
        citations=[ClaimCitation(source_url=URL, excerpt=statement)],
    )
    source = SimpleNamespace(id=uuid4(), extracted_text=statement)
    sources = {normalize_url(URL): source}
    return draft, sources, ClaimSupportValidator().validate(draft, sources)


# 功能：验证旧格式可解析，空范围仅表示未提取；限制字段长度及空白列表值。
def test_scope_schema_is_bounded_and_legacy_drafts_remain_readable():
    draft = ClaimDraft(statement="旧主张", topic="VIO", confidence=0.5)
    assert draft.scope.terms() == []
    assert ClaimScope.model_validate({}).terms() == []
    with pytest.raises(ValidationError):
        ClaimScope(conditions=["   "])
    with pytest.raises(ValidationError):
        ClaimScope(units=["m"] * 21)
    assert ClaimScope(**SCOPE).missing_from(STATEMENT) == []


# 功能：验证缺失、未知或未覆盖范围的“supports”观察不能成为晋升支持来源。
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "coverage,status",
    [
        ("complete", "supported"),
        ("incomplete", "conditional"),
        ("unknown", "unclear"),
    ],
)
async def test_scope_requires_explicit_complete_coverage(coverage, status):
    draft, sources, validation = _inputs()
    judge = _Judge(coverage)
    result = await ClaimSupportAssessor(judge).assess(draft, validation, sources)
    record = result.records[0]
    assert record.status == status
    assert bool(result.supported_source_ids) == (status == "supported")
    assert record.assessment["scope"] == SCOPE
    assert record.assessment["protocol"] == SUPPORT_PROTOCOL
    assert record.assessment["context"] == STATEMENT
    assert len(record.assessment["context_hash"]) == 64
    assert "2025年" in judge.calls[0][1] and "m/s" in judge.calls[0][1]


# 功能：验证模型省略新字段时默认为未知，不利用旧响应冒充范围覆盖。
def test_legacy_verdict_has_unknown_scope_coverage():
    assert SupportVerdict(relation="supports", reason="legacy").scope_coverage == "unknown"


# 功能：正文遗漏限定时不调用模型，不能只靠 metadata 将无条件正文晋升。
@pytest.mark.asyncio
async def test_hidden_scope_is_rejected_before_model_call():
    draft, sources, validation = _inputs("该方法的速度误差为0.2 m/s。")
    judge = _Judge()
    result = await ClaimSupportAssessor(judge).assess(draft, validation, sources)
    assert judge.calls == []
    assert result.supported_source_ids == []
    assert result.records[0].assessment["missing_from_statement"] == ["低照度", "2025年"]


# 功能：范围核验提供方失败时保留未完成审计，锚点成功也不能放行。
@pytest.mark.asyncio
async def test_scope_provider_failure_is_audited_without_promotion():
    draft, sources, validation = _inputs()
    result = await ClaimSupportAssessor(_Judge(fail=True)).assess(draft, validation, sources)
    assert result.supported_source_ids == []
    assert result.records[0].status == "unclear"
    assert result.records[0].assessment["error"] == "TimeoutError"


class _Result:
    # 功能：保存内存会话查询结果。
    def __init__(self, rows):
        self.rows = rows

    # 功能：返回模拟标量列表供仓库迭代。
    def scalars(self):
        return self.rows

    # 功能：返回模拟单实体或空。
    def scalar_one_or_none(self):
        return self.rows[0] if self.rows else None


class _MemorySession:
    # 功能：记录实体和提交次数，用于持久化逻辑证明，不模拟真实 PostgreSQL 事务。
    def __init__(self):
        self.rows, self.commits = [], 0

    # 功能：按真实选择实体和绑定参数筛选内存记录，索引 INSERT 不连接数据库。
    async def execute(self, statement):
        if not statement.is_select:
            return _Result([])
        model = statement.column_descriptions[0]["entity"]
        params = statement.compile().params
        fields = ["learning_session_id", "claim_id", "source_id", "excerpt_hash"]
        return _Result(
            [
                row
                for row in self.rows
                if isinstance(row, model)
                and all(
                    getattr(row, key) == params[key + "_1"]
                    for key in fields
                    if key + "_1" in params
                )
            ]
        )

    # 功能：按主键返回预置模型实体。
    async def get(self, model, item_id):
        return next(
            (row for row in self.rows if isinstance(row, model) and row.id == item_id), None
        )

    # 功能：记录新实体供回归断言使用。
    def add(self, row):
        self.rows.append(row)

    # 功能：给新实体分配模拟主键。
    async def flush(self):
        for row in self.rows:
            if row.id is None:
                row.id = uuid4()

    # 功能：模拟提交并分配主键，不写在线库。
    async def commit(self):
        await self.flush()
        self.commits += 1

    # 功能：提供仓库所需刷新接口，实体已在内存中。
    async def refresh(self, row):
        pass


# 功能：验证从主张提取到语义核验、引文持久化和人工审计的完整范围链路与重放幂等。
@pytest.mark.asyncio
async def test_scope_and_evidence_audit_persist_end_to_end():
    draft, sources, validation = _inputs()
    checked = await ClaimSupportAssessor(_Judge()).assess(draft, validation, sources)
    session = _MemorySession()
    repo = Repository(session)  # type: ignore[arg-type]
    source = next(iter(sources.values()))
    session.add(models.Source(id=source.id, url=URL, extracted_text=STATEMENT))
    attempt_id = uuid4()
    claim = await repo.add_claim(draft, attempt_id, checked.supported_source_ids)
    await repo.record_claim_evidence(claim, checked.records)
    before = session.commits
    assert await repo.add_claim(draft, attempt_id, []) is claim
    await repo.record_claim_evidence(claim, checked.records)
    assert session.commits == before
    assert claim.scope == SCOPE
    audit = await repo.claim_audit(claim.id)
    assert audit["scope"] == SCOPE and audit["truncated"] is False
    assert audit["evidence"][0]["assessment"]["scope_coverage"] == "complete"
    assert audit["evidence"][0]["excerpt"] == STATEMENT
    assert RetrievalHit("claim", claim, 0.1, {}).as_dict()["claim_scope"] == SCOPE


# 功能：验证重复提取漏范围或核验范围不同不能覆盖既有范围支持门槛。
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "assessment",
    [
        {},
        {"scope": SCOPE, "scope_coverage": "unknown"},
        {"scope": {}, "scope_coverage": "complete"},
    ],
)
async def test_persisted_scope_cannot_be_bypassed_by_reassessment(assessment):
    session = _MemorySession()
    claim = models.Claim(id=uuid4(), statement=STATEMENT, scope=SCOPE, source_ids=[])
    session.add(claim)
    repo = Repository(session)  # type: ignore[arg-type]
    source_id = uuid4()
    record = ClaimEvidenceVerification(
        str(source_id), URL, STATEMENT, "hash", "supported", "fixture", assessment
    )
    await repo.record_claim_evidence(claim, [record])
    assert claim.source_ids == []
    row = next(row for row in session.rows if isinstance(row, models.ClaimEvidence))
    assert row.status == "unclear" and row.reason == "persisted_scope_not_covered"
    assert row.assessment["scope_gate"] == "rejected"


# 功能：核对未锚定引用也有范围审计，但不会调用模型或供晋升使用。
@pytest.mark.asyncio
async def test_unanchored_quote_retains_scope_failure_audit():
    draft, sources, _ = _inputs()
    draft.citations[0].excerpt = "不存在于这份原文中的伪造摘录文字。"
    validation = ClaimSupportValidator().validate(draft, sources)
    judge = _Judge()
    result = await ClaimSupportAssessor(judge).assess(draft, validation, sources)
    assert judge.calls == [] and result.supported_source_ids == []
    assert result.records[0].status == "unanchored"
    assert result.records[0].assessment["scope_coverage"] == "not_assessed"


# 功能：即便外部调用者填 complete，仓库仍拒绝正文未体现的已存范围。
@pytest.mark.asyncio
async def test_repository_rejects_hidden_scope_even_with_complete_observation():
    session = _MemorySession()
    claim = models.Claim(id=uuid4(), statement="该方法误差很低。", scope=SCOPE, source_ids=[])
    session.add(claim)
    record = ClaimEvidenceVerification(
        str(uuid4()),
        URL,
        STATEMENT,
        "hash",
        "supported",
        "fixture",
        {"scope": SCOPE, "scope_coverage": "complete"},
    )
    await Repository(session).record_claim_evidence(claim, [record])  # type: ignore[arg-type]
    assert claim.source_ids == []
    assert (
        next(row for row in session.rows if isinstance(row, models.ClaimEvidence)).status
        == "unclear"
    )


# 功能：核对主张核验协议变化会改变续跑身份，不能沿用旧协议的部分学习结果。
def test_scope_protocol_participates_in_resume_signature(monkeypatch):
    import autodidact.agent as agent_module

    learner = agent_module.AutonomousLearner.__new__(agent_module.AutonomousLearner)
    learner.cfg = SimpleNamespace(model_dump=lambda **kwargs: {})
    llm = SimpleNamespace(provider_name="test", model_name="fixture")
    learner.llm, learner.evaluator = llm, SimpleNamespace(judge=llm)
    learner.embedding = SimpleNamespace(fingerprint="disabled")
    settings = SimpleNamespace(
        llm_base_url="",
        judge_llm_base_url="",
        llm_temperature=0,
        llm_max_output_tokens=100,
        search_provider="test",
        brave_search_base_url="",
        enabled_web_models="",
        web_models_config="",
    )
    monkeypatch.setattr(agent_module, "runtime_settings", lambda: settings)
    goal = SimpleNamespace(title="scope-test", description="", metadata_json={})
    before = learner._resume_signature(goal)
    monkeypatch.setattr(agent_module, "SUPPORT_PROTOCOL", "future-protocol")
    assert learner._resume_signature(goal) != before
