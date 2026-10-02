# Autodidact V0.1 架构

## 架构不变量

1. `LLM` 是可替换的，它不是智能体的身份。
2. `ModelAnswer` 和生成文本只是观察或提议，绝不是权威真相。
3. 持久认知存在于 `Goal`、`Belief`、`Evidence`、`Dispute`、`BeliefHistory`、`Evaluation`、`Skill` 和基准历史中。
4. 不能仅因新模型提出异议就覆盖旧知识；矛盾必须创建争议。
5. 外部网页或模型内容是不受信任的数据，不能向控制器发出可执行指令。
6. 学习进展由测试和保留状态衡量，而不是由模型自报的置信度衡量。

## 运行时依赖关系

```text
命令行界面（CLI）
 -> 自主学习器（AutonomousLearner）
    -> 目标生成器 / 评分器（GoalGenerator / Scorer）
    -> 规划器（Planner）
    -> 搜索提供方（SearchProvider）
    -> 网页读取器（WebReader）
    -> 综合器（Synthesizer）
    -> 冲突检测器（ConflictDetector）
    -> 评估器（Evaluator）
    -> 反思器（Reflector）
    -> 仓储层（Repository）
       -> PostgreSQL / pgvector
```

可选的外部认知链路：

```text
Playwright 网页模型适配器（PlaywrightWebModelAdapter）
 -> 模型回答（ModelAnswer）
 -> 模型观察（ModelObservation，计划中的集成点）
 -> 主张提取
 -> 独立证据检索
 -> 信念 / 争议处理管线
```

## 主要状态转换

```text
目标（Goal）：
已发现（DISCOVERED） -> 已规划（PLANNED） -> 研究中（RESEARCHING） -> 综合中（SYNTHESIZING） -> 测试中（TESTING） -> 反思中（REFLECTING）
                                                                                                      -> 已通过（PASSED）| 已失败（FAILED）

信念（Belief）：
临时（PROVISIONAL） -> 有支持（SUPPORTED） -> 已验证（VERIFIED）
                  \-> 有争议（DISPUTED） -> 已验证（VERIFIED）| 已削弱（WEAKENED）| 已撤回（RETRACTED）| 未解决（UNRESOLVED）
```

## 数据表职责

- `agents`：长期使命与高层状态。
- `goals`：学习意图、来源、优先级和结果。
- `learning_sessions`：一次完整的学习尝试及其反思。
- `sources`：检索到的证据材料，按内容哈希去重。
- `claims`：从证据中综合出的原子命题。
- `beliefs`：当前认识论承诺，而不是原始聊天记录。
- `evidence`：支持、反驳或上下文类型的证据边，以及证据等级。
- `disputes`：需要调查的明确矛盾。
- `belief_history`：只追加的信念演化轨迹。
- `evaluations`：可测量的学习结果。
- `skills`：程序性记忆。
- `model_observations`：来自 API、本地或网页模型的输出。
- `model_profiles`：按领域统计的模型经验可信度和性能。

数据库结构由 Alembic 迁移管理。`init-db` 会升级到最新迁移版本；旧版 `create_all` 数据库只有在核心表与字段完全匹配初始基线时才会被无损标记，结构不一致时停止自动迁移，以保护已有学习状态。

## 2026-10-02 实现增量

`ObservedLLM` 记录等级 0 模型观察，`OperationBudget` 持久预留调用资源，PostgreSQL advisory lock 约束单控制器写入。评估请求分成出题、闭卷回答及独立检索核源；争议目标接入 `DisputeInvestigator` 与现有决议门槛。`MemoryConsolidator` 提取候选技能并保留每日快照，冻结实验验证后才在规划中使用。

新增 `operation_events` 与 `research_reports` 数据表；`skills.metadata_json` 记录候选/验证状态。工作台通过本机 HTTP 入口管理目标与查询认知状态，不绕过晋升门槛。实现与待验收边界见 `doc/20261002功能实施与使用说明.md`。

## 模型迁移协议

将模型 A 替换为模型 B 之前：

1. 冻结基准集和当前信念数据库。
2. 在不使用记忆的条件下运行模型 A。
3. 在使用记忆和上下文检索的条件下运行模型 A。
4. 在不使用记忆的条件下运行模型 B。
5. 在使用相同记忆和上下文检索的条件下运行模型 B。
6. 比较基础模型增益与累积记忆增益。
7. 对高置信度信念进行抽样，并刻意让模型 B 独立作答。
8. 任何分歧都进入 `Dispute`，绝不直接改写信念。
9. 根据观察到的领域错误和校准表现更新 `ModelProfile`。

## 浏览器模型策略

基于浏览器的对话模型是可选的外部认知通道。连接器只能在获得授权的范围内输入和读取内容。它不得自动绕过 CAPTCHA、规避反爬措施、提取凭据、执行支付、实施破坏性操作或提升权限。界面失效只代表该来源能力降级，不能导致整个智能体失败。

## 搜索提供方策略

`SearchProvider` 隔离搜索服务实现。配置 Brave Search API 时，运行时优先使用结构化 API，并将 DuckDuckGo HTML 保留为无密钥后备；主提供方网络错误、协议错误或空结果只会触发下一提供方，不会直接终止学习器。

## 来源依赖、持久重放与统一召回增量

迁移 `20261002_0005` 增加 `source_links`、`learning_steps` 和 `retrieval_entries`，不修改旧认知行。`SourceLineage` 区分共同工作/转载依赖与普通引用，依赖图闭包用于晋升、决议及独立核源计票；网页声明只能保守减少独立票数。

`DurableSteps` 以会话内工作项唯一键保存成功输出，新会话从规划前开始留痕。模型、查询材料和角色隔离保持不变，重放调用使用冻结输入，数据库副作用通过幂等保护。旧版缺少细粒度快照的会话仍保留证据后重试。

`HybridRetriever` 在 Belief / Claim / Goal 上融合关键词与语义排名；工作台只检索已接受且无开放争议的信念。检索索引可重建，不授予候选主张真值资格。完整使用与局限见 [来源血缘阶段续跑与混合检索](doc/来源血缘阶段续跑与混合检索.md)。
