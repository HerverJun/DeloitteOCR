param(
    [string]$DevelopmentRoot = (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent),
    [string]$Data = 'F:\OCR\output\audit-platform-dev-20261002\workspace\M2-business-preview',
    [int]$Port = 8879
)
$ErrorActionPreference = 'Stop'
$taskDevRoot = [IO.Path]::GetFullPath($DevelopmentRoot)
if (-not $taskDevRoot.StartsWith('F:\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Development files must remain on the local F drive'
}
$taskTemp = Join-Path $taskDevRoot 'temp'
New-Item -ItemType Directory -Force -Path $taskTemp | Out-Null
$env:TEMP = $taskTemp
$env:TMP = $taskTemp
$env:TMPDIR = $taskTemp
$env:CLEAR_PADDLE_TEMP = $taskTemp
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
Set-Location -LiteralPath $taskDevRoot
& (Join-Path $taskDevRoot 'program\runtimes\platform\python.exe') -I -B -u `
    (Join-Path $taskDevRoot 'worktrees\platform\scripts\local_development.py') `
    --development-root $taskDevRoot --data $Data --port $Port
if ($LASTEXITCODE -ne 0) { throw "Development server exited with code $LASTEXITCODE" }
