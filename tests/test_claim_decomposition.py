# 文件职责：离线验证复合主张拆分、范围映射、逐子句核验、结构门控及父子关系重放，不调用真实模型。
from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

import pytest
from test_claim_scope import _MemorySession

from autodidact import models
from autodidact.knowledge.claim_support import ClaimSupportValidator
from autodidact.knowledge.decomposition import (
    ClaimDecomposer,
    DecompositionProposal,
    eligible_claim,
)
from autodidact.knowledge.support_assessment import ClaimSupportAssessor
from autodidact.repository import Repository
from autodidact.schemas import ClaimCitation, ClaimDraft

URL = "https://research.example/article"
PARENT = "2024年低照度时，方法甲误差为2m，且方法乙速度为3m/s。"
FIRST = "2024年低照度时，方法甲误差为2m。"
SECOND = "2024年低照度时，方法乙速度为3m/s。"
PARTS = [
    {"statement": FIRST, "parent_excerpt": "方法甲误差为2m"},
    {"statement": SECOND, "parent_excerpt": "方法乙速度为3m/s"},
]


# 功能：构造具有共同条件/时间、各自单位和仅支持第一子句的引文的复合主张。
def _draft():
    return ClaimDraft(
        statement=PARENT,
        topic="传感器",
        confidence=0.8,
        scope={"conditions": ["低照度"], "time_scope": "2024年", "units": ["2m", "3m/s"]},
        citations=[ClaimCitation(source_url=URL, excerpt=FIRST)],
    )


class _Model:
    provider_name, model_name = "test", "atomic-observer"

    # 功能：预置拆分/复核结果和故障，记录调用用于重放与失败门控断言。
    def __init__(self, *, kind="compound", parts=None, reject=False, fail=False):
        self.kind, self.parts = kind, deepcopy(PARTS if parts is None else parts)
        self.reject, self.fail, self.calls = reject, fail, 0

    # 功能：返回结构观察或语义支持替身，原文只有第一子句时不支持整父句/第二子句。
    async def structured(self, system, user, schema):
        self.calls += 1
        if self.fail:
            raise TimeoutError("offline")
        if schema is DecompositionProposal:
            return schema(kind=self.kind, parts=self.parts, reason="fixture")
        if schema.__name__ == "DecompositionReview":
            return schema(
                equivalent=not self.reject,
                all_atomic=True,
                scope_preserved=True,
                no_added_facts=True,
                reason="fixture",
            )
        return schema(
            relation="supports" if user.splitlines()[0] == "Claim: " + FIRST else "unclear",
            scope_coverage="complete",
            reason="只有甲句有原文支持",
        )


# 功能：验证共同时空条件继承和单位分别映射，引用继承只作为核证候选而非支持传递。
@pytest.mark.asyncio
async def test_decomposition_preserves_common_scope_and_maps_units():
    result = await ClaimDecomposer(_Model()).decompose(_draft())
    assert result.audit["kind"] == "compound" and result.audit["status"] == "accepted"
    assert [child.scope.units for child in result.children] == [["2m"], ["3m/s"]]
    assert all(
        child.scope.conditions == ["低照度"] and child.scope.time_scope == "2024年"
        for child in result.children
    )
    assert all(child.citations == _draft().citations for child in result.children)


# 功能：分别拒绝漏条件、伪造父片段、重复子句、漏单位或只有单一子句的复合提议。
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["lost_scope", "unanchored", "duplicates", "lost_units", "one_part"]
)
async def test_bad_decomposition_is_not_eligible(case):
    parts = deepcopy(PARTS)
    if case == "lost_scope":
        parts[0]["statement"] = FIRST.replace("低照度", "")
    elif case == "unanchored":
        parts[0]["parent_excerpt"] = "这个片段不在任何父句中"
    elif case == "duplicates":
        parts[1] = parts[0]
    elif case == "lost_units":
        parts[1]["statement"] = SECOND.replace("3m/s", "低速")
    else:
        parts = parts[:1]
    result = await ClaimDecomposer(_Model(parts=parts)).decompose(_draft())
    assert result.audit["status"] == "rejected" and result.children == []


# 功能：复核拒绝或模型故障不生成可晋升子句，且故障审计保留原因。
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options",
    [{"reject": True}, {"fail": True}, {"kind": "uncertain", "parts": []}, {"kind": "atomic"}],
)
async def test_review_or_provider_failure_keeps_unverified_parent(options):
    result = await ClaimDecomposer(_Model(**options)).decompose(_draft())
    assert result.children == [] and result.audit["status"] == "rejected"


