# 文件职责：离线检查迁移链、生成 SQL 和已知旧表列结构识别，不代替实际升级验收。
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
    retrieval_schema_columns,
    scope_schema_columns,
    source_provenance_schema_columns,
)


# 功能：验证 Alembic 链只有当前预期 HEAD。
def test_migration_graph_has_expected_single_head():
    scripts = ScriptDirectory.from_config(alembic_config())
    assert scripts.get_heads() == [HEAD_REVISION]


# 功能：验证离线 SQL 含初始结构及后续增量，不连接数据库。
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

    assert "CREATE TABLE claim_evidence" in sql
    assert "ALTER TABLE claims ADD COLUMN structure JSONB DEFAULT '{}'::jsonb NOT NULL" in sql
    assert "ADD COLUMN statement_key" in sql
    assert "uq_claims_session_statement_key_current" in sql
    assert "uq_evidence_dedup_key_current" in sql
    assert "uq_disputes_dedup_key_current" in sql
    assert "ALTER TABLE claims ADD COLUMN scope JSONB DEFAULT '{}'::jsonb NOT NULL" in sql
    assert (
        "ALTER TABLE claim_evidence ADD COLUMN assessment JSONB DEFAULT '{}'::jsonb NOT NULL" in sql
    )


# 功能：验证空库/已有版本表使用正常 upgrade 路径。
def test_fresh_or_versioned_schema_uses_upgrade():
    assert decide_initialization_action({}) == "upgrade"
    assert decide_initialization_action({"alembic_version": {"version_num"}}) == "upgrade"


# 功能：验证完整当前表列集合可识别为 HEAD 兼容结构。
def test_current_create_all_schema_is_stamped_at_head():
    assert decide_initialization_action(expected_schema_columns()) == "stamp_head"


# 功能：验证完整 0001 旧结构先 stamp 后升级。
def test_baseline_create_all_schema_is_stamped_then_upgraded():
    assert decide_initialization_action(baseline_schema_columns()) == "stamp_then_upgrade"


# 功能：验证历史结构集合与各 revision 引入字段相对应。
def test_legacy_schemas_match_the_columns_introduced_at_each_revision():
    baseline = baseline_schema_columns()
    provenance = source_provenance_schema_columns()
    assert "claim_evidence" not in baseline
    assert "claim_evidence" not in provenance
    assert "statement_key" not in baseline["claims"]
    assert "statement_key" not in provenance["claims"]
    assert "normalized_url" not in baseline["sources"]
    assert "normalized_url" in provenance["sources"]


# 功能：验证完整 0002 结构选择来源版兼容升级路径。
def test_source_provenance_schema_is_stamped_at_0002_then_upgraded():
    assert (
        decide_initialization_action(source_provenance_schema_columns())
        == "stamp_source_provenance_then_upgrade"
    )


# 功能：验证缺表旧结构拒绝自动 stamp，保护已有学习状态。
def test_partial_legacy_schema_is_rejected_to_protect_existing_state():
    legacy = expected_schema_columns()
    legacy.pop("belief_history")

    with pytest.raises(RuntimeError, match="缺少数据表：belief_history"):
        decide_initialization_action(legacy)


# 功能：验证表列不匹配旧库不会被误标迁移版本。
def test_mismatched_legacy_columns_are_rejected():
    legacy = expected_schema_columns()
    legacy["beliefs"] = legacy["beliefs"] - {"confidence"}

    with pytest.raises(RuntimeError, match="beliefs"):
        decide_initialization_action(legacy)


# 功能：验证完整未版本化 0005 结构升级路径，旧结构不含范围/审计列。
def test_retrieval_schema_is_stamped_at_0005_then_upgraded():
    legacy = retrieval_schema_columns()
    assert "scope" not in legacy["claims"]
    assert "assessment" not in legacy["claim_evidence"]
    assert "scope" not in baseline_schema_columns()["claims"]
    assert decide_initialization_action(legacy) == "stamp_retrieval_then_upgrade"


# 功能：验证部分新增列不会被错误 stamp 为完整 0005/0006。
def test_partial_scope_expansion_is_rejected():
    legacy = retrieval_schema_columns()
    legacy["claims"].add("scope")
    with pytest.raises(RuntimeError, match="claim_evidence"):
        decide_initialization_action(legacy)


# 功能：离线核对回退仅删新增列，不删除核心认知表；不执行实际降级。
def test_scope_downgrade_only_removes_added_columns(capsys):
    command.downgrade(alembic_config(), "20261003_0006:20261002_0005", sql=True)
    sql = capsys.readouterr().out
    assert "ALTER TABLE claims DROP COLUMN scope" in sql
    assert "ALTER TABLE claim_evidence DROP COLUMN assessment" in sql
    assert "DROP TABLE" not in sql


# 功能：验证完整无版本0006识别与0007只增加structure，部分混合结构不会冒充版本。
def test_scope_schema_is_stamped_at_0006_then_upgraded():
    legacy = scope_schema_columns()
    assert "structure" not in legacy["claims"] and "scope" in legacy["claims"]
    assert decide_initialization_action(legacy) == "stamp_scope_then_upgrade"
    legacy["claims"].add("structure")
    legacy["claim_evidence"].discard("assessment")
    with pytest.raises(RuntimeError, match="claim_evidence"):
        decide_initialization_action(legacy)


# 功能：离线核对0007回退只丢拆分结构审计，不删除原始主张或证据。
def test_structure_downgrade_only_removes_added_column(capsys):
    command.downgrade(alembic_config(), "20261003_0007:20261003_0006", sql=True)
    sql = capsys.readouterr().out
    assert "ALTER TABLE claims DROP COLUMN structure" in sql and "DROP TABLE" not in sql
