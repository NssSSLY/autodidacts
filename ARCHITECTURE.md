# 当前技术架构

更新：2026-10-03；业务功能基线 d7f6583，文档/注释起点 a92bbad，f47ff0a 后补争议缓存刷新修复，schema HEAD 20261002_0005。本文描述当前代码，不替代 [实现/缺口对照](doc/03现有能力与实现对照.md)或 [原始要求历史](Autodidact_Full_Conversation_Codex_Handoff.md)。

第一次看代码，可先读第 8 节的通俗说明，再按第 3 节逐文件查职责；方法的具体功能直接写在代码定义上方。

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

外部调用 → OperationBudget(OperationEvent) + ModelObservation
数据库 → PostgreSQL + JSONB + pgvector；Alembic 维护结构
```

搜索/阅读/模型成功结果入库后可重放；核心数据库写入有唯一键、行锁和幂等保护。外部调用与数据库不是原子事务，不能保证绝对 exactly-once。

## 3. 每个代码文件分别负责什么

以下逐文件索引覆盖仓库自己维护的 **89 个 Python 文件、3 个 shell/PowerShell 脚本、1 个 PyInstaller spec、1 个迁移模板**，不包含 .venv、第三方包、构建产物或临时检查工具。每一行链接到实际文件，不把同目录几个文件混写为一个职责。

每个 Python 文件首部有中文“文件职责”；全部 **396 个显式方法/函数（含私有方法、抽象协议和嵌套回调）**前有中文“功能”注释。已有 docstring、提示词和接口保持原样，用注释补说明以避免改变 __doc__ 或运行行为。数据模型自动生成的方法不在手工函数计数中。

### 3.1 入口、控制器、配置与持久状态

| 文件 | 本文件职责 | 优先查看的入口 |
| --- | --- | --- |
| [src/autodidact/__init__.py](src/autodidact/__init__.py) | 声明 autodidact 包并保留版本标识，具体能力由各模块提供。 | 包边界 / 无显式方法 |
| [src/autodidact/advanced_commands.py](src/autodidact/advanced_commands.py) | 注册三实体检索、来源血缘回填和持久学习会话检查/恢复 CLI。 | `register_advanced_commands` |
| [src/autodidact/agent.py](src/autodidact/agent.py) | 组织持久学习主循环：召回、研究、核证、争议、评估、晋升和续跑，模型不直接决定真相。 | `AutonomousLearner._beliefs_for_claim`、`AutonomousLearner._resume_signature`、`AutonomousLearner._planning_inputs`、`AutonomousLearner._index_entity`、其余见代码内注释 |
| [src/autodidact/benchmark.py](src/autodidact/benchmark.py) | 保留早期文件式基准评分接口；当前 CLI 冻结实验走 experiments.py。 | `run_benchmark` |
| [src/autodidact/cli.py](src/autodidact/cli.py) | 注册命令行主入口、日志、初始化、学习循环、状态和冻结基准入口。 | `configure_logging`、`init_db_cmd`、`bootstrap_cmd`、`bootstrap_cmd._run`、其余见代码内注释 |
| [src/autodidact/commands.py](src/autodidact/commands.py) | 注册目标、记忆、技能、模型比较、索引、导入、网页模型与工作台 CLI。 | `register_commands` |
| [src/autodidact/config.py](src/autodidact/config.py) | 读取 .env/环境变量与 YAML，校验模型、身份、预算、来源和安全配置。 | `runtime_settings`、`agent_config` |
| [src/autodidact/db.py](src/autodidact/db.py) | 创建异步 PostgreSQL engine/会话工厂，并经 Alembic 初始化数据库。 | `session_scope`、`init_database` |
| [src/autodidact/embedding_runtime.py](src/autodidact/embedding_runtime.py) | 为嵌入调用添加提供方指纹、预算和观察记录，不把向量当证据。 | `RecordedEmbedding.embed` |
| [src/autodidact/embeddings.py](src/autodidact/embeddings.py) | 提供固定 1536 维的嵌入接口、关闭模式及兼容服务适配。 | `EmbeddingProvider.embed`、`DisabledEmbeddingProvider.embed`、`OpenAICompatibleEmbeddingProvider.embed`、`build_embedding_provider` |
| [src/autodidact/enums.py](src/autodidact/enums.py) | 集中定义目标、信念、争议、来源访问和证据立场状态名。 | 数据结构、常量或兼容别名 |
| [src/autodidact/experiments.py](src/autodidact/experiments.py) | 冻结题集与记忆快照，执行基准、四组模型迁移比较和研究方法验证。 | `canonical_hash`、`freeze_suite`、`evaluate_suite`、`ModelMigrationProtocol.compare`、其余见代码内注释 |
| [src/autodidact/learning_state.py](src/autodidact/learning_state.py) | 处理非晋升规则的运行持久状态：恢复、检查点、报告、模型统计和技能使用结果。 | `LearningState.recover_interrupted`、`LearningState.resumable_attempt`、`LearningState.checkpoint`、`LearningState.accepted_memory`、其余见代码内注释 |
| [src/autodidact/memory.py](src/autodidact/memory.py) | 按日整合接纳记忆、失败模式与证据图，提出复核目标和候选研究技能。 | `MemoryConsolidator.consolidate` |
| [src/autodidact/metrics.py](src/autodidact/metrics.py) | 提供数据库状态计数与学习/信念比例，不将数量当成智能提升证明。 | `dashboard`、`dashboard.count` |
| [src/autodidact/migrations.py](src/autodidact/migrations.py) | 识别已知旧库结构并串行 Alembic 升级，拒绝不完整或未知状态的自动 stamp。 | `alembic_config`、`expected_schema_columns`、`baseline_schema_columns`、`operational_schema_columns`、其余见代码内注释 |
| [src/autodidact/models.py](src/autodidact/models.py) | 定义 19 个持久业务表、外键、唯一约束和向量字段。 | `uuid_pk` |
| [src/autodidact/normalization.py](src/autodidact/normalization.py) | 集中规范文本与计算稳定摘要，统一目标去重和认识论幂等键。 | `normalize_text_key`、`stable_key`、`claim_statement_key` |
| [src/autodidact/reporting.py](src/autodidact/reporting.py) | 汇总指定时间窗的闭卷趋势、资源用量、错误及冻结实验报告。 | `longitudinal_report` |
| [src/autodidact/repository.py](src/autodidact/repository.py) | 封装认知数据库读写及事务：目标、来源、主张锚点、信念、争议、历史和评估。 | `_expected_unique_violation`、`Repository.get_or_create_agent`、`Repository.add_goal`、`Repository.add_goal_if_absent`、其余见代码内注释 |
| [src/autodidact/research.py](src/autodidact/research.py) | 复用研究收集路径，供学习、独立核验与争议调查取得可读且去重的资料。 | `ResearchCollector.fetch`、`ResearchCollector.filter_independent` |
| [src/autodidact/resources.py](src/autodidact/resources.py) | 兼容源码、wheel 和 EXE 的迁移/配置资源路径定位。 | `resource_root` |
| [src/autodidact/resume.py](src/autodidact/resume.py) | 按不可变学习尝试缓存成功工作项；恢复外部结果，不恢复模型内部思考。 | `DurableSteps.bind`、`DurableSteps.run`、`ResumableLLM.bind`、`ResumableLLM.text`、其余见代码内注释 |
| [src/autodidact/retrieval.py](src/autodidact/retrieval.py) | 维护 Goal/Claim/Belief 派生索引并融合关键词、向量和有界降级召回。 | `tokens`、`entity_text`、`RetrievalIndex.sync`、`RetrievalIndex.embed_entity`、其余见代码内注释 |
| [src/autodidact/runtime.py](src/autodidact/runtime.py) | 提供控制器互斥和 UTC 每日预算预留/结算，避免无界外部调用。 | `controller_lock`、`OperationBudget.reserve`、`OperationBudget.finish`、`BudgetedSearch.search` |
| [src/autodidact/scheduling.py](src/autodidact/scheduling.py) | 在实际学习运行时检查可选 Day 0/7/14/21/30 冻结基准里程碑。 | `scheduled_benchmarks` |
| [src/autodidact/schemas.py](src/autodidact/schemas.py) | 定义规划、来源、主张、评估、目标、争议及模型回答的 Pydantic 协议和约束。 | 数据结构、常量或兼容别名 |
| [src/autodidact/workbench.py](src/autodidact/workbench.py) | 提供仅回环地址可访问的研究工作台，操作目标/学习并基于接纳记忆解释问题。 | `Workbench.learn`、`Workbench.dispatch`、`Workbench.connection`、`serve` |

### 3.2 模型与目标

| 文件 | 本文件职责 | 优先查看的入口 |
| --- | --- | --- |
| [src/autodidact/brain/__init__.py](src/autodidact/brain/__init__.py) | 声明可替换认知模型子包，不在导入时运行模型调用。 | 包边界 / 无显式方法 |
| [src/autodidact/brain/factory.py](src/autodidact/brain/factory.py) | 构造可选独立裁判模型，并添加与学习模型一致的调用审计。 | `build_judge` |
| [src/autodidact/brain/llm.py](src/autodidact/brain/llm.py) | 统一可替换模型的文本/结构化接口，提供 Mock 和兼容 API 实现。 | `LLM.text`、`LLM.structured`、`MockLLM.text`、`MockLLM.structured`、其余见代码内注释 |
| [src/autodidact/brain/observed.py](src/autodidact/brain/observed.py) | 为模型调用保存等级 0 观察、错误、时延、usage 及预算账本。 | `ObservedLLM.bind`、`ObservedLLM._call`、`ObservedLLM.text`、`ObservedLLM.structured` |
| [src/autodidact/brain/prompts.py](src/autodidact/brain/prompts.py) | 集中存放规划、提取、冲突、反思等提示词；文本协议不是持久认知状态。 | 数据结构、常量或兼容别名 |
| [src/autodidact/goals/__init__.py](src/autodidact/goals/__init__.py) | 声明目标生成与评分子包，不在导入时新增目标。 | 包边界 / 无显式方法 |
| [src/autodidact/goals/generator.py](src/autodidact/goals/generator.py) | 根据使命和认知上下文提出候选目标；持久化与配额由控制器处理。 | `GoalGenerator.generate` |
| [src/autodidact/goals/scorer.py](src/autodidact/goals/scorer.py) | 将重要性、未知度、探索、效用和成本组合为目标调度优先级。 | `goal_score` |

### 3.3 主张、来源、信念与争议规则

| 文件 | 本文件职责 | 优先查看的入口 |
| --- | --- | --- |
| [src/autodidact/knowledge/__init__.py](src/autodidact/knowledge/__init__.py) | 声明主张、证据、信念与争议规则子包。 | 包边界 / 无显式方法 |
| [src/autodidact/knowledge/claim_support.py](src/autodidact/knowledge/claim_support.py) | 验证候选主张引用是否来自已读原文，区分锚定成功与语义支持。 | `ClaimSupportValidation.anchored_source_ids`、`ClaimSupportValidator.validate`、`ClaimSupportValidator._validate_citation` |
| [src/autodidact/knowledge/conflicts.py](src/autodidact/knowledge/conflicts.py) | 用结构化观察比较旧信念和新主张的关系，是否开争议由控制器门槛决定。 | `ConflictDetector.compare` |
| [src/autodidact/knowledge/disputes.py](src/autodidact/knowledge/disputes.py) | 定义四结果争议决议协议，用数据库支持证据约束模型提议。 | `DisputeRepository.qualified_resolution_sources`、`DisputeRepository.apply_dispute_resolution`、`DisputeResolutionPolicy.decide`、`DisputeResolver.resolve` |
| [src/autodidact/knowledge/investigation.py](src/autodidact/knowledge/investigation.py) | 独立研究争议双方和条件结论，保存调查主张后交给受门控的 Resolver。 | `DisputeInvestigator.investigate` |
| [src/autodidact/knowledge/lineage.py](src/autodidact/knowledge/lineage.py) | 从网页声明和观察提取来源依赖并持久化，传递合并同源而不提高质量等级。 | `safe_target`、`extract_lineage`、`extract_lineage.add`、`SourceLineage.record`、其余见代码内注释 |
| [src/autodidact/knowledge/promotion.py](src/autodidact/knowledge/promotion.py) | 结合评估、开放争议、独立来源数量和质量控制信念晋升。 | `BeliefPromotionPolicy.decide` |
| [src/autodidact/knowledge/resolution_claim.py](src/autodidact/knowledge/resolution_claim.py) | 限制条件化结论必须来自对应争议已记录的调查会话。 | `conditional_claim` |
| [src/autodidact/knowledge/sources.py](src/autodidact/knowledge/sources.py) | 规范 URL/出版方、规则初分来源质量，按出版方/正文/血缘折叠独立证据组。 | `normalize_url`、`publisher_key`、`RuleBasedSourceQualityClassifier.assess`、`_is_government_host`、其余见代码内注释 |
| [src/autodidact/knowledge/support_assessment.py](src/autodidact/knowledge/support_assessment.py) | 对已定位原文判定支持、矛盾、条件或不清楚，失败保持未验证。 | `SupportAssessment.supported_source_ids`、`ClaimSupportAssessor.assess` |

### 3.4 规划、综合、闭卷评估与反思

| 文件 | 本文件职责 | 优先查看的入口 |
| --- | --- | --- |
| [src/autodidact/learning/__init__.py](src/autodidact/learning/__init__.py) | 声明规划、综合、评估与反思子包。 | 包边界 / 无显式方法 |
| [src/autodidact/learning/closed_book.py](src/autodidact/learning/closed_book.py) | 隔离出题、记忆辅助闭卷作答、独立核源和裁判评分。 | `ClosedBookEvaluator.evaluate` |
| [src/autodidact/learning/evaluation_contracts.py](src/autodidact/learning/evaluation_contracts.py) | 定义考试、答案、核源评分与候选方法的结构化 schema。 | 数据结构、常量或兼容别名 |
| [src/autodidact/learning/evaluator.py](src/autodidact/learning/evaluator.py) | 保留 Evaluator 名称作为 ClosedBookEvaluator 的兼容别名，不是第二套评估流程。 | 数据结构、常量或兼容别名 |
| [src/autodidact/learning/planner.py](src/autodidact/learning/planner.py) | 将研究目标与认知上下文转成查询计划，不直接执行网页操作。 | `Planner.plan` |
| [src/autodidact/learning/reflection.py](src/autodidact/learning/reflection.py) | 依据学习结果与评估提出失败原因、缺口、行动和研究经验。 | `Reflector.reflect` |
| [src/autodidact/learning/synthesizer.py](src/autodidact/learning/synthesizer.py) | 从已读资料提出候选主张、引用与知识缺口；输出仍需核验。 | `Synthesizer.synthesize` |

### 3.5 外部资料工具与网页模型

| 文件 | 本文件职责 | 优先查看的入口 |
| --- | --- | --- |
| [src/autodidact/tools/__init__.py](src/autodidact/tools/__init__.py) | 声明网络搜索、读取与本地文档工具子包。 | 包边界 / 无显式方法 |
| [src/autodidact/tools/documents.py](src/autodidact/tools/documents.py) | 读取有大小/页数限制的 UTF-8 TXT/MD 或可选 PDF，导入不直接提升信念。 | `read_local_document` |
| [src/autodidact/tools/reader.py](src/autodidact/tools/reader.py) | 安全读取网页、提取正文及质量/血缘 metadata，并可附加网页预算。 | `WebReader.read`、`WebReader.with_budget`、`WebReader.hash_text` |
| [src/autodidact/tools/safe_fetch.py](src/autodidact/tools/safe_fetch.py) | 限制公网 HTTP(S) 读取，固定已校验 IP 并保留 TLS/Host 身份及响应上限。 | `public_address`、`fetch_public` |
| [src/autodidact/tools/search.py](src/autodidact/tools/search.py) | 抽象网络搜索，提供 Brave API、DuckDuckGo HTML、降级和结果 URL 去重。 | `deduplicate_search_hits`、`SearchProvider.search`、`DuckDuckGoHtmlSearch.search`、`BraveSearchProvider._request`、其余见代码内注释 |
| [src/autodidact/web_models/__init__.py](src/autodidact/web_models/__init__.py) | 声明外部网页认知适配子包，不自动打开浏览器或登录。 | 包边界 / 无显式方法 |
| [src/autodidact/web_models/base.py](src/autodidact/web_models/base.py) | 声明外部认知来源的提问与健康检查协议，模型回答保持观察身份。 | `ExternalCognitiveSource.ask`、`ExternalCognitiveSource.health_check` |
| [src/autodidact/web_models/playwright_adapter.py](src/autodidact/web_models/playwright_adapter.py) | 使用人工已登录的浏览器资料与站点选择器读取网页模型回答，不自动登录或绕过验证。 | `PlaywrightWebModelAdapter.health_check`、`PlaywrightWebModelAdapter.ask` |
| [src/autodidact/web_models/service.py](src/autodidact/web_models/service.py) | 装载网页提供方配置，执行预算化提问并保存成功/失败的等级 0 观察。 | `WebModelService.adapter`、`WebModelService.ask` |

### 3.6 数据库迁移与开发/打包脚本

| 文件 | 本文件职责 | 优先查看的入口 |
| --- | --- | --- |
| [migrations/env.py](migrations/env.py) | 提供 Alembic 离线 SQL 与异步/已注入连接的在线迁移执行环境。 | `run_migrations_offline`、`do_run_migrations`、`run_async_migrations`、`run_migrations_online` |
| [migrations/script.py.mako](migrations/script.py.mako) | 生成未来 Alembic revision 的模板；生成文件的 upgrade/downgrade 必须审查兼容性与数据风险。 | upgrade / downgrade 模板 |
| [migrations/versions/20260914_0001_initial_schema.py](migrations/versions/20260914_0001_initial_schema.py) | 创建初始 13 个认知表与 pgvector 扩展；这是不可改写的历史迁移。 | `_uuid_column`、`upgrade`、`downgrade` |
| [migrations/versions/20260915_0002_source_provenance.py](migrations/versions/20260915_0002_source_provenance.py) | 为来源增加规范 URL、出版方、质量类别/原因与查询索引。 | `upgrade`、`downgrade` |
| [migrations/versions/20260920_0003_epistemic_integrity.py](migrations/versions/20260920_0003_epistemic_integrity.py) | 增加主张引文、认识论幂等约束、争议历史 metadata 和信念向量索引。 | `upgrade`、`downgrade` |
| [migrations/versions/20261002_0004_learning_operations.py](migrations/versions/20261002_0004_learning_operations.py) | 增加持久预算事件、研究报告和技能验证 metadata，不删除旧认知。 | `upgrade`、`downgrade` |
| [migrations/versions/20261002_0005_lineage_resume_retrieval.py](migrations/versions/20261002_0005_lineage_resume_retrieval.py) | 增加来源依赖、学习工作项和三实体派生检索索引。 | `upgrade`、`downgrade` |
| [scripts/autodidact.spec](scripts/autodidact.spec) | 定义 PyInstaller 资源、隐藏模块和文件夹发布结构，不打包真实密钥、学习数据库或 Chromium 用户资料。 | Analysis / EXE / COLLECT |
| [scripts/autodidact_launcher.py](scripts/autodidact_launcher.py) | 作为 EXE 入口分离用户配置与程序资源；默认启动本地工作台。 | `main` |
| [scripts/build_exe.ps1](scripts/build_exe.ps1) | 使用项目 .venv 安装打包/PDF依赖并生成 EXE 文件夹；会重写构建输出，数据库与用户密钥另行配置。 | 顺序执行的准备/构建步骤 |
| [scripts/dev_setup.ps1](scripts/dev_setup.ps1) | Windows 开发环境快捷准备：启动示例数据库，安装开发依赖/Chromium并初始化；会修改环境和数据库，不能当只读检查。 | 顺序执行的准备/构建步骤 |
| [scripts/dev_setup.sh](scripts/dev_setup.sh) | 类 Unix 开发环境快捷准备：启动示例数据库，安装开发依赖/Chromium并初始化；不是正式服务部署。 | 顺序执行的准备/构建步骤 |

### 3.7 测试文件（不是业务运行入口）

| 文件 | 本文件职责 | 优先查看的入口 |
| --- | --- | --- |
| [tests/test_claim_evidence_persistence.py](tests/test_claim_evidence_persistence.py) | 检查主张证据的语义状态持久化与已知唯一约束竞争识别。 | `test_legacy_anchor_without_semantic_support_cannot_remain_claim_evidence`、`test_mixed_supported_and_contradictory_quotes_disqualify_same_source`、`test_only_known_unique_constraint_is_safe_to_recover`、`test_asyncpg_wrapped_unique_constraint_is_recovered` |
| [tests/test_claim_support.py](tests/test_claim_support.py) | 检查引文是否能逐字定位到已读来源，拒绝伪造或未读 URL。 | `test_verbatim_citation_is_anchored_to_read_source`、`test_unread_or_nonverbatim_citation_cannot_supply_claim_source_id` |
| [tests/test_dispute_resolution.py](tests/test_dispute_resolution.py) | 检查四结果决议策略与 Resolver 的数据库证据门控。 | `test_resolution_policy_accepts_all_four_explicit_outcomes_when_qualified`、`test_resolution_policy_rejects_unanchored_or_incomplete_proposal`、`test_resolver_applies_only_a_policy_approved_decision`、`test_resolver_rejects_source_ids_without_database_backed_evidence` |
| [tests/test_dispute_transactions.py](tests/test_dispute_transactions.py) | 检查决议事务、原文引文、行锁缓存刷新和重放幂等。 | `test_unresolved_resolution_writes_one_history_and_retries_idempotently`、`test_adopt_new_resolution_commits_new_belief_evidence_and_histories_together`、`test_conditional_resolution_rejects_statement_not_supported_by_the_claim`、`test_repository_rejects_unlinked_sources_for_new_claim`、其余见代码内注释 |
| [tests/test_embeddings.py](tests/test_embeddings.py) | 使用 HTTP 替身检查嵌入响应形状和关闭模式降级。 | `test_openai_compatible_embedding_provider_validates_vector_shape`、`test_disabled_embedding_provider_is_a_graceful_fallback` |
| [tests/test_epistemic_idempotency.py](tests/test_epistemic_idempotency.py) | 用内存会话替身检查目标、主张和争议的重复写入保护。 | `test_active_goal_title_is_deduplicated_after_text_normalization`、`test_claim_is_idempotent_within_learning_session`、`test_open_dispute_is_idempotent_and_does_not_rewrite_belief_history`、`test_legacy_open_dispute_without_dedup_key_is_reused` |
| [tests/test_goal_retry.py](tests/test_goal_retry.py) | 检查目标失败计数达到上限后进入 blocked，防止无限重试。 | `test_failed_goal_becomes_blocked_at_retry_limit` |
| [tests/test_goal_score.py](tests/test_goal_score.py) | 检查目标评分优先关注重要且不确定的知识缺口。 | `test_goal_score_prefers_important_uncertain_goal` |
| [tests/test_migrations.py](tests/test_migrations.py) | 离线检查迁移链、生成 SQL 和已知旧表列结构识别，不代替实际升级验收。 | `test_migration_graph_has_expected_single_head`、`test_initial_migration_generates_offline_sql`、`test_fresh_or_versioned_schema_uses_upgrade`、`test_current_create_all_schema_is_stamped_at_head`、其余见代码内注释 |
| [tests/test_postgres_integration.py](tests/test_postgres_integration.py) | 仅在显式 *_test 数据库内临时 schema 验证升级、并发唯一约束和向量召回。 | `test_unversioned_0001_upgrade_preserves_agent`、`test_concurrent_claim_uniqueness_and_pgvector_recall` |
| [tests/test_promotion.py](tests/test_promotion.py) | 检查晋升的评估/争议/可追溯来源门槛与单源/多源状态。 | `test_open_dispute_blocks_belief_promotion`、`test_claim_without_traceable_evidence_is_not_promoted`、`test_single_source_claim_is_provisional`、`test_multi_source_claim_can_be_verified` |
| [tests/test_promotion_quality.py](tests/test_promotion_quality.py) | 检查来源质量与出版方独立性共同限制 verified。 | `test_two_pages_from_same_publisher_are_not_independent_verification`、`test_low_quality_independent_pages_do_not_create_verified_belief`、`test_independent_quality_sources_can_create_verified_belief` |
| [tests/test_schemas.py](tests/test_schemas.py) | 检查结构化主张 schema 的置信度范围约束。 | `test_claim_confidence_is_bounded` |
| [tests/test_search.py](tests/test_search.py) | 用 HTTP/提供方替身检查 Brave 结果映射、认证和有序降级。 | `test_brave_search_maps_structured_results_and_authenticates`、`test_fallback_search_uses_next_provider_after_failure`、`test_fallback_search_reports_all_provider_failures` |
| [tests/test_search_hygiene.py](tests/test_search_hygiene.py) | 检查搜索 URL 安全过滤、规范化、去重和返回数量。 | `test_search_results_are_normalized_deduplicated_and_limited_to_web_urls` |
| [tests/test_semantic_retrieval.py](tests/test_semantic_retrieval.py) | 使用查询替身检查兼容信念向量召回的 SQL 排序和空向量排除。 | `test_semantic_recall_uses_pgvector_cosine_distance_and_skips_null_vectors` |
| [tests/test_source_schema.py](tests/test_source_schema.py) | 检查来源证据等级及可信度字段的合法范围。 | `test_source_evidence_metadata_is_bounded` |
| [tests/test_sources.py](tests/test_sources.py) | 检查 URL、出版方、规则来源质量及同源独立性折叠。 | `test_url_normalization_removes_tracking_and_fragment_but_keeps_content_query`、`test_publisher_key_groups_subdomains_but_not_public_hosting_tenants`、`test_quality_classifier_is_conservative_and_model_output_is_level_zero`、`test_source_independence_collapses_same_publisher_and_mirrored_content` |
| [tests/test_support_assessment.py](tests/test_support_assessment.py) | 检查原文语义支持、条件/矛盾排除和模型失败降级。 | `test_only_supported_anchored_quote_can_supply_promotion_evidence`、`test_provider_failure_keeps_claim_unverified` |
| [tests/test_web_model_config.py](tests/test_web_model_config.py) | 检查网页模型适配器配置字段和默认值，不启动浏览器。 | `test_web_model_config` |

### 3.8 配置、构建及数据文件

这些不是研究逻辑方法，但决定程序如何启动和运行：

| 文件 | 作用 / 风险 |
| --- | --- |
| [pyproject.toml](pyproject.toml) | Python 版本、依赖/可选依赖、CLI 入口、wheel 资源和检查配置 |
| [.env.example](.env.example) | 运行连接及提供方字段模板；真实 .env 由用户维护，不提交密钥 |
| [config/agent.yaml](config/agent.yaml) | 使命、学习/晋升门槛、预算、重试和安全策略 |
| [config/web_models.example.yaml](config/web_models.example.yaml) | 网页提供方选择器和浏览器资料配置模板，不提供现成登录凭据 |
| [docker-compose.yml](docker-compose.yml) | 示例 PostgreSQL+pgvector 服务和持久卷；演示密码/端口需部署者加固 |
| [alembic.ini](alembic.ini) | 迁移脚本位置/日志/连接模板；应用入口会注入实际 DATABASE_URL |
| [data/benchmark/sample.json](data/benchmark/sample.json) | 两题演示基准，不是有效性证明或完整技能验证题集 |
| [.gitignore](.gitignore) | 排除 .venv、密钥、浏览器资料和构建目录；备份另做保护 |

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

上次文档整合仅做静态核对；本次新增中文注释仍无新迁移、无学习数据库写入，方法 AST 与注释前一致。最新功能运行验收、私有真值、旧信念盲审、完整 Mastery/世界模型、多模型管线和长期实证仍待完成，见 [04](doc/04待开发能力清单.md)及 [06](doc/06后续开发计划与构想.md)。

## 8. 适合第一次读代码的架构解释

可以把项目理解为四部分：**入口告诉它研究什么，控制器决定怎么做，数据库保存它学过什么，证据/评估规则限制它能相信什么**。模型是控制器调用的工具；搜索和浏览器也是工具，不能自己取得写信念、执行命令或提高权限的权力。

| 层 | 负责什么 | 不负责什么 |
| --- | --- | --- |
| 用户入口 | CLI / 本地浏览器 / 文件导入，将问题转成目标或记忆解释请求 | 不把每条对话直接变成真理 |
| 控制器与运行层 | AutonomousLearner + 预算/锁/续跑，选择目标并协调模块 | 不训练底层模型参数，不允许任意工具调用 |
| 认知规则层 | 原文锚点、语义支持、独立性、晋升与四结果争议 | 不因更强模型的意见覆盖旧知识 |
| 持久数据与测量层 | PostgreSQL 中保存会话、证据、信念/历史、方法和冻结实验 | 不用模型自评或记录数量证明智能提升 |

### 8.1 一次普通学习的实际代码路径

1. cli.run_once_cmd（或 Workbench.learn）初始化数据库并构造 AutonomousLearner。
2. run_cycle 获取控制器锁；_run_cycle 恢复已有尝试或从 Repository.next_goal 选目标，先持久化 LearningSession。
3. _planning_inputs 组织相关认知/已验证方法；Planner.plan 生成查询，搜索/阅读包装器保存成功工作项。
4. WebReader.read 取得原文；Repository.upsert_source 保存来源与 SourceLineage 依赖边；Synthesizer.synthesize 提出 ClaimDraft。
5. ClaimSupportValidator.validate 定位引文，ClaimSupportAssessor.assess 判断语义支持；Repository.add_claim / record_claim_evidence 保存候选及判定。
6. _beliefs_for_claim 混合召回旧信念，ConflictDetector.compare 给出关系观察；达到冲突门槛则创建 Dispute。
7. ClosedBookEvaluator.evaluate 隔离出题/答题/独立核源/评分，再由 Reflector.reflect 总结缺口与经验。
8. BeliefPromotionPolicy.decide 检查评估、争议和独立证据；允许后创建/复用 Belief，追加 Evidence 和 BeliefHistory。
9. 保存目标终态和会话结果，记录方法使用效果、提出后续目标；普通学习收尾可触发每日整合/基准维护。

例如研究“GNSS 中断时 IMU 能否一直无漂移定位”：模型先给出候选说法，不立刻认定正确。引文不支持“无限期”就不能贡献支持；若与旧高置信信念冲突进入争议；来源/评估不足可保留候选或失败。流程成功运行和结论已验证是两个不同状态。

### 8.2 三条旁路不要和学习混淆

- **争议目标**：_run_cycle 识别 dispute_id 后走 DisputeInvestigator.investigate → DisputeResolver → Repository.apply_dispute_resolution，调查双方并记录四种结果，不照搬普通学习晋升。
- **工作台提问**：Workbench.dispatch 的 /api/ask 只召回无开放争议的接纳记忆，ObservedLLM 生成待核查解释并返回来源；不因此生成新已验证信念。
- **实验命令**：experiments 冻结题集/记忆做评估和 A1/A2/B1/B2 比较，写 ResearchReport；不是自动切换模型或清洗知识库。

### 8.3 推荐阅读顺序

先看本节和第 2 节数据流 → cli.py/commands.py → agent.py → schemas.py/models.py → repository.py/learning_state.py → knowledge 原文支持/晋升/争议 → learning/closed_book.py → resume.py/runtime.py/retrieval.py → experiments.py/memory.py。需要使用或部署时返回 [文档索引](doc/README.md)，不要先执行 dev_setup 或迁移降级来“试着看”。

## 9. 本次注释工作的验证与维护

本次注释覆盖 89 个 Python 文件与 396 个显式函数/方法，以及辅助构建/迁移模板职责。注释前后逐文件 AST（包含原 docstring，不含行号）完全一致，未改函数签名、提示词、条件、事务、配置或 schema。compileall 通过。

按 AGENTS 在修改前后分别运行已有 pytest，两次结果一致：56 passed、2 failed、2 skipped。两项失败均来自 test_dispute_transactions 中模拟 _Session 缺少 scalars，发生在新增注释之前；未配置在线测试数据库，两项 PostgreSQL 集成测试跳过。本次不顺带修复测试替身，也不把跳过当通过。CLI 帮助检查正常；完整验证范围见 [最新开发记录](doc/05开发过程与增量记录.md)。

以后新增/修改方法时同步维护职责注释及本节逐文件索引；若注释与代码冲突，以实际实现核对后修正文档，不用注释替代测试或证据。

## 10. 后续修复：争议锁定查询刷新

2026-10-03 在 f47ff0a 后修复 apply_dispute_resolution：SELECT FOR UPDATE 配合 populate_existing=True，避免会话已加载争议时仍用旧决议属性。相同决议重试复用已生成信念，不同决议重试拒绝改写；不改变证据门槛。事务测试补齐 scalars/all 和按 Claim/Source/status 过滤原文，并断言新 Evidence 保留真实引用。

新增原文筛选及两类缓存重试回归；当前完整 pytest 为 61 passed / 2 skipped，compileall、Ruff 通过。跳过项仍是未配置隔离库的 PostgreSQL 集成测试，真实数据库并发验收另属 V03。本次没有 schema 迁移、回填或学习数据写入；第 9 节保留上一轮注释工作的历史结果。
