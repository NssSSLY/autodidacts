from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from typing import Literal

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from autodidact import models  # noqa: F401
from autodidact.config import runtime_settings
from autodidact.db import Base
from autodidact.resources import resource_root

HEAD_REVISION = "20261002_0005"
OPERATIONS_REVISION = "20261002_0004"
POST_OPERATIONS_TABLES = {"source_links", "learning_steps", "retrieval_entries"}
EPISTEMIC_REVISION = "20260920_0003"
POST_EPISTEMIC_TABLES = {"operation_events", "research_reports"}
SOURCE_PROVENANCE_REVISION = "20260915_0002"
POST_BASELINE_COLUMNS = {
    "sources": {"normalized_url", "publisher_key", "quality_class", "quality_reason"},
}
POST_SOURCE_PROVENANCE_COLUMNS = {
    "claims": {"statement_key"},
    "evidence": {"dedup_key"},
    "disputes": {"dedup_key", "previous_belief_state", "resolution_metadata"},
}
POST_SOURCE_PROVENANCE_TABLES = {"claim_evidence"}
BASELINE_REVISION = "20260914_0001"
PROJECT_ROOT = resource_root()
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"

MigrationAction = Literal[
    "upgrade",
    "stamp_then_upgrade",
    "stamp_source_provenance_then_upgrade",
    "stamp_epistemic_then_upgrade",
    "stamp_operations_then_upgrade",
    "stamp_head",
]


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


def baseline_schema_columns() -> dict[str, set[str]]:
    baseline = source_provenance_schema_columns()
    for table_name, column_names in POST_BASELINE_COLUMNS.items():
        baseline[table_name].difference_update(column_names)
    return baseline


def operational_schema_columns() -> dict[str, set[str]]:
    return {
        name: set(values)
        for name, values in expected_schema_columns().items()
        if name not in POST_OPERATIONS_TABLES
    }


def epistemic_schema_columns() -> dict[str, set[str]]:
    columns = {
        name: set(values)
        for name, values in operational_schema_columns().items()
        if name not in POST_EPISTEMIC_TABLES
    }
    columns["skills"].discard("metadata_json")
    return columns


def source_provenance_schema_columns() -> dict[str, set[str]]:
    source_provenance = epistemic_schema_columns()
    for table_name, column_names in POST_SOURCE_PROVENANCE_COLUMNS.items():
        source_provenance[table_name].difference_update(column_names)
    for table_name in POST_SOURCE_PROVENANCE_TABLES:
        source_provenance.pop(table_name)
    return source_provenance


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

    actual = {
        table_name: set(columns)
        for table_name, columns in existing_columns.items()
        if table_name in expected
    }
    if actual == expected:
        return "stamp_head"
    if actual == operational_schema_columns():
        return "stamp_operations_then_upgrade"
    if actual == epistemic_schema_columns():
        return "stamp_epistemic_then_upgrade"

    source_provenance = source_provenance_schema_columns()
    if actual == source_provenance:
        return "stamp_source_provenance_then_upgrade"

    baseline = baseline_schema_columns()
    if actual == baseline:
        return "stamp_then_upgrade"

    missing_tables = set(expected) - set(actual)
    if missing_tables:
        names = ", ".join(sorted(missing_tables))
        raise RuntimeError(
            "检测到未受 Alembic 管理的不完整 Autodidact 数据库；"
            f"缺少数据表：{names}。为保护已有学习状态，已停止自动迁移。"
        )

    mismatches: list[str] = []
    for table_name, expected_names in expected.items():
        actual_names = actual[table_name]
        if actual_names != expected_names:
            missing = sorted(expected_names - actual_names)
            extra = sorted(actual_names - expected_names)
            mismatches.append(f"{table_name}(缺少={missing or '无'}, 多出={extra or '无'})")
    if mismatches:
        details = "; ".join(mismatches)
        raise RuntimeError(
            f"检测到旧数据库结构与 V0.1 基线不一致。为避免错误标记版本，已停止自动迁移：{details}"
        )

    raise AssertionError("unreachable schema comparison")


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
    elif action == "stamp_source_provenance_then_upgrade":
        command.stamp(config, SOURCE_PROVENANCE_REVISION)
    elif action == "stamp_epistemic_then_upgrade":
        command.stamp(config, EPISTEMIC_REVISION)
    elif action == "stamp_operations_then_upgrade":
        command.stamp(config, OPERATIONS_REVISION)
    elif action == "stamp_head":
        command.stamp(config, "head")
    command.upgrade(config, "head")


async def upgrade_database(engine: AsyncEngine) -> None:
    """Upgrade a fresh, Alembic-managed, or verified legacy database to head."""
    config = alembic_config()
    async with engine.begin() as connection:
        await connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 718204611})
        await connection.run_sync(_run_upgrade, config)
