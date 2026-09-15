from autodidact.knowledge.promotion import BeliefPromotionPolicy
from autodidact.knowledge.sources import EvidenceSource


def test_two_pages_from_same_publisher_are_not_independent_verification():
    policy = BeliefPromotionPolicy(2, min_evidence_level_for_verified=2)
    decision = policy.decide(
        evaluation_passed=True,
        has_open_dispute=False,
        sources=[
            EvidenceSource("a", publisher_key="example.org", evidence_level=4),
            EvidenceSource("b", publisher_key="example.org", evidence_level=4),
        ],
    )

    assert decision.promote is True
    assert decision.verified is False
    assert decision.independent_source_count == 1


def test_low_quality_independent_pages_do_not_create_verified_belief():
    policy = BeliefPromotionPolicy(2, min_evidence_level_for_verified=2)
    decision = policy.decide(
        evaluation_passed=True,
        has_open_dispute=False,
        sources=[
            EvidenceSource("a", publisher_key="one.org", evidence_level=1),
            EvidenceSource("b", publisher_key="two.org", evidence_level=1),
        ],
    )

    assert decision.promote is True
    assert decision.verified is False
    assert decision.qualifying_source_count == 0


def test_independent_quality_sources_can_create_verified_belief():
    policy = BeliefPromotionPolicy(2, min_evidence_level_for_verified=2)
    decision = policy.decide(
        evaluation_passed=True,
        has_open_dispute=False,
        sources=[
            EvidenceSource("a", publisher_key="one.org", evidence_level=2, credibility_score=0.6),
            EvidenceSource("b", publisher_key="two.org", evidence_level=4, credibility_score=0.9),
        ],
    )

    assert decision.verified is True
    assert decision.qualifying_source_count == 2
    assert decision.evidence_score == 0.48
