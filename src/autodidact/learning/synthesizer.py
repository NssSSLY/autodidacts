from autodidact.brain.llm import LLM
from autodidact.brain.prompts import SYNTHESIS_SYSTEM
from autodidact.schemas import LearningResult, SourceDocument


class Synthesizer:
    def __init__(self, llm: LLM):
        self.llm = llm

    async def synthesize(self, goal: str, docs: list[SourceDocument]) -> LearningResult:
        packed = []
        for d in docs:
            packed.append(f"SOURCE URL: {d.url}\nTITLE: {d.title}\nCONTENT:\n{d.text[:12000]}")
        user = f"Learning goal: {goal}\n\n" + "\n\n---\n\n".join(packed)
        return await self.llm.structured(SYNTHESIS_SYSTEM, user, LearningResult)
