from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from autodidact.db import Base


def uuid_pk() -> Mapped[UUID]:
    return mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)


class Agent(Base):
    __tablename__ = "agents"
    id: Mapped[UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    mission: Mapped[str] = mapped_column(Text, nullable=False)
    current_focus: Mapped[str | None] = mapped_column(Text)
    total_learning_hours: Mapped[float] = mapped_column(Float, default=0.0)
    cycle_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Goal(Base):
    __tablename__ = "goals"
    id: Mapped[UUID] = uuid_pk()
    parent_goal_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("goals.id")
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    uncertainty: Mapped[float] = mapped_column(Float, default=0.5)
    novelty: Mapped[float] = mapped_column(Float, default=0.5)
    utility: Mapped[float] = mapped_column(Float, default=0.5)
    prerequisite_score: Mapped[float] = mapped_column(Float, default=0.5)
    estimated_cost: Mapped[float] = mapped_column(Float, default=0.5)
    priority_score: Mapped[float] = mapped_column(Float, default=0.0)
    confidence_before: Mapped[float | None] = mapped_column(Float)
    confidence_after: Mapped[float | None] = mapped_column(Float)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LearningSession(Base):
    __tablename__ = "learning_sessions"
    id: Mapped[UUID] = uuid_pk()
    goal_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("goals.id"), nullable=False
    )
    plan: Mapped[dict] = mapped_column(JSONB, default=dict)
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
    reflection: Mapped[dict] = mapped_column(JSONB, default=dict)
    success: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[UUID] = uuid_pk()
    url: Mapped[str | None] = mapped_column(Text)
    normalized_url: Mapped[str | None] = mapped_column(Text, index=True)
    publisher_key: Mapped[str | None] = mapped_column(String(255), index=True)
    title: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(50), default="web")
    quality_class: Mapped[str] = mapped_column(String(50), default="ordinary_web")
    quality_reason: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    credibility_score: Mapped[float] = mapped_column(Float, default=0.3)
    evidence_level: Mapped[int] = mapped_column(Integer, default=1)
    content_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    extracted_text: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Claim(Base):
    __tablename__ = "claims"
    id: Mapped[UUID] = uuid_pk()
    learning_session_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("learning_sessions.id")
    )
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    topic: Mapped[str | None] = mapped_column(Text)
    reasoning: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    source_ids: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Belief(Base):
    __tablename__ = "beliefs"
    id: Mapped[UUID] = uuid_pk()
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    explanation: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    evidence_score: Mapped[float] = mapped_column(Float, default=0.0)
    test_score: Mapped[float] = mapped_column(Float, default=0.0)
    stability_score: Mapped[float] = mapped_column(Float, default=0.0)
    source_count: Mapped[int] = mapped_column(Integer, default=0)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Evidence(Base):
    __tablename__ = "evidence"
    id: Mapped[UUID] = uuid_pk()
    belief_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("beliefs.id"), nullable=False
    )
    source_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), ForeignKey("sources.id"))
    model_observation_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("model_observations.id")
    )
    kind: Mapped[str] = mapped_column(String(50), default="web_source")
    stance: Mapped[str] = mapped_column(String(20), default="support")
    evidence_level: Mapped[int] = mapped_column(Integer, default=1)
    strength: Mapped[float] = mapped_column(Float, default=0.5)
    excerpt: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Dispute(Base):
    __tablename__ = "disputes"
    id: Mapped[UUID] = uuid_pk()
    belief_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("beliefs.id"), nullable=False
    )
    incoming_claim_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("claims.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    contradiction_score: Mapped[float] = mapped_column(Float, nullable=False)
    resolution_notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BeliefHistory(Base):
    __tablename__ = "belief_history"
    id: Mapped[UUID] = uuid_pk()
    belief_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("beliefs.id"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    previous_state: Mapped[dict] = mapped_column(JSONB, default=dict)
    new_state: Mapped[dict] = mapped_column(JSONB, default=dict)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Skill(Base):
    __tablename__ = "skills"
    id: Mapped[UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(250), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    trigger_condition: Mapped[str | None] = mapped_column(Text)
    procedure: Mapped[list] = mapped_column(JSONB, default=list)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Evaluation(Base):
    __tablename__ = "evaluations"
    id: Mapped[UUID] = uuid_pk()
    goal_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("goals.id"), nullable=False
    )
    learning_session_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("learning_sessions.id"), nullable=False
    )
    score: Mapped[float] = mapped_column(Float, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    components: Mapped[dict] = mapped_column(JSONB, default=dict)
    questions: Mapped[list] = mapped_column(JSONB, default=list)
    answers: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ModelObservation(Base):
    __tablename__ = "model_observations"
    id: Mapped[UUID] = uuid_pk()
    provider: Mapped[str] = mapped_column(String(120), nullable=False)
    access_type: Mapped[str] = mapped_column(String(20), nullable=False)
    displayed_model: Mapped[str | None] = mapped_column(String(200))
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    response: Mapped[str] = mapped_column(Text, nullable=False)
    response_hash: Mapped[str | None] = mapped_column(String(64))
    conversation_ref: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ModelProfile(Base):
    __tablename__ = "model_profiles"
    id: Mapped[UUID] = uuid_pk()
    provider: Mapped[str] = mapped_column(String(120), nullable=False)
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)
    domain_scores: Mapped[dict] = mapped_column(JSONB, default=dict)
    error_profile: Mapped[dict] = mapped_column(JSONB, default=dict)
    benchmark_score: Mapped[float | None] = mapped_column(Float)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
