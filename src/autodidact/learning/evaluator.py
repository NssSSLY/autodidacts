from autodidact.brain.llm import LLM
from autodidact.brain.prompts import EVALUATOR_SYSTEM
from autodidact.config import agent_config
from autodidact.schemas import EvaluationResult, LearningResult, SourceDocument


class Evaluator:
    def __init__(self, llm: LLM):
        self.llm = llm

    async def evaluate(self, goal: str, learned: LearningResult, docs: list[SourceDocument]) -> EvaluationResult:
        evidence = "\n".join(
            f"[{d.url}] {d.text[:3500]}" for d in docs[:6]
        )
        learned_json = learned.model_dump_json(indent=2)
        prompt = f"""
Goal: {goal}

Learner result (do not assume correct):
{learned_json}

Evidence excerpts:
{evidence}

Create several closed-book/transfer questions internally, assess whether the learned claims would
support correct answers, and return the score components. Pass threshold is {agent_config().learning.pass_score}.
"""
        result = await self.llm.structured(EVALUATOR_SYSTEM, prompt, EvaluationResult)
        result.passed = result.score >= agent_config().learning.pass_score
        return result
