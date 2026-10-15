param([Parameter(Mandatory=$true)][string]$TaskRoot, [int]$Port=55439)
$ErrorActionPreference='Stop'
$TaskRoot=[IO.Path]::GetFullPath($TaskRoot)
if(-not $TaskRoot.StartsWith('F:\') -or (Get-Item -LiteralPath $TaskRoot).LinkType) { throw 'Use a physical F-drive directory' }
$env:TEMP=Join-Path $TaskRoot 'temp'; $env:TMP=$env:TEMP
$env:LOCALAPPDATA=Join-Path $TaskRoot 'cache\localappdata'; $env:APPDATA=Join-Path $TaskRoot 'cache\appdata'
$env:PGPASSFILE=Join-Path $TaskRoot 'runs\pgpass.conf'
$env:PGSERVICEFILE=Join-Path $TaskRoot 'runs\pg_service.conf'
$env:PSQL_HISTORY=Join-Path $TaskRoot 'runs\psql_history'
$bin=Join-Path $TaskRoot 'dependencies\postgresql\pgsql\bin'
$data=Join-Path $TaskRoot 'runs\postgres-data'
$config=Join-Path $TaskRoot 'runs\central-private.json'
if(Test-Path -LiteralPath $config) { throw 'Existing credentials found; reuse the registered cluster rather than overwrite' }
$bytes=New-Object byte[] 36
[Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
$password=[Convert]::ToBase64String($bytes).Replace('+','-').Replace('/','_').TrimEnd('=')
$pwfile=Join-Path $TaskRoot 'runs\postgres-password.txt'
[IO.File]::WriteAllText($pwfile,$password)
& "$bin\initdb.exe" -D $data -U clear_admin --pwfile=$pwfile --encoding=UTF8 --locale=C --data-checksums --auth-local=scram-sha-256 --auth-host=scram-sha-256
if($LASTEXITCODE -ne 0) {throw 'initdb failed'}
Add-Content -LiteralPath (Join-Path $data 'postgresql.conf') -Value "`nlisten_addresses = '127.0.0.1'`nport = $Port`nfsync = on`nsynchronous_commit = on`nfull_page_writes = on`nlog_directory = 'log'`nlogging_collector = on`nmax_wal_size = '512MB'`nshared_buffers = '128MB'"
& "$bin\pg_ctl.exe" start -D $data -l (Join-Path $TaskRoot 'runs\postgres-start.log') -w
if($LASTEXITCODE -ne 0) {throw 'PostgreSQL start failed'}
$env:PGPASSWORD=$password
& "$bin\createdb.exe" -h 127.0.0.1 -p $Port -U clear_admin clear_tasks
if($LASTEXITCODE -ne 0) {throw 'createdb failed'}
$workerToken=[Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(48)).Replace('+','-').Replace('/','_').TrimEnd('=')
@{database_url="postgresql+psycopg://clear_admin:${password}@127.0.0.1:$Port/clear_tasks";worker_token=$workerToken;postgres_version='17.11';postgres_port=$Port;data_directory=$data} | ConvertTo-Json | Set-Content -LiteralPath $config -Encoding utf8
$identity=[Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls.exe $config /inheritance:r /grant:r "${identity}:(F)" | Out-Null
& icacls.exe $pwfile /inheritance:r /grant:r "${identity}:(F)" | Out-Null
Remove-Item Env:\PGPASSWORD
Write-Output "PostgreSQL 17 ready on 127.0.0.1:$Port; credentials saved in the private F-drive configuration."
