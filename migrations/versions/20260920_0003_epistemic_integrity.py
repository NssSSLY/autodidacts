"""增加主张锚点、写入幂等键与争议决议元数据。

Revision ID: 20260920_0003
Revises: 20260915_0002
Create Date: 2026-09-20

本迁移只扩展结构，不回填旧学习记录。旧记录的幂等键保持 NULL，
新写入通过部分唯一索引受到数据库级保护。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260920_0003"
down_revision: str | None = "20260915_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("claims", sa.Column("statement_key", sa.String(length=64), nullable=True))
    op.add_column("evidence", sa.Column("dedup_key", sa.String(length=64), nullable=True))
    op.add_column("disputes", sa.Column("dedup_key", sa.String(length=64), nullable=True))
    op.add_column(
        "disputes",
        sa.Column("previous_belief_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "disputes",
        sa.Column("resolution_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_table(
        "claim_evidence",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("claim_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column("excerpt_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["claim_id"], ["claims.id"]),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "claim_id", "source_id", "excerpt_hash", name="uq_claim_evidence_anchor"
        ),
    )
    op.create_index("ix_claims_statement_key", "claims", ["statement_key"], unique=False)
    op.create_index("ix_evidence_dedup_key", "evidence", ["dedup_key"], unique=False)
    op.create_index("ix_disputes_dedup_key", "disputes", ["dedup_key"], unique=False)
    op.create_index(
        "uq_claims_session_statement_key_current",
        "claims",
        ["learning_session_id", "statement_key"],
        unique=True,
        postgresql_where=sa.text("statement_key IS NOT NULL"),
    )
    op.create_index(
        "uq_evidence_dedup_key_current",
        "evidence",
        ["dedup_key"],
        unique=True,
        postgresql_where=sa.text("dedup_key IS NOT NULL"),
    )
    op.create_index(
        "uq_disputes_dedup_key_current",
        "disputes",
        ["dedup_key"],
        unique=True,
        postgresql_where=sa.text("dedup_key IS NOT NULL"),
    )
    op.execute(
        "CREATE INDEX ix_beliefs_embedding_cosine ON beliefs USING hnsw (embedding vector_cosine_ops) WHERE embedding IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_index("uq_disputes_dedup_key_current", table_name="disputes")
    op.drop_index("uq_evidence_dedup_key_current", table_name="evidence")
    op.drop_index("uq_claims_session_statement_key_current", table_name="claims")
    op.drop_index("ix_disputes_dedup_key", table_name="disputes")
    op.drop_index("ix_beliefs_embedding_cosine", table_name="beliefs")
    op.drop_index("ix_evidence_dedup_key", table_name="evidence")
    op.drop_index("ix_claims_statement_key", table_name="claims")
    op.drop_table("claim_evidence")
    op.drop_column("disputes", "resolution_metadata")
    op.drop_column("disputes", "previous_belief_state")
    op.drop_column("disputes", "dedup_key")
    op.drop_column("evidence", "dedup_key")
    op.drop_column("claims", "statement_key")
