from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from autodidact.brain.llm import LLM


class BenchmarkItem(BaseModel):
    question: str
    rubric: str
    category: str


class BenchmarkJudge(BaseModel):
    score: float
    rationale: str


async def run_benchmark(llm: LLM, path: str) -> dict:
    items = [BenchmarkItem.model_validate(x) for x in json.loads(Path(path).read_text(encoding="utf-8"))]
    scores = []
    details = []
    for item in items:
        answer = await llm.text("Answer the benchmark question without external tools.", item.question, temperature=0.1)
        judge = await llm.structured(
            "Score an answer from 0 to 1 using the rubric. Return JSON only.",
            f"Question: {item.question}\nRubric: {item.rubric}\nAnswer: {answer}", BenchmarkJudge
        )
        scores.append(judge.score)
        details.append({"question": item.question, "score": judge.score, "rationale": judge.rationale})
    return {"score": sum(scores) / len(scores) if scores else 0.0, "n": len(scores), "details": details}
