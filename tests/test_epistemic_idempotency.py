# 文件职责：用内存会话替身检查目标、主张和争议的重复写入保护。
from types import SimpleNamespace
from uuid import uuid4

import pytest

from autodidact.enums import BeliefStatus, DisputeStatus, GoalStatus
from autodidact.repository import Repository
from autodidact.schemas import CandidateGoal, ClaimDraft


class Result:
    # 功能：保存模拟查询实体集合。
    def __init__(self, items):
        self.items = items

    # 功能：返回模拟集合供迭代过滤使用。
    def scalars(self):
        return self.items

    # 功能：返回首个实体或空，模拟单行读取。
    def scalar_one_or_none(self):
        return self.items[0] if self.items else None


class FakeSession:
    # 功能：保存返回实体和写入/提交统计，构造无数据库会话替身。
    def __init__(self, items):
        self.items = items
        self.added = []
        self.commits = 0

    # 功能：返回预设结果，不执行 SQL。
    async def execute(self, _statement):
        return Result(self.items)

    # 功能：记录待写实体，供幂等断言检查。
    def add(self, item):
        self.added.append(item)

    # 功能：累计提交次数。
    async def commit(self):
        self.commits += 1

    # 功能：提供无实际数据库刷新的兼容异步接口。
    async def refresh(self, _item):
        return None


class SequentialSession(FakeSession):
    # 功能：保存一系列查询结果，以模拟连续调用的不同状态。
    def __init__(self, result_sets):
        super().__init__([])
        self.result_sets = list(result_sets)

    # 功能：依次返回预设查询集，供同事务多个查询路径使用。
    async def execute(self, _statement):
        return Result(self.result_sets.pop(0))


# 功能：验证大小写/空白等规范化后复用活动目标。
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


# 功能：验证同一学习会话的重复主张不会新建第二份。
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


# 功能：验证重复开放冲突复用争议且不重复写旧信念历史。
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


# 功能：验证旧争议缺幂等键时仍可通过关系查询复用。
@pytest.mark.asyncio
async def test_legacy_open_dispute_without_dedup_key_is_reused():
    legacy_dispute = SimpleNamespace(status=DisputeStatus.UNRESOLVED, dedup_key=None)
    session = SequentialSession([[], [legacy_dispute]])
    repo = Repository(session)  # type: ignore[arg-type]
    belief = SimpleNamespace(id=uuid4(), status=BeliefStatus.VERIFIED, confidence=0.9)
    claim = SimpleNamespace(id=uuid4())

    dispute, created = await repo.get_or_create_dispute(belief, claim, 0.95, "冲突")

    assert dispute is legacy_dispute
    assert created is False
    assert belief.status == BeliefStatus.VERIFIED
