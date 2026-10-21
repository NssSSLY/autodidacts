# 数据库迁移说明

更新：2026-10-21。当前HEAD `20261021_0011`，F13在Belief/Skill扩六列，仍22表；维护报告复用ResearchReport。早期各节保留当时revision，最新操作按第12节。本轮未执行实数据库迁移。

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
| 20261013_0009 | retrieval_vectors候选版本/工作项、唯一键、HNSW和状态索引 | 只增加派生索引状态，任务/指针复用报告；旧认知/向量保留，无自动回填/收费；回退丢候选/进度 |
| 20261021_0010 | concepts/knowledge_relations、概念/断言唯一键、方向/谓词检查及双向查询索引 | 仅新增2表，无旧认知/主张/血缘回填，不请求模型；降级丢概念/关系，原认知表保留 |
| 20261021_0011 | beliefs/skills各增lifecycle_state/lifecycle_version/canonical_id、检查/自引用FK/状态索引 | 旧项active/0/null，不自动归档、改真值或转移证据；降级丢生命周期/分组可见性，不删原行 |

初始13表+0003一表+0004两表+0005三表+0009一表+0010两表=当前22业务表，另有alembic_version。Revision日期标识不因推送日期修改。

## 2. 推荐升级入口

项目根目录，先备份再运行：

```powershell
.\.venv\Scripts\python.exe -m autodidact.cli init-db
```

db.init_database → migrations.upgrade_database，数据库事务 advisory lock 串行化迁移。已有 Alembic version 时升级到 HEAD；新库创建全部 revision。

早期create_all且无版本表的**完整已知结构**可识别0001–0010或当前HEAD，stamp对应版本再升级；未知/混合/部分结构拒绝自动迁移。

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

## 6. F06配置与健康状态兼容（无新revision）

2026-10-08 F06仍用0008/19表，不改旧认知或冻结报告。新增providers YAML读取者具有默认值，旧配置缺此节仍可运行；新健康写者使用既有research_reports.kind/report_key唯一键，kind=provider_health，payload协议provider_health_v1。此kind是可变运营快照，专用HealthStore按身份事务锁更新；其它冻结评估/版本报告仍不可变，不共用save_report回写。每次模型观察/预算事件独立追加，旧事件不补造费用。

升级停止旧写者、备份、更新并核对0008、重启；新程序读取已存冷却与探针租约，崩溃后等待租约再由下一次实际请求单探针。完整端点/配置/密钥只参与身份摘要，不存健康JSON；配置变更产生新身份，保留旧快照。运行策略不加入认知评分签名，不使已有成功工作项凭空失效；旧失败尝试仍走原人工恢复入口。

不能混跑旧程序：它不执行持久熔断/每叶搜索计账，可能继续调用已封锁提供方。回退代码无schema downgrade，保留全部JSONB和逐次事件；回退后F06运营约束不生效，需停止不适合的调用或恢复新程序。不要删报告、清库、删volume“重置健康”；既有0004 downgrade会丢失全部报告与预算历史，仍是破坏性回退。需要恢复升级前环境时，从完整备份恢复到新空库验证后切换连接，期间新增审计另存，不覆盖唯一库。

旧YAML/分类/逐次预算/并发单探针替身回归通过；新增隔离库真实事务与旧信念保留用例缺测试库而跳过。实库多进程/重启/锁竞争、真实服务故障、长期恢复和账单未验收，见V17。未执行生产库迁移、回填、降级或外部模型调用。

## 7. F07候选版本迁移0009

Revision链追加 `20261003_0008 → 20261013_0009`，新增retrieval_vectors一表（总20业务表），generation_id外键research_reports，三元工作项唯一约束，工作状态索引和1536维cosine HNSW。旧表/认知/旧向量不改，不自动回填或发API。模型/迁移结构一致；完整未版本化0008识别后stamp_review_then_upgrade，部分结构拒绝并列明缺表与列差异。

任务JSONB为retrieval_job_v1，活动指针为retrieval_active_v1；专用可变运营写者，不回写其它冻结研究报告。先备份→停所有旧写者→更新→init-db→核对HEAD0009→重启→手动创建/运行/核验候选→显式启用。旧程序不认识新HEAD/指针，不承诺混合版本写入；无需服务或依赖迁移。

暂停/重启不丢已提交工作项；未知外部请求费用继续保守预留，retry不清零次数。校验涵盖当前新增/变化实体、关键词入口和有效物理索引。激活版本封存，旧索引保留，rollback优先use-legacy-retrieval而非结构downgrade；当前配置指纹仍限制旧向量。

