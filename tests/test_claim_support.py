# 文件职责：检查引文是否能逐字定位到已读来源，拒绝伪造或未读 URL。
from types import SimpleNamespace
from uuid import uuid4

from autodidact.knowledge.claim_support import ClaimSupportValidator
from autodidact.knowledge.sources import normalize_url
from autodidact.schemas import ClaimCitation, ClaimDraft


# 功能：验证逐字引文获得 anchored 状态并关联实际来源 ID。
def test_verbatim_citation_is_anchored_to_read_source():
    url = "https://example.org/report?utm_source=test"
    source = SimpleNamespace(
        id=uuid4(),
        extracted_text="The navigation filter fuses IMU samples with camera observations.",
    )
    draft = ClaimDraft(
        statement="滤波器融合 IMU 与相机观测。",
        topic="VIO",
        confidence=0.8,
        citations=[
            ClaimCitation(
                source_url=url,
                excerpt="navigation filter fuses IMU samples with camera observations",
            )
        ],
    )

    result = ClaimSupportValidator().validate(draft, {normalize_url(url): source})

    assert result.anchored_source_ids == [str(source.id)]
    assert result.records[0].status == "anchored"


# 功能：验证未读来源或不匹配引文不能提供合格来源身份。
def test_unread_or_nonverbatim_citation_cannot_supply_claim_source_id():
    source = SimpleNamespace(id=uuid4(), extracted_text="A short factual source paragraph.")
    draft = ClaimDraft(
        statement="待验证主张",
        topic="测试",
        confidence=0.5,
        citations=[
            ClaimCitation(source_url="https://example.org/read", excerpt="invented quotation text"),
            ClaimCitation(source_url="https://unread.example/", excerpt="unread quotation text"),
        ],
    )

    result = ClaimSupportValidator().validate(
        draft,
        {normalize_url("https://example.org/read"): source},
    )

    assert result.anchored_source_ids == []
    assert [record.status for record in result.records] == ["unanchored", "unresolved_source"]
