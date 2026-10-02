# 文件职责：结合评估、开放争议、独立来源数量和质量控制信念晋升。
from __future__ import annotations

from dataclasses import dataclass

from autodidact.knowledge.sources import (
    EvidenceSource,
    independent_source_representatives,
)


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    promote: bool
    verified: bool
    reason: str
    independent_source_count: int = 0
    qualifying_source_count: int = 0
    evidence_score: float = 0.0


class BeliefPromotionPolicy:
    # 功能：校验 verified 所需独立来源数量及最小等级，保存晋升门槛。
    def __init__(
        self,
        min_independent_sources_for_verified: int,
        min_evidence_level_for_verified: int = 1,
    ):
        if min_independent_sources_for_verified < 1:
            raise ValueError("min_independent_sources_for_verified must be at least 1")
        if not 0 <= min_evidence_level_for_verified <= 5:
            raise ValueError("min_evidence_level_for_verified must be between 0 and 5")
        self.min_independent_sources_for_verified = min_independent_sources_for_verified
        self.min_evidence_level_for_verified = min_evidence_level_for_verified

    # 功能：评估失败/开放争议/无证据时拒绝晋升；合格独立证据不足仅允许非 verified 状态。
    def decide(
        self,
        *,
        evaluation_passed: bool,
        has_open_dispute: bool,
        sources: list[EvidenceSource] | None = None,
        source_ids: list[str] | None = None,
    ) -> PromotionDecision:
        if not evaluation_passed:
            return PromotionDecision(False, False, "evaluation_failed")
        if has_open_dispute:
            return PromotionDecision(False, False, "open_dispute")

        # ``source_ids`` keeps the public API backward compatible. New code must
        # provide rich source records so publisher/content independence can be checked.
        if sources is None:
            sources = [
                EvidenceSource(
                    source_id=source_id,
                    publisher_key=source_id,
                    content_hash=source_id,
                    evidence_level=self.min_evidence_level_for_verified,
                )
                for source_id in (source_ids or [])
            ]
        independent = independent_source_representatives(sources)
        if not independent:
            return PromotionDecision(False, False, "missing_traceable_evidence")

        qualifying = [
            source
            for source in independent
            if source.evidence_level >= self.min_evidence_level_for_verified
        ]
        verified = len(qualifying) >= self.min_independent_sources_for_verified
        evidence_score = sum(
            min(1.0, source.evidence_level / 5) * source.credibility_score for source in independent
        ) / len(independent)
        reason = "qualified" if verified else "insufficient_independent_quality_sources"
        return PromotionDecision(
            True,
            verified,
            reason,
            independent_source_count=len(independent),
            qualifying_source_count=len(qualifying),
            evidence_score=round(evidence_score, 4),
        )
