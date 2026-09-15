from __future__ import annotations


def normalize_text_key(value: str) -> str:
    """Create a comparison key without changing the human-readable stored text."""
    return " ".join(value.split()).casefold()
