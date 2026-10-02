# Autodidact V0.1

一个与模型无关、具备持久状态的自学习智能体。LLM 是可替换的推理引擎；智能体的长期身份存在于目标、信念、证据、争议、技能、测试与学习历史中。

## 阅读入口

- [2026-10-02 功能与使用说明](doc/20261002功能实施与使用说明.md)：闭卷核源、争议调查、长期记忆、模型迁移、本地网页及 EXE 构建配方。本轮按要求未运行测试，新增流程待实机验收。

- [路线评估与初始构想对照](doc/路线评估与初始构想对照.md)：完成后能做到什么、与初衷的差距和建议实施顺序。
- [部署安装与验证手册](doc/部署安装与验证手册.md)：环境准备、Windows 命令、数据库与真实学习验收、备份换机、服务器及打包边界。
- [项目全量迁移交接与技术说明](doc/项目全量迁移交接与技术说明.md)：项目历史、当前代码结构与详细技术说明。

## 核心认识论规则

**模型输出是提议，而不是真相。** API 模型、本地模型和基于浏览器的对话模型都可以提出主张。主张在晋升前必须与已有信念比较，并得到独立检索证据的支持。矛盾会创建一个 `Dispute`（争议），而不会覆盖旧知识。

## V0.1 包含的内容

- 持久化的 PostgreSQL/pgvector 数据结构。
- 智能体使命，以及自主目标和跟进目标。
- 网络搜索、URL 规范化、结果去重与网页读取。
- 将资料结构化综合为关联来源的主张。
- 基于发布者、正文哈希和证据等级的来源独立性与质量初筛。
- 信念、证据与历史记录表。
- 冲突检测与争议创建。
- 评估与反思循环。
- 目标评分和好奇心目标脚手架。
- 模型画像与模型观察数据结构。
- 基于 Playwright 的通用浏览器对话模型适配器。
- CLI、Docker 数据库、示例基准和冒烟测试。

## 项目结构

```text
config/
  agent.yaml                 使命、学习策略、安全策略
  web_models.example.yaml    可选的浏览器模型选择器
src/autodidact/
  agent.py                   自主学习主循环
  models.py                  持久化数据结构
  repository.py              数据库操作
  brain/llm.py               可替换的 LLM 适配器
  goals/                     好奇心目标生成与确定性评分
  learning/                  规划 -> 综合 -> 测试 -> 反思
  knowledge/conflicts.py     旧信念与新主张的比较
  tools/search.py            无密钥搜索的后备实现
  tools/reader.py            不受信任网页的内容提取
  web_models/                基于浏览器的外部认知来源
  metrics.py                 长期学习指标
  benchmark.py               冻结基准运行器
  cli.py                     命令行命令
migrations/                  Alembic 数据库结构迁移
```

## Windows 快速开始

前置条件：Python 3.12+、Docker Desktop、Git。

```powershell
cd autodidact_v0_1
Copy-Item .env.example .env
docker compose up -d
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -e ".[dev]"
playwright install chromium
autodidact init-db
autodidact bootstrap
```

`autodidact init-db` 会使用 Alembic 将数据库升级到最新结构。对于早期由 `create_all` 创建的数据库，系统会先核对核心表与字段；完全匹配 V0.1 基线或来源元数据版本时才无损标记对应版本再升级。检测到不完整或不一致的旧结构时会停止，避免覆盖已有学习状态。

初始 `.env` 使用 `LLM_PROVIDER=mock`。这能让初始化和测试正常工作，但不会执行有实际价值的研究。第一次运行真实学习循环之前，请配置真实模型：

```env
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://YOUR_PROVIDER_BASE_URL/v1
LLM_API_KEY=YOUR_KEY
LLM_MODEL=YOUR_MODEL_NAME
```

适配器预期服务提供常见的 `/chat/completions` 兼容端点。如果某个提供方的接口不同，应实现另一个 `LLM` 子类，而不是修改智能体循环。


语义信念召回默认关闭，不要求额外密钥；关闭或嵌入服务失败时，系统会安全回退到最近信念。若要启用与 OpenAI 兼容的嵌入端点，请配置一个**输出 1536 维向量**的模型：

```env
EMBEDDING_PROVIDER=openai_compatible
EMBEDDING_BASE_URL=https://YOUR_PROVIDER_BASE_URL/v1
EMBEDDING_API_KEY=YOUR_KEY
EMBEDDING_MODEL=YOUR_1536_DIMENSION_EMBEDDING_MODEL
```

迁移会建立 `pgvector` 的 HNSW 余弦索引；首次使用前需确保 PostgreSQL 已安装并允许 `vector` 扩展。向量只用于候选信念召回，后续冲突判断仍是待验证的模型观察，不能直接改变信念状态。
搜索层默认使用 `SEARCH_PROVIDER=auto`。配置 `BRAVE_SEARCH_API_KEY` 后，系统优先调用 Brave Search API，并在请求失败或没有结果时自动回退到 DuckDuckGo HTML；未配置密钥时直接使用无密钥后备搜索。也可以显式设置 `SEARCH_PROVIDER=brave` 或 `SEARCH_PROVIDER=duckduckgo`。

