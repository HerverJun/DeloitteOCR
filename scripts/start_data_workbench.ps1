param(
    [string]$RunRoot = 'E:\project\platform-dev-runs\dev-20260925-001',
    [string]$DataDirectory = '',
    [int]$Port = 8766
)
$ErrorActionPreference = 'Stop'
$platformRoot = Split-Path $PSScriptRoot -Parent
$platformPython = Join-Path $RunRoot 'workspaces\platform-venv\Scripts\python.exe'
$dataPython = Join-Path $RunRoot 'workspaces\data-venv\Scripts\python.exe'
$portalDist = Join-Path $platformRoot 'apps\portal\dist'
foreach ($required in @($platformPython, $dataPython, (Join-Path $portalDist 'index.html'))) {
    if (!(Test-Path -LiteralPath $required)) { throw "缺少启动文件：$required。请先准备独立运行时并构建门户。" }
}
if (!$DataDirectory) { $DataDirectory = Join-Path $RunRoot 'workspaces\data-workbench-live' }
New-Item -ItemType Directory -Path $DataDirectory -Force | Out-Null
$env:WORKBENCH_DATA_DIR = (Resolve-Path -LiteralPath $DataDirectory).Path
$env:WORKBENCH_DATA_PYTHON = $dataPython
$env:WORKBENCH_PORTAL_DIST = $portalDist
$env:PYTHONPATH = (Join-Path $platformRoot 'src') + [IO.Path]::PathSeparator + $platformRoot
$env:PYTHONUTF8 = '1'
Set-Location -LiteralPath $platformRoot
Write-Host "数据工作台：http://127.0.0.1:$Port/apps/data"
Write-Host "数据目录：$($env:WORKBENCH_DATA_DIR)"
& $platformPython -B -m uvicorn workbench_platform.app:app --host 127.0.0.1 --port $Port
exit $LASTEXITCODE
