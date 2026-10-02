# 文件职责：检查决议事务、原文引文、行锁缓存刷新和重放幂等。
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from autodidact import models
from autodidact.enums import BeliefStatus, DisputeStatus
from autodidact.knowledge.sources import EvidenceSource
from autodidact.repository import Repository


class _Result:
    # 功能：保存争议查询的模拟返回值。
    def __init__(self, values):
        self.values = values

    # 功能：返回预置单实体供行锁查询替身使用。
    def scalar_one(self):
        return self.values[0]

    # 功能：返回模拟结果内的对象列表，不连接数据库。
    def scalars(self):
        return self.values

    # 功能：模拟 ScalarResult.all，返回匹配查询条件的原文片段。
    def all(self):
        return self.values


class _Session:
    # 功能：初始化争议/信念、待写实体及提交/回滚计数；不是完整 AsyncSession 实现。
    def __init__(self, dispute, belief):
        self.dispute = dispute
        self.belief = belief
        self.added = []
        self.commits = 0
        self.rollbacks = 0
        self.claim_evidence = []
        self.locked_dispute = dispute
        self.beliefs = {belief.id: belief}

    # 功能：模拟行锁查询；只有显式刷新身份缓存时返回数据库中的最新决议。
    async def execute(self, statement):
        dispute = (
            self.locked_dispute
            if statement.get_execution_options().get("populate_existing")
            else self.dispute
        )
        return _Result([dispute])

    # 功能：按真实 SQL 参数筛选主张、来源和 supported 状态，模拟原文查询接口。
    async def scalars(self, statement):
        params = statement.compile().params
        assert "claim_evidence.excerpt" in str(statement)
        return _Result(
            [
                record.excerpt
                for record in self.claim_evidence
                if record.claim_id == params["claim_id_1"]
                and record.source_id == params["source_id_1"]
                and record.status == params["status_1"]
            ]
        )

    # 功能：按主键返回预置或新写入信念，其它类型返回空以控制测试路径。
    async def get(self, model, _item_id):
        if model is models.Belief:
            return self.beliefs.get(_item_id)
        return None

    # 功能：把新实体加入模拟待写列表，便于检查证据和历史。
    def add(self, item):
        self.added.append(item)

    # 功能：为新信念生成模拟 UUID，供后续外键关联断言使用。
    async def flush(self):
        for item in self.added:
            if isinstance(item, models.Belief) and item.id is None:
                item.id = uuid4()
            if isinstance(item, models.Belief):
                self.beliefs[item.id] = item

    # 功能：累计事务提交次数，不真正写库。
    async def commit(self):
        self.commits += 1

    # 功能：累计事务回滚次数，供错误路径检查。
    async def rollback(self):
        self.rollbacks += 1


# 功能：构造旧 verified 信念、新主张和未解决争议供各事务测试复用。
def _entities():
    belief = models.Belief(
        id=uuid4(),
        topic="VIO",
        statement="旧结论",
        explanation="旧解释",
        status=BeliefStatus.VERIFIED,
        confidence=0.8,
        evidence_score=0.8,
        test_score=0.9,
        stability_score=0.8,
        source_count=2,
    )
    claim = models.Claim(
        id=uuid4(),
        statement="仅在低照度条件下新结论成立",
        topic="VIO",
        reasoning="新解释",
        confidence=0.9,
        source_ids=[],
    )
    dispute = models.Dispute(
        id=uuid4(),
        belief_id=belief.id,
        incoming_claim_id=claim.id,
        status=DisputeStatus.UNRESOLVED,
        previous_belief_state={"status": "verified"},
        resolution_metadata={},
        contradiction_score=0.9,
    )
    return dispute, belief, claim


# 功能：验证重复未解决决议不重复产生历史记录。
@pytest.mark.asyncio
async def test_unresolved_resolution_writes_one_history_and_retries_idempotently():
    dispute, belief, claim = _entities()
    session = _Session(dispute, belief)
    repo = Repository(session)  # type: ignore[arg-type]

    for _ in range(2):
        result = await repo.apply_dispute_resolution(
            dispute,
            belief,
            claim,
            outcome="unresolved",
            rationale="证据不足",
            conditional_statement="",
            conditions=[],
            evidence_source_ids=[],
        )
        assert result is belief

    assert session.commits == 2
    assert sum(isinstance(item, models.BeliefHistory) for item in session.added) == 1
    assert belief.status == BeliefStatus.UNRESOLVED
    assert dispute.resolution_metadata["outcome"] == "unresolved"


