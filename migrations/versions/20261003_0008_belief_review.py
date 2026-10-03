# 文件职责：新增信念证据的原文核验关联与立场审计，不回填或改写已有信念。
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261003_0008"
down_revision = "20261003_0007"
branch_labels = None
depends_on = None


# 功能：扩展证据审计字段，旧证据关联为空、审计未知，保留旧立场和摘录。
def upgrade():
    op.add_column(
        "evidence", sa.Column("claim_evidence_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column(
        "evidence",
        sa.Column(
            "assessment", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
    )
    op.create_foreign_key(
        "fk_evidence_claim_evidence", "evidence", "claim_evidence", ["claim_evidence_id"], ["id"]
    )


# 功能：回退仅移除新增审计关联；会丢这些字段，不删除证据、主张或信念。
def downgrade():
    op.drop_constraint("fk_evidence_claim_evidence", "evidence", type_="foreignkey")
    op.drop_column("evidence", "assessment")
    op.drop_column("evidence", "claim_evidence_id")
