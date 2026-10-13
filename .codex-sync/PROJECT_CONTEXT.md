# Autodidact 项目背景

Autodidact 是持久自学习研究智能体 V0.1 原型。模型是可替换认知工具；持久身份来自目标、信念、证据、争议、历史、评估与技能。不能将项目缩减为普通聊天机器人或 RAG。

当前入口：README.md、doc/README.md 的六类文档；技术结构看 doc/ARCHITECTURE.md，开发约束看 doc/AGENTS.md，开发入口看 doc/CODEX_BUILD_PROMPT.md。doc/05开发过程与增量记录.md 是累计日志，doc/04待开发能力清单.md 与 doc/06后续开发计划与构想.md 记录缺口和路线。

主要目录：src/autodidact/ 为程序；tests/ 为回归；migrations/ 为 Alembic；config/ 和 scripts/ 为配置与工具。环境为 Python 3.12+、PostgreSQL/pgvector，可选搜索/嵌入/模型提供方。

本目录只同步开发上下文，不保存真实 .env、数据库数据、浏览器资料、Codex 原生聊天或整个用户配置。归档与原始完整会话交接稿保留历史要求，不能冒充最新实现状态。

2026-10-13新增F07版本化嵌入重建运营，migration HEAD20261013_0009/20业务表。候选工作项、后台有界批次、暂停/恢复/重试、完整性核验与原子启用已接通，旧认知/向量保留；详细边界看当前03/04与架构第18节。
