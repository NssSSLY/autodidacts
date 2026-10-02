# 文件职责：检查结构化主张 schema 的置信度范围约束。
import pytest
from pydantic import ValidationError

from autodidact.schemas import ClaimDraft


# 功能：验证超出合法概率范围的 ClaimDraft 不能通过 Pydantic。
def test_claim_confidence_is_bounded():
    with pytest.raises(ValidationError):
        ClaimDraft(statement="x", topic="t", confidence=1.2)
