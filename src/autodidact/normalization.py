# 文件职责：集中规范文本与计算稳定摘要，统一目标去重和认识论幂等键。
from __future__ import annotations

import hashlib


# 功能：合并空白并忽略大小写，生成去重比较键，不改变保存的原文。
def normalize_text_key(value: str) -> str:
    """Create a comparison key without changing the human-readable stored text."""
    return " ".join(value.split()).casefold()


# 功能：为输入字段序列生成稳定摘要，用于证据或争议等重复写入保护。
def stable_key(*parts: object) -> str:
    """Return a fixed-width idempotency key for database-enforced writes."""
    payload = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# 功能：对规范主张正文计算摘要，供会话内候选主张唯一约束使用。
def claim_statement_key(statement: str) -> str:
    return stable_key(normalize_text_key(statement))
