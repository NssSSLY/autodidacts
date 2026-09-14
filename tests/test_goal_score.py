from autodidact.goals.scorer import goal_score
from autodidact.schemas import CandidateGoal


def test_goal_score_prefers_important_uncertain_goal():
    high = CandidateGoal(title="a", importance=1, uncertainty=1, novelty=0.8, utility=1, prerequisite_score=0.8, estimated_cost=0.2)
    low = CandidateGoal(title="b", importance=0.2, uncertainty=0.2, novelty=0.2, utility=0.2, prerequisite_score=0.2, estimated_cost=0.8)
    assert goal_score(high) > goal_score(low)
