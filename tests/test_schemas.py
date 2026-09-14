import pytest
from pydantic import ValidationError

from autodidact.schemas import ClaimDraft


def test_claim_confidence_is_bounded():
    with pytest.raises(ValidationError):
        ClaimDraft(statement="x", topic="t", confidence=1.2)
