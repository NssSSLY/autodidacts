# 当前技术架构

更新：2026-10-03；实现基线 d7f6583，schema HEAD 20261002_0005。本文描述当前代码，不替代 [实现/缺口对照](doc/03现有能力与实现对照.md)或 [原始要求历史](Autodidact_Full_Conversation_Codex_Handoff.md)。

## 1. 认知身份与边界

控制器掌握流程和权限，模型只提议结构化结果。持久认知存在数据库中的目标、会话、来源、主张、证据、信念、争议、历史、评估、技能与报告，而不是当前 LLM 的聊天上下文。

外部网页、网页模型、API/本地模型输出全部是不受信任的数据。模型意见初始等级 0；网页引用必须独立读取、核验后才能贡献证据。高置信旧信念冲突必须开争议，不能静默覆盖。测量提升要固定题集、评分和模型，区分记忆增益与模型换代。

## 2. 调用与持久化结构

```text
CLI / 本地 Workbench
  └─ 初始化迁移 / 控制器互斥 / AutonomousLearner
      ├─ 目标池 + 混合认知召回 + 已验证研究方法
      ├─ LearningSession + 配置签名 + DurableSteps
      │   └─ 规划 → 搜索/阅读 → 提取 Claim → 原文锚点/语义支持
      ├─ 来源质量 + SourceLineage → 独立证据集合
      ├─ 旧信念召回 → 冲突/Dispute → 独立调查/四结果决议
      ├─ 隔离出题 → 闭卷作答 → 新来源核验 → Evaluation
      └─ 晋升门控 → Belief/Evidence/History → 反思/后续目标
          └─ 日记忆报告 / 候选技能 / 可选里程碑基准

外部调用 → BudgetLedger(OperationEvent) + ModelObservation
数据库 → PostgreSQL + JSONB + pgvector；Alembic 维护结构
```

搜索/阅读/模型成功结果入库后可重放；核心数据库写入有唯一键、行锁和幂等保护。外部调用与数据库不是原子事务，不能保证绝对 exactly-once。

## 3. 代码结构与职责

| 路径（src/autodidact 下） | 职责 |
| --- | --- |
| cli.py、commands.py、advanced_commands.py | 命令注册、初始化、受控操作、实验、恢复及索引维护 |
| workbench.py | 本地单用户 HTTP 页面；目标、学习控制、状态、基于接纳记忆问答 |
| agent.py | 主学习循环、续跑选择、冲突/晋升、争议调查、维护触发 |
| config.py、resources.py、db.py、migrations.py | 环境/策略、源码/安装资源定位、异步数据库、旧库识别和迁移 |
| models.py、schemas.py、enums.py | SQLAlchemy 持久结构、Pydantic 输入输出协议与状态 |
| repository.py、learning_state.py | 认知读写、事务/幂等、会话状态和故障恢复 |
| runtime.py | PostgreSQL 控制器锁、每日预算预留/实耗事件 |
| resume.py | durable_replay_v1 输入签名/工作项缓存及研究/模型包装 |
| retrieval.py、embeddings.py、embedding_runtime.py | 三实体关键词/向量融合、指纹、派生索引与嵌入审计 |
| research.py、tools/search.py | 提供方搜索、降级、研究资料集合 |
| tools/reader.py、safe_fetch.py、documents.py | 安全读取、原文提取、血缘信号、本地文档 |
| knowledge/sources.py、lineage.py | 质量规则、来源独立性、依赖边与传递闭包 |
| knowledge/claim_support.py、support_assessment.py | 原文锚点及引文支持/反对/条件/不清楚判定 |
| knowledge/promotion.py、conflicts.py | 评估+证据晋升门控，旧信念冲突判定 |
| knowledge/disputes.py、investigation.py、resolution_claim.py | 独立调查、四结果门控与条件结论约束 |
| brain/llm.py、factory.py、observed.py、prompts.py | 可替换 LLM、裁判构造、调用观测与提示协议 |
| goals/scorer.py、generator.py | 目标优先级、种子和后续目标提议 |
| learning/planner.py、synthesizer.py、reflection.py | 规划、主张结构化提取、反思 |
| learning/closed_book.py、evaluation_contracts.py | 当前闭卷/核源/迁移评估协议 |
| learning/evaluator.py | 较早评估接口，不能替代当前主循环的 closed_book 协议 |
| memory.py | 每日快照、失败模式、复核目标与候选方法 |
| experiments.py、benchmark.py | 冻结题集、当前实验/四组比较/方法验证及较早基准接口 |
| metrics.py、reporting.py、scheduling.py | 状态、纵向报告、实际运行日里程碑 |
| web_models/ | 外部认知来源抽象、Playwright 适配器、等级 0 观察服务 |

