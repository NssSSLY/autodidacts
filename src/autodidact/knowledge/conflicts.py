from autodidact.brain.llm import LLM
from autodidact.brain.prompts import CONTRADICTION_SYSTEM
from autodidact.schemas import ContradictionResult


class ConflictDetector:
    def __init__(self, llm: LLM):
        self.llm = llm

    async def compare(self, existing_belief: str, incoming_claim: str) -> ContradictionResult:
        prompt = f"Existing belief:\n{existing_belief}\n\nIncoming claim:\n{incoming_claim}"
        return await self.llm.structured(CONTRADICTION_SYSTEM, prompt, ContradictionResult)
