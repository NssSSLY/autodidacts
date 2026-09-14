from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    promote: bool
    verified: bool
    reason: str


class BeliefPromotionPolicy:
    def __init__(self, min_independent_sources_for_verified: int):
        if min_independent_sources_for_verified < 1:
            raise ValueError("min_independent_sources_for_verified must be at least 1")
        self.min_independent_sources_for_verified = min_independent_sources_for_verified

    def decide(
        self,
        *,
        evaluation_passed: bool,
        source_ids: list[str],
        has_open_dispute: bool,
    ) -> PromotionDecision:
        if not evaluation_passed:
            return PromotionDecision(False, False, "evaluation_failed")
        if has_open_dispute:
            return PromotionDecision(False, False, "open_dispute")
        independent_source_ids = set(source_ids)
        if not independent_source_ids:
            return PromotionDecision(False, False, "missing_traceable_evidence")
        verified = (
            len(independent_source_ids) >= self.min_independent_sources_for_verified
        )
        return PromotionDecision(True, verified, "qualified")
