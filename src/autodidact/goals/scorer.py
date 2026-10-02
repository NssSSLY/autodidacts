# 文件职责：将重要性、未知度、探索、效用和成本组合为目标调度优先级。
from autodidact.schemas import CandidateGoal


# 功能：计算候选目标的加权优先级，探索率调节新颖性权重而非强制 70/30 配额。
def goal_score(goal: CandidateGoal, exploration_rate: float = 0.30) -> float:
    base = (
        0.30 * goal.importance
        + 0.25 * goal.uncertainty
        + 0.15 * goal.novelty
        + 0.20 * goal.utility
        + 0.10 * goal.prerequisite_score
        - 0.15 * goal.estimated_cost
    )
    return max(0.0, min(1.0, base + exploration_rate * 0.10 * goal.novelty))
