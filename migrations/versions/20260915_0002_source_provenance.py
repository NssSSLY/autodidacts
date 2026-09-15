"""增加来源规范化与质量元数据。

Revision ID: 20260915_0002
Revises: 20260914_0001
Create Date: 2026-09-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260915_0002"
down_revision: str | None = "20260914_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("normalized_url", sa.Text(), nullable=True))
    op.add_column("sources", sa.Column("publisher_key", sa.String(length=255), nullable=True))
    op.add_column(
        "sources",
        sa.Column(
            "quality_class",
            sa.String(length=50),
            server_default="ordinary_web",
            nullable=False,
        ),
    )
    op.add_column("sources", sa.Column("quality_reason", sa.Text(), nullable=True))
    op.execute("UPDATE sources SET normalized_url = url WHERE normalized_url IS NULL")
    op.alter_column("sources", "quality_class", server_default=None)
    op.create_index("ix_sources_normalized_url", "sources", ["normalized_url"], unique=False)
    op.create_index("ix_sources_publisher_key", "sources", ["publisher_key"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_sources_publisher_key", table_name="sources")
    op.drop_index("ix_sources_normalized_url", table_name="sources")
    op.drop_column("sources", "quality_reason")
    op.drop_column("sources", "quality_class")
    op.drop_column("sources", "publisher_key")
    op.drop_column("sources", "normalized_url")
