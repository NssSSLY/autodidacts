# 数据库迁移说明

更新：2026-10-03。当前 HEAD：`20261003_0008`。本轮 F02 仅新增 evidence.claim_evidence_id/assessment，已验证离线 SQL/结构识别；未执行实数据库迁移。

## 1. Revision 链与已有状态影响

| Revision | 增量 | 升级对旧认知的影响 |
| --- | --- | --- |
| 20260914_0001 | 初始 13 表与 vector 扩展 | 空库创建基础结构，不清理已有认知 |
| 20260915_0002 | Source 规范 URL、出版方、质量类别/原因 | 初步回填 normalized_url 为原 url；质量默认 ordinary_web，不伪造高质量 |
| 20260920_0003 | ClaimEvidence、Claim/Evidence/Dispute 幂等字段/部分唯一索引、争议旧状态/决议、Belief HNSW | 旧幂等字段可为 NULL，不批量改旧主张/证据；没有旧锚点不能推断已支持 |
| 20261002_0004 | OperationEvent、ResearchReport、Skill metadata | 增加预算审计/报告与验证身份；旧认知保留，旧技能不凭空获得新验证结果 |
| 20261002_0005 | SourceLink、LearningStep、RetrievalEntry；来源边/工作项唯一约束、GIN/HNSW | 原有认知表不删除；旧会话无历史工作项，旧来源/索引需显式分批回填 |
| 20261003_0006 | claims.scope、claim_evidence.assessment：非空 JSONB，默认 {} | 旧记录范围和审计保持未知；不改原文、状态、评分、唯一键，不伪造核验结果 |
| 20261003_0007 | claims.structure：非空 JSONB，默认 {} | 保留父句、拆分/复核观察、父/子/调查子ID；旧主张未知，不批量拆分、重新晋升或撤回历史 |
| 20261003_0008 | evidence.claim_evidence_id：可空外键；assessment：非空JSONB默认{} | 关联原文核验并保存信念立场快照；旧证据关联空/审计未知，无回填，保留旧支持/结论/评分 |

初始 13 表 + 0003 一表 + 0004 两表 + 0005 三表 = 当前 19 个业务表，不含 alembic_version。Revision 日期标识不可因推送日期不同而修改。

## 2. 推荐升级入口

项目根目录，先备份再运行：

```powershell
.\.venv\Scripts\python.exe -m autodidact.cli init-db
```

db.init_database → migrations.upgrade_database，数据库事务 advisory lock 串行化迁移。已有 Alembic version 时升级到 HEAD；新库创建全部 revision。

早期 create_all 生成且没有版本表的**完整已知结构**可按表/列识别为 0001/0002/0003/0004/0005/0006/0007/HEAD，stamp 对应版本后升级。未知、混合或部分结构拒绝自动迁移，以保护状态。

该兼容判断主要比较表/列名，不等于检查所有索引、约束、类型或数据一致性。不要手动 stamp HEAD 让不完整库“看起来成功”；管理员需先在副本核对修复。

源码的 init-db 会装载运行配置 DATABASE_URL。裸 `alembic` 命令可能读取 alembic.ini 的演示连接，不应默认它自动读取 .env；日常使用项目入口，避免误操作别的数据库。

## 3. 升级前后检查与回填

0008同样expand-only，停止旧写入并备份后init-db。仍19表；不回填历史Evidence立场、未知scope或既有Belief。沿用0003的证据dedup_key唯一索引，新复核键按belief/原文核验锚点/stance区分，不覆写旧support；唯一键竞争用savepoint仅恢复指定约束，不把外键错误当幂等成功。ClaimEvidence当前状态可重核，Evidence保存当时核验快照供追溯，不以锚点后来变化篡改历史。

旧SQL省略新增字段可用默认值，但旧迁移入口不认识0008；停旧控制器再升级，不承诺混合版本并行。claim_scope_v2/belief_review_v1改变续跑签名，旧进行中尝试保留并人工restart-session。新增0007→0008旧信念/支持摘录保留及并发反对Evidence用例，仅显式*_test随机schema运行；本轮未配置而跳过，不代表在线迁移通过。

