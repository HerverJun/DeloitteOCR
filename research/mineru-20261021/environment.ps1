$ErrorActionPreference = 'Stop'
$mineruResearchRoot = 'F:\OCR\research\mineru-20261021'
$mineruResearchFolders = @('temp', 'cache', 'cache\localappdata', 'cache\appdata')
foreach ($folder in $mineruResearchFolders) {
    New-Item -ItemType Directory -Force -Path (Join-Path $mineruResearchRoot $folder) | Out-Null
}
$env:TEMP = Join-Path $mineruResearchRoot 'temp'
$env:TMP = $env:TEMP
$env:LOCALAPPDATA = Join-Path $mineruResearchRoot 'cache\localappdata'
$env:APPDATA = Join-Path $mineruResearchRoot 'cache\appdata'
$env:PYTHONDONTWRITEBYTECODE = '1'
# This session only runs pure local probes. No installer, downloader or model is invoked.
