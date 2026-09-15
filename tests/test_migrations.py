from __future__ import annotations

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from autodidact.migrations import (
    HEAD_REVISION,
    alembic_config,
    baseline_schema_columns,
    decide_initialization_action,
    expected_schema_columns,
)


def test_migration_graph_has_expected_single_head():
    scripts = ScriptDirectory.from_config(alembic_config())
    assert scripts.get_heads() == [HEAD_REVISION]


def test_initial_migration_generates_offline_sql(capsys):
    command.upgrade(alembic_config(), "head", sql=True)
    sql = capsys.readouterr().out

    assert "CREATE EXTENSION IF NOT EXISTS vector" in sql
    assert "CREATE TABLE agents" in sql
    assert "CREATE TABLE beliefs" in sql
    assert "CREATE TABLE belief_history" in sql
    assert "ADD COLUMN normalized_url" in sql
    assert "ADD COLUMN quality_class" in sql
    assert "CREATE TABLE model_observations" in sql


def test_fresh_or_versioned_schema_uses_upgrade():
    assert decide_initialization_action({}) == "upgrade"
    assert decide_initialization_action({"alembic_version": {"version_num"}}) == "upgrade"


def test_current_create_all_schema_is_stamped_at_head():
    assert decide_initialization_action(expected_schema_columns()) == "stamp_head"


def test_baseline_create_all_schema_is_stamped_then_upgraded():
    assert decide_initialization_action(baseline_schema_columns()) == "stamp_then_upgrade"


def test_partial_legacy_schema_is_rejected_to_protect_existing_state():
    legacy = expected_schema_columns()
    legacy.pop("belief_history")

    with pytest.raises(RuntimeError, match="缺少数据表：belief_history"):
        decide_initialization_action(legacy)


def test_mismatched_legacy_columns_are_rejected():
    legacy = expected_schema_columns()
    legacy["beliefs"] = legacy["beliefs"] - {"confidence"}

    with pytest.raises(RuntimeError, match="beliefs"):
        decide_initialization_action(legacy)
