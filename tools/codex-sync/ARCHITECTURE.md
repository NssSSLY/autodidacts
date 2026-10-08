# V2 架构

| 层次 | 负责方 | 保存或迁移的内容 |
| --- | --- | --- |
| 原生聊天 | Codex 官方 Handoff | 已连接主机之间选定的一段聊天及其 Git 状态 |
| 远程访问 | ChatGPT/Codex 官方 Remote | iPhone 或受支持设备控制一台在线电脑 |
| 项目上下文 | 本工具及 Skill | Git 中的 `.codex-sync/` 文档、检查点和历史记录 |
| 项目代码 | Git | 代码及可审阅的项目上下文提交 |

命令行工具读取 Git 元数据和明确提供的项目文字。`ProvidedContextAdapter.export_current_context()` 是当前唯一的聊天上下文输入接口：它使用用户或 Codex 明确提供的字段，以及可移植的项目文件。将来若出现官方导出接口，可另加适配器，无须改变检查点的存储格式。现有适配器不读取原生会话或数据库。

V2 的 `project.json` 保存一个长期不变的 UUID。`repository_root: "."` 指向包含 `.codex-sync/` 的 Git 根目录，因此电脑路径不同也能使用同一个项目标识。检查点文件名包含 UTC 时间和随机后缀，避免两台电脑在相近时间创建时重名。每个检查点有 `.md`、`.json` 和一条 `.jsonl` 历史记录。`resume` 会限制输出长度，并给出原文件路径以便查看详情。

`sync` 获取远端信息并报告本地领先或落后的提交数。可选的 `sync --pull` 要求工作区干净、分支没有分叉，且 Git 可以快进。用户审阅后，将代码和 `.codex-sync/` 作为一次 Git 变更提交并推送。

`CODEX_HOME` 可以指向 `E:\Codex\home` 等位置。V2 不枚举也不复制该目录。请勿用云盘直接镜像原生 `sessions/`、SQLite 数据库或整个 Codex 用户目录。
