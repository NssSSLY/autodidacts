# 文件职责：检查四结果决议策略与 Resolver 的数据库证据门控。
from types import SimpleNamespace

import pytest

from autodidact.knowledge.disputes import DisputeResolutionPolicy, DisputeResolver
from autodidact.knowledge.sources import EvidenceSource
from autodidact.schemas import DisputeResolutionProposal


# 功能：构造两个独立高等级来源，用于决议资格样例。
def _sources() -> list[EvidenceSource]:
    return [
        EvidenceSource("source-1", publisher_key="alpha.example", evidence_level=4),
        EvidenceSource("source-2", publisher_key="beta.example", evidence_level=4),
    ]


# 功能：验证证据合格时四类明确决议均能经过策略核验。
def test_resolution_policy_accepts_all_four_explicit_outcomes_when_qualified():
    dispute = SimpleNamespace(previous_belief_state={"status": "verified"})
    policy = DisputeResolutionPolicy(min_independent_sources=2)
    common = {"evidence_source_ids": ["source-1", "source-2"]}

    for outcome in ("keep_old", "adopt_new", "conditional"):
        proposal = DisputeResolutionProposal(
            outcome=outcome,
            rationale="independent anchored evidence was reviewed",
            conditional_statement="仅在低照度条件下成立" if outcome == "conditional" else "",
            conditions=["低照度"] if outcome == "conditional" else [],
            **common,
        )
        decision = policy.decide(dispute, proposal, _sources(), {"source-1", "source-2"})
        assert decision.allowed is True
        assert decision.outcome == outcome
        assert decision.independent_source_count == 2

    unresolved = DisputeResolutionProposal(outcome="unresolved", rationale="证据仍不足")
    decision = policy.decide(dispute, unresolved, [], set())
    assert decision.allowed is True
    assert decision.outcome == "unresolved"


# 功能：验证未锚定来源或不完整条件提议被决议策略拒绝。
def test_resolution_policy_rejects_unanchored_or_incomplete_proposal():
    dispute = SimpleNamespace(previous_belief_state={"status": "verified"})
    policy = DisputeResolutionPolicy(min_independent_sources=2)
    proposal = DisputeResolutionProposal(
        outcome="conditional",
        rationale="需要条件化",
        evidence_source_ids=["source-1", "source-2"],
    )

    decision = policy.decide(dispute, proposal, _sources(), {"source-1", "source-2"})

    assert decision.allowed is False
    assert decision.reason == "missing_conditional_statement"


class _Repository:
    # 功能：保存预置合格来源与 applied 标志，模拟争议仓库。
    def __init__(self, sources=None):
        self.calls = []
        self.sources = _sources() if sources is None else sources

    # 功能：返回测试预设的数据库合格来源替身。
    async def qualified_resolution_sources(
        self, _dispute, _belief, _claim, _outcome, requested_source_ids
    ):
        return [source for source in self.sources if source.source_id in requested_source_ids]

    # 功能：记录被批准决议是否应用，避免实际修改数据库。
    async def apply_dispute_resolution(self, *args, **kwargs):
        self.calls.append((args, kwargs))


# 功能：验证 Resolver 仅提交通过策略的结果。
@pytest.mark.asyncio
async def test_resolver_applies_only_a_policy_approved_decision():
    repository = _Repository()
    resolver = DisputeResolver(repository, DisputeResolutionPolicy(min_independent_sources=2))
    dispute = SimpleNamespace(previous_belief_state={"status": "verified"})
    proposal = DisputeResolutionProposal(
        outcome="adopt_new",
        rationale="两份独立来源的原文锚点支持新主张",
        evidence_source_ids=["source-1", "source-2"],
    )

    decision = await resolver.resolve(
        dispute,
        SimpleNamespace(),
        SimpleNamespace(),
        proposal,
    )

    assert decision.allowed is True
    assert repository.calls[0][1]["outcome"] == "adopt_new"


# 功能：验证模型列出的 source_id 缺库内证据时不能直接决议。
@pytest.mark.asyncio
async def test_resolver_rejects_source_ids_without_database_backed_evidence():
    repository = _Repository(sources=[])
    resolver = DisputeResolver(repository, DisputeResolutionPolicy(min_independent_sources=2))
    proposal = DisputeResolutionProposal(
        outcome="adopt_new",
        rationale="未经验证的来源",
        evidence_source_ids=["source-1", "source-2"],
    )
    decision = await resolver.resolve(
        SimpleNamespace(previous_belief_state={"status": "verified"}),
        SimpleNamespace(),
        SimpleNamespace(),
        proposal,
    )
    assert decision.allowed is False
    assert repository.calls == []
