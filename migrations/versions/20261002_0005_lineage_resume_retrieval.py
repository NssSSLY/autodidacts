# 文件职责：增加来源依赖、学习工作项和三实体派生检索索引。
"""Add source dependency graph, durable work items and rebuildable hybrid index."""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql as pg

revision = "20261002_0005"
down_revision = "20261002_0004"
branch_labels = None
depends_on = None


# 功能：创建 source_links、learning_steps、retrieval_entries 及唯一/GIN/HNSW 索引，保留核心认知表。
def upgrade():
    op.create_table(
        "source_links",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_id", pg.UUID(as_uuid=True), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("dedup_key", sa.String(64), nullable=False),
        sa.Column("from_url", sa.Text(), nullable=False),
        sa.Column("to_url", sa.Text(), nullable=False),
        sa.Column("relation", sa.String(30), nullable=False),
        sa.Column("details", pg.JSONB(), nullable=False),
        sa.UniqueConstraint("dedup_key", name="uq_source_links_relation"),
    )
    op.create_index("ix_source_links_from", "source_links", ["from_url"], postgresql_using="hash")
    op.create_index("ix_source_links_to", "source_links", ["to_url"], postgresql_using="hash")
    op.create_table(
        "learning_steps",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "learning_session_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("learning_sessions.id"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("step_key", sa.String(64), nullable=False),
        sa.Column("payload", pg.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "learning_session_id", "kind", "step_key", name="uq_learning_steps_item"
        ),
    )
    op.create_table(
        "retrieval_entries",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("entity_kind", sa.String(20), nullable=False),
        sa.Column("entity_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("lexemes", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(1536)),
        sa.Column("fingerprint", sa.String(64)),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("entity_kind", "entity_id", name="uq_retrieval_entity"),
    )
    op.create_index(
        "ix_retrieval_embedding_cosine",
        "retrieval_entries",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.execute(
        "CREATE INDEX ix_retrieval_lexemes ON retrieval_entries USING gin "
        "(to_tsvector('simple'::regconfig, lexemes))"
    )


# 功能：删除本次三个新表；派生索引可重建，但历史血缘/成功工作项需备份才可恢复。
def downgrade():
    # Export these new records before downgrade; original learning tables are untouched.
    op.drop_table("retrieval_entries")
    op.drop_table("learning_steps")
    op.drop_table("source_links")
