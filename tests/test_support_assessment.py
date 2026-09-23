from types import SimpleNamespace
from uuid import uuid4

import pytest

from autodidact.knowledge.claim_support import ClaimSupportValidator
from autodidact.knowledge.sources import normalize_url
from autodidact.knowledge.support_assessment import ClaimSupportAssessor, SupportVerdict
from autodidact.schemas import ClaimCitation, ClaimDraft


class VerdictLLM:
    provider_name = "test"
    model_name = "fixture"

    def __init__(self, relation):
        self.relation = relation

    async def structured(self, _system, _user, _schema):
        return SupportVerdict(relation=self.relation, reason="scope checked")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "relation,expected",
    [
        ("supports", True),
        ("contradicts", False),
        ("conditional", False),
        ("unclear", False),
    ],
)
async def test_only_supported_anchored_quote_can_supply_promotion_evidence(relation, expected):
    url = "https://example.org/source"
    passage = (
        "The filter uses a camera input."
        if relation == "supports"
        else "The filter does not use a camera input."
    )
    source = SimpleNamespace(id=uuid4(), extracted_text=passage)
    draft = ClaimDraft(
        statement="The filter uses a camera input.",
        topic="VIO",
        confidence=0.8,
        citations=[ClaimCitation(source_url=url, excerpt=passage)],
    )
    sources = {normalize_url(url): source}
    anchored = ClaimSupportValidator().validate(draft, sources)
    assessment = await ClaimSupportAssessor(VerdictLLM(relation)).assess(draft, anchored, sources)

    assert bool(assessment.supported_source_ids) is expected
    assert assessment.records[0].reason.startswith("test/fixture:")


@pytest.mark.asyncio
async def test_provider_failure_keeps_claim_unverified():
    class FailingLLM(VerdictLLM):
        async def structured(self, _system, _user, _schema):
            raise TimeoutError("offline")

    url = "https://example.org/source"
    source = SimpleNamespace(id=uuid4(), extracted_text="The system uses camera measurements.")
    draft = ClaimDraft(
        statement="The system uses camera measurements.",
        topic="VIO",
        confidence=0.7,
        citations=[ClaimCitation(source_url=url, excerpt="The system uses camera measurements.")],
    )
    sources = {normalize_url(url): source}
    anchored = ClaimSupportValidator().validate(draft, sources)
    assessment = await ClaimSupportAssessor(FailingLLM("supports")).assess(draft, anchored, sources)
    assert assessment.supported_source_ids == []
    assert assessment.records[0].status == "unclear"
