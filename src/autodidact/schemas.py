# 文件职责：定义规划、来源、主张、评估、目标、争议及模型回答的 Pydantic 协议和约束。
from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints


class SearchQueryPlan(BaseModel):
    queries: list[str] = Field(min_length=1, max_length=8)
    rationale: str = ""


class SourceDocument(BaseModel):
    url: str
    normalized_url: str = ""
    publisher_key: str = ""
    title: str = ""
    text: str
    lineage_key: str = ""
    metadata: dict = Field(default_factory=dict)
    source_type: str = "web"
    quality_class: str = "ordinary_web"
    quality_reason: str = ""
    evidence_level: int = Field(default=1, ge=0, le=5)
    credibility_score: float = Field(default=0.3, ge=0, le=1)


ScopeText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class ClaimScope(BaseModel):
    # 未提供范围表示未知/未提取，不代表主张无条件、永久有效或无单位。
    conditions: list[ScopeText] = Field(default_factory=list, max_length=20)
    time_scope: str = Field(default="", max_length=500)
    units: list[ScopeText] = Field(default_factory=list, max_length=20)

    # 功能：列出待核验的范围限制，空值不被解释为普遍适用。
    def terms(self) -> list[str]:
        return [
            *self.conditions,
            *([self.time_scope.strip()] if self.time_scope.strip() else []),
            *self.units,
        ]

    # 功能：找出未出现在主张正文的显式范围，避免结构字段隐藏或扩大主张语义。
    def missing_from(self, statement: str) -> list[str]:
        from autodidact.normalization import normalize_text_key

        text = normalize_text_key(statement)
        return [term for term in self.terms() if normalize_text_key(term) not in text]


class ClaimDraft(BaseModel):
    statement: str
    topic: str
    reasoning: str = ""
    confidence: float = Field(ge=0, le=1)
    source_urls: list[str] = Field(default_factory=list)
    citations: list[ClaimCitation] = Field(default_factory=list)
    scope: ClaimScope = Field(default_factory=ClaimScope)


class ClaimCitation(BaseModel):
    source_url: str
    excerpt: str = Field(min_length=1, max_length=2000)


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
    audit: dict = Field(default_factory=dict)


class ContradictionResult(BaseModel):
    relation: Literal["supports", "contradicts", "unrelated", "conditional"]
    score: float = Field(ge=0, le=1)
    explanation: str = ""


class DisputeResolutionProposal(BaseModel):
    outcome: Literal["keep_old", "adopt_new", "conditional", "unresolved"]
    rationale: str = Field(min_length=1, max_length=4000)
    evidence_source_ids: list[str] = Field(default_factory=list)
    conditional_statement: str = ""
    conditional_claim_id: UUID | None = None
    conditions: list[str] = Field(default_factory=list)


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
