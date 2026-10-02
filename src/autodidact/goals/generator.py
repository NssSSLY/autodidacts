# 文件职责：根据使命和认知上下文提出候选目标；持久化与配额由控制器处理。
from pydantic import BaseModel, Field

from autodidact.brain.llm import LLM
from autodidact.brain.prompts import CURIOSITY_SYSTEM
from autodidact.schemas import CandidateGoal


class CandidateGoalList(BaseModel):
    goals: list[CandidateGoal] = Field(default_factory=list, max_length=12)


class GoalGenerator:
    # 功能：绑定负责目标提议的模型，不直接写入目标库。
    def __init__(self, llm: LLM):
        self.llm = llm

    # 功能：将使命和上下文交给结构化模型，返回候选目标列表供控制器筛选。
    async def generate(self, mission: str, context: str) -> list[CandidateGoal]:
        prompt = f"""
Long-term mission:\n{mission}\n\nCurrent epistemic state:\n{context}\n\n
Generate learning goals that close important gaps or disputes. Keep each goal narrow enough for one learning cycle.
"""
        result = await self.llm.structured(CURIOSITY_SYSTEM, prompt, CandidateGoalList)
        return result.goals
