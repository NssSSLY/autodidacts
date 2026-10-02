# 文件职责：依据学习结果与评估提出失败原因、缺口、行动和研究经验。
from autodidact.brain.llm import LLM
from autodidact.brain.prompts import REFLECTION_SYSTEM
from autodidact.schemas import EvaluationResult, LearningResult, ReflectionResult


class Reflector:
    # 功能：绑定反思模型，持久记录由控制器负责。
    def __init__(self, llm: LLM):
        self.llm = llm

    # 功能：将目标、学习输出和评估传入模型，返回反思提议供后续目标/方法提取使用。
    async def reflect(self, goal: str, result: LearningResult, evaluation: EvaluationResult) -> ReflectionResult:
        prompt = (
            f"Goal: {goal}\n\nLearning result:\n{result.model_dump_json(indent=2)}"
            f"\n\nEvaluation:\n{evaluation.model_dump_json(indent=2)}"
        )
        return await self.llm.structured(REFLECTION_SYSTEM, prompt, ReflectionResult)
