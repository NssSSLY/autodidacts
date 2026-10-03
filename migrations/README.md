# 数据库迁移说明

更新：2026-10-03。当前 HEAD：`20261003_0006`。本轮 F01-A 新增两列，已验证离线 SQL/结构识别；未执行实数据库迁移。

## 1. Revision 链与已有状态影响

| Revision | 增量 | 升级对旧认知的影响 |
| --- | --- | --- |
| 20260914_0001 | 初始 13 表与 vector 扩展 | 空库创建基础结构，不清理已有认知 |
| 20260915_0002 | Source 规范 URL、出版方、质量类别/原因 | 初步回填 normalized_url 为原 url；质量默认 ordinary_web，不伪造高质量 |
| 20260920_0003 | ClaimEvidence、Claim/Evidence/Dispute 幂等字段/部分唯一索引、争议旧状态/决议、Belief HNSW | 旧幂等字段可为 NULL，不批量改旧主张/证据；没有旧锚点不能推断已支持 |
| 20261002_0004 | OperationEvent、ResearchReport、Skill metadata | 增加预算审计/报告与验证身份；旧认知保留，旧技能不凭空获得新验证结果 |
| 20261002_0005 | SourceLink、LearningStep、RetrievalEntry；来源边/工作项唯一约束、GIN/HNSW | 原有认知表不删除；旧会话无历史工作项，旧来源/索引需显式分批回填 |
| 20261003_0006 | claims.scope、claim_evidence.assessment：非空 JSONB，默认 {} | 旧记录范围和审计保持未知；不改原文、状态、评分、唯一键，不伪造核验结果 |

初始 13 表 + 0003 一表 + 0004 两表 + 0005 三表 = 当前 19 个业务表，不含 alembic_version。Revision 日期标识不可因推送日期不同而修改。

## 2. 推荐升级入口

项目根目录，先备份再运行：

```powershell
.\.venv\Scripts\python.exe -m autodidact.cli init-db
```

db.init_database → migrations.upgrade_database，数据库事务 advisory lock 串行化迁移。已有 Alembic version 时升级到 HEAD；新库创建全部 revision。

早期 create_all 生成且没有版本表的**完整已知结构**可按表/列识别为 0001/0002/0003/0004/0005/HEAD，stamp 对应版本后升级。未知、混合或部分结构拒绝自动迁移，以保护状态。

该兼容判断主要比较表/列名，不等于检查所有索引、约束、类型或数据一致性。不要手动 stamp HEAD 让不完整库“看起来成功”；管理员需先在副本核对修复。

源码的 init-db 会装载运行配置 DATABASE_URL。裸 `alembic` 命令可能读取 alembic.ini 的演示连接，不应默认它自动读取 .env；日常使用项目入口，避免误操作别的数据库。

## 3. 升级前后检查与回填

0006 是 expand-only：停止学习写入并备份，部署新代码后执行 init-db。表数仍 19，无删表、改唯一键或旧认知回填。旧程序 SQL 写入省略新增列时使用空默认值，但旧程序的迁移入口不认识 0006，因此不承诺新旧控制器混跑或直接回退旧代码。核验协议签名增加 claim_scope_v1，旧进行中尝试需人工 restart-session，历史保留；不修改旧签名冒充兼容。

升级前停止学习写入，记录代码/模型/嵌入/配置版本；按 [部署指南](../doc/02部署指南.md)备份。迁移会建索引，数据量大时需要维护时间，不能保证零停机。

升级后先确认 migration HEAD，再分批维护派生信息：

```powershell
.\.venv\Scripts\python.exe -m autodidact.cli rebuild-lineage --limit 200
.\.venv\Scripts\python.exe -m autodidact.cli rebuild-retrieval --limit 200
# 配置了兼容 1536 维嵌入服务才执行，可能计费：
.\.venv\Scripts\python.exe -m autodidact.cli rebuild-retrieval --limit 200 --vectors
.\.venv\Scripts\python.exe -m autodidact.cli rebuild-embeddings --limit 100
```

来源血缘只回填已有 metadata，不替旧网页猜测关系；索引可由当前实体重建，向量指纹不匹配需重新计算。旧学习会话不能凭空重建模型/网页调用结果，只有新协议保存工作项后才有续跑。

本次未执行上述命令；空库/历史副本升级、状态完整性和恢复检查列为 V02 待验收。

## 4. 回退风险

- 0006 downgrade 仅删除 scope/assessment 两列，新范围、上下文 hash、模型身份和覆盖判定审计将丢失。需先导出这两列并做完整备份；不能称无损回退。本轮只离线检查回退 SQL，未执行在线 downgrade。

- 0005 downgrade 删除来源边和持久续跑记录以及派生索引；索引可重建，历史工作项和来源边不一定可复原。
- 0004 downgrade 删除预算事件、报告和技能 metadata，其中冻结题集/比较/方法验证审计会丢失。
- 0003 downgrade 删除主张锚点/决议 metadata/幂等保护；不能称认知数据无损。
- 0002 downgrade 删除新增来源质量/独立性信息。
- 0001 downgrade 删除核心认知表，是破坏性操作。

不提供可直接在唯一学习库运行的批量降级命令。回退前导出新增记录并备份完整数据库，优先恢复到新空库，验证后切换连接。不要删 volume、reset schema 或覆盖唯一备份来解决迁移问题。
