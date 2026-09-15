import pytest
from pydantic import ValidationError

from autodidact.schemas import SourceDocument


@pytest.mark.parametrize(
    ("field", "value"),
    [("evidence_level", -1), ("evidence_level", 6), ("credibility_score", 1.1)],
)
def test_source_evidence_metadata_is_bounded(field, value):
    payload = {"url": "https://example.org", "text": "evidence", field: value}

    with pytest.raises(ValidationError):
        SourceDocument(**payload)
