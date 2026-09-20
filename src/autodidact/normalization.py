from __future__ import annotations

import hashlib


def normalize_text_key(value: str) -> str:
    """Create a comparison key without changing the human-readable stored text."""
    return " ".join(value.split()).casefold()


def stable_key(*parts: object) -> str:
    """Return a fixed-width idempotency key for database-enforced writes."""
    payload = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def claim_statement_key(statement: str) -> str:
    return stable_key(normalize_text_key(statement))
