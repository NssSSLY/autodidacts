from pydantic import BaseModel, Field

from autodidact.brain.llm import LLM
from autodidact.brain.prompts import CURIOSITY_SYSTEM
from autodidact.schemas import CandidateGoal


class CandidateGoalList(BaseModel):
    goals: list[CandidateGoal] = Field(default_factory=list, max_length=12)


class GoalGenerator:
    def __init__(self, llm: LLM):
        self.llm = llm

    async def generate(self, mission: str, context: str) -> list[CandidateGoal]:
        prompt = f"""
Long-term mission:\n{mission}\n\nCurrent epistemic state:\n{context}\n\n
Generate learning goals that close important gaps or disputes. Keep each goal narrow enough for one learning cycle.
"""
        result = await self.llm.structured(CURIOSITY_SYSTEM, prompt, CandidateGoalList)
        return result.goals