结构downgrade只删除retrieval_vectors并移除retrieval_active/vectors报告指针，保留任务审计与所有旧认知/向量。候选向量和进度会丢失，需先导出与备份，不自动执行降级。离线SQL/结构识别通过，新增两项在线用例未配置测试库已跳过；没有执行日常库升级或降级。操作见 [使用指南第14节](../doc/01使用指南.md#14-f07版本化嵌入重建)。

## 8. F04新报告协议兼容（无新revision）

2026-10-20复用0009/20表，无列/索引/依赖变化。新kind=frozen_belief_audit、belief_audit_item、belief_audit_answer、belief_audit_review，协议belief_migration_audit_v1；按类型/键不可变保存，digest检查，不覆盖旧冻结评估/信念。专用blocked目标/会话和LearningStep保存审计映射、次数及成功工作项，不参与普通自动学习恢复。

新读者先验证协议/hash及候选身份；当前快照变化拒绝收费续跑。普通F02调用保持默认，F04可额外带完整冻结旧来源排除集。旧知识和历史身份不回填，缺模型归因保留null；既有证据更新/创建争议仍经F02/原事务，不引入直接替换结论入口。

升级停旧写者/备份/更新，已有0009无需新迁移；较旧库先按原流程init-db到0009。回退代码不删报告/会话/证据/争议，不做结构downgrade；旧程序不认识新kind，不应把审计题目或报告交给旧评估/普通学习器。新增一项显式隔离库幂等/旧认知保留测试未配置跳过，本轮只有离线验证。

## 9. F08服务化兼容行为（无新revision）

HEAD仍20261013_0009/20业务表，无列/索引变更和旧认知回填。运行进度复用ResearchReport(kind=service_run, protocol=single_user_run_v1)，属于专用可变运营快照，不交给不可变冻结报告保存逻辑、不混评估分组。新增配置run_plan协议严格校验，旧YAML/env默认兼容；服务运行必需依赖不变，PyInstaller/documents只是可选打包依赖。

兼容变化：serve不再隐含init_database，先备份/停旧写者/显式init-db，再service-health和serve；已有0009也须确认就绪，不因打开网页自动改库。源码serve不传plan默认1轮；EXE无参数预览个人plan，均不自动学习/重启收费。回退代码保留service_run记录/日志/新备份/旧包，不做结构降级或删除报告，旧程序不得把运营报告当可靠评估。

F08备份只接受当前HEAD专用public库，用同一导出快照及20表摘要；恢复只到显式新空_restore/_test库，不支持自动跨revision/跨大版本迁移，不覆盖当前库或自动切换。必须先用可信归档在新副本验证，再人工决定配置切换；失败留现场，完整步骤及原生工具/权限要求见[部署指南第12节](../doc/02部署指南.md#12-f08一致快照备份新空库恢复与交付)。本轮原生备份/恢复及真实迁移未执行，新增两项实库服务测试未配置跳过，待V20。

## 10. F09派生画像与可选续跑身份（无新revision）

仍20261013_0009/20业务表，无表列/索引/依赖变更。topic_mastery_v1复用可靠冻结报告/历史答卷和当前认知读模型；显式mastery_profile按既有kind/key唯一约束保存不可变依据快照，旧答卷/信念/评估不回填。知识缺口目标仍source=knowledge_gap、metadata记录范围/组/触发与报告引用，现有Goal/预算/History不清空。

默认MASTERY_BENCHMARK_ID/GROUP_ID为空，保留原配置和续跑签名；仅启用范围时加入F09协议/基准/组身份，改变范围的未结束尝试拒绝盲续跑，先核对再人工restart-session保留旧工作项/次数。该配置与BENCHMARK_SNAPSHOT_ID收费里程碑分离，不隐式运行评估。旧日整合mastery计数保留并标legacy，新topic_inventory/可选画像只新增在未来日整合，已有同日报告不回写。

回退停新进程/关闭可选配置，不删除画像、目标、认知/历史，不做结构downgrade；旧程序不把新kind当可靠评分。较旧库仍须管理员停写/备份/显式init-db，打开网页不自动迁移。本轮按用户要求未运行pytest或任何真实数据库/模型/备份恢复，只有编译/静态/帮助/脚本语法，兼容/幂等/并发/旧状态保留须V21集中验收。操作见[01第17节](../doc/01使用指南.md#17-f09主题掌握与知识缺口)和[02第13节](../doc/02部署指南.md#13-f09掌握画像环境与兼容)。

## 11. F10概念关系迁移0010

`20261013_0009 → 20261021_0010`：Concept用领域/规范标签稳定摘要唯一，KnowledgeRelation以端点/谓词/原Claim/原scope摘要唯一，外键指向概念/主张；拒绝自环及未知谓词。不同主张/范围/方向的断言分别保留。新表默认无记录，不把SourceLink依赖或历史模型猜测回填成关系；原20表的列/记录/索引不变。

迁移识别补vector_schema_columns，完整无版本0009先stamp_vector_then_upgrade，已版本库仍走正常upgrade；部分混合结构拒绝自动stamp，列名识别不是约束/数据验收。无新的运行必需依赖，打包资源沿已有迁移目录配方包含新revision源码，但未构建/运行新EXE。

知识关系模型观察存在关系assessment，状态只读派生；knowledge_relation_history/knowledge_relation_extraction保存不可变报告，旧信念/评分/四结果不由图谱改写。新目标metadata与索引同事务入库；模型阶段预算/观察/成功工作项复用已有机制。当前直接关系和因果语义仍是待人工标注验证的模型观察，独立性只基于已知血缘。

升级必须停全部写者，**先用0009匹配旧版本备份或可信原生备份**，再新代码显式init-db到0010并检查就绪。新版本backup-db/readiness要求新HEAD，不能在旧库上跳过备份验证。0010备份库存为22表，旧0009归档不能用当前restore-db跨revision自动恢复；用旧版恢复新空副本后再升级，不覆盖唯一源库。

不支持新旧混跑，旧F08 EXE仍0009；停用KNOWLEDGE_GRAPH_ENABLED保留新表/历史，默认不改旧续跑签名，启用追加协议/关系含义hash/领域身份，变更后旧尝试需人工restart保留历史。回退优先保留0010备份并恢复升级前备份到新空副本/旧代码，期间新增记录另存审计。显式0010 downgrade会删两表所有概念和关系，报告/Goal中的UUID只剩历史引用，重新升级为空图；不是无损回退，不提供唯一库上可照抄的破坏性命令。

本轮只核对代码/编译/迁移图元数据与文档，未运行pytest、真实迁移/并发/恢复/模型；保全证明及故障重放留V02/V22。使用与配置见[01第18节](../doc/01使用指南.md#18-f10概念关系与前置知识)、[02第14节](../doc/02部署指南.md#14-f10关系图谱升级与配置)。

## 12. F13生命周期迁移0011

`20261021_0010 → 20261021_0011`：beliefs和skills各扩三列，默认active、版本0、canonical_id可空自引用；状态/版本/自环及canonical只允许archived的检查、两表状态索引。无新表/依赖/模型调用，不以元数据补写旧“已归档”判断。完整无版本0010用graph_schema_columns识别，再stamp_graph_then_upgrade；更早结构剥离新增列后沿原识别链，未知部分结构拒绝。

旧记录增加默认列使整行JSON摘要改变，验证应比较旧列投影/行数/认识论状态/原文/证据/历史，另核新列active/0/null；本轮没有执行这些实库验收。模型/迁移同表约束对应，应用归并还检查目标active根、无串链/成员/开放争议，数据库外键不等于证明所有业务策略或阻止任意特权SQL。

新写者：memory_change_proposal与memory_change_decision不可变，类型/键复用原唯一约束；执行者原控制器锁，按UUID锁行/刷新，原依据快照与当前重核后更新单项版本/可见性、保留原updated_at、追加BeliefHistory及decision、递增专用可变memory_lifecycle_epoch，并同事务commit。提议/拒绝不改epoch。模型/网页不参与维护决定，actor仅用户声明，不是权限认证或专家核准。

新读者：默认接纳记忆/召回/技能选择过滤active；冲突召回仍含归档，审计保留原记录。F09当前覆盖/F10接纳支持受可见性影响但不隐去风险；冻结旧实验/答卷不回写。首次实际变更后旧学习签名含epoch，阻断旧规划/技能缓存重用；F04生命周期版本变化也使原盲审快照失效。未变更active/0时保持旧学习签名/旧F04快照兼容。

升级：先停全部写者，在更新前用匹配0010代码/可信原生工具备份，再授权隔离副本验收并显式init-db至0011；新backup要求新HEAD，不能对旧库跳过备份。表数仍22但含新列及维护报告。旧EXE/F10代码不认识归档过滤，不允许新旧混跑；打开serve不自动迁移/付费，新维护动作不联网。

回退优先保存完整0011备份，并用升级前备份在新空副本恢复旧代码；不要覆盖唯一源库。显式downgrade仅删新增列/约束/索引，原记忆/证据/报告保留，但归档与分组当前状态、版本丢失，旧程序会重新看见所有原项；旧提议/报告只剩历史，不应复用为现状，重新升级需人工核对重提。删除报告/清库不是撤销归档，不能宣称结构降级无损。日常撤销请使用restore提议/确认而非downgrade。

未运行pytest、实际迁移/事务回滚/锁竞争、备份恢复、模型/浏览器或新EXE；V02/V20/V23保留集中验收。操作见[01第19节](../doc/01使用指南.md#19-f13记忆维护与可恢复归档)、[02第15节](../doc/02部署指南.md#15-f13生命周期升级与回退)。
