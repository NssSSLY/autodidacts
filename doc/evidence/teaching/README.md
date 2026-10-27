# 示范学习功能验收（2026-10-25）

验证的是“人工示范/反馈能保存并进入后续同类任务的实际模型上下文”，不是模型权重训练或真实任务增益。使用合成任务、替身模型和独立SQLite，真实模型调用/研究PostgreSQL连接均为0。

本轮全量443通过/16跳过，新增20项教学回归；13个真实数据库、2个原研究HTTP和1个原研究浏览器测试仍跳过。脚本显式隔离真实.env与业务网络，只放行合成项目所需的6次Python子进程、1个回环监听；拦截事件0。Ruff/compileall/PowerShell语法检查通过。机器可读计数、边界及源码SHA256见[validation.json](validation.json)。

2026-10-26交付核对：14个相关源文件与本次验收时的工作区字节摘要完全一致，本轮推送未重新运行测试。`source_sha256`保留2026-10-25 Windows原始字节；另补`source_git_lf_sha256`对应Git暂存区的LF规范化字节。只有teaching_page.py的CRLF被Git规范化，逐字节确认除换行外相同，相关Python语法检查通过；不修改旧测试结果或原始摘要。

首轮定向45通过/2失败，定位为无当前对话ID时误排手工示范；修复后47通过，再补3项额度/版本/URL保护回归，最终全量443通过。原失败XML和随后结果均保留在忽略目录，不把失败抹为成功。累计日志见[05第51–52节](../../05开发过程与增量记录.md#51-2026-10-25--20261025-teaching按人工示范学习具体任务)。

实际Microsoft Edge无头点击教学页：

1. 创建“项目周报”，保存输入/理想输出；未配置模型也能存，调用数0。
2. Mock发送引导连接模型，正式应用不输出替身答案。
3. 仅隔离验收应用注入替身，发送新材料，核对实际收到的示范。
4. 反馈要求分标题且不漏问题，生成第2版，旧答案保留；刷新不丢反馈草稿。
5. 认可修订答案后保存为示范，下一条新材料的实际提示包含该示范和明确保存的要求。
6. 停用原示范/要求，下一次模型上下文不再选择它们；刷新后历史仍在，HTML作为文字显示。

流程替身调用4次，页面错误0、外部页面请求0。原文件工具页面也重跑了确认写入、真实语法检查、刷新历史与人工关闭。新流程原始JSON/截图在`build/demonstration_20261025/teaching-ui-result.json`、`teaching-workbench.png`，旧工具在`build/local-client-ui/`；产物不提交Git，相关脚本可复跑。

```powershell
.venv\Scripts\python.exe -u scripts\validate_local.py --local-runtime -q tests --tb=short --junitxml=build/demonstration_20261025/full.xml
.venv\Scripts\python.exe -u scripts\check_teaching_ui.py --browser-path 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
.venv\Scripts\python.exe -u scripts\check_local_ui.py --browser-path 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
```

本机8766实际服务已从默认启动脚本启动，首页教学和/tools均可读，未带Token的教学API被拒绝。启动前备份个人SQLite，启动后旧两条工具任务正文SHA256一致；个人仍Mock/课程0/模型调用0，没有用测试数据覆盖用户记录。

真实模型尚未配置，不能判断输出是否足够准确、是否会遵守示范、示范相对无示范是否提高成绩，亦未验证费用、语义泛化或真实保持。下一步需要一种具体任务、少量人工示范和独立新材料，以人工标准比较全部输出。研究事实仍沿原证据/争议门控，认可任务答案不会自动晋升信念。
