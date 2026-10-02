# 文件职责：检查原文语义支持、条件/矛盾排除和模型失败降级。
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

    # 功能：设置预期支持关系供语义判定替身返回。
    def __init__(self, relation):
        self.relation = relation

    # 功能：返回指定关系的结构化判定，不调用真实模型。
    async def structured(self, _system, _user, _schema):
        return SupportVerdict(relation=self.relation, reason="scope checked")


# 功能：验证只有已锚定且语义支持的引文能贡献晋升来源。
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


# 功能：验证模型判定失败保留 unclear 而非放行主张。
@pytest.mark.asyncio
async def test_provider_failure_keeps_claim_unverified():
    class FailingLLM(VerdictLLM):
        # 功能：主动抛出提供方错误，模拟语义核验不可用。
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
