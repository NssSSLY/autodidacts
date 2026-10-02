from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from autodidact.enums import DisputeStatus
from autodidact.knowledge.sources import EvidenceSource, independent_source_representatives
from autodidact.schemas import DisputeResolutionProposal


class DisputeRecord(Protocol):
    previous_belief_state: dict


class DisputeRepository(Protocol):
    async def qualified_resolution_sources(
        self,
        dispute: object,
        belief: object,
        claim: object,
        outcome: str,
        requested_source_ids: list[str],
    ) -> list[EvidenceSource]: ...

    async def apply_dispute_resolution(
        self,
        dispute: object,
        belief: object,
        claim: object,
        *,
        outcome: str,
        rationale: str,
        conditional_statement: str,
        conditions: list[str],
        evidence_source_ids: list[str],
    ) -> object: ...


ResolutionOutcome = Literal["keep_old", "adopt_new", "conditional", "unresolved"]


@dataclass(frozen=True, slots=True)
class ResolutionDecision:
    outcome: ResolutionOutcome
    allowed: bool
    reason: str
    independent_source_count: int
    qualified_source_ids: tuple[str, ...] = ()


class DisputeResolutionPolicy:
    """Gate resolution proposals with independently anchored evidence."""

    def __init__(self, min_independent_sources: int = 2):
        if min_independent_sources < 1:
            raise ValueError("min_independent_sources must be at least 1")
        self.min_independent_sources = min_independent_sources

    def decide(
        self,
        dispute: DisputeRecord,
        proposal: DisputeResolutionProposal,
        sources: list[EvidenceSource],
        anchored_source_ids: set[str],
    ) -> ResolutionDecision:
        if proposal.outcome == "unresolved":
            return ResolutionDecision("unresolved", True, "explicitly_unresolved", 0)
        if proposal.outcome == "keep_old" and not dispute.previous_belief_state.get("status"):
            return ResolutionDecision("keep_old", False, "missing_prior_belief_state", 0)
        if proposal.outcome == "conditional" and not proposal.conditional_statement.strip():
            return ResolutionDecision("conditional", False, "missing_conditional_statement", 0)
        if proposal.outcome == "conditional" and not proposal.conditions:
            return ResolutionDecision("conditional", False, "missing_conditions", 0)

        requested = set(proposal.evidence_source_ids)
        eligible = [
            source
            for source in sources
            if source.source_id in requested and source.source_id in anchored_source_ids
        ]
        independent = independent_source_representatives(eligible)
        if len(independent) < self.min_independent_sources:
            return ResolutionDecision(
                proposal.outcome,
                False,
                "insufficient_independent_anchored_evidence",
                len(independent),
            )
        return ResolutionDecision(
            proposal.outcome,
            True,
            "qualified",
            len(independent),
            tuple(source.source_id for source in independent),
        )


class DisputeResolver:
    """Applies one of four evidence-gated outcomes; never trusts a proposal alone."""

    def __init__(self, repository: DisputeRepository, policy: DisputeResolutionPolicy):
        self.repository = repository
        self.policy = policy

    async def resolve(
        self,
        dispute: DisputeRecord,
        belief: object,
        claim: object,
        proposal: DisputeResolutionProposal,
    ) -> ResolutionDecision:
        extra = (
            {"conditional_claim_id": proposal.conditional_claim_id}
            if proposal.outcome == "conditional" and proposal.conditional_claim_id
            else {}
        )
        sources = await self.repository.qualified_resolution_sources(
            dispute, belief, claim, proposal.outcome, proposal.evidence_source_ids, **extra
        )
        decision = self.policy.decide(
            dispute, proposal, sources, {source.source_id for source in sources}
        )
        if not decision.allowed:
            return decision
        await self.repository.apply_dispute_resolution(
            dispute,
            belief,
            claim,
            outcome=decision.outcome,
            rationale=proposal.rationale,
            conditional_statement=proposal.conditional_statement,
            conditions=proposal.conditions,
            evidence_source_ids=list(decision.qualified_source_ids),
            **extra,
        )
        return decision


DISPUTE_STATUS_BY_OUTCOME = {
    "keep_old": DisputeStatus.RESOLVED_OLD,
    "adopt_new": DisputeStatus.RESOLVED_NEW,
    "conditional": DisputeStatus.RESOLVED_CONDITIONAL,
    "unresolved": DisputeStatus.UNRESOLVED,
}
