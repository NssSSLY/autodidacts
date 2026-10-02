# 文件职责：检查来源质量与出版方独立性共同限制 verified。
from autodidact.knowledge.promotion import BeliefPromotionPolicy
from autodidact.knowledge.sources import EvidenceSource


# 功能：验证同出版方两个页面不能算两份独立证明。
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


# 功能：验证独立但低等级页面不足以标 verified。
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


# 功能：验证多份合格独立来源满足 verified 门槛。
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
