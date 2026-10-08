---
name: codex-sync
description: 通过 Git 仓库中的 .codex-sync 保存和恢复项目上下文；适用于创建检查点、更换电脑，以及在同一项目中新建聊天后继续工作。
---

# 项目上下文同步

以 Git 项目的 `.codex-sync/` 作为可跨电脑使用的长期上下文。接手不熟悉的任务前，确认 Git 根目录，读取 `PROJECT_CONTEXT.md`、`DECISIONS.md`、`CURRENT_STATE.md`、`TODO.md` 和最新检查点。`codex-sync resume` 为新聊天生成长度受限的上下文；`codex-sync status` 显示当前 Git 与检查点状态。回答和项目使用文档使用中文，命令及文件名保留原文。

在阶段性节点，按用户与当前任务的真实目标、进展、决定、待办和阻塞创建检查点。命令行工具会记录 Git 状态、变更文件名、增删行数、分支、HEAD 和最近提交。审阅生成的 `.codex-sync/` 文件后，再与对应代码一起提交。`codex-sync sync` 只检查上游；`sync --pull` 要求工作区干净且可以快进。不要自动提交或推送。

若要继续当前原生聊天，优先使用两台已连接主机、相应 Git 项目之间的官方 [Handoff](https://learn.chatgpt.com/docs/remote-connections)。iPhone Remote 访问在线主机。不要把 `CODEX_HOME`、原生 `sessions/`、SQLite 或 Codex 数据库当作双机同步目标，`CODEX_HOME=E:\Codex\home` 时也一样。

V1 迁移时，`init` 会先把旧的小写文档保存在 `.codex-sync/legacy-v1/`，再创建大写的 V2 文档；创建检查点前先审阅这些文件。Windows 上，从仓库的 `tools/codex-sync/install-codex-sync.ps1` 安装或更新命令和本 Skill。完整迁移与使用步骤见该目录内的中文文档。
