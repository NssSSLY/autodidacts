from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
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
    statement_key: Mapped[str | None] = mapped_column(String(64), index=True)
    topic: Mapped[str | None] = mapped_column(Text)
    reasoning: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    source_ids: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ClaimEvidence(Base):
    __tablename__ = "claim_evidence"
    __table_args__ = (
        UniqueConstraint("claim_id", "source_id", "excerpt_hash", name="uq_claim_evidence_anchor"),
    )
    id: Mapped[UUID] = uuid_pk()
    claim_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("claims.id"), nullable=False
    )
    source_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sources.id"), nullable=False
    )
    excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    excerpt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
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
    dedup_key: Mapped[str | None] = mapped_column(String(64), index=True)
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
    dedup_key: Mapped[str | None] = mapped_column(String(64), index=True)
    previous_belief_state: Mapped[dict] = mapped_column(JSONB, default=dict)
    resolution_metadata: Mapped[dict] = mapped_column(JSONB, default=dict)
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
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
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


class OperationEvent(Base):
    __tablename__ = "operation_events"
    __table_args__ = (
        Index("ix_operations_resource_created", "resource", "created_at"),
        Index("ix_operations_batch", "batch_id"),
    )
    id: Mapped[UUID] = uuid_pk()
    batch_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    resource: Mapped[str] = mapped_column(String(40), nullable=False)
    reserved: Mapped[float] = mapped_column(Float, nullable=False)
    actual: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ResearchReport(Base):
    __tablename__ = "research_reports"
    __table_args__ = (UniqueConstraint("kind", "report_key", name="uq_reports_kind_key"),)
    id: Mapped[UUID] = uuid_pk()
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    report_key: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SourceLink(Base):
    __tablename__ = "source_links"
    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_source_links_relation"),
        Index("ix_source_links_from", "from_url", postgresql_using="hash"),
        Index("ix_source_links_to", "to_url", postgresql_using="hash"),
    )
    id: Mapped[UUID] = uuid_pk()
    source_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("sources.id"))
    dedup_key: Mapped[str] = mapped_column(String(64), nullable=False)
    from_url: Mapped[str] = mapped_column(Text, nullable=False)
    to_url: Mapped[str] = mapped_column(Text, nullable=False)
    relation: Mapped[str] = mapped_column(String(30), nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)


class LearningStep(Base):
    __tablename__ = "learning_steps"
    __table_args__ = (
        UniqueConstraint("learning_session_id", "kind", "step_key", name="uq_learning_steps_item"),
    )
    id: Mapped[UUID] = uuid_pk()
    learning_session_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("learning_sessions.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    step_key: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RetrievalEntry(Base):
    __tablename__ = "retrieval_entries"
    __table_args__ = (
        UniqueConstraint("entity_kind", "entity_id", name="uq_retrieval_entity"),
        Index(
            "ix_retrieval_embedding_cosine",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index(
            "ix_retrieval_lexemes",
            text("to_tsvector('simple'::regconfig, lexemes)"),
            postgresql_using="gin",
        ),
    )
    id: Mapped[UUID] = uuid_pk()
    entity_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    lexemes: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536))
    fingerprint: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