0006/0007 均为 expand-only：停止写入并备份，部署新代码后执行 init-db。表数仍19，无删表、改唯一键或旧认知回填。0007 只增加 structure；来源书目复用既有 author/published_at/metadata_json，质量审计也存metadata。旧程序SQL省略新增列使用空默认值，但其迁移入口不认识0007，不承诺混跑/直接回退旧代码。新拆分/书目/正文质量协议加入续跑签名，旧进行中尝试需人工 restart-session，保留历史，不改旧签名冒充兼容。

升级本身不改变旧来源等级或信念；新流程消费旧来源时重新计算正文上限，只降不升。同内容来源再次入库可能更新 Source 的有效质量及补缺书目，不改原正文/既有作者日期或历史信念。旧空structure沿兼容支持规则处理，不能称已经过原子化检查；新候选则必须完成结构提议/复核与各句核证。

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

- 0008 downgrade仅删除Evidence.assessment/claim_evidence_id及其外键；证据立场/摘录、Claim/Belief、旧状态及ResearchReport复核报告保留，但原文核验关联与快照丢失。先导出新增字段、完整备份，不称无损回退；本轮只离线SQL检查，未执行实际降级。

- 0007 downgrade 仅删除 claims.structure，不删除父/子主张和引文，但会丢失拆分观察、关系及结构门控。须先导出结构审计并完整备份；保留原句不等于无损回退，也不能在丢门控后继续当作已验证原子结论。书目/质量metadata在0007回退后仍保留，因为它们复用既有列。本轮只离线检查降级SQL，新增隔离库旧主张保留用例未运行。

- 0006 downgrade 仅删除 scope/assessment 两列，新范围、上下文 hash、模型身份和覆盖判定审计将丢失。需先导出这两列并做完整备份；不能称无损回退。本轮只离线检查回退 SQL，未执行在线 downgrade。

- 0005 downgrade 删除来源边和持久续跑记录以及派生索引；索引可重建，历史工作项和来源边不一定可复原。
- 0004 downgrade 删除预算事件、报告和技能 metadata，其中冻结题集/比较/方法验证审计会丢失。
- 0003 downgrade 删除主张锚点/决议 metadata/幂等保护；不能称认知数据无损。
- 0002 downgrade 删除新增来源质量/独立性信息。
- 0001 downgrade 删除核心认知表，是破坏性操作。

不提供可直接在唯一学习库运行的批量降级命令。回退前导出新增记录并备份完整数据库，优先恢复到新空库，验证后切换连接。不要删 volume、reset schema 或覆盖唯一备份来解决迁移问题。

## 5. F03评估协议兼容迁移（无新revision）

2026-10-03 F03仍使用20261003_0008，不改19表/列/索引。新可靠manifest、版本占位、运行/保持/重评分写入既有research_reports.payload JSONB；版本占位复用uq_reports_kind_key与save_report锁/幂等，不清空旧题集。写占位后失败允许同内容重试；同suite_id+suite_version不同摘要拒绝，应增加版本而不是改原数据。

新读者识别reliable_benchmark_v1/ exact_numeric_v1及旧legacy协议；未知协议拒绝，完整manifest hash校验包含真值/审核/评分。旧JSON数组摘要算法不变，旧报告不猜测回填、不覆盖历史分数/认知。Profile及整合的分组只是派生统计，不能把旧模型裁判记录变为独立真值。

升级：停所有旧写者→完整备份→新代码/init-db→核对HEAD0008→重启。新旧程序不可混跑：旧代码不认识新payload且可能错误评分，新可靠冻结ID不得交旧程序消费。动态闭卷评分身份进入续跑签名，旧未完成尝试保留并人工restart，不能伪造兼容缓存。

回退代码没有新的schema可downgrade；保留全部新旧JSONB，禁止让旧评估器运行新冻结ID。若必须恢复旧运行状态，使用经验证的升级前完整备份恢复到新空库并切连接，期间新增报告需另存审计，不覆盖唯一库。既有0004 downgrade会删全部报告，仍是数据损失，不是F03安全回退方法。

本轮离线新旧协议/摘要/版本冲突/重评分回归通过；新增*_test随机schema JSONB/旧信念保留用例因未配置测试库跳过，未执行真实迁移、并发写入或生产库回填。实际验收见V02/V03/V16。
