param([ValidateRange(1, 65535)][int]$Port = 8765)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { throw '请先运行 scripts/setup.ps1 创建环境。' }
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'apps\web\dist\index.html'))) { throw '请先在 apps/web 运行 pnpm build。' }
$env:PYTHONPATH = Join-Path $projectRoot 'src'
$env:PYTHONUTF8 = '1'
$listeners = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
if ($listeners | Where-Object { $_.Port -eq $Port }) { throw "端口 $Port 已被占用。若平台已启动，请直接打开网页；也可使用 -Port 指定其他端口。" }
$dataRoot = if ($env:ICP_DATA_DIR) { [System.IO.Path]::GetFullPath($env:ICP_DATA_DIR) } else { Join-Path $projectRoot '.local-data' }
$logRoot = Join-Path $dataRoot 'logs'
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
$workerProcess = Start-Process -FilePath $pythonExe -ArgumentList '-m','image_platform.cli','worker' -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logRoot 'worker.stdout.log') -RedirectStandardError (Join-Path $logRoot 'worker.stderr.log')
try {
    Write-Host "图像分类工作台：http://127.0.0.1:$Port"
    Write-Host '按 Ctrl+C 停止当前启动的 API 与工作进程。'
    & $pythonExe -m image_platform.cli serve --port $Port
    if ($LASTEXITCODE -ne 0) { throw "API 已退出，代码 $LASTEXITCODE" }
} finally {
    if (-not $workerProcess.HasExited) {
        # Windows venv launchers spawn a child interpreter; stop the owned tree.
        & taskkill.exe /PID $workerProcess.Id /T /F | Out-Null
    }
}
