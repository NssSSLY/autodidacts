from types import SimpleNamespace
from uuid import uuid4

import pytest

from autodidact.enums import BeliefStatus, DisputeStatus, GoalStatus
from autodidact.repository import Repository
from autodidact.schemas import CandidateGoal, ClaimDraft


class Result:
    def __init__(self, items):
        self.items = items

    def scalars(self):
        return self.items

    def scalar_one_or_none(self):
        return self.items[0] if self.items else None


class FakeSession:
    def __init__(self, items):
        self.items = items
        self.added = []
        self.commits = 0

    async def execute(self, _statement):
        return Result(self.items)

    def add(self, item):
        self.added.append(item)

    async def commit(self):
        self.commits += 1

    async def refresh(self, _item):
        return None


@pytest.mark.asyncio
async def test_active_goal_title_is_deduplicated_after_text_normalization():
    existing = SimpleNamespace(title="调查  VIO 冲突", status=GoalStatus.DISCOVERED)
    session = FakeSession([existing])
    repo = Repository(session)  # type: ignore[arg-type]

    goal, created = await repo.add_goal_if_absent(
        CandidateGoal(title="  调查 vio 冲突  "),
        score=0.8,
    )

    assert goal is existing
    assert created is False
    assert session.added == []
    assert session.commits == 0


@pytest.mark.asyncio
async def test_claim_is_idempotent_within_learning_session():
    existing = SimpleNamespace(statement="IMU 提供短期约束")
    session = FakeSession([existing])
    repo = Repository(session)  # type: ignore[arg-type]

    claim = await repo.add_claim(
        ClaimDraft(statement=" IMU  提供短期约束 ", topic="VIO", confidence=0.8),
        uuid4(),
        ["source-a"],
    )

    assert claim is existing
    assert session.added == []


@pytest.mark.asyncio
async def test_open_dispute_is_idempotent_and_does_not_rewrite_belief_history():
    existing_dispute = SimpleNamespace(status=DisputeStatus.UNRESOLVED)
    session = FakeSession([existing_dispute])
    repo = Repository(session)  # type: ignore[arg-type]
    belief = SimpleNamespace(id=uuid4(), status=BeliefStatus.VERIFIED, confidence=0.9)
    claim = SimpleNamespace(id=uuid4())

    dispute, created = await repo.get_or_create_dispute(belief, claim, 0.95, "冲突")

    assert dispute is existing_dispute
    assert created is False
    assert belief.status == BeliefStatus.VERIFIED
    assert session.added == []