# 功能：检查采用新结论时信念、证据和历史一并提交。
@pytest.mark.asyncio
async def test_adopt_new_resolution_commits_new_belief_evidence_and_histories_together():
    dispute, belief, claim = _entities()
    session = _Session(dispute, belief)
    repo = Repository(session)  # type: ignore[arg-type]
    first, second = uuid4(), uuid4()
    session.claim_evidence = [
        models.ClaimEvidence(
            claim_id=claim.id, source_id=source_id, status="supported", excerpt=excerpt
        )
        for source_id, excerpt in ((first, "原文支持片段一"), (second, "原文支持片段二"))
    ]

    # 功能：提供两个库内合格来源替身，隔离来源查询路径。
    async def qualified(*_args):
        return [
            EvidenceSource(str(first), publisher_key="one.example", evidence_level=4),
            EvidenceSource(str(second), publisher_key="two.example", evidence_level=4),
        ]

    repo.qualified_resolution_sources = qualified  # type: ignore[method-assign]
    result = await repo.apply_dispute_resolution(
        dispute,
        belief,
        claim,
        outcome="adopt_new",
        rationale="两份独立证据",
        conditional_statement="",
        conditions=[],
        evidence_source_ids=[str(first), str(second)],
    )

    assert session.commits == 1
    assert session.rollbacks == 0
    assert result.status == BeliefStatus.SUPPORTED
    assert result.test_score == 0.0
    assert belief.status == BeliefStatus.RETRACTED
    assert dispute.status == DisputeStatus.RESOLVED_NEW
    evidence = [item for item in session.added if isinstance(item, models.Evidence)]
    assert len(evidence) == 2
    assert {item.source_id: item.excerpt for item in evidence} == {
        first: "原文支持片段一",
        second: "原文支持片段二",
    }
    assert sum(isinstance(item, models.BeliefHistory) for item in session.added) == 2


# 功能：验证条件化结论不能偏离已支持 Claim 的正文。
@pytest.mark.asyncio
async def test_conditional_resolution_rejects_statement_not_supported_by_the_claim():
    dispute, belief, claim = _entities()
    session = _Session(dispute, belief)
    repo = Repository(session)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="supported conditional claim"):
        await repo.apply_dispute_resolution(
            dispute,
            belief,
            claim,
            outcome="conditional",
            rationale="需限定范围",
            conditional_statement="无关的新结论",
            conditions=["低照度"],
            evidence_source_ids=[],
        )
    assert session.commits == 0
    assert session.rollbacks == 1


# 功能：验证与新主张无关联支持记录的来源不能用于替换旧结论。
@pytest.mark.asyncio
async def test_repository_rejects_unlinked_sources_for_new_claim():
    dispute, belief, claim = _entities()
    source_id = uuid4()

    class SourceSession(_Session):
        # 功能：模拟无关联证据的查询结果，供决议拒绝路径使用。
        async def execute(self, statement):
            assert "claim_evidence" in str(statement)
            return _Result([SimpleNamespace(source_id=source_id)])

        # 功能：为特定模型返回受控实体，模拟来源/主张查询。
        async def get(self, model, _item_id):
            if model is models.Source:
                return SimpleNamespace(
                    evidence_level=4,
                    normalized_url="https://example.org",
                    publisher_key="example.org",
                    content_hash="hash",
                    credibility_score=0.8,
                )
            return await super().get(model, _item_id)

    repo = Repository(SourceSession(dispute, belief))  # type: ignore[arg-type]
    assert (
        await repo.qualified_resolution_sources(dispute, belief, claim, "adopt_new", [str(uuid4())])
        == []
    )


