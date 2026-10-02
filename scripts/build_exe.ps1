# 文件职责：使用项目 .venv 安装打包/PDF依赖并生成 EXE 文件夹；会重写构建输出，数据库与用户密钥另行配置。
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$projectPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) { throw '请先按部署手册创建项目 .venv' }
Push-Location $projectRoot
try {
    & $projectPython -m pip install -e '.[packaging,documents]'
    if ($LASTEXITCODE -ne 0) { throw '安装打包依赖失败' }
    & $projectPython -m PyInstaller --noconfirm 'scripts\autodidact.spec'
    if ($LASTEXITCODE -ne 0) { throw '打包失败，查看上方输出' }
    Write-Output '构建输出：dist\Autodidact\Autodidact.exe。需分发整个文件夹，数据库与模型另行配置。'
} finally {
    Pop-Location
}
