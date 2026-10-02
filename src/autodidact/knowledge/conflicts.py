# 文件职责：用结构化观察比较旧信念和新主张的关系，是否开争议由控制器门槛决定。
from autodidact.brain.llm import LLM
from autodidact.brain.prompts import CONTRADICTION_SYSTEM
from autodidact.schemas import ContradictionResult


class ConflictDetector:
    # 功能：绑定关系判定模型，避免在此层直接修改信念。
    def __init__(self, llm: LLM):
        self.llm = llm

    # 功能：比较旧信念与候选主张并返回矛盾/条件等关系及分数，作为可审查的提议。
    async def compare(self, existing_belief: str, incoming_claim: str) -> ContradictionResult:
        prompt = f"Existing belief:\n{existing_belief}\n\nIncoming claim:\n{incoming_claim}"
        return await self.llm.structured(CONTRADICTION_SYSTEM, prompt, ContradictionResult)
