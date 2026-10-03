# 文件职责：增量保存主张范围与逐引文核验审计，不猜测回填旧认知。
"""Add claim scope and evidence assessment audit without changing historical verdicts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "20261003_0006"
down_revision = "20261002_0005"
branch_labels = None
depends_on = None


# 功能：只增加 JSONB 范围/核验审计列，旧记录为空未知，原文/状态/唯一键保持不变。
def upgrade():
    op.add_column(
        "claims",
        sa.Column("scope", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.add_column(
        "claim_evidence",
        sa.Column("assessment", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )


# 功能：回退仅移除本增量两列；新增范围与审计将丢失，操作前须导出并备份。
def downgrade():
    op.drop_column("claim_evidence", "assessment")
    op.drop_column("claims", "scope")
