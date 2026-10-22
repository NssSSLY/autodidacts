# 文件职责：统一接纳信念的SQL读取门控，供记忆快照和混合检索各通道复用。
from sqlalchemy import and_, exists, select

from autodidact import models


# 功能：仅接纳active、supported/verified、无开放争议且有正等级来源支持证据的信念。
def accepted_belief_predicate():
    belief = models.Belief
    disputed = exists(
        select(models.Dispute.id)
        .where(
            models.Dispute.belief_id == belief.id,
            models.Dispute.status.in_(["open", "investigating", "unresolved"]),
        )
        .correlate(belief)
    )
    supported = exists(
        select(models.Evidence.id)
        .where(
            models.Evidence.belief_id == belief.id,
            models.Evidence.stance == "support",
            models.Evidence.evidence_level > 0,
            models.Evidence.source_id.is_not(None),
        )
        .correlate(belief)
    )
    return and_(
        belief.status.in_(["supported", "verified"]),
        belief.lifecycle_state == "active",
        ~disputed,
        supported,
    )
