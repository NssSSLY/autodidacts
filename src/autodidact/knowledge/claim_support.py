# 文件职责：验证候选主张引用是否来自已读原文，区分锚定成功与语义支持。
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Protocol

from autodidact.knowledge.sources import normalize_url
from autodidact.schemas import ClaimCitation, ClaimDraft


class SourceText(Protocol):
    id: object
    extracted_text: str | None


@dataclass(frozen=True, slots=True)
class ClaimEvidenceVerification:
    source_id: str | None
    source_url: str
    excerpt: str
    excerpt_hash: str
    status: str
    reason: str
    assessment: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ClaimSupportValidation:
    records: list[ClaimEvidenceVerification]

    # 功能：提取逐字定位成功的去重来源 ID；仅锚定不证明支持主张。
    @property
    def anchored_source_ids(self) -> list[str]:
        return list(
            dict.fromkeys(
                record.source_id
                for record in self.records
                if record.source_id is not None and record.status == "anchored"
            )
        )


class ClaimSupportValidator:
    """Verify citation provenance without treating a model quote as semantic proof."""

    # 功能：设置并校验最短引文长度，避免用过短片段伪造支持。
    def __init__(self, min_excerpt_chars: int = 12):
        if min_excerpt_chars < 1:
            raise ValueError("min_excerpt_chars must be positive")
        self.min_excerpt_chars = min_excerpt_chars

    # 功能：逐条检查 ClaimDraft 引文，返回含状态、原因和摘要的锚点结果。
    def validate(
        self,
        draft: ClaimDraft,
        sources_by_normalized_url: dict[str, SourceText],
    ) -> ClaimSupportValidation:
        records = [
            self._validate_citation(citation, sources_by_normalized_url)
            for citation in draft.citations
        ]
        return ClaimSupportValidation(records)

    # 功能：规范 URL/空白并检查已读来源、最短长度和逐字匹配，返回单条定位判定。
    def _validate_citation(
        self,
        citation: ClaimCitation,
        sources_by_normalized_url: dict[str, SourceText],
    ) -> ClaimEvidenceVerification:
        normalized_url = normalize_url(citation.source_url)
        excerpt = " ".join(citation.excerpt.split())
        excerpt_hash = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
        source = sources_by_normalized_url.get(normalized_url)
        if source is None:
            return ClaimEvidenceVerification(
                None,
                normalized_url,
                excerpt,
                excerpt_hash,
                "unresolved_source",
                "citation URL was not read",
            )
        if len(excerpt) < self.min_excerpt_chars:
            return ClaimEvidenceVerification(
                str(source.id),
                normalized_url,
                excerpt,
                excerpt_hash,
                "invalid_excerpt",
                "excerpt is too short",
            )
        source_text = " ".join((source.extracted_text or "").split())
        if not source_text:
            return ClaimEvidenceVerification(
                str(source.id),
                normalized_url,
                excerpt,
                excerpt_hash,
                "missing_source_text",
                "source has no extracted text",
            )
        if excerpt.casefold() not in source_text.casefold():
            return ClaimEvidenceVerification(
                str(source.id),
                normalized_url,
                excerpt,
                excerpt_hash,
                "unanchored",
                "excerpt was not found verbatim in source text",
            )
        return ClaimEvidenceVerification(
            str(source.id),
            normalized_url,
            excerpt,
            excerpt_hash,
            "anchored",
            "excerpt found verbatim in source text",
        )
