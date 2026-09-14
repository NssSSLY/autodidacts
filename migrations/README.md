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
