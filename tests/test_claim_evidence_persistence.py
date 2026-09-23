from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from autodidact.repository import Repository, _expected_unique_violation


class _Result:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self.values


class _Session:
    def __init__(self, evidence):
        self.evidence = evidence
        self.commits = 0

    async def execute(self, _query):
        return _Result(self.evidence)

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_legacy_anchor_without_semantic_support_cannot_remain_claim_evidence():
    source_id = uuid4()
    claim = SimpleNamespace(id=uuid4(), source_ids=[str(source_id)])
    session = _Session([SimpleNamespace(source_id=source_id, status="anchored")])
    repo = Repository(session)  # type: ignore[arg-type]

    await repo.record_claim_evidence(claim, [])

    assert claim.source_ids == []
    assert session.commits == 1


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


def test_asyncpg_wrapped_unique_constraint_is_recovered():
    server_error = Exception("duplicate key")
    server_error.constraint_name = "uq_claim_evidence_anchor"
    wrapper = Exception("translated asyncpg exception")
    wrapper.sqlstate = "23505"
    wrapper.__cause__ = server_error
    assert _expected_unique_violation(
        IntegrityError("insert", {}, wrapper), "uq_claim_evidence_anchor"
    )
