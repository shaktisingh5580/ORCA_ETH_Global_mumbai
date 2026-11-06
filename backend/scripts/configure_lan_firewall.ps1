# Purpose: permit the requested same-LAN demo API through Windows Firewall.
# Behavior: add only inbound TCP 8000 from LocalSubnet; preserve existing network profiles.
# Refs: FRONTEND_DASHBOARD_INTEGRATION_HANDOFF.md; Phase: demo LAN integration.
$ErrorActionPreference = 'Stop'
$orcaRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$orcaCache = Join-Path $orcaRoot '.cache/lan-server'
New-Item -ItemType Directory -Path $orcaCache -Force | Out-Null
try {
    $orcaRule = Get-NetFirewallRule -Name 'ORCA-Demo-LAN-8000' -ErrorAction SilentlyContinue
    if (-not $orcaRule) {
        New-NetFirewallRule -Name 'ORCA-Demo-LAN-8000' -DisplayName 'ORCA demo API - local subnet TCP 8000' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8000 -RemoteAddress LocalSubnet -Profile Any -ErrorAction Stop | Out-Null
    }
    @{ status='configured'; rule='ORCA-Demo-LAN-8000'; port=8000; remote_address='LocalSubnet' } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $orcaCache 'firewall-result.json') -Encoding utf8
} catch {
    @{ status='failed'; reason='Administrator permission required or firewall policy denied the rule' } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $orcaCache 'firewall-result.json') -Encoding utf8
    exit 1
}
