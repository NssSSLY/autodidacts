param([switch]$InstallSkill, [switch]$NoPathUpdate)

$ErrorActionPreference = 'Stop'

$sourceScript = Join-Path $PSScriptRoot 'codex-sync.ps1'
if (-not (Test-Path -LiteralPath $sourceScript)) {
    throw "缺少命令脚本：$sourceScript"
}
$sourcePackage = Join-Path $PSScriptRoot 'src'
$sourceTemplates = Join-Path $PSScriptRoot 'templates'
if (-not (Test-Path -LiteralPath (Join-Path $sourcePackage 'codex_sync\cli.py')) -or
    -not (Test-Path -LiteralPath (Join-Path $sourceTemplates 'PROJECT_CONTEXT.md'))) {
    throw '缺少项目上下文同步程序或模板。'
}
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw 'codex-sync V2 需要 Python 3.10 或更新版本。'
}
& python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'
if ($LASTEXITCODE -ne 0) { throw 'codex-sync V2 需要 Python 3.10 或更新版本。' }

$localAppDataPath = $env:LOCALAPPDATA
if (-not $localAppDataPath) {
    $localAppDataPath = Join-Path $env:USERPROFILE 'AppData\Local'
}
$binPath = Join-Path $localAppDataPath 'CodexSync\bin'
New-Item -ItemType Directory -Force -Path $binPath | Out-Null
Copy-Item -LiteralPath $sourceScript -Destination (Join-Path $binPath 'codex-sync.ps1') -Force
Copy-Item -LiteralPath $sourcePackage -Destination $binPath -Recurse -Force
Copy-Item -LiteralPath $sourceTemplates -Destination $binPath -Recurse -Force
$oldNativeHelper = Join-Path $binPath 'codex-native-snapshot.ps1'
if (Test-Path -LiteralPath $oldNativeHelper) {
    Remove-Item -LiteralPath $oldNativeHelper -Force
}
if ($InstallSkill) {
    $skillSource = Join-Path $PSScriptRoot 'skills\codex-sync\SKILL.md'
    if (-not (Test-Path -LiteralPath $skillSource)) { throw "缺少 Skill 文件：$skillSource" }
    $codexDirectory = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
    $skillTarget = Join-Path $codexDirectory 'skills\codex-sync'
    New-Item -ItemType Directory -Force -Path $skillTarget | Out-Null
    Copy-Item -LiteralPath $skillSource -Destination (Join-Path $skillTarget 'SKILL.md') -Force
    Write-Output "已安装 codex-sync Skill：$skillTarget"
}

$launcher = '@echo off' + "`r`n" + 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0codex-sync.ps1" %*' + "`r`n"
Set-Content -LiteralPath (Join-Path $binPath 'codex-sync.cmd') -Encoding ASCII -Value $launcher

if (-not $NoPathUpdate) {
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $pathItems = @($userPath -split ';' | Where-Object { $_ })
    if ($pathItems -notcontains $binPath) {
        [Environment]::SetEnvironmentVariable('Path', (($pathItems + $binPath) -join ';'), 'User')
    }
    if (@($env:Path -split ';') -notcontains $binPath) {
        $env:Path += ";$binPath"
    }
}
Write-Output "已安装 codex-sync 命令：$binPath"
Write-Output '请重新打开终端，再运行：codex-sync --help'
