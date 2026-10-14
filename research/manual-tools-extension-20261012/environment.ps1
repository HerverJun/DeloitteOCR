$ErrorActionPreference = 'Stop'
$extensionResearchRoot = 'F:\OCR\research\manual-tools-extension-20261012'
foreach ($extensionResearchFolder in @('','sources','evidence','temp','cache','appdata','localappdata','logs')) {
    $extensionResearchPath = Join-Path $extensionResearchRoot $extensionResearchFolder
    $extensionResearchCheck = $extensionResearchPath
    while ($extensionResearchCheck) {
        $extensionResearchItem = Get-Item -LiteralPath $extensionResearchCheck -Force
        if ($extensionResearchItem.FullName -notmatch '^F:\\' -or ($extensionResearchItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Unsafe research path: $extensionResearchCheck" }
        $extensionResearchCheck = Split-Path -Parent $extensionResearchCheck
    }
}
$env:TEMP = "$extensionResearchRoot\temp"
$env:TMP = $env:TEMP
$env:TMPDIR = $env:TEMP
$env:APPDATA = "$extensionResearchRoot\appdata"
$env:LOCALAPPDATA = "$extensionResearchRoot\localappdata"
$env:XDG_CACHE_HOME = "$extensionResearchRoot\cache"
$env:XDG_CONFIG_HOME = "$extensionResearchRoot\cache\config"
$env:XDG_DATA_HOME = "$extensionResearchRoot\cache\data"
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:GIT_OPTIONAL_LOCKS = '0'
$env:PIP_CACHE_DIR = "$extensionResearchRoot\cache\pip"
$env:npm_config_cache = "$extensionResearchRoot\cache\npm"
$env:HF_HOME = "$extensionResearchRoot\cache\huggingface"
$env:TORCH_HOME = "$extensionResearchRoot\cache\torch"
$env:MPLCONFIGDIR = "$extensionResearchRoot\cache\matplotlib"
$env:HF_HUB_DISABLE_TELEMETRY = '1'
