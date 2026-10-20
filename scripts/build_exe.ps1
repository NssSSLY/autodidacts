# 文件职责：从干净Git代码生成全新版本目录和文件/依赖清单；UTF-8 BOM兼容Windows PowerShell5.1中文，不覆盖旧发布。
param([switch]$InstallDependencies, [string]$RequirementsLock = '')
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$projectPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) { throw '请先按部署手册创建项目 .venv' }
Push-Location $projectRoot
try {
    git diff --quiet
    if ($LASTEXITCODE -ne 0) { throw '先提交或处理已有跟踪文件修改，不从脏代码生成发布' }
    git diff --cached --quiet
    if ($LASTEXITCODE -ne 0) { throw '存在未提交暂存修改' }
    $f08Untracked = git ls-files --others --exclude-standard -- src migrations scripts config pyproject.toml alembic.ini .env.example data/benchmark
    if ($f08Untracked) { throw '构建输入存在未跟踪文件，请先核对并提交；不自动纳入' }
    $f08Revision = git rev-parse HEAD
    if ($LASTEXITCODE -ne 0 -or $f08Revision -notmatch '^[0-9a-f]{40}$') { throw '无法读取Git提交' }
    if ($InstallDependencies) {
        if ($RequirementsLock) {
            & $projectPython -m pip install -r $RequirementsLock
        } else {
            & $projectPython -m pip install -e '.[packaging,documents]'
        }
        if ($LASTEXITCODE -ne 0) { throw '安装打包依赖失败' }
    }
    & $projectPython -m PyInstaller --version
    if ($LASTEXITCODE -ne 0) { throw '缺PyInstaller，请先显式-InstallDependencies或按锁定清单准备环境' }
    $f08Base = [IO.Path]::GetFullPath((Join-Path $projectRoot 'dist\releases'))
    $f08Id = (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmss') + '-' + $f08Revision.Substring(0,8) + '-' + [guid]::NewGuid().ToString('N').Substring(0,6)
    $f08Root = [IO.Path]::GetFullPath((Join-Path $f08Base $f08Id))
    if (-not $f08Root.StartsWith($f08Base + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw '输出路径越界' }
    if (Test-Path -LiteralPath $f08Root) { throw '发布目录已存在，不覆盖' }
    New-Item -ItemType Directory -Path $f08Root | Out-Null
    $f08Package = Join-Path $f08Root 'package'
    & $projectPython -m PyInstaller --distpath $f08Package --workpath (Join-Path $f08Root 'work') 'scripts\autodidact.spec'
    if ($LASTEXITCODE -ne 0) { throw '打包失败，查看上方输出' }
    & $projectPython -m autodidact.delivery $f08Package --source-revision $f08Revision
    if ($LASTEXITCODE -ne 0) { throw '发布清单生成/核验失败，不标为可分发' }
    Write-Output "构建输出：$f08Package。分发package整个目录，清单通过不代表干净机器/签名验收。"
} finally {
    Pop-Location
}
