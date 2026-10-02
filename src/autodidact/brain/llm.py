from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from autodidact.config import RuntimeSettings, runtime_settings

T = TypeVar("T", bound=BaseModel)


class LLM(ABC):
    provider_name: str
    model_name: str

    @abstractmethod
    async def text(self, system: str, user: str, *, temperature: float | None = None) -> str:
        raise NotImplementedError

    async def structured(self, system: str, user: str, schema: type[T]) -> T:
        instruction = (
            user
            + "\n\nReturn ONLY valid JSON matching this JSON schema:\n"
            + json.dumps(schema.model_json_schema(), ensure_ascii=False)
        )
        raw = await self.text(system, instruction, temperature=0.1)
        self.last_response = raw
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:].lstrip()
        return schema.model_validate_json(raw)


class MockLLM(LLM):
    provider_name = "mock"
    model_name = "mock-v0"

    async def text(self, system: str, user: str, *, temperature: float | None = None) -> str:
        return "Mock provider active. Configure an LLM provider for real learning."

    async def structured(self, system: str, user: str, schema: type[T]) -> T:
        # Minimal deterministic fixtures for smoke tests and first boot.
        name = schema.__name__
        if name == "SearchQueryPlan":
            return schema.model_validate(
                {"queries": ["无人机自主系统 基础 架构"], "rationale": "bootstrap"}
            )
        if name == "LearningResult":
            return schema.model_validate(
                {
                    "claims": [],
                    "concepts": [],
                    "unanswered_questions": ["需要配置真实 LLM"],
                    "contradictions": [],
                    "discovered_dependencies": [],
                }
            )
        if name == "EvaluationResult":
            return schema.model_validate(
                {
                    "score": 0.0,
                    "passed": False,
                    "factual_accuracy": 0.0,
                    "reasoning": 0.0,
                    "transfer": 0.0,
                    "completeness": 0.0,
                    "calibration": 1.0,
                    "questions": [],
                    "answers": [],
                    "feedback": "mock provider",
                }
            )
        if name == "ReflectionResult":
            return schema.model_validate(
                {
                    "failure_reason": "missing_model",
                    "missing_knowledge": [],
                    "recommended_actions": ["configure real LLM"],
                    "lessons": [],
                }
            )
        if name == "ContradictionResult":
            return schema.model_validate(
                {"relation": "unrelated", "score": 0.0, "explanation": "mock"}
            )
        if name == "CandidateGoalList":
            return schema.model_validate({"goals": []})
        if name == "ResearchAnswer":
            return schema.model_validate(
                {
                    "explanation": "请配置真实模型，并先执行学习目标。",
                    "belief_ids": [],
                    "missing_knowledge": [],
                }
            )
        if name in {"ExamPaper", "SkillDrafts"}:
            return schema.model_validate(
                {"questions": []} if name == "ExamPaper" else {"skills": []}
            )
        if name == "ExamAnswer":
            return schema.model_validate({"answer": "Mock：尚未配置模型", "confidence": 0})
        if name == "AnswerGrade":
            return schema.model_validate(
                {
                    "correct": False,
                    "factual_accuracy": 0,
                    "reasoning": 0,
                    "completeness": 0,
                    "explanation": "mock",
                }
            )
        if name in {"BenchmarkGrade", "BenchmarkJudge"}:
            return schema.model_validate({"score": 0, "rationale": "mock"})
        if name == "DisputeResolutionProposal":
            return schema.model_validate(
                {"outcome": "unresolved", "rationale": "Mock 不作真实决议"}
            )
        raise RuntimeError(f"MockLLM has no fixture for schema {name}")


class OpenAICompatibleLLM(LLM):
    """Works with services exposing the common /chat/completions request shape."""

    def __init__(self, settings: RuntimeSettings):
        if not settings.llm_api_key or not settings.llm_model:
            raise ValueError("LLM_API_KEY and LLM_MODEL are required")
        self.provider_name = "openai_compatible"
        self.model_name = settings.llm_model
        self.base_url = settings.llm_base_url.rstrip("/")
        self.api_key = settings.llm_api_key
        self.default_temperature = settings.llm_temperature
        self.max_output_tokens = settings.llm_max_output_tokens
        self.last_usage: dict = {}

    async def text(self, system: str, user: str, *, temperature: float | None = None) -> str:
        self.last_usage = {}
        payload: dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": self.max_output_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.default_temperature if temperature is None else temperature,
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions", json=payload, headers=headers
            )
            response.raise_for_status()
            data = response.json()
        self.last_usage = data.get("usage", {})
        return data["choices"][0]["message"]["content"]


def build_llm() -> LLM:
    s = runtime_settings()
    if s.llm_provider == "mock":
        return MockLLM()
    if s.llm_provider == "openai_compatible":
        return OpenAICompatibleLLM(s)
    raise ValueError(f"Unknown LLM_PROVIDER={s.llm_provider}")
