from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class SearchQueryPlan(BaseModel):
    queries: list[str] = Field(min_length=1, max_length=8)
    rationale: str = ""


class SourceDocument(BaseModel):
    url: str
    normalized_url: str = ""
    publisher_key: str = ""
    title: str = ""
    text: str
    source_type: str = "web"
    quality_class: str = "ordinary_web"
    quality_reason: str = ""
    evidence_level: int = Field(default=1, ge=0, le=5)
    credibility_score: float = Field(default=0.3, ge=0, le=1)


class ClaimDraft(BaseModel):
    statement: str
    topic: str
    reasoning: str = ""
    confidence: float = Field(ge=0, le=1)
    source_urls: list[str] = Field(default_factory=list)


class LearningResult(BaseModel):
    claims: list[ClaimDraft] = Field(default_factory=list)
    concepts: list[str] = Field(default_factory=list)
    unanswered_questions: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    discovered_dependencies: list[str] = Field(default_factory=list)


class CandidateGoal(BaseModel):
    title: str
    description: str = ""
    source: str = "curiosity"
    importance: float = Field(default=0.5, ge=0, le=1)
    uncertainty: float = Field(default=0.5, ge=0, le=1)
    novelty: float = Field(default=0.5, ge=0, le=1)
    utility: float = Field(default=0.5, ge=0, le=1)
    prerequisite_score: float = Field(default=0.5, ge=0, le=1)
    estimated_cost: float = Field(default=0.5, ge=0, le=1)


class EvaluationResult(BaseModel):
    score: float = Field(ge=0, le=1)
    passed: bool
    factual_accuracy: float = Field(ge=0, le=1)
    reasoning: float = Field(ge=0, le=1)
    transfer: float = Field(ge=0, le=1)
    completeness: float = Field(ge=0, le=1)
    calibration: float = Field(ge=0, le=1)
    questions: list[str] = Field(default_factory=list)
    answers: list[str] = Field(default_factory=list)
    feedback: str = ""


class ContradictionResult(BaseModel):
    relation: Literal["supports", "contradicts", "unrelated", "conditional"]
    score: float = Field(ge=0, le=1)
    explanation: str = ""


class ReflectionResult(BaseModel):
    failure_reason: str | None = None
    missing_knowledge: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    lessons: list[str] = Field(default_factory=list)


class ModelAnswer(BaseModel):
    provider: str
    access_type: str
    displayed_model: str | None = None
    prompt: str
    response: str
    citations: list[str] = Field(default_factory=list)
    conversation_ref: str | None = None
    metadata: dict = Field(default_factory=dict)


class BeliefSnapshot(BaseModel):
    id: UUID
    topic: str
    statement: str
    confidence: float
    status: str
