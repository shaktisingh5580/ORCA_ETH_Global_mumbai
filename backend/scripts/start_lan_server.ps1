# Purpose: run the authorized ORCA demo API as a hidden server accessible to same-LAN clients.
# Behavior: bind port 8000, retain private logs/PID locally and optionally allow local-subnet traffic.
# Refs: FRONTEND_DASHBOARD_INTEGRATION_HANDOFF.md; Phase: demo frontend integration.
param([switch]$ConfigureFirewall)
$ErrorActionPreference = 'Stop'
$orcaRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$orcaPython = Join-Path $orcaRoot 'backend/.venv/Scripts/python.exe'
$orcaEnv = Join-Path $orcaRoot '.env.demo'
$orcaCache = Join-Path $orcaRoot '.cache/lan-server'
if (-not (Test-Path -LiteralPath $orcaEnv)) { throw 'Private .env.demo is required' }
New-Item -ItemType Directory -Path $orcaCache -Force | Out-Null
if ($ConfigureFirewall) {
    $orcaRule = Get-NetFirewallRule -Name 'ORCA-Demo-LAN-8000' -ErrorAction SilentlyContinue
    if (-not $orcaRule) {
        New-NetFirewallRule -Name 'ORCA-Demo-LAN-8000' -DisplayName 'ORCA demo API - local subnet TCP 8000' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8000 -RemoteAddress LocalSubnet -Profile Any -ErrorAction Stop | Out-Null
    }
}
$orcaListener = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($orcaListener) { throw 'Port 8000 already has a listener; stop the previous ORCA server before restarting' }
$orcaArgs = @('-m','uvicorn','orca.bootstrap:create_application','--factory','--env-file',('"' + $orcaEnv + '"'),'--host','0.0.0.0','--port','8000')
$env:OPENBLAS_NUM_THREADS='1'
$env:OMP_NUM_THREADS='1'
$orcaProcess = Start-Process -FilePath $orcaPython -ArgumentList $orcaArgs -WorkingDirectory $orcaRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $orcaCache 'stdout.log') -RedirectStandardError (Join-Path $orcaCache 'stderr.log') -PassThru
@{ process_id=$orcaProcess.Id; port=8000; bind_address='0.0.0.0'; started_at=(Get-Date).ToString('o') } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $orcaCache 'server.json') -Encoding utf8
Write-Output ('ORCA LAN server started; launcher PID ' + $orcaProcess.Id)
Write-Output ('Logs: ' + $orcaCache)