运行一个学习循环：

```powershell
autodidact run-once
```

运行多个自主学习循环：

```powershell
autodidact run --max-cycles 5
```

查看指标：

```powershell
autodidact status
```

运行冻结的示例基准：

```powershell
autodidact benchmark --path data/benchmark/sample.json
```

## 单次学习循环

```text
目标池
   -> 选择价值最高的目标
   -> 检索认识论上下文
   -> 规划搜索，包括失败案例和反例查询
   -> 搜索去重并读取不受信任的网页，记录规范 URL、发布者和质量初筛
   -> 综合候选主张和原文摘录，并逐字验证摘录确实存在于已读取原文
   -> 将每条主张与已有信念比较
       -> 出现矛盾 => 创建争议
   -> 评估事实性、推理、迁移能力和校准度
   -> 反思失败或成功原因
   -> 合格主张 => 临时或已验证信念 + 证据
   -> 未回答问题或前置依赖 => 新目标
```

没有经过原文逐字锚定的来源不会进入 Claim 的 `source_ids`，因此不能支持晋升。存在未解决争议或没有可追溯来源的主张不会晋升为信念。同一发布者的多个页面、正文完全相同的镜像页面只计为一个独立证据组；只有达到 `min_evidence_level_for_verified_belief` 的独立来源才参与 `VERIFIED` 判定，普通低等级网页最多支持临时信念。

活动目标、同一学习会话中的等价 Claim、重复证据和同一 Claim/Belief 对应的 Dispute 会幂等复用。新记录由数据库部分唯一索引保护，并在竞争写入时读取已创建记录；旧学习记录不回填幂等键，保持原样。失败目标最多重试 `max_retry` 次，达到上限后进入 `BLOCKED`，避免长期占据目标队首。

## 为什么更换模型不会抹除学习成果

数据库在模型更换后仍会保留。`Belief`、`Evidence`、`Dispute`、`BeliefHistory`、`Evaluation`、`Goal` 和 `Skill` 都独立于 `LLM`。当新模型不同意一个高置信度旧信念时，系统会发起研究，而不是覆盖旧信念。`ModelProfile` 用于根据基准测试与历史错误，逐步积累模型在不同领域的可信度评分。

## 使用没有 API 的浏览器对话模型

`PlaywrightWebModelAdapter` 被刻意设计为通用适配器。它可以在获准使用的网页界面中输入提示词并提取回答。它**不会**自动登录、绕过 CAPTCHA、规避反爬措施，也不会赋予网页模型控制智能体的权限。

工作流：

```text
浏览器模型回答
    -> 模型观察（证据等级 0）
    -> 提取候选主张
    -> 独立检索引用或原始来源
    -> 与已有信念比较
    -> 验证 / 创建争议 / 保持未解决
```

如需使用浏览器模型，请复制 `config/web_models.example.yaml`，为使用条款允许自动化的网站添加选择器，并使用由你手动完成身份验证的持久化浏览器配置目录。界面自动化较为脆弱，因此浏览器模型应始终只是可选来源，而不能成为单点依赖。

## 当前 V0.1 的限制（有意保留）

- Brave Search API 需要自行申请并配置密钥；DuckDuckGo HTML 仅作为无密钥后备方案。
- 语义召回目前仅索引信念，尚未实现主张、目标的混合检索与重排。
- 评估器由模型辅助完成；V0.2 应更严格地分离问题生成、闭卷回答和来源验证。
- `DisputeResolver` 已实现四种可审计结果，但仍需独立的争议调查工作流来生成高质量决议提案。
- 每个网站的浏览器对话选择器都需要单独维护。
- 自主循环未开放 shell、文件删除、登录或支付能力。

这些限制是有意设计的：V0.1 首先要证明，在不赋予智能体危险权限的前提下，一个完整学习循环能够生成可追溯、可测试的知识。

## 建议开展的 30 天实验

第 0 天：冻结一套学习器在训练期间不可见的私有基准。在仅使用基础模型和使用智能体记忆两种条件下分别运行基准；在第 7、14、21 和 30 天重复。跟踪以下指标：

- 基准得分
- 已验证信念占比
- 目标成功率
- 自生成目标占比
- 迁移得分
- 校准误差
- 未结争议比例
- 旧信念保持得分
- 程序性技能数量
- 更换模型后模型能力与记忆增益的差值

关键结果不是数据库规模，而是：同一个推理模型或替换后的推理模型，在积累的智能体状态支持下，是否能取得可测量的性能提升。

## 将仓库交给 Codex 继续开发

在 Codex 中打开此仓库，并向它提供 `CODEX_BUILD_PROMPT.md`。该提示文档说明了 Codex 必须保留的架构不变量以及下一步建设内容。让它逐项完成任务、每次修改后运行测试，并确保数据库迁移与已有学习状态兼容。
