# 文件职责：保留早期文件式基准评分接口；当前 CLI 冻结实验走 experiments.py。
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


# 功能：读取早期题集、无工具回答后用模型 rubric 评分并汇总；不提供当前冻结快照与独立真值保证。
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
