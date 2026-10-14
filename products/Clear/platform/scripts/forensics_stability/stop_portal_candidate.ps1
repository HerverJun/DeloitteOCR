param([string]$ServerOutput='F:\OCR\output\forensics-stability-20261014\evidence\portal-server-001')
. 'F:\OCR\output\forensics-stability-20261014\environment.ps1'
$forensicsOutput = [IO.Path]::GetFullPath($ServerOutput)
if ($forensicsOutput -notmatch '^F:\\OCR\\output\\forensics-stability-20261014\\evidence\\') { throw 'Server output must be beneath this F: run evidence directory' }
for ($forensicsParent = $forensicsOutput; $forensicsParent; $forensicsParent = Split-Path $forensicsParent -Parent) {
    if ((Test-Path -LiteralPath $forensicsParent) -and ((Get-Item -Force -LiteralPath $forensicsParent).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Redirected shutdown path: $forensicsParent" }
}
$forensicsReceiptPath = Join-Path $forensicsOutput 'server-receipt.json'
$forensicsReceipt = Get-Content -LiteralPath $forensicsReceiptPath -Raw -Encoding UTF8 | ConvertFrom-Json
$forensicsPid = [int]$forensicsReceipt.pid
$forensicsOwned = Get-CimInstance Win32_Process -Filter "ProcessId=$forensicsPid"
if ($forensicsOwned) {
    if ($forensicsOwned.ExecutablePath -ne $forensicsReceipt.runtime -or $forensicsOwned.CommandLine -notlike '*serve_portal_candidate.py*') { throw 'Process identity changed; no shutdown signal was written' }
    Set-Content -LiteralPath (Join-Path $forensicsOutput 'stop.request') -Value 'graceful-shutdown' -Encoding UTF8
    $forensicsProcess = Get-Process -Id $forensicsPid
    if (!$forensicsProcess.WaitForExit(50000)) { throw 'Owned server did not close within 50 seconds; retain evidence and investigate' }
}
$forensicsClosed = Get-Content -LiteralPath $forensicsReceiptPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (!$forensicsClosed.server_closed) { throw 'Server process ended without a clean shutdown receipt' }
if (Get-NetTCPConnection -LocalPort 8894 -State Listen -ErrorAction SilentlyContinue) { throw 'Port 8894 is still listening after server shutdown' }
[ordered]@{server_closed=$forensicsClosed.server_closed; pid=$forensicsPid; port=8894; blocked_actions=$forensicsClosed.blocked_actions} | ConvertTo-Json -Depth 6
