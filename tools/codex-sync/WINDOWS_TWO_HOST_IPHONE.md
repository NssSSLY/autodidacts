# 两台 Windows 电脑与 iPhone 的使用步骤

## 电脑 A：记录并推送

在 Git 仓库根目录安装命令和 Skill。若已安装，也可重跑安装器更新：

```powershell
Set-Location 'C:\file\project\autodidact_v0_1'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\codex-sync\install-codex-sync.ps1 -InstallSkill
codex-sync status
```

完成一阶段任务后，创建检查点。把引号内的示例改为本次真实工作内容：

```powershell
codex-sync checkpoint --goal "本次目标" --summary "已经完成的工作和验证" --decision "已经确定的决定" --todo "下一步" --blocker "尚未解决的问题"
```

审阅代码与 `.codex-sync/` 的变动，再提交并推送。首次从 V1 迁移时，暂存 `.codex-sync/` 应使用[迁移说明](MIGRATION_V1_V2.md)中的 Windows 大小写命令。之后照常使用 Git。`checkpoint` 本身不会提交或推送。

## 电脑 B：首次安装并让 Codex 读取项目

先确保 A 的代码、V2 工具和 `.codex-sync/` 已经提交并推送。B 如果尚无仓库，先从同一个 Git 远端克隆；如果已有仓库且工作区干净，在仓库根目录执行：

```powershell
Set-Location 'E:\project\autodidact_v0_1'
git status --short
git pull --ff-only origin main
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\codex-sync\install-codex-sync.ps1 -InstallSkill
```

如果 `git status --short` 显示 B 有未提交的改动，先处理这些改动，再拉取；不要直接覆盖。安装要求 Git 和 Python 3.10 或更新版本。安装器会把命令装进当前 Windows 用户目录，并把仓库内的 Skill 复制到该电脑的 `$CODEX_HOME\skills\codex-sync`；未设置 `CODEX_HOME` 时使用 `%USERPROFILE%\.codex\skills\codex-sync`。**Git 拉取只取得 Skill 源文件，不会自动完成 B 的用户级安装，所以 B 也要运行带 `-InstallSkill` 的命令。**

如果 B 的 Codex 使用自定义 `CODEX_HOME`，先确认安装脚本运行时也能读到同一个环境变量；例如该电脑确实使用 `E:\Codex\home` 时，可先在同一终端设置 `$env:CODEX_HOME='E:\Codex\home'` 再运行安装器。不要只凭示例路径更改现有配置。

重新打开终端与 Codex，进入 B 的 Git 仓库。先验证：

```powershell
codex-sync status
codex-sync resume
```

`resume` 只在终端打印上下文，不会自行写入 Codex 聊天。新建 Codex 聊天后，可直接发送下面这句话来调用已安装的 Skill：

> 请使用 `$codex-sync` 读取当前仓库的 `.codex-sync/PROJECT_CONTEXT.md`、`DECISIONS.md`、`CURRENT_STATE.md`、`TODO.md` 和最新检查点，并核对 Git 状态与 `doc/README.md`。先用中文概括项目现状、已确定的决定及下一步，暂不修改代码。

如果 Codex 尚未识别新装的 Skill，就把 `codex-sync resume` 的输出粘贴到新聊天中，再要求它核对上述文件。后续每阶段仍由 B 创建检查点、审阅并提交推送；A 在工作区干净时运行 `codex-sync sync --pull` 获取更新。

## 原生聊天与手机

上述 Git 流程供**新聊天**恢复项目上下文。若要接着 A 上的**同一段原生聊天**工作，在两台电脑的 Codex 设置中连接主机，并分别把对应的 Git 根目录保存为项目；然后在聊天底部选择目标主机并使用 Handoff。功能入口可能因版本或开放范围而异，参见[官方连接及 Handoff 说明](https://learn.chatgpt.com/docs/remote-connections)。以下路径为示例，另一台电脑应使用自己的实际仓库路径。本机 A 的根目录是 `C:\file\project\autodidact_v0_1`，B 是 `E:\project\autodidact_v0_1`；A 上的外层目录 `C:\file\project` 不是同一个 Git 项目。

iPhone 可通过 ChatGPT 应用的 Codex/Remote 功能连接在线电脑。手机控制该电脑上的项目与聊天，不保存第二份本地仓库。

V2 不同步所有原生聊天或 Codex 用户配置。即使 `CODEX_HOME=E:\Codex\home`，该目录也不应作为双机直接同步目标。参见 [Codex 环境变量官方说明](https://learn.chatgpt.com/docs/config-file/environment-variables)。
