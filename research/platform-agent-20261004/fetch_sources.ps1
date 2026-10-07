param([ValidateSet('langchain','langgraph','alternatives','repositories','pinned')][string]$Group)
$ErrorActionPreference = 'Stop'
function Resolve-ResearchPath([string]$Value) {
    $full = [IO.Path]::GetFullPath($Value)
    if (-not $full.StartsWith('F:\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Non-F output' }
    $parent = $full
    while ($parent) {
        if (Test-Path -LiteralPath $parent) {
            $item = Get-Item -Force -LiteralPath $parent
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Linked output ancestor: $parent" }
        }
        $parent = [IO.Path]::GetDirectoryName($parent)
    }
    return $full
}
$researchRoot = Resolve-ResearchPath 'F:\OCR\research\platform-agent-20261004'
$taskTemp = Resolve-ResearchPath 'F:\OCR\temp\platform-agent-plan-20261004'
$taskCache = Resolve-ResearchPath 'F:\OCR\.cache\platform-agent-plan-20261004'
foreach ($path in @($researchRoot, $taskTemp, $taskCache)) {
    New-Item -ItemType Directory -Force -Path $path | Out-Null
}
$env:TEMP = $taskTemp
$env:TMP = $taskTemp
$env:APPDATA = Resolve-ResearchPath (Join-Path $taskCache 'appdata')
$env:LOCALAPPDATA = Resolve-ResearchPath (Join-Path $taskCache 'localappdata')
$env:PSModuleAnalysisCachePath = Resolve-ResearchPath (Join-Path $taskCache 'powershell-analysis')
New-Item -ItemType Directory -Force -Path $env:APPDATA,$env:LOCALAPPDATA,(Resolve-ResearchPath (Join-Path $researchRoot 'snapshots')) | Out-Null
$sources = switch ($Group) {
    'langchain' { @(
        @('langchain-overview', 'https://docs.langchain.com/oss/python/langchain/overview.md'),
        @('langchain-agents', 'https://docs.langchain.com/oss/python/langchain/agents.md'),
        @('langchain-tools', 'https://docs.langchain.com/oss/python/langchain/tools.md'),
        @('langchain-middleware', 'https://docs.langchain.com/oss/python/langchain/middleware/custom.md')
    ) }
    'langgraph' { @(
        @('langgraph-overview', 'https://docs.langchain.com/oss/python/langgraph/overview.md'),
        @('langgraph-workflows-agents', 'https://docs.langchain.com/oss/python/langgraph/workflows-agents.md'),
        @('langgraph-durable-execution', 'https://docs.langchain.com/oss/python/langgraph/durable-execution.md'),
        @('langgraph-interrupts', 'https://docs.langchain.com/oss/python/langgraph/interrupts.md')
    ) }
    'alternatives' { @(
        @('deepagents-overview', 'https://docs.langchain.com/oss/python/deepagents/overview.md'),
        @('langchain-sql-agent', 'https://docs.langchain.com/oss/python/langchain/sql-agent.md'),
        @('pydantic-ai-tools', 'https://ai.pydantic.dev/tools/')
    ) }
    'repositories' { @(
        @('langchain-repository', 'https://api.github.com/repos/langchain-ai/langchain'),
        @('langgraph-repository', 'https://api.github.com/repos/langchain-ai/langgraph'),
        @('deepagents-repository', 'https://api.github.com/repos/langchain-ai/deepagents'),
        @('pydantic-ai-repository', 'https://api.github.com/repos/pydantic/pydantic-ai')
    ) }
    'pinned' { @(
        @('langgraph-checkpointers', 'https://docs.langchain.com/oss/python/langgraph/checkpointers.md'),
        @('pydantic-ai-durable', 'https://ai.pydantic.dev/durable_execution/')
    ) }
}
$records = @()
foreach ($source in $sources) {
    $id,$url = $source
    $extension = if ($Group -eq 'repositories') { '.json' } elseif ($id.StartsWith('pydantic-ai-')) { '.html' } else { '.md' }
    $relative = 'snapshots/' + $id + $extension
    $destination = Resolve-ResearchPath (Join-Path $researchRoot $relative)
    try {
        $response = Invoke-WebRequest -Uri $url -Headers @{'User-Agent'='ClearPlatform-Agent-Planning';'Accept'='application/vnd.github+json, text/plain, text/html'} -TimeoutSec 35
        [IO.File]::WriteAllText($destination, [string]$response.Content, [Text.UTF8Encoding]::new($false))
        $records += [ordered]@{id=$id;url=$url;retrieved_utc=[DateTime]::UtcNow.ToString('o');status=[int]$response.StatusCode;path=$relative;sha256=(Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant();bytes=(Get-Item -LiteralPath $destination).Length}
        Write-Output "$id : saved $((Get-Item -LiteralPath $destination).Length) bytes"
    } catch {
        $records += [ordered]@{id=$id;url=$url;retrieved_utc=[DateTime]::UtcNow.ToString('o');status='failed';error=$_.Exception.Message}
        Write-Output "$id : failed $($_.Exception.Message)"
    }
}
if ($Group -eq 'pinned') {
    foreach ($entry in @(
        @('langchain', 'langchain-ai/langchain', 'master', 'libs/langchain_v1/langchain/agents/factory.py'),
        @('langgraph', 'langchain-ai/langgraph', 'main', 'libs/prebuilt/langgraph/prebuilt/tool_node.py'),
        @('deepagents', 'langchain-ai/deepagents', 'main', 'libs/deepagents/deepagents/graph.py')
    )) {
        $id,$repo,$branch,$sourcePath = $entry
        try {
            $commitUrl = "https://api.github.com/repos/$repo/commits/$branch"
            $commit = Invoke-RestMethod -Uri $commitUrl -Headers @{'User-Agent'='ClearPlatform-Agent-Planning'} -TimeoutSec 35
            $sha = $commit.sha
            $url = "https://raw.githubusercontent.com/$repo/$sha/$sourcePath"
            $response = Invoke-WebRequest -Uri $url -TimeoutSec 35
            $relative = "snapshots/$id-pinned-source.py"
            $destination = Resolve-ResearchPath (Join-Path $researchRoot $relative)
            [IO.File]::WriteAllText($destination, [string]$response.Content, [Text.UTF8Encoding]::new($false))
            $records += [ordered]@{id="$id-pinned-source";url=$url;commit_url=$commitUrl;commit=$sha;source_path=$sourcePath;retrieved_utc=[DateTime]::UtcNow.ToString('o');status=200;path=$relative;sha256=(Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant();bytes=(Get-Item -LiteralPath $destination).Length}
            Write-Output "$id : pinned $sha"
        } catch {
            $records += [ordered]@{id="$id-pinned-source";status='failed';error=$_.Exception.Message}
            Write-Output "$id : failed $($_.Exception.Message)"
        }
    }
    foreach ($package in @('langchain','langchain-core','langgraph','langgraph-checkpoint-sqlite','langchain-openai','langchain-anthropic')) {
        try {
            $latest = Invoke-RestMethod -Uri "https://pypi.org/pypi/$package/json" -TimeoutSec 35
            $version = $latest.info.version
            $url = "https://pypi.org/pypi/$package/$version/json"
            $response = Invoke-WebRequest -Uri $url -TimeoutSec 35
            $relative = "snapshots/$package-pypi.json"
            $destination = Resolve-ResearchPath (Join-Path $researchRoot $relative)
            [IO.File]::WriteAllText($destination, [string]$response.Content, [Text.UTF8Encoding]::new($false))
            $records += [ordered]@{id="$package-pypi";url=$url;version=$version;retrieved_utc=[DateTime]::UtcNow.ToString('o');status=200;path=$relative;sha256=(Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant();bytes=(Get-Item -LiteralPath $destination).Length}
            Write-Output "$package : $version"
        } catch {
            $records += [ordered]@{id="$package-pypi";status='failed';error=$_.Exception.Message}
            Write-Output "$package : failed $($_.Exception.Message)"
        }
    }
}
[IO.File]::WriteAllText((Resolve-ResearchPath (Join-Path $researchRoot "sources-$Group.json")), (ConvertTo-Json -InputObject $records -Depth 8), [Text.UTF8Encoding]::new($false))
