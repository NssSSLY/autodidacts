# 文件职责：将研究目标与认知上下文转成查询计划，不直接执行网页操作。
from autodidact.brain.llm import LLM
from autodidact.brain.prompts import PLANNER_SYSTEM
from autodidact.schemas import SearchQueryPlan


class Planner:
    # 功能：绑定规划模型，供后续生成结构化搜索计划。
    def __init__(self, llm: LLM):
        self.llm = llm

    # 功能：输入目标和参考上下文，返回结构化 SearchQueryPlan；实际查询数量限制由后续调用路径控制。
    async def plan(self, goal_title: str, known_context: str) -> SearchQueryPlan:
        user = f"Goal: {goal_title}\n\nKnown context:\n{known_context}\n\nCreate targeted web search queries."
        return await self.llm.structured(PLANNER_SYSTEM, user, SearchQueryPlan)
