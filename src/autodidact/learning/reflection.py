from autodidact.brain.llm import LLM
from autodidact.brain.prompts import REFLECTION_SYSTEM
from autodidact.schemas import EvaluationResult, LearningResult, ReflectionResult


class Reflector:
    def __init__(self, llm: LLM):
        self.llm = llm

    async def reflect(self, goal: str, result: LearningResult, evaluation: EvaluationResult) -> ReflectionResult:
        prompt = (
            f"Goal: {goal}\n\nLearning result:\n{result.model_dump_json(indent=2)}"
            f"\n\nEvaluation:\n{evaluation.model_dump_json(indent=2)}"
        )
        return await self.llm.structured(REFLECTION_SYSTEM, prompt, ReflectionResult)
