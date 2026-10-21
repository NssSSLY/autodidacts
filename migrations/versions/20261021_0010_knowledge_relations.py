# 文件职责：仅新增概念与关系断言表，保留原主张/信念/证据；不自动回填或调用模型。
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "20261021_0010"
down_revision = "20261013_0009"
branch_labels = None
depends_on = None


# 功能：扩展有唯一身份、方向约束和主张外键的关系表，不把旧来源血缘伪装成知识关系。
def upgrade():
    op.create_table(
        "concepts",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("concept_key", sa.String(64), nullable=False, unique=True),
        sa.Column("domain", sa.String(200), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "knowledge_relations",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("relation_key", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "subject_id", pg.UUID(as_uuid=True), sa.ForeignKey("concepts.id"), nullable=False
        ),
        sa.Column("object_id", pg.UUID(as_uuid=True), sa.ForeignKey("concepts.id"), nullable=False),
        sa.Column("predicate", sa.String(30), nullable=False),
        sa.Column("claim_id", pg.UUID(as_uuid=True), sa.ForeignKey("claims.id"), nullable=False),
        sa.Column("scope", pg.JSONB(), server_default="{}", nullable=False),
        sa.Column("assessment", pg.JSONB(), server_default="{}", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("subject_id <> object_id", name="ck_relation_distinct_concepts"),
        sa.CheckConstraint(
            "predicate IN ('is_a','part_of','requires','causes','contradicts','supports',"
            "'example_of','used_for','related_to')",
            name="ck_relation_predicate",
        ),
    )
    op.create_index("ix_relation_subject", "knowledge_relations", ["subject_id", "predicate"])
    op.create_index("ix_relation_object", "knowledge_relations", ["object_id", "predicate"])


# 功能：显式降级会丢失概念/关系及当前评估；不可变报告保留但成为历史引用，旧认知表不删除。
def downgrade():
    op.drop_table("knowledge_relations")
    op.drop_table("concepts")
