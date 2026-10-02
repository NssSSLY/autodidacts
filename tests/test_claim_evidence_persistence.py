# 文件职责：检查主张证据的语义状态持久化与已知唯一约束竞争识别。
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from autodidact.repository import Repository, _expected_unique_violation


class _Result:
    # 功能：保存模拟查询返回值，供证据持久化单元测试使用。
    def __init__(self, values):
        self.values = values

    # 功能：返回模拟标量集合，不执行数据库查询。
    def scalars(self):
        return self.values


class _Session:
    # 功能：保存待复核证据和提交计数，模拟证据状态写入会话。
    def __init__(self, evidence):
        self.evidence = evidence
        self.commits = 0

    # 功能：以预置证据构造查询结果，隔离真实数据库。
    async def execute(self, _query):
        return _Result(self.evidence)

    # 功能：累计模拟提交次数以检查持久化动作。
    async def commit(self):
        self.commits += 1


# 功能：验证旧 anchored 记录不能绕过新增语义支持判定。
@pytest.mark.asyncio
async def test_legacy_anchor_without_semantic_support_cannot_remain_claim_evidence():
    source_id = uuid4()
    claim = SimpleNamespace(id=uuid4(), source_ids=[str(source_id)])
    session = _Session([SimpleNamespace(source_id=source_id, status="anchored")])
    repo = Repository(session)  # type: ignore[arg-type]

    await repo.record_claim_evidence(claim, [])

    assert claim.source_ids == []
    assert session.commits == 1


# 功能：验证同来源混合支持/反对记录时不能贡献晋升证据。
@pytest.mark.asyncio
async def test_mixed_supported_and_contradictory_quotes_disqualify_same_source():
    source_id = uuid4()
    claim = SimpleNamespace(id=uuid4(), source_ids=[str(source_id)])
    session = _Session(
        [
            SimpleNamespace(source_id=source_id, status="supported"),
            SimpleNamespace(source_id=source_id, status="contradicts"),
        ]
    )
    repo = Repository(session)  # type: ignore[arg-type]

    await repo.record_claim_evidence(claim, [])

    assert claim.source_ids == []


# 功能：验证只有明确已知的唯一约束错误可作为重复写入恢复。
def test_only_known_unique_constraint_is_safe_to_recover():
    known = SimpleNamespace(sqlstate="23505", constraint_name="uq_claim_evidence_anchor")
    foreign_key = SimpleNamespace(sqlstate="23503", constraint_name="claim_evidence_source_id_fkey")
    wrong_unique = SimpleNamespace(sqlstate="23505", constraint_name="other_unique")
    assert _expected_unique_violation(
        IntegrityError("insert", {}, known), "uq_claim_evidence_anchor"
    )
    assert not _expected_unique_violation(
        IntegrityError("insert", {}, foreign_key), "uq_claim_evidence_anchor"
    )
    assert not _expected_unique_violation(
        IntegrityError("insert", {}, wrong_unique), "uq_claim_evidence_anchor"
    )


# 功能：验证能从 asyncpg 包装异常的底层原因识别指定约束。
def test_asyncpg_wrapped_unique_constraint_is_recovered():
    server_error = Exception("duplicate key")
    server_error.constraint_name = "uq_claim_evidence_anchor"
    wrapper = Exception("translated asyncpg exception")
    wrapper.sqlstate = "23505"
    wrapper.__cause__ = server_error
    assert _expected_unique_violation(
        IntegrityError("insert", {}, wrapper), "uq_claim_evidence_anchor"
    )
