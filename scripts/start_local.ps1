# 文件职责：从当前项目虚拟环境启动示范学习，工具工作目录默认项目根，不安装依赖或修改系统配置。
param(
    [string]$Workspace = '',
    [ValidateRange(1024, 65535)][int]$Port = 8766,
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
$taskProjectRoot = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $taskProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    throw '缺少项目虚拟环境。请先按部署指南创建 .venv 并安装项目依赖。'
}
$taskUrl = "http://127.0.0.1:$Port"
$taskExisting = $null
try { $taskExisting = Invoke-RestMethod -Uri "$taskUrl/healthz" -TimeoutSec 1 }
catch { } # 尚未启动时继续；不停止或替换占用端口的其它程序。
if ($taskExisting -and $taskExisting.alive -and $taskExisting.mode -eq 'local') {
    if (-not $NoBrowser) { Start-Process -FilePath $taskUrl }
    Write-Output "示范学习已经运行：$taskUrl；文件与命令工具可在单独页面选择项目。"
    return
}
if (-not $Workspace) {
    $Workspace = $taskProjectRoot
}
$taskArgs = @('-m', 'autodidact.local_client', '--workspace', $Workspace, '--port', $Port)
if (-not $NoBrowser) { $taskArgs += '--open-browser' }
Push-Location -LiteralPath $taskProjectRoot
try {
    & $taskPython @taskArgs
    if ($LASTEXITCODE -ne 0) { throw "本机运行器退出，状态码：$LASTEXITCODE" }
}
finally { Pop-Location }
