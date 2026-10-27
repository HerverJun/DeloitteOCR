param(
    [string]$PackageRoot = (Join-Path $PSScriptRoot '../../..'),
    [Parameter(Mandatory=$true)][string]$DataRoot,
    [ValidateSet('single','multi')][string]$Mode = 'single',
    [string]$Listen = '127.0.0.1',
    [int]$PortalPort = 8889,
    [int]$WorkerPort = 8890,
    [int]$PostgresPort = 55439,
    [string]$AdminPasswordFile,
    [switch]$ReadOnlyProgram
)
$ErrorActionPreference = 'Stop'

function Assert-PhysicalLocal([string]$Value) {
    $absolute = [IO.Path]::GetFullPath($Value)
    if ($absolute -notmatch '^[A-Za-z]:\\' -or [IO.DriveInfo]::new([IO.Path]::GetPathRoot($absolute)).DriveType -ne 'Fixed') {
        throw "An ordinary fixed local disk is required: $absolute"
    }
    $current = $absolute
    while ($current) {
        if ((Test-Path -LiteralPath $current) -and (Get-Item -LiteralPath $current -Force).Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) {
            throw "A reparse point is not allowed: $current"
        }
        $parent = [IO.Path]::GetDirectoryName($current.TrimEnd('\'))
        if ($parent -eq $current) { break }
        $current = $parent
    }
    return $absolute
}

$PackageRoot = Assert-PhysicalLocal $PackageRoot
$DataRoot = Assert-PhysicalLocal $DataRoot
$launcher = Join-Path $PackageRoot 'Clear.cmd'
if (-not (Test-Path -LiteralPath $launcher)) { throw 'Run against a complete offline Clear directory' }
if (Test-Path -LiteralPath $DataRoot) { throw 'Acceptance requires new DATA; existing files are retained' }
$taskRoot = Assert-PhysicalLocal ($DataRoot + '.acceptance-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $taskRoot | Out-Null
foreach ($entry in @(
    @('TEMP','temp'),@('TMP','temp'),@('TMPDIR','temp'),@('APPDATA','cache\appdata'),
    @('LOCALAPPDATA','cache\localappdata'),@('XDG_CACHE_HOME','cache\xdg'),
    @('XDG_CONFIG_HOME','cache\config'),@('XDG_DATA_HOME','cache\data'),
    @('PIP_CACHE_DIR','cache\pip'),@('npm_config_cache','cache\npm'),
    @('HF_HOME','cache\huggingface'),@('TORCH_HOME','cache\torch'),
    @('CUDA_CACHE_PATH','cache\cuda'),@('NODE_COMPILE_CACHE','cache\node'),
    @('DSH_HOME','cache\dsh')
)) {
    $target = Join-Path $taskRoot $entry[1]
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    Set-Item -Path ('Env:' + $entry[0]) -Value $target
}
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PSModuleAnalysisCachePath=Join-Path $taskRoot 'cache\powershell-analysis'
$env:PYTHONUTF8='1'
$env:HF_HUB_OFFLINE='1'
$env:TRANSFORMERS_OFFLINE='1'
$records = [Collections.Generic.List[object]]::new()
$oldAcl = $null

function Invoke-Clear([string]$Label, [string[]]$Arguments) {
    $log = Join-Path $taskRoot ($Label + '.json.txt')
    & $launcher @Arguments 2>&1 | Tee-Object -FilePath $log | Out-Host
    $exitCode = $LASTEXITCODE
    $records.Add(@{ step=$Label; exit_code=$exitCode; output=$log })
    if ($exitCode -ne 0) { throw "Acceptance step $Label failed; inspect $log" }
}

$configuration = Join-Path $DataRoot 'clear.install.json'
$asciiAncestor = [IO.Path]::GetDirectoryName($taskRoot)
while (($asciiAncestor.ToCharArray() | Where-Object { [int]$_ -gt 127 }).Count -gt 0) {
    $asciiAncestor = [IO.Path]::GetDirectoryName($asciiAncestor.TrimEnd('\'))
}
$paddleTemporary = Assert-PhysicalLocal (Join-Path $asciiAncestor ('clear-acceptance-paddle-' + [Guid]::NewGuid().ToString('N')))
$passed = $false
$failure = $null
try {
    if ($ReadOnlyProgram) {
        $oldAcl = Get-Acl -LiteralPath $PackageRoot
        $oldAcl.Sddl | Set-Content -LiteralPath (Join-Path $taskRoot 'program-original.sddl')
        $restricted = Get-Acl -LiteralPath $PackageRoot
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent().User
        $deny = [Security.AccessControl.FileSystemAccessRule]::new($identity,
            [Security.AccessControl.FileSystemRights]'Write,Delete,DeleteSubdirectoriesAndFiles',
            [Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit',
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Deny)
        $restricted.AddAccessRule($deny)
        Set-Acl -LiteralPath $PackageRoot -AclObject $restricted
    }
    $initArguments = @('init','--mode',$Mode,'--data',$DataRoot,'--portal-port',"$PortalPort",
        '--worker-port',"$WorkerPort",'--postgres-port',"$PostgresPort",'--cache',(Join-Path $taskRoot 'cache'),
        '--temp',(Join-Path $taskRoot 'temp'),'--paddle-temp',$paddleTemporary)
    if ($Mode -eq 'multi') {
        if ($Listen -eq '127.0.0.1') { throw 'Specify an explicit server LAN address for multi acceptance' }
        $initArguments += @('--transport','lan-http','--listen',$Listen,'--public-origin',"http://${Listen}:$PortalPort")
        if ($AdminPasswordFile) { $initArguments += @('--admin-password-file',(Assert-PhysicalLocal $AdminPasswordFile)) }
    }
    Invoke-Clear '01-init' $initArguments
    foreach ($command in @('doctor','verify','qualify','start','status')) {
        Invoke-Clear $command @('--config',$configuration,$command)
    }
    if ($Mode -eq 'multi') { Invoke-Clear 'backup' @('--config',$configuration,'backup') }
    Invoke-Clear 'stop-all' @('--config',$configuration,'stop','--all')
    if ($Mode -eq 'single') { Invoke-Clear 'backup' @('--config',$configuration,'backup') }
    $passed = $true
} catch {
    $failure = $_.Exception.Message
} finally {
    if ($oldAcl) { Set-Acl -LiteralPath $PackageRoot -AclObject $oldAcl }
    $report = @{ format='clear-new-machine-acceptance-v1'; passed=$passed; failure=$failure;
        machine=$env:COMPUTERNAME; windows_account=[Security.Principal.WindowsIdentity]::GetCurrent().Name;
        os=[Environment]::OSVersion.VersionString; package=$PackageRoot; data=$DataRoot; mode=$Mode;
        readonly_program_requested=[bool]$ReadOnlyProgram; steps=$records;
        independent_machine='Compare this machine evidence to build host; not inferred';
        cross_drive=([IO.Path]::GetPathRoot($PackageRoot) -ne [IO.Path]::GetPathRoot($DataRoot));
        credential_cross_account='Requires a separate Windows-account execution';
        browser_permissions='Run two browser sessions after init; not implied by CLI success' }
    $report | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $taskRoot 'acceptance.json') -Encoding utf8
}
if (-not $passed) { throw $failure }
Write-Output "Acceptance evidence: $taskRoot"
