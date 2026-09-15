# 数据库迁移

本目录保存 Autodidact 的 Alembic 数据库结构迁移。

常用命令：

```powershell
alembic current
alembic history
alembic upgrade head
```

应用命令 `autodidact init-db` 和 `autodidact bootstrap` 会自动升级到最新版本。

对于早期由 SQLAlchemy `create_all` 创建、尚无 `alembic_version` 表的数据库，初始化逻辑会先核对全部核心表和字段。只有结构与 V0.1 基线完全一致时，才会无损标记为基线版本；结构不完整或字段不一致时会停止迁移，避免误伤已有学习状态。

迁移降级可能删除数据，仅应在已经备份并明确理解影响时手动执行。

## `20260915_0002` 对已有学习状态的影响

该迁移只为 `sources` 增加 `normalized_url`、`publisher_key`、`quality_class`、`quality_reason` 和两个非唯一索引，不删除或改写 Claim、Belief、Evidence、Dispute、Goal 或学习历史。

已有来源的 `normalized_url` 会先回填原始 `url`，`quality_class` 回填为 `ordinary_web`；不会在迁移时把旧来源自动提升为高等级证据。来源在后续重新读取时，仓储层会补齐缺失的发布者和质量元数据。

没有 `alembic_version` 的旧 `create_all` 基线结构仍会先标记为 `20260914_0001`，再执行本迁移；与当前完整模型一致的未版本化结构会直接标记到最新版本。任何部分匹配或未知字段组合继续拒绝自动标记，以保护已有状态。

降级会删除上述四个派生元数据字段和索引，但不删除来源正文；执行降级前仍应备份数据库。