# 功能：检查未解决争议补证后可采用新主张并保留支持原文。
@pytest.mark.asyncio
async def test_unresolved_dispute_can_later_adopt_new_supported_claim():
    dispute, belief, claim = _entities()
    session = _Session(dispute, belief)
    repo = Repository(session)  # type: ignore[arg-type]
    await repo.apply_dispute_resolution(
        dispute,
        belief,
        claim,
        outcome="unresolved",
        rationale="暂缺证据",
        conditional_statement="",
        conditions=[],
        evidence_source_ids=[],
    )

    first, second = uuid4(), uuid4()
    session.claim_evidence = [
        models.ClaimEvidence(
            claim_id=claim.id, source_id=source_id, status="supported", excerpt=excerpt
        )
        for source_id, excerpt in ((first, "原文支持片段一"), (second, "原文支持片段二"))
    ]

    # 功能：补入两个独立来源替身，模拟后续调查取得新证据。
    async def qualified(*_args):
        return [
            EvidenceSource(str(first), publisher_key="one.example", evidence_level=4),
            EvidenceSource(str(second), publisher_key="two.example", evidence_level=4),
        ]

    repo.qualified_resolution_sources = qualified  # type: ignore[method-assign]
    result = await repo.apply_dispute_resolution(
        dispute,
        belief,
        claim,
        outcome="adopt_new",
        rationale="补充了独立证据",
        conditional_statement="",
        conditions=[],
        evidence_source_ids=[str(first), str(second)],
    )
    assert result.status == BeliefStatus.SUPPORTED
    assert dispute.status == DisputeStatus.RESOLVED_NEW
    assert dispute.resolution_metadata["outcome"] == "adopt_new"


# 功能：验证只拼接同一主张与来源的 supported 原文，不混入其它状态或关系的引用。
@pytest.mark.asyncio
async def test_supported_excerpt_excludes_unrelated_or_unsupported_quotes():
    dispute, belief, claim = _entities()
    session = _Session(dispute, belief)
    source_id = uuid4()
    session.claim_evidence = [
        models.ClaimEvidence(
            claim_id=claim_id, source_id=record_source, status=status, excerpt=excerpt
        )
        for claim_id, record_source, status, excerpt in [
            (claim.id, source_id, "supported", "引文一"),
            (claim.id, source_id, "supported", "引文二"),
            (claim.id, source_id, "contradicts", "矛盾引文"),
            (claim.id, source_id, "anchored", "仅定位未核验"),
            (uuid4(), source_id, "supported", "其它主张"),
            (claim.id, uuid4(), "supported", "其它来源"),
        ]
    ]
    repo = Repository(session)  # type: ignore[arg-type]
    assert await repo.supported_excerpt(claim.id, source_id) == "引文一\n引文二"
    assert await repo.supported_excerpt(claim.id, uuid4()) == ""


# 功能：验证锁定后读取最新决议，缓存仍未解决时也不能重复写入或改变既有决议。
@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["adopt_new", "keep_old"])
async def test_resolution_replay_refreshes_stale_dispute_after_lock(outcome):
    dispute, belief, claim = _entities()
    session = _Session(dispute, belief)
    replacement = models.Belief(
        id=uuid4(), statement=claim.statement, status=BeliefStatus.SUPPORTED
    )
    session.beliefs[replacement.id] = replacement
    latest = deepcopy(dispute)
    latest.status = DisputeStatus.RESOLVED_NEW
    latest.resolution_metadata = {
        "outcome": "adopt_new",
        "resolved_belief_id": str(replacement.id),
    }
    session.locked_dispute = latest
    repo = Repository(session)  # type: ignore[arg-type]
    kwargs = {
        "outcome": outcome,
        "rationale": "重试请求",
        "conditional_statement": "",
        "conditions": [],
        "evidence_source_ids": [],
    }
    if outcome == "adopt_new":
        assert await repo.apply_dispute_resolution(dispute, belief, claim, **kwargs) is replacement
        assert session.commits == 1
    else:
        with pytest.raises(ValueError, match="already been resolved differently"):
            await repo.apply_dispute_resolution(dispute, belief, claim, **kwargs)
        assert session.rollbacks == 1
    assert session.added == []
    assert belief.status == BeliefStatus.VERIFIED