# 功能：真实仓库/锚点/核验链路只给有支持的子句来源ID，父句及另一子句不能借第一句证据晋升。
@pytest.mark.asyncio
async def test_parent_and_children_are_separately_verified_and_replayed():
    session = _MemorySession()
    repo, model = Repository(session), _Model()
    source = models.Source(id=uuid4(), url=URL, extracted_text=FIRST)
    session.add(source)
    decomposer = ClaimDecomposer(model)
    pairs = await decomposer.persist(repo, _draft(), uuid4())
    sources = {URL: source}
    for claim, draft in pairs:
        checked = await ClaimSupportAssessor(model).assess(
            draft, ClaimSupportValidator().validate(draft, sources), sources
        )
        await repo.record_claim_evidence(claim, checked.records)
    parent, first, second = [claim for claim, _ in pairs]
    assert parent.statement == PARENT and parent.source_ids == [] and not eligible_claim(parent)
    assert first.source_ids == [str(source.id)] and second.source_ids == []
    assert first.structure["parent_claim_ids"] == [str(parent.id)]
    assert parent.structure["child_claim_ids"] == [str(first.id), str(second.id)]
    with pytest.raises(ValueError, match="不能直接晋升"):
        await repo.create_belief_from_claim(parent, 1.0, True)
    before = model.calls
    replay = await decomposer.persist(repo, _draft(), parent.learning_session_id)
    assert [claim.id for claim, _ in replay] == [claim.id for claim, _ in pairs]
    assert model.calls == before
    assert (await repo.claim_audit(first.id))["structure"]["parent_claim_ids"] == [str(parent.id)]


# 功能：已存不确定结构不能用随后成功观察覆写门控；兼容旧空结构不凭空变成已拆分。
@pytest.mark.asyncio
async def test_first_structure_is_preserved_and_legacy_is_unknown():
    session = _MemorySession()
    repo = Repository(session)
    claim = await repo.add_claim(_draft(), uuid4(), [])
    rejected = {"kind": "uncertain", "status": "rejected", "reason": "unknown"}
    await repo.record_claim_structure(claim, rejected)
    await repo.record_claim_structure(claim, {"kind": "atomic", "status": "accepted"})
    assert claim.structure == rejected and not eligible_claim(claim)
    legacy = models.Claim(id=uuid4(), statement="旧主张", structure={})
    assert eligible_claim(legacy) and legacy.structure == {}


# 功能：原子父句也需结构复核，保留原句而不创建多余子记录。
@pytest.mark.asyncio
async def test_atomic_claim_has_no_children_and_requires_review():
    draft = _draft().model_copy(
        update={"statement": FIRST, "scope": _draft().scope.model_copy(update={"units": ["2m"]})}
    )
    result = await ClaimDecomposer(_Model(kind="atomic", parts=[])).decompose(draft)
    assert (
        result.audit["kind"] == "atomic"
        and result.audit["status"] == "accepted"
        and result.children == []
    )


# 功能：争议新调查重用父句但生成本次会话子句，满足条件化候选会话门控，重放不重复创建。
@pytest.mark.asyncio
async def test_investigation_children_belong_to_current_attempt():
    session = _MemorySession()
    repo, decomposer = Repository(session), ClaimDecomposer(_Model())
    original = await decomposer.persist(repo, _draft(), uuid4())
    parent = original[0][0]
    attempt_id = uuid4()
    investigated = await decomposer.persist(repo, _draft(), attempt_id, existing=parent)
    assert investigated[0][0] is parent
    for child, _ in investigated[1:]:
        assert child.learning_session_id == attempt_id
        assert str(child.id) in parent.structure["investigation_child_claim_ids"]
        assert child.id not in [row.id for row, _ in original[1:]]
    replay = await decomposer.persist(repo, _draft(), attempt_id, existing=parent)
    assert [row.id for row, _ in replay] == [row.id for row, _ in investigated]


# 功能：即使复合父句残留旧supported记录，采用新结论的仓库门控也不允许直接提交父句。
@pytest.mark.asyncio
async def test_compound_parent_cannot_be_adopted_in_dispute():
    session = _MemorySession()
    repo = Repository(session)
    parent = (await ClaimDecomposer(_Model()).persist(repo, _draft(), uuid4()))[0][0]
    belief = models.Belief(id=uuid4(), statement="旧结论", topic="传感器")
    dispute = models.Dispute(id=uuid4(), belief_id=belief.id, incoming_claim_id=parent.id)
    assert (
        await repo.qualified_resolution_sources(
            dispute, belief, parent, "adopt_new", [str(uuid4())]
        )
        == []
    )
