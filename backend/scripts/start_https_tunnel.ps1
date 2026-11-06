# Purpose: expose the authorized demo API through a temporary HTTPS endpoint for Vercel.
# Behavior: verify official cloudflared download, start hidden, and record a temporary public URL.
# Refs: FRONTEND_DASHBOARD_INTEGRATION_HANDOFF.md section 19; Phase: demo HTTPS integration.
$ErrorActionPreference = 'Stop'
$orcaRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$orcaTools = Join-Path $orcaRoot '.cache/tools'
$orcaTunnel = Join-Path $orcaRoot '.cache/https-tunnel'
New-Item -ItemType Directory -Path $orcaTools,$orcaTunnel -Force | Out-Null
$orcaHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/livez' -TimeoutSec 10
if ($orcaHealth.status -ne 'alive') { throw 'Start the ORCA API before opening the HTTPS tunnel' }
$orcaRelease = Invoke-RestMethod -Uri 'https://api.github.com/repos/cloudflare/cloudflared/releases/latest' -Headers @{ 'User-Agent'='ORCA-demo-setup' }
$orcaAsset = $orcaRelease.assets | Where-Object { $_.name -eq 'cloudflared-windows-amd64.exe' } | Select-Object -First 1
if (-not $orcaAsset -or $orcaAsset.digest -notmatch '^sha256:[0-9a-f]{64}$') { throw 'Official release does not supply the expected executable and SHA-256 digest' }
$orcaBinary = Join-Path $orcaTools 'cloudflared.exe'
$orcaExpected = $orcaAsset.digest.Substring(7)
if (-not (Test-Path -LiteralPath $orcaBinary) -or (Get-FileHash -LiteralPath $orcaBinary -Algorithm SHA256).Hash.ToLowerInvariant() -ne $orcaExpected) {
    Invoke-WebRequest -Uri $orcaAsset.browser_download_url -OutFile $orcaBinary
}
if ((Get-FileHash -LiteralPath $orcaBinary -Algorithm SHA256).Hash.ToLowerInvariant() -ne $orcaExpected) { throw 'Downloaded executable failed official checksum verification' }
$orcaState = Join-Path $orcaTunnel 'server.json'
if (Test-Path -LiteralPath $orcaState) {
    $orcaPrevious = Get-Content -LiteralPath $orcaState -Raw | ConvertFrom-Json
    if (Get-Process -Id $orcaPrevious.process_id -ErrorAction SilentlyContinue) { throw 'A recorded tunnel process is already running; inspect it before starting another' }
}
$orcaErrorLog = Join-Path $orcaTunnel 'stderr.log'
$orcaProcess = Start-Process -FilePath $orcaBinary -ArgumentList @('tunnel','--no-autoupdate','--protocol','http2','--url','http://127.0.0.1:8000') -WorkingDirectory $orcaRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $orcaTunnel 'stdout.log') -RedirectStandardError $orcaErrorLog -PassThru
$orcaUrl = $null
$orcaDeadline = (Get-Date).AddSeconds(45)
do {
    Start-Sleep -Milliseconds 500
    if (Test-Path -LiteralPath $orcaErrorLog) {
        $orcaMatch = [regex]::Match((Get-Content -LiteralPath $orcaErrorLog -Raw),'https://[a-z0-9-]+\.trycloudflare\.com')
        if ($orcaMatch.Success) { $orcaUrl = $orcaMatch.Value }
    }
} while (-not $orcaUrl -and (Get-Date) -lt $orcaDeadline -and -not $orcaProcess.HasExited)
@{ process_id=$orcaProcess.Id; public_url=$orcaUrl; release=$orcaRelease.tag_name; sha256=$orcaExpected; origin='http://127.0.0.1:8000' } | ConvertTo-Json | Set-Content -LiteralPath $orcaState -Encoding utf8
if (-not $orcaUrl) { throw 'No public URL received; inspect .cache/https-tunnel/stderr.log before retrying' }
Write-Output ('Temporary HTTPS API: '+$orcaUrl)
Write-Output 'The tunnel exposes the API publicly while running. Private routes still require Supabase user authentication.'
Write-Output 'Configure the exact Vercel frontend origin in ORCA_CORS_ORIGINS and restart the backend before browser integration.'
