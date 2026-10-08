# 项目开发入口

开发规则沿用 `doc/AGENTS.md`，业务状态与缺口从 `doc/README.md` 的六类文档读取；业务代码修改需遵循该文件的验证要求。

## Codex 项目上下文同步

新聊天或换电脑接手时，先使用 `$codex-sync` 读取 `.codex-sync/PROJECT_CONTEXT.md`、`DECISIONS.md`、`CURRENT_STATE.md`、`TODO.md` 和最新检查点，再核对 Git 状态及本项目权威文档。Skill 未安装时直接读取这些文件，不以缺少 Skill 为由跳过上下文核对。

阶段任务完成后按真实目标、进展、决定与待办更新长期上下文并创建检查点；历史测试须标明来自既有文档。项目文档使用中文。只有用户明确要求时才提交或推送，不能同步原生 Codex 会话、数据库或整个 `CODEX_HOME`。
