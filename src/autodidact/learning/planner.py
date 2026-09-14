from autodidact.brain.llm import LLM
from autodidact.brain.prompts import PLANNER_SYSTEM
from autodidact.schemas import SearchQueryPlan


class Planner:
    def __init__(self, llm: LLM):
        self.llm = llm

    async def plan(self, goal_title: str, known_context: str) -> SearchQueryPlan:
        user = f"Goal: {goal_title}\n\nKnown context:\n{known_context}\n\nCreate targeted web search queries."
        return await self.llm.structured(PLANNER_SYSTEM, user, SearchQueryPlan)
