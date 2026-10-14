param(
    [Parameter(Mandatory=$true)][string]$Data,
    [string]$Output='F:\OCR\output\forensics-stability-20261014\evidence\portal-server-001',
    [switch]$Start
)
. 'F:\OCR\output\forensics-stability-20261014\environment.ps1'
$forensicsCandidate = 'F:\OCR\output\forensics-stability-20261014\delivery\Clear-0.11.1-forensics-candidate-001'
$forensicsRuntime = Join-Path $forensicsCandidate 'program\runtimes\platform\python.exe'
$forensicsHarness = Join-Path $PSScriptRoot 'serve_portal_candidate.py'
$forensicsDataPath = [IO.Path]::GetFullPath($Data)
$forensicsServerOutput = [IO.Path]::GetFullPath($Output)
foreach ($forensicsTarget in @($forensicsDataPath,$forensicsServerOutput,$forensicsRuntime,$forensicsHarness)) {
    if ($forensicsTarget -notmatch '^F:\\OCR\\output\\forensics-stability-20261014\\') { throw "Path escapes this F: task: $forensicsTarget" }
    for ($forensicsParent = $forensicsTarget; $forensicsParent; $forensicsParent = Split-Path $forensicsParent -Parent) {
        if ((Test-Path -LiteralPath $forensicsParent) -and ((Get-Item -Force -LiteralPath $forensicsParent).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Redirected server path: $forensicsParent" }
    }
}
& $forensicsRuntime -I -B $forensicsHarness --candidate $forensicsCandidate --data $forensicsDataPath --output $forensicsServerOutput --port 8894 --check-only
if ($LASTEXITCODE -ne 0) { throw 'Candidate server preflight failed' }
if (!$Start) { return }
if (Test-Path -LiteralPath $forensicsServerOutput) { throw 'Use a fresh server evidence directory' }
if (Get-NetTCPConnection -LocalPort 8894 -State Listen -ErrorAction SilentlyContinue) { throw 'Port 8894 is already in use; existing server is not touched' }
$forensicsLogName = Split-Path $forensicsServerOutput -Leaf
$forensicsStdout = Join-Path $forensicsRunRoot "logs\$forensicsLogName.stdout.log"
$forensicsStderr = Join-Path $forensicsRunRoot "logs\$forensicsLogName.stderr.log"
$forensicsArguments = @('-I','-B','-u',('"'+$forensicsHarness+'"'),'--candidate',('"'+$forensicsCandidate+'"'),
    '--data',('"'+$forensicsDataPath+'"'),'--output',('"'+$forensicsServerOutput+'"'),'--port','8894')
$forensicsServerProcess = Start-Process -FilePath $forensicsRuntime -ArgumentList $forensicsArguments -WorkingDirectory (Join-Path $forensicsCandidate 'platform') -WindowStyle Hidden -PassThru -RedirectStandardOutput $forensicsStdout -RedirectStandardError $forensicsStderr
$forensicsStarted = [ordered]@{
    pid=$forensicsServerProcess.Id; candidate=$forensicsCandidate; data=$forensicsDataPath; output=$forensicsServerOutput;
    base_url='http://127.0.0.1:8894'; stop_file=(Join-Path $forensicsServerOutput 'stop.request');
    stdout=$forensicsStdout; stderr=$forensicsStderr
}
$forensicsStarted | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $forensicsRunRoot "logs\$forensicsLogName.started.json") -Encoding UTF8
$forensicsStarted | ConvertTo-Json
