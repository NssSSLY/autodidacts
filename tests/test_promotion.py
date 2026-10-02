# 文件职责：检查晋升的评估/争议/可追溯来源门槛与单源/多源状态。
from autodidact.knowledge.promotion import BeliefPromotionPolicy


# 功能：验证开放争议禁止晋升候选信念。
def test_open_dispute_blocks_belief_promotion():
    policy = BeliefPromotionPolicy(min_independent_sources_for_verified=2)

    decision = policy.decide(
        evaluation_passed=True,
        source_ids=["source-a", "source-b"],
        has_open_dispute=True,
    )

    assert decision.promote is False
    assert decision.reason == "open_dispute"


# 功能：验证无可追溯证据不能创建接纳结论。
def test_claim_without_traceable_evidence_is_not_promoted():
    policy = BeliefPromotionPolicy(min_independent_sources_for_verified=2)

    decision = policy.decide(
        evaluation_passed=True,
        source_ids=[],
        has_open_dispute=False,
    )

    assert decision.promote is False
    assert decision.reason == "missing_traceable_evidence"


# 功能：验证单来源不足以标 verified。
def test_single_source_claim_is_provisional():
    policy = BeliefPromotionPolicy(min_independent_sources_for_verified=2)

    decision = policy.decide(
        evaluation_passed=True,
        source_ids=["source-a"],
        has_open_dispute=False,
    )

    assert decision.promote is True
    assert decision.verified is False


# 功能：验证满足独立来源门槛的候选可以进入 verified 判断。
def test_multi_source_claim_can_be_verified():
    policy = BeliefPromotionPolicy(min_independent_sources_for_verified=2)

    decision = policy.decide(
        evaluation_passed=True,
        source_ids=["source-a", "source-b", "source-b"],
        has_open_dispute=False,
    )

    assert decision.promote is True
    assert decision.verified is True
