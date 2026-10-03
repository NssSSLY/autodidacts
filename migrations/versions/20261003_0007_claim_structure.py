# 文件职责：为主张增加结构/拆分审计；旧主张默认未知，不重新拆分、撤回或晋升已有状态。
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261003_0007"
down_revision = "20261003_0006"
branch_labels = None
depends_on = None


# 功能：仅增加带空对象默认值的结构审计列，旧状态和证据不改写。
def upgrade():
    op.add_column(
        "claims",
        sa.Column(
            "structure", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
    )


# 功能：回退只删结构审计列，会丢父子关系和拆分观察；须停写并备份后人工执行。
def downgrade():
    op.drop_column("claims", "structure")