仓库其它部分：config/ 策略及网页模型模板，migrations/ 五个 revision，tests/ 历史自动测试，scripts/ 环境及 EXE 构建，data/benchmark/ 示例题，doc/ 六类主文档与历史归档。当前未见 .github CI。

## 4. 持久数据模型

共 19 个业务表，另有 Alembic 版本表：

| 分组 | 表 | 作用 |
| --- | --- | --- |
| 身份/意图 | agents、goals | 使命、目标状态与优先级、父子关系 |
| 情景记忆/续跑 | learning_sessions、learning_steps | 计划/结果/反思/尝试历史，成功工作项及输入键 |
| 原文和来源 | sources、source_links | 提取文字、质量 metadata、依赖/引用关系 |
| 候选及支持 | claims、claim_evidence | 主张提议、原文片段与锚点/支持状态 |
| 接纳知识 | beliefs、evidence、belief_history | 状态/置信/向量、信念证据和每次变更历史 |
| 矛盾 | disputes | 旧状态、调查、决议与未解决状态 |
| 评估/方法 | evaluations、skills | 分维度评估审计，研究步骤和验证 metadata |
| 模型经验 | model_observations、model_profiles | 请求/输出/错误/时延/usage 与统计汇总 |
| 预算/报告 | operation_events、research_reports | 资源预留/实耗、冻结快照与研究/成长报告 |
| 派生召回 | retrieval_entries | Goal/Claim/Belief 文本 hash、关键词和指纹向量 |

Goal 是意图，Claim 是待核验提议，Belief 是带状态的接纳结论；检索相关性不赋予任何事实等级。四记忆类型通过上述记录映射，不代表已实现四套完整独立记忆系统。

## 5. 可靠性机制

### 证据与争议

逐字锚点解决“有没有这句原文”，语义支持解决“有没有支持这个主张”，质量/独立性决定能否晋升。当前质量规则仍主要是 URL/类型启发式。合格独立来源默认至少 2 个、等级 ≥2 才可 verified；低等级支持不足不能冒充已验证。

血缘的同作品、转载、派生等依赖边传递合并独立性；普通 cites 保留展示但不合并。网页自报 metadata 不能提高质量。四结果决议必须使用库内合格证据，并在锁/事务内记录历史；条件化需已有调查主张而非自由生成一句折中。

### 评估与观测

出题器可看研究原文；答题器只看问题及允许的学习记忆，不看来源、答案或 rubric；核源裁判再看新检索资料并提供逐字引文。当前仍有模型裁判偏差；校准是代理值，总分权重见 03。

API、本地兼容服务和网页模型回答写 ModelObservation；Profile 从观测/实验统计刷新，不是正式可信路由分数。A1/A2/B1/B2 只生成比较报告，不自动改模型或信念。

### 续跑、索引和预算

durable_replay_v1 冻结上下文和配置身份；不兼容时拒绝盲重放，人工 retry/restart 保留历史。派生索引可重建，固定 1536 维；关键词 GIN、向量 HNSW、RRF 合并，向量异常通过 savepoint 降级。查询旧数据有 bounded 文本 fallback，尚无大库质量实证。

每日 UTC 预算在调用前预留，usage 返回后调整；价格未填和美元限制 0 不代表免费。进程中断保守保留预留。控制器锁防止同库控制器并行，不是任务 lease 或分布式协调系统。

## 6. 安全与交付边界

safe_fetch 解析公网 IP 并固定连接目标，保留原 Host/TLS 身份，逐重定向校验，限制协议/端口/正文并关闭环境代理；只读远端 HTML/plain。此机制仍需专项验收，不宣称彻底消除 SSRF 或提示注入。

工作台只监听 127.0.0.1，校验 Host/Origin/Token 并设置 CSP，没有公网认证/权限隔离。网页认知需要用户合法授权和手动登录，禁止验证码/反爬绕过。智能体不能自主执行 shell、删除文件、改核心代码、支付或提权。

EXE 是外部数据库模式的文件夹构建配方，用户配置保存在 LOCALAPPDATA；不是已签名正式安装器。持久数据库和外部模型服务仍由部署者准备。

## 7. 兼容与验证事实

所有 schema 变化走 Alembic；完整旧结构可识别 stamp 后升级，未知部分结构拒绝自动迁移。列名匹配不是数据/索引/约束完整性证明。新索引/血缘/续跑不删除旧核心认知，也不伪造历史；详见 [迁移说明](migrations/README.md)。

本次仅静态核对和文档整理，没有新迁移，没有数据库写入。最新功能运行验收、私有真值、旧信念盲审、完整 Mastery/世界模型、多模型管线和长期实证仍待完成，见 [04](doc/04待开发能力清单.md)及 [06](doc/06后续开发计划与构想.md)。
