# 文件职责：从已读资料提出候选主张、引用与知识缺口；输出仍需核验。
from autodidact.brain.llm import LLM
from autodidact.brain.prompts import SYNTHESIS_SYSTEM
from autodidact.schemas import LearningResult, SourceDocument


class Synthesizer:
    # 功能：绑定负责资料综合的模型，不在此阶段晋升信念。
    def __init__(self, llm: LLM):
        self.llm = llm

    # 功能：组织目标与来源原文，提取 LearningResult 候选主张及引文，不将模型摘要直接当真。
    async def synthesize(self, goal: str, docs: list[SourceDocument]) -> LearningResult:
        packed = []
        for d in docs:
            packed.append(f"SOURCE URL: {d.url}\nTITLE: {d.title}\nCONTENT:\n{d.text[:12000]}")
        user = f"Learning goal: {goal}\n\n" + "\n\n---\n\n".join(packed)
        return await self.llm.structured(SYNTHESIS_SYSTEM, user, LearningResult)
