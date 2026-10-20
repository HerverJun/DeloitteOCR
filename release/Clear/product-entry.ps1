param(
    [ValidateSet('start','status','stop')][string]$Action = 'start',
    [string]$RegistrationFile = (Join-Path $PSScriptRoot 'current.json')
)
$ErrorActionPreference = 'Stop'
$env:PSModulePath = [IO.Path]::Combine($PSHOME, 'Modules') + [IO.Path]::PathSeparator +
    [IO.Path]::Combine($env:SystemRoot, 'System32\WindowsPowerShell\v1.0\Modules')
[Console]::OutputEncoding = [Text.Encoding]::UTF8
function Resolve-ProductPath([string]$Value) {
    $resolvedProductPath = [IO.Path]::GetFullPath($Value)
    if (-not $resolvedProductPath.StartsWith('F:\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "产品路径必须在本机 F 盘：$resolvedProductPath"
    }
    $productAncestor = $resolvedProductPath
    while ($productAncestor) {
        if ([IO.File]::Exists($productAncestor) -or [IO.Directory]::Exists($productAncestor)) {
            if (([IO.File]::GetAttributes($productAncestor) -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "产品入口不接受链接路径：$productAncestor"
            }
        }
        $productParent = [IO.Directory]::GetParent($productAncestor)
        $productAncestor = if ($productParent) { $productParent.FullName } else { $null }
    }
    return $resolvedProductPath
}
function Read-ProductJson([string]$Value) {
    return [IO.File]::ReadAllText((Resolve-ProductPath $Value), [Text.Encoding]::UTF8) | ConvertFrom-Json
}
function Assert-ProductHash([string]$Value, [string]$Expected) {
    $checkedProductFile = Resolve-ProductPath $Value
    if (-not [IO.File]::Exists($checkedProductFile)) {
        throw "登记文件缺失或内容改变：$checkedProductFile"
    }
    $productHasher = [Security.Cryptography.SHA256]::Create()
    $productHashStream = [IO.File]::OpenRead($checkedProductFile)
    try { $productActualHash = [BitConverter]::ToString($productHasher.ComputeHash($productHashStream)).Replace('-', '').ToLowerInvariant() }
    finally { $productHashStream.Dispose(); $productHasher.Dispose() }
    if ($productActualHash -ne $Expected) { throw "登记文件缺失或内容改变：$checkedProductFile" }
}
function Write-ProductJson([string]$Value, $Body) {
    $productTarget = Resolve-ProductPath $Value
    $productTemporary = Resolve-ProductPath ($productTarget + '.' + [guid]::NewGuid().ToString('N') + '.new')
    $productJsonBytes = [Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Depth 30) + [Environment]::NewLine)
    $productOutputStream = [IO.File]::Open($productTemporary, [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $productOutputStream.Write($productJsonBytes, 0, $productJsonBytes.Length); $productOutputStream.Flush($true) }
    finally { $productOutputStream.Dispose() }
    Move-Item -LiteralPath $productTemporary -Destination $productTarget -Force
}

$productScriptRoot = Resolve-ProductPath $PSScriptRoot
$env:PSModuleAnalysisCachePath = Resolve-ProductPath ([IO.Path]::Combine($productScriptRoot, 'runtime\cache\powershell-analysis'))
[IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($env:PSModuleAnalysisCachePath)) | Out-Null
$productRegistrationPath = Resolve-ProductPath $RegistrationFile
$productRuntimeRoot = Resolve-ProductPath (Join-Path (Split-Path $productRegistrationPath -Parent) 'runtime')
New-Item -ItemType Directory -Force -Path $productRuntimeRoot | Out-Null
$productLeasePath = Resolve-ProductPath (Join-Path $productRuntimeRoot 'entry.lock')
$productLease = $null
$productLeaseDeadline = [DateTime]::UtcNow.AddSeconds(80)
while (-not $productLease) {
    try {
        $productLease = [IO.File]::Open($productLeasePath, [IO.FileMode]::OpenOrCreate,
            [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    } catch [IO.IOException] {
        if ([DateTime]::UtcNow -gt $productLeaseDeadline) { throw '另一启动或停止操作仍在进行，请稍后重试' }
        Start-Sleep -Milliseconds 100
    }
}
try {
$productRegistration = Read-ProductJson $productRegistrationPath
if ($productRegistration.format -ne 'clear-current-product-v1') { throw '当前产品登记格式无效' }
$productAgentSetting = $productRegistration.PSObject.Properties['agent_enabled']
if ($productAgentSetting -and $productAgentSetting.Value -isnot [bool]) { throw 'Agent 启用登记必须是布尔值' }
$productAgentEnabled = $productAgentSetting -and $productAgentSetting.Value -eq $true
$productRoot = Resolve-ProductPath $productRegistration.candidate
$productData = Resolve-ProductPath $productRegistration.data
$productProgram = Resolve-ProductPath $productRegistration.shared_program
$productPython = Resolve-ProductPath (Join-Path $productProgram 'runtimes\platform\python.exe')
$productLauncher = Resolve-ProductPath (Join-Path $productRoot 'launch.py')
$productCompatibility = Resolve-ProductPath (Join-Path $productRoot 'platform\scripts\product_compatibility.py')
$productPort = [int]$productRegistration.port
if ($productPort -lt 1 -or $productPort -gt 65535) { throw '端口登记无效' }
Assert-ProductHash (Join-Path $productRoot 'candidate.json') $productRegistration.candidate_manifest_sha256
$productManifest = Read-ProductJson (Join-Path $productRoot 'candidate.json')
$productDeclaredProgram = if ([IO.Path]::IsPathRooted($productManifest.shared_program)) { Resolve-ProductPath $productManifest.shared_program } else { Resolve-ProductPath (Join-Path $productRoot $productManifest.shared_program) }
if ($productDeclaredProgram -ne $productProgram -or
    ($productManifest.data_compatibility | ConvertTo-Json -Depth 20 -Compress) -ne
    ($productRegistration.data_compatibility | ConvertTo-Json -Depth 20 -Compress)) {
    throw '候选资产或数据兼容声明与当前登记不一致'
}
foreach ($productFile in $productManifest.files.PSObject.Properties) {
    $productMember = Resolve-ProductPath (Join-Path $productRoot $productFile.Name)
    if (-not $productMember.StartsWith($productRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw '候选文件越出目录'
    }
    Assert-ProductHash $productMember ([string]$productFile.Value)
}
foreach ($productFile in $productManifest.shared_identity.PSObject.Properties) {
    $productSharedMember = if ([IO.Path]::IsPathRooted($productFile.Name)) { $productFile.Name } else { Join-Path $productRoot $productFile.Name }
    Assert-ProductHash $productSharedMember ([string]$productFile.Value)
}

foreach ($productFolder in @('temp','cache','appdata','localappdata','logs')) {
    New-Item -ItemType Directory -Force -Path (Resolve-ProductPath (Join-Path $productRuntimeRoot $productFolder)) | Out-Null
}
$env:TEMP = Join-Path $productRuntimeRoot 'temp'
$env:TMP = $env:TEMP
$env:TMPDIR = $env:TEMP
$env:APPDATA = Join-Path $productRuntimeRoot 'appdata'
$env:LOCALAPPDATA = Join-Path $productRuntimeRoot 'localappdata'
$env:XDG_CACHE_HOME = Join-Path $productRuntimeRoot 'cache'
$env:PIP_CACHE_DIR = Resolve-ProductPath (Join-Path $productRuntimeRoot 'cache\pip')
$env:npm_config_cache = Resolve-ProductPath (Join-Path $productRuntimeRoot 'cache\npm')
$env:HF_HOME = Resolve-ProductPath (Join-Path $productRuntimeRoot 'cache\huggingface')
$env:TORCH_HOME = Resolve-ProductPath (Join-Path $productRuntimeRoot 'cache\torch')
$env:HF_HUB_OFFLINE = '1'
$env:HF_HUB_DISABLE_TELEMETRY = '1'
$env:LANGSMITH_TRACING = 'false'
$env:LANGCHAIN_TRACING_V2 = 'false'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$productCompatibilityJson = & $productPython -I -B $productCompatibility --data $productData --candidate $productRoot
if ($LASTEXITCODE -ne 0) { throw '工作区结构或记录格式不兼容；未启动服务、未修改工作区' }
$productCheck = $productCompatibilityJson | ConvertFrom-Json

$productReceiptPath = Join-Path $productRuntimeRoot 'process.json'
$productReceipt = $null
$productProcess = $null
if (Test-Path -LiteralPath $productReceiptPath) {
    $productReceipt = Read-ProductJson $productReceiptPath
    $productProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$productReceipt.pid)"
    if ($productProcess) {
        $productBirth = $productProcess.CreationDate.ToUniversalTime().Ticks
        $productReceiptBirth = if ($productReceipt.created -is [DateTime] -or $productReceipt.created -is [DateTimeOffset]) {
            ([DateTimeOffset]$productReceipt.created).UtcDateTime.Ticks
        } else {
            [DateTimeOffset]::Parse([string]$productReceipt.created, [Globalization.CultureInfo]::InvariantCulture).UtcDateTime.Ticks
        }
        if ($productReceipt.format -ne 'clear-product-process-v1' -or
            $productReceipt.candidate -ne $productRoot -or $productReceipt.data -ne $productData -or
            [int]$productReceipt.port -ne $productPort -or $productReceiptBirth -ne $productBirth -or
            $productProcess.ExecutablePath -ne $productPython -or
            -not [regex]::IsMatch($productProcess.CommandLine, ('(?:^|\s)"?' + [regex]::Escape($productLauncher) + '"?(?:\s|$)')) -or
            -not [regex]::IsMatch($productProcess.CommandLine, ('(?:^|\s)"?' + [regex]::Escape($productData) + '"?(?:\s|$)')) -or
            -not [regex]::IsMatch($productProcess.CommandLine, ('(?:^|\s)--port\s+' + $productPort + '(?:\s|$)'))) {
            throw '进程身份不属于当前登记，未停止或替换任何进程'
        }
    }
}
$productListener = Get-NetTCPConnection -State Listen -LocalPort $productPort -ErrorAction SilentlyContinue
if ($productListener -and (-not $productProcess -or
    @($productListener | Where-Object OwningProcess -ne $productProcess.ProcessId).Count -gt 0)) {
    throw '端口被其它进程占用，未停止或替换任何进程'
}
if ($Action -eq 'status') {
    @{status=$(if ($productProcess) {'running'} else {'stopped'});url="http://127.0.0.1:$productPort/";
      pid=$(if ($productProcess) {$productProcess.ProcessId} else {$null});product_version=$productManifest.product_version;
      candidate=$productRoot;data=$productData;compatible=$productCheck.compatible;
      agent_requested=[bool]$productAgentEnabled;
      agent_process_enabled=$(if ($productProcess) { [bool]($productProcess.CommandLine -match '(?:^|\s)--enable-agent(?:\s|$)') } else { $false })} | ConvertTo-Json
    return
}
if ($Action -eq 'stop') {
    if (-not $productProcess) { @{status='already_stopped'} | ConvertTo-Json; return }
    $productActivityJson = & $productPython -I -B $productCompatibility --data $productData --active
    if ($LASTEXITCODE -ne 0) { throw '活动任务读取失败，未停止服务' }
    if (@(($productActivityJson | ConvertFrom-Json).active_work).Count -gt 0) {
        throw '还有活动 Agent 或原生任务，请先完成或在对应工具停止'
    }
    Stop-Process -Id $productProcess.ProcessId
    Wait-Process -Id $productProcess.ProcessId -Timeout 20 -ErrorAction SilentlyContinue
    Write-ProductJson (Join-Path $productRuntimeRoot ('logs\stop-' + [guid]::NewGuid().ToString('N') + '.json')) @{
        status='stopped';pid=$productProcess.ProcessId;candidate=$productRoot;data=$productData;at=[DateTime]::UtcNow.ToString('o')}
    @{status='stopped';pid=$productProcess.ProcessId} | ConvertTo-Json
    return
}
if ($productProcess) {
    $productRunningAgent = [bool]($productProcess.CommandLine -match '(?:^|\s)--enable-agent(?:\s|$)')
    if ($productRunningAgent -ne [bool]$productAgentEnabled) { throw 'Agent 启用设置已改变，请先停止本产品再重新启动' }
    @{status='already_running';url="http://127.0.0.1:$productPort/";pid=$productProcess.ProcessId;agent_requested=[bool]$productAgentEnabled} | ConvertTo-Json; return
}
$productLaunchId = [guid]::NewGuid().ToString('N')
$productAgentArguments = @()
if ($productAgentEnabled) { $productAgentArguments += '--enable-agent' }
$productDetached = & $productPython -I -B (Resolve-ProductPath (Join-Path $productScriptRoot 'product-launch.py')) `
    --launcher $productLauncher --data $productData --port $productPort `
    --stdout (Join-Path $productRuntimeRoot ("logs\start-$productLaunchId.stdout.log")) `
    --stderr (Join-Path $productRuntimeRoot ("logs\start-$productLaunchId.stderr.log")) @productAgentArguments
if ($LASTEXITCODE -ne 0) { throw '后台服务启动失败，查看本地日志' }
$productChild = Get-Process -Id ([int](($productDetached | ConvertFrom-Json).pid))
$productOwnedProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $($productChild.Id)"
if (-not $productOwnedProcess) { throw '产品进程启动后已退出，查看本地日志' }
Write-ProductJson $productReceiptPath @{format='clear-product-process-v1';pid=$productChild.Id;candidate=$productRoot;
    data=$productData;port=$productPort;created=$productOwnedProcess.CreationDate.ToUniversalTime().ToString('o');agent_enabled=[bool]$productAgentEnabled}
$productDeadline = [DateTime]::UtcNow.AddSeconds(60)
do {
    if ($productChild.HasExited) { throw '产品启动失败，查看本地日志；当前登记未改变' }
    try {
        $productHealth = Invoke-RestMethod -Uri "http://127.0.0.1:$productPort/api/platform/v1/agent/health" -TimeoutSec 2
        $productReady = $true
    } catch { $productReady = $false }
    if (-not $productReady) { Start-Sleep -Milliseconds 250 }
} until ($productReady -or [DateTime]::UtcNow -gt $productDeadline)
if (-not $productReady) { throw '启动尚未完成；可查询状态和本地日志，未重复启动' }
if ($productAgentEnabled -and -not $productHealth.enabled) { throw '平台已启动，但 Agent 初始化失败；请检查依赖及本地日志，未重复启动' }
$productReaderMarker = Join-Path $productData 'product-reader-requirements.json'
$productStartupFeatures = if ($null -ne $productManifest.startup_reader_features) { @($productManifest.startup_reader_features) } else { @($productManifest.data_compatibility.reader_features) }
if ([IO.File]::Exists($productReaderMarker)) { $productStartupFeatures += @((Read-ProductJson $productReaderMarker).reader_features) }
Write-ProductJson $productReaderMarker @{
    format='clear-product-reader-requirements-v1';reader_features=@($productStartupFeatures | Sort-Object -Unique);
    candidate_manifest_sha256=$productRegistration.candidate_manifest_sha256;at=[DateTime]::UtcNow.ToString('o')}
Write-ProductJson (Join-Path $productRuntimeRoot ("logs\start-$productLaunchId.json")) @{
    status='running';pid=$productChild.Id;candidate=$productRoot;data=$productData;compatibility=$productCheck;at=[DateTime]::UtcNow.ToString('o')}
@{status='running';url="http://127.0.0.1:$productPort/";pid=$productChild.Id;agent_enabled=[bool]$productHealth.enabled} | ConvertTo-Json
} finally {
    if ($productLease) { $productLease.Dispose() }
}
