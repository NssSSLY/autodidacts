from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from pathlib import Path
from typing import Literal

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from autodidact import models  # noqa: F401
from autodidact.config import runtime_settings
from autodidact.db import Base

BASELINE_REVISION = "20260914_0001"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"

MigrationAction = Literal["upgrade", "stamp_then_upgrade"]


def alembic_config() -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    # ConfigParser treats percent signs as interpolation markers.
    database_url = runtime_settings().database_url.replace("%", "%%")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def expected_schema_columns() -> dict[str, set[str]]:
    return {
        table.name: {column.name for column in table.columns}
        for table in Base.metadata.sorted_tables
    }


def decide_initialization_action(
    existing_columns: Mapping[str, AbstractSet[str]],
) -> MigrationAction:
    """Choose a non-destructive initialization path for fresh or legacy schemas."""
    if "alembic_version" in existing_columns:
        return "upgrade"

    expected = expected_schema_columns()
    present_app_tables = set(existing_columns).intersection(expected)
    if not present_app_tables:
        return "upgrade"

    missing_tables = set(expected).difference(existing_columns)
    if missing_tables:
        names = ", ".join(sorted(missing_tables))
        raise RuntimeError(
            "检测到未受 Alembic 管理的不完整 Autodidact 数据库；"
            f"缺少数据表：{names}。为保护已有学习状态，已停止自动迁移。"
        )

    mismatches: list[str] = []
    for table_name, expected_names in expected.items():
        actual_names = set(existing_columns[table_name])
        if actual_names != expected_names:
            missing = sorted(expected_names - actual_names)
            extra = sorted(actual_names - expected_names)
            mismatches.append(
                f"{table_name}(缺少={missing or '无'}, 多出={extra or '无'})"
            )
    if mismatches:
        details = "; ".join(mismatches)
        raise RuntimeError(
            "检测到旧数据库结构与 V0.1 基线不一致。为避免错误标记版本，"
            f"已停止自动迁移：{details}"
        )

    return "stamp_then_upgrade"


def _existing_columns(connection: Connection) -> dict[str, set[str]]:
    inspector = inspect(connection)
    return {
        table_name: {column["name"] for column in inspector.get_columns(table_name)}
        for table_name in inspector.get_table_names()
    }


def _run_upgrade(connection: Connection, config: Config) -> None:
    config.attributes["connection"] = connection
    action = decide_initialization_action(_existing_columns(connection))
    if action == "stamp_then_upgrade":
        command.stamp(config, BASELINE_REVISION)
    command.upgrade(config, "head")


async def upgrade_database(engine: AsyncEngine) -> None:
    """Upgrade a fresh, Alembic-managed, or verified legacy database to head."""
    config = alembic_config()
    async with engine.begin() as connection:
        await connection.run_sync(_run_upgrade, config)
