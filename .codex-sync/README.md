# 项目上下文同步

本项目有独立 project_id 与四份长期上下文。安装与操作见 ../tools/codex-sync/README.md；业务状态以 PROJECT_CONTEXT.md 指向的权威文档和 Git 为准。

## Codex 跨电脑继续开发

本机已安装的 `$codex-sync` 可用于这个项目。仓库中的 `tools/codex-sync/` 是可供另一台电脑安装的源码。先安装 Git 与 Python 3.10+，在本仓库根目录运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\codex-sync\install-codex-sync.ps1 -InstallSkill
codex-sync status
codex-sync resume
```

`resume` 只在终端显示上下文。重开 Codex，在本仓库的新聊天中发送“请使用 `$codex-sync` 读取本项目四份长期上下文和最新检查点，核对 Git 状态及当前文档入口，先用中文概括项目现状与下一步”。未识别 Skill 时直接读取 `.codex-sync/` 文件，或把 `resume` 输出粘贴到聊天。

完成阶段任务后创建检查点、审阅改动；只有明确要求时才将代码与 `.codex-sync/` 一起提交推送。另一台电脑在工作区干净时拉取同一分支，并各自运行安装器。每个项目拥有独立 `project_id`，不要把其他项目的 `.codex-sync/` 复制进来。此流程同步开发上下文，不迁移应用数据库或原生 Codex 聊天。
