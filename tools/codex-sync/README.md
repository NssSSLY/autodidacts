# Codex 项目上下文同步（V2）

`codex-sync` 保存项目级上下文，让两台 Windows 电脑上的新 Codex 会话能够从同一个 Git 仓库继续工作。它不读取或复制 Codex 原生聊天、SQLite、`sessions/` 或整个 `CODEX_HOME`。指定原生聊天的跨主机迁移使用官方 [Handoff](https://learn.chatgpt.com/docs/remote-connections)；iPhone 使用官方 Remote 连接在线主机。

## 安装

需要 Git 和 Python 3.10+。在项目仓库根目录运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\codex-sync\install-codex-sync.ps1 -InstallSkill
```

重开终端后运行 `codex-sync --help`，并重开 Codex 以加载新安装的 Skill。安装器将 Python CLI 和模板复制到 `%LOCALAPPDATA%\CodexSync\bin`，并将该目录加入当前用户 PATH。`-InstallSkill` 将仓库中的 Skill 复制到 `$CODEX_HOME\skills\codex-sync`（未设置时为 `%USERPROFILE%\.codex\skills\codex-sync`），不会读取或同步 Codex 用户数据。每台电脑都要各自执行安装命令；Git 只传递安装脚本和 Skill 源文件。

## 命令

在 Git 仓库内执行：

```powershell
codex-sync init
codex-sync status
codex-sync checkpoint --goal "当前目标" --summary "本阶段进展" --decision "关键决定" --todo "下一步" --blocker "未解决问题"
codex-sync resume
codex-sync sync
codex-sync sync --pull
```

- `init` 创建 `.codex-sync/project.json`、四份长期文档和 `checkpoints/`、`history/`。已有 V1 小写文档先移入 `legacy-v1/` 留存，再复制为新文档，避免 Windows 文件名大小写冲突；重复执行不会换掉 `project_id`。
- `status` 只读，显示仓库、分支、HEAD、dirty 状态和最近 checkpoint。
- `checkpoint` 从显式参数、长期文档和 Git 元数据生成 Markdown、JSON 和单条 JSONL。记录文件名、增删行数和 Git 状态，不保存原始代码补丁，也不覆盖手工维护的 `CURRENT_STATE.md`；请审阅生成内容再提交。
- `resume` 在终端输出长度受限的恢复上下文，包含长期文档、最近检查点、近期历史和当前工作区状态。此命令不会自动把文字送进 Codex 聊天；新聊天可调用 `$codex-sync` 读取项目文件，或把终端输出粘贴进去。
- `sync` 仅 fetch 并检查上游状态；`sync --pull` 只在工作区干净、能够快进时拉取。它不自动提交或推送。

`project.json` 的 `repository_root` 固定为 `.`，表示该文件所在项目的 Git 根目录，因此不会把 A 机盘符写进 B 机的上下文。`project_id` 在首次初始化时生成一次，需与 `.codex-sync/` 一起提交到 Git；另一台电脑先拉取，再运行 `resume`。

首次从 V1 迁移时，Windows Git 暂存大小写改名需用 `git -c core.ignorecase=false add -A -- .codex-sync`，并检查暂存路径。详见迁移说明。

迁移细节见[迁移说明](MIGRATION_V1_V2.md)，分层设计见[架构说明](ARCHITECTURE.md)，电脑 A 推送、电脑 B 首次安装及 Codex 读取步骤见[双机操作指南](WINDOWS_TWO_HOST_IPHONE.md)。

## 测试

```powershell
Set-Location .\tools\codex-sync
python -m unittest discover -s tests -v
```
