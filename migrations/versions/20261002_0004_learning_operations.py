"""Add durable operations, research reports and skill validation metadata.

Revision ID: 20261002_0004
Revises: 20260920_0003
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261002_0004"
down_revision = "20260920_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operation_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("resource", sa.String(40), nullable=False),
        sa.Column("reserved", sa.Float(), nullable=False),
        sa.Column("actual", sa.Float(), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_operations_resource_created", "operation_events", ["resource", "created_at"]
    )
    op.create_index("ix_operations_batch", "operation_events", ["batch_id"])
    op.create_table(
        "research_reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("report_key", sa.String(200), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", "report_key", name="uq_reports_kind_key"),
    )
    op.add_column(
        "skills",
        sa.Column(
            "metadata_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    # Removing these objects also removes the new audit records; back them up first.
    op.drop_column("skills", "metadata_json")
    op.drop_table("research_reports")
    op.drop_index("ix_operations_batch", table_name="operation_events")
    op.drop_index("ix_operations_resource_created", table_name="operation_events")
    op.drop_table("operation_events")
