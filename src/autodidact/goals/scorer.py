from autodidact.schemas import CandidateGoal


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
