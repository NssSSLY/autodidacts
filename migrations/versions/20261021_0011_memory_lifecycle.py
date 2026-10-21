# 文件职责：为语义信念/程序性技能新增独立生命周期，不改真值状态、评分、原文和证据。
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "20261021_0011"
down_revision = "20261021_0010"
branch_labels = None
depends_on = None


# 功能：旧记录默认active/版本0/无归并指针，只扩展可恢复可见性，不自动归档或合并旧记忆。
def upgrade():
    for table, prefix in (("beliefs", "belief"), ("skills", "skill")):
        op.add_column(
            table,
            sa.Column("lifecycle_state", sa.String(20), nullable=False, server_default="active"),
        )
        op.add_column(
            table, sa.Column("lifecycle_version", sa.Integer(), nullable=False, server_default="0")
        )
        op.add_column(table, sa.Column("canonical_id", pg.UUID(as_uuid=True), nullable=True))
        op.create_foreign_key(f"fk_{prefix}_canonical", table, table, ["canonical_id"], ["id"])
        op.create_check_constraint(
            f"ck_{prefix}_lifecycle", table, "lifecycle_state IN ('active','archived')"
        )
        op.create_check_constraint(
            f"ck_{prefix}_lifecycle_version", table, "lifecycle_version >= 0"
        )
        op.create_check_constraint(
            f"ck_{prefix}_canonical",
            table,
            "canonical_id IS NULL OR (canonical_id <> id AND lifecycle_state = 'archived')",
        )
        op.create_index(f"ix_{prefix}_lifecycle", table, ["lifecycle_state", "id"])


# 功能：显式降级丢生命周期/归并可见性但不删除记忆；旧代码会重新看到归档项，需停写后审慎回退。
def downgrade():
    for table, prefix in (("skills", "skill"), ("beliefs", "belief")):
        op.drop_index(f"ix_{prefix}_lifecycle", table_name=table)
        for name in ("canonical", "lifecycle_version", "lifecycle"):
            op.drop_constraint(f"ck_{prefix}_{name}", table, type_="check")
        op.drop_constraint(f"fk_{prefix}_canonical", table, type_="foreignkey")
        for name in ("canonical_id", "lifecycle_version", "lifecycle_state"):
            op.drop_column(table, name)
