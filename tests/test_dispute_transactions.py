from types import SimpleNamespace
from uuid import uuid4

import pytest

from autodidact import models
from autodidact.enums import BeliefStatus, DisputeStatus
from autodidact.knowledge.sources import EvidenceSource
from autodidact.repository import Repository


class _Result:
    def __init__(self, values):
        self.values = values

    def scalar_one(self):
        return self.values[0]

    def scalars(self):
        return self.values


class _Session:
    def __init__(self, dispute, belief):
        self.dispute = dispute
        self.belief = belief
        self.added = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, _statement):
        return _Result([self.dispute])

    async def get(self, model, _item_id):
        if model is models.Belief:
            return self.belief
        return None

    def add(self, item):
        self.added.append(item)

    async def flush(self):
        for item in self.added:
            if isinstance(item, models.Belief) and item.id is None:
                item.id = uuid4()

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


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


@pytest.mark.asyncio
async def test_adopt_new_resolution_commits_new_belief_evidence_and_histories_together():
    dispute, belief, claim = _entities()
    session = _Session(dispute, belief)
    repo = Repository(session)  # type: ignore[arg-type]
    first, second = uuid4(), uuid4()

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
    assert sum(isinstance(item, models.Evidence) for item in session.added) == 2
    assert sum(isinstance(item, models.BeliefHistory) for item in session.added) == 2


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


@pytest.mark.asyncio
async def test_repository_rejects_unlinked_sources_for_new_claim():
    dispute, belief, claim = _entities()
    source_id = uuid4()

    class SourceSession(_Session):
        async def execute(self, statement):
            assert "claim_evidence" in str(statement)
            return _Result([SimpleNamespace(source_id=source_id)])

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
