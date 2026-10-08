# 从 V1 迁移到 V2

V1 在 `.codex-sync/` 中保存 `current-state.md`、`decisions.md`，可能还有 `conversations/`。普通 Windows 磁盘会把 V1 的小写文件名和 V2 的大写文件名视为同一路径。因此，V2 的 `init` 先把两份原文件原样移入 `legacy-v1/`，再在缺少 V2 文件时复制为根目录的 `CURRENT_STATE.md`、`DECISIONS.md`。它还创建 `PROJECT_CONTEXT.md`、`TODO.md`、`project.json`、`checkpoints/` 和 `history/`。原有 `conversations/` 留在原处。首次创建检查点前，请审阅复制的文字，并补全两份新增长期文档。

只在**一台电脑**先运行 `init`，审阅 `.codex-sync/`，再连同相关代码提交、推送。另一台电脑应先拉取这次提交，再使用 V2。如果两台电脑已经分别生成了不同的 `project_id`，不要直接合并两个 JSON 文件；选定已有的项目 ID，保留两边的上下文历史，再通过普通 Git 提交协调文档。

Windows 上，Git 索引可能在文件移动后继续保留旧的小写路径。准备暂存迁移结果时，使用仅对本次命令有效的设置，然后检查暂存文件名：

```powershell
git -c core.ignorecase=false add -A -- .codex-sync
git diff --cached --name-status -- .codex-sync
```

确认暂存区有 `DECISIONS.md`、`CURRENT_STATE.md` 和 `legacy-v1/` 下的两份旧文件，且根目录没有旧的 `decisions.md`、`current-state.md`。

| V1 命令 | V2 对应操作 |
| --- | --- |
| `init`、`status`、`resume` | 保留同名命令，输出采用 V2 格式 |
| `checkpoint --summary ... --next ...` | 改用 `checkpoint --goal ... --summary ... --todo ...` |
| `pull` | 检查本地改动后运行 `sync --pull` |
| `push` | 审阅文件后，明确运行 `git add`、`git commit`、`git push` |
| `native-export`、`native-import` | 已移除；选定原生聊天使用官方 Handoff |
| `--capture-session` | 已移除；检查点只记录明确提供的上下文及 Git 元数据 |

Windows 电脑拉取 V2 后，应重新运行 `install-codex-sync.ps1`，更新已安装命令。安装器会移除旧版安装目录里的原生快照辅助程序。迁移不会触碰此前保存在外部的快照，但这些快照不属于 V2 项目上下文。
