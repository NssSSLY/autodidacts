# Autodidact：持久自学习研究智能体

Autodidact 面向限定领域的长期研究：自主维护目标、检索独立资料、提出主张、核查证据、处理争议、评估掌握程度并积累研究方法。LLM 是可替换的认知工具，不是智能体身份；模型输出不是事实。

当前版本是 **主要链路已接通、仍待实环境与长期实验验收的 V0.1 原型**。不能将能力数量或旧测试结果解释为“已经证明自动变聪明”。

## 文档按六类阅读

从 [文档总导航](doc/README.md)进入：

| 类别 | 文档 |
| --- | --- |
| 怎么使用 | [使用指南](doc/01使用指南.md) |
| 怎么部署 | [部署指南](doc/02部署指南.md) |
| 现有能力与原路线对照 | [实现对照](doc/03现有能力与实现对照.md) |
| 还需要开发的能力 | [待开发清单](doc/04待开发能力清单.md) |
| 每次开发过程与增量 | [开发记录](doc/05开发过程与增量记录.md) |
| 后续计划与初始构想 | [计划与构想](doc/06后续开发计划与构想.md) |

技术结构、逐文件职责与阅读路径：[ARCHITECTURE.md](ARCHITECTURE.md)；开发约束：[AGENTS.md](AGENTS.md)；开发入口：[CODEX_BUILD_PROMPT.md](CODEX_BUILD_PROMPT.md)；迁移：[migrations/README.md](migrations/README.md)。

[原始完整会话交接稿](Autodidact_Full_Conversation_Codex_Handoff.md)保留架构和要求历史；[归档旧稿](doc/归档/README.md)保留独有讨论。历史稿不是最新完成度或操作指南。

## 当前已经接通什么

- 目标池与持久学习循环：规划、搜索、阅读、Claim、Belief、Evidence、Dispute 与历史。
- 原文锚点和语义支持判断、来源质量与传递血缘去重、冲突晋升禁入、四结果争议调查。
- F01-A 主张条件/时间/单位提议、范围覆盖核验与逐引文审计，show-claim 可查看原文及理由；未知范围不等于普遍适用。
- F01-B/C/D 来源作者/发布日期/DOI、arXiv、PMID 声明与出处，复合父句/原子子句分别核验，正文质量锚点/等级上限；show-source、show-claim 可审计。
- PostgreSQL/Alembic 0001–0007、数据库唯一约束、控制器锁、预算与工作项续跑。
- Goal/Claim/Belief 关键词 + pgvector 混合召回，嵌入失败降级和分批维护。
- 隔离闭卷/独立核源、观察/Profile、冻结基准、四组模型比较、记忆/候选技能/30 天报告机制。
- 本地浏览器研究工作台、文档导入、可选网页模型和 Windows 文件夹 EXE 构建配方。

F01 的最小功能范围已接通，仍待真实来源/模型/数据库验收。仍缺负面证据闭环、严格保持/真值评估、迁移旧信念盲审、完整多模型认知/世界模型、成熟运维和真实长期效果证明。细节见实现对照和待开发清单。

## Windows 最短启动路径

需要 Python 3.12+ 和带 pgvector 的 PostgreSQL。Docker 仅用于数据库，不要求服务器。先按部署指南检查 Compose 演示密码及端口：当前为 `5432:5432`，共享网络部署应改回环绑定并换强密码。

在项目根目录 PowerShell 执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
}
# 编辑 .env：数据库、真实模型和可选搜索/嵌入配置
notepad .env
docker compose up -d postgres
.\.venv\Scripts\python.exe -m autodidact.cli init-db
.\.venv\Scripts\python.exe -m autodidact.cli bootstrap
.\.venv\Scripts\python.exe -m autodidact.cli serve --port 8765
```

浏览器访问 http://127.0.0.1:8765。不用网页也可执行 `run-once`、`run --max-cycles 5`、`status`。默认 mock 只演示程序路径，不证明真实研究可用。`pytest` 仅需开发时安装 `.[dev]`；普通工作台不需要 Playwright 浏览器二进制。

EXE 配方、服务器运行、原生数据库、备份恢复和完整命令见部署/使用指南，不要将示例当作生产安全配置。

## 安全与验证边界

外部资料是不受信任的数据，不能成为控制器指令。不得绕过 CAPTCHA/反爬、提取凭据、自主提权、无人值守支付或自主修改核心代码。新模型不能只凭不同意见覆盖高置信旧信念。

最新增量：2026-10-03 F01-B/C/D，迁移 HEAD 为 20261003_0007，仅新增 claims.structure，旧值默认未知，不批量改写认知。当前 pytest：121 passed / 3 skipped，compileall 和 Ruff 通过。三项 PostgreSQL 集成测试因未配置隔离测试库跳过；未运行在线库迁移、付费学习或 EXE 构建。书目是未认证声明，质量规则最高自动等级3，不证明真伪或实验复现。历史测试结果不替代当前验收，细节见累计开发记录。
