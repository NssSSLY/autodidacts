# 本机任务版本验证（2026-10-25）

基线为`871d0603f3144f8c28b8752a802a6da423afae40`。本轮源码尚未提交；摘要和实际结果见[validation.json](validation.json)。不使用历史测试数量代替本轮验收。

## 已运行

- 修改前离线基线387通过/16跳过。
- 修改后全量423通过/16跳过，439项；包含30个本机协议/文件/任务用例及6个显式真实本机执行用例。
- 真实子进程仅操作合成项目：Python语法正常/错误、pytest失败/大输出/环境过滤、超时/人工取消及后代清理、替身模型规划→确认写入→真实pytest→人工审阅。
- 真实回环HTTP验证Token拒绝及创建/预览/确认写入；其它Host/Origin/JSON/文本展示规则有内存HTTP用例。
- 独立Edge无头浏览器验证页面建任务、预览前未写入、确认后文件落盘、真实Python检查、刷新重载历史、恶意HTML不执行、人工关闭。页面错误0、外部页面请求0、真实模型调用0、研究数据库连接0。
- Ruff、Python编译、Git空白检查和PowerShell脚本语法检查。

## 复跑

```powershell
.\.venv\Scripts\python.exe -u scripts/validate_local.py --local-runtime -q tests --tb=short --junitxml=build/local_agent_20261025/full.xml
.\.venv\Scripts\python.exe -u scripts/check_local_ui.py --browser-path 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
```

`--local-runtime`仅允许合成临时项目中的当前Python可执行文件及回环连接，禁业务网络/DNS/其它子进程，并禁读真实.env。默认不加此开关时，真实本机执行用例跳过。浏览器脚本使用新建项目和浏览器配置，拦截非本轮回环地址的页面请求。原始XML在`build/local_agent_20261025/`；UI JSON和截图在`build/local-client-ui/`，均为本机忽略产物。

16个跳过仍是13个PostgreSQL、2个原研究工作台HTTP、1个原研究浏览器用例，不能当作通过。本轮没有真实模型规划质量/收费和usage准确性、真实学习库迁移/并发、Linux进程组、原生桌面窗口、打包/签名/升级或恶意项目系统级隔离验收。
