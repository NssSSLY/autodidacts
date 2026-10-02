# 文件职责：生成未来 Alembic revision 的模板；生成文件的 upgrade/downgrade 必须审查兼容性与数据风险。
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


# 功能：实现本 revision 的向前兼容升级；生成后需填写并审查实际数据影响。
def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


# 功能：撤销本 revision；生成后必须说明删除字段/表是否丢失学习状态。
def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
