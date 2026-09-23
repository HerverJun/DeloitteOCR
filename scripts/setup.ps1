param(
    [string]$Python = 'python',
    [string]$CacheDir = (Join-Path $env:LOCALAPPDATA 'PrismImagePlatform\cache'),
    [string]$IndexUrl = 'https://pypi.org/simple',
    [string]$TorchWheelBase = 'https://download.pytorch.org/whl/cu128'
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
function Invoke-Checked {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Executable 执行失败，退出代码 $LASTEXITCODE" }
}

Push-Location $projectRoot
try {
    $pythonCommand = (Get-Command $Python -ErrorAction Stop).Source
    $pnpmCommand = (Get-Command 'pnpm.cmd' -ErrorAction Stop).Source
    $env:PYTHONUTF8 = '1'
    Invoke-Checked $pythonCommand @('-c', 'import sys,platform; assert sys.version_info[:2]==(3,12) and platform.system()=="Windows" and platform.machine().lower() in ("amd64","x86_64"), "Requires Windows x64 Python 3.12"')
    Invoke-Checked 'node' @('-e', 'const [a,b]=process.versions.node.split(".").map(Number); if(!((a===20&&b>=19)||(a===22&&b>=12)||a>22))throw Error("Vite requires Node 20.19+ or 22.12+")')
    $requiredPnpm = ((Get-Content -LiteralPath (Join-Path $projectRoot 'apps\web\package.json') -Raw | ConvertFrom-Json).packageManager -split '@')[-1]
    $actualPnpm = (& $pnpmCommand --version).Trim()
    if ($LASTEXITCODE -ne 0 -or $actualPnpm -ne $requiredPnpm) { throw "需要 pnpm $requiredPnpm，当前为 $actualPnpm" }
    $CacheDir = [System.IO.Path]::GetFullPath($CacheDir)
    foreach ($child in @('pip','tmp','wheels','pnpm')) { New-Item -ItemType Directory -Force -Path (Join-Path $CacheDir $child) | Out-Null }
    $env:PIP_CACHE_DIR = Join-Path $CacheDir 'pip'
    $env:TEMP = Join-Path $CacheDir 'tmp'
    $env:TMP = $env:TEMP
    $pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $pythonExe)) { Invoke-Checked $pythonCommand @('-m','venv','.venv') }
    Invoke-Checked $pythonExe @('-c', 'import sys; assert sys.version_info[:2]==(3,12), "Existing .venv must use Python 3.12"')

    $wheels = @(
        @{ Name='torch-2.9.1+cu128-cp312-cp312-win_amd64.whl'; Size='2862033275'; Sha='3a01f0b64c10a82d444d9fd06b3e8c567b1158b76b2764b8f51bfd8f535064b0' },
        @{ Name='torchvision-0.24.1+cu128-cp312-cp312-win_amd64.whl'; Size='9630268'; Sha='33ecea57afa1daeedfed443a8a0cb8e4b0b403fdf18c2a328ba6f9069d403384' }
    )
    foreach ($wheel in $wheels) {
        $wheelPath = Join-Path (Join-Path $CacheDir 'wheels') $wheel.Name
        $url = $TorchWheelBase.TrimEnd('/') + '/' + [Uri]::EscapeDataString($wheel.Name)
        Invoke-Checked $pythonExe @('scripts/download_wheel.py', $url, $wheelPath, '--size', $wheel.Size, '--sha256', $wheel.Sha)
        Invoke-Checked $pythonExe @('-m','pip','install','--no-deps', $wheelPath)
    }
    Invoke-Checked $pythonExe @('-m','pip','install','--index-url',$IndexUrl,'-r','requirements-windows-cu128.lock.txt')
    Invoke-Checked $pythonExe @('-m','pip','install','--no-deps','--no-build-isolation','-e','.')
    Invoke-Checked $pythonExe @('-m','pip','check')
    Push-Location (Join-Path $projectRoot 'apps\web')
    try {
        Invoke-Checked $pnpmCommand @('install','--frozen-lockfile','--store-dir',(Join-Path $CacheDir 'pnpm'))
        Invoke-Checked $pnpmCommand @('build')
    } finally { Pop-Location }
    Invoke-Checked $pythonExe @('-m','image_platform.cli','doctor')
    Write-Host '安装完成。运行 .\scripts\start.ps1，然后打开 http://127.0.0.1:8765 。'
} finally { Pop-Location }
