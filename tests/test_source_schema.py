# 文件职责：检查来源证据等级及可信度字段的合法范围。
import pytest
from pydantic import ValidationError

from autodidact.schemas import SourceDocument


# 功能：参数化验证超范围等级/可信度不能通过来源 schema。
@pytest.mark.parametrize(
    ("field", "value"),
    [("evidence_level", -1), ("evidence_level", 6), ("credibility_score", 1.1)],
)
def test_source_evidence_metadata_is_bounded(field, value):
    payload = {"url": "https://example.org", "text": "evidence", field: value}

    with pytest.raises(ValidationError):
        SourceDocument(**payload)
