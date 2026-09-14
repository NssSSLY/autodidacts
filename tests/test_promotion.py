from autodidact.knowledge.promotion import BeliefPromotionPolicy


def test_open_dispute_blocks_belief_promotion():
    policy = BeliefPromotionPolicy(min_independent_sources_for_verified=2)

    decision = policy.decide(
        evaluation_passed=True,
        source_ids=["source-a", "source-b"],
        has_open_dispute=True,
    )

    assert decision.promote is False
    assert decision.reason == "open_dispute"


def test_claim_without_traceable_evidence_is_not_promoted():
    policy = BeliefPromotionPolicy(min_independent_sources_for_verified=2)

    decision = policy.decide(
        evaluation_passed=True,
        source_ids=[],
        has_open_dispute=False,
    )

    assert decision.promote is False
    assert decision.reason == "missing_traceable_evidence"


def test_single_source_claim_is_provisional():
    policy = BeliefPromotionPolicy(min_independent_sources_for_verified=2)

    decision = policy.decide(
        evaluation_passed=True,
        source_ids=["source-a"],
        has_open_dispute=False,
    )

    assert decision.promote is True
    assert decision.verified is False


def test_multi_source_claim_can_be_verified():
    policy = BeliefPromotionPolicy(min_independent_sources_for_verified=2)

    decision = policy.decide(
        evaluation_passed=True,
        source_ids=["source-a", "source-b", "source-b"],
        has_open_dispute=False,
    )

    assert decision.promote is True
    assert decision.verified is True
