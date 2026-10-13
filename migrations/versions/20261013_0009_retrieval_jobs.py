# 文件职责：新增版本化检索向量/持久工作项，不改旧认知和当前索引；回退丢候选版本及工作进度。
import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "20261013_0009"
down_revision = "20261003_0008"
branch_labels = None
depends_on = None


# 功能：扩展候选版本工作表和查询索引，旧库不自动付费回填或切换版本。
def upgrade():
    op.create_table(
        "retrieval_vectors",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "generation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("research_reports.id"),
            nullable=False,
        ),
        sa.Column("entity_kind", sa.String(20), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content_hash", sa.String(64)),
        sa.Column("embedding", Vector(1536)),
        sa.Column("fingerprint", sa.String(64)),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_category", sa.String(40)),
        sa.UniqueConstraint(
            "generation_id", "entity_kind", "entity_id", name="uq_retrieval_vector_item"
        ),
    )
    op.create_index("ix_retrieval_vector_work", "retrieval_vectors", ["generation_id", "status"])
    op.create_index(
        "ix_retrieval_vector_cosine",
        "retrieval_vectors",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


# 功能：删除候选版本表前移除活动指针，保留旧索引/认知及报告审计；新增工作进度和向量将丢失。
def downgrade():
    op.execute(
        "DELETE FROM research_reports WHERE kind = 'retrieval_active' AND report_key = 'vectors'"
    )
    op.drop_table("retrieval_vectors")
