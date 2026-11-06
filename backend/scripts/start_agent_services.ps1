# Purpose: start the configured separately keyed local A2A agents and durable coordinator worker.
# Behavior: run hidden with private logs/PIDs and refuse duplicate provider listeners/workers.
# Refs: H04-agent-coordination.json; ARKIV_A2A_FRONTEND_HANDOFF.md; Phase: H04.
$ErrorActionPreference='Stop'
$orcaRoot=(Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$orcaPython=Join-Path $orcaRoot 'backend/.venv/Scripts/python.exe'
$orcaState=Join-Path $orcaRoot '.cache/network/services'
$orcaConfig=Join-Path $orcaRoot '.cache/network/operator-bindings.json'
if (-not (Test-Path -LiteralPath $orcaConfig)) {throw 'Initialize and register provider bindings first'}
$orcaBindings=Get-Content -LiteralPath $orcaConfig -Raw | ConvertFrom-Json
foreach ($orcaAgent in $orcaBindings.agents) {
 $orcaUri=[Uri]$orcaAgent.endpoint
 if (Get-NetTCPConnection -LocalPort $orcaUri.Port -State Listen -ErrorAction SilentlyContinue) {throw ('Agent port already occupied: '+$orcaUri.Port)}
}
New-Item -ItemType Directory -Path $orcaState -Force | Out-Null
$orcaPidFile=Join-Path $orcaState 'processes.json'
if (Test-Path -LiteralPath $orcaPidFile) {
 $orcaPrior=Get-Content -LiteralPath $orcaPidFile -Raw | ConvertFrom-Json
 foreach ($orcaService in $orcaPrior) {
  if (Get-Process -Id $orcaService.process_id -ErrorAction SilentlyContinue) {throw 'A recorded agent service is still running; inspect its command before restarting'}
 }
}
$env:OPENBLAS_NUM_THREADS='1'
$env:OMP_NUM_THREADS='1'
$orcaProcesses=@()
foreach ($orcaRole in @('ocean','reviewer','coordinator-worker')) {
 $orcaModule=if ($orcaRole -eq 'coordinator-worker') {'orca.network_worker'} else {'orca.agent_provider'}
 $orcaEnvironment=if ($orcaRole -eq 'coordinator-worker') {Join-Path $orcaRoot '.env.demo'} else {Join-Path $orcaRoot ('.env.provider-'+$orcaRole)}
 if (-not (Test-Path -LiteralPath $orcaEnvironment)) {throw ('Private service environment missing: '+$orcaRole)}
 $orcaArguments=@('-m',$orcaModule,'--env-file',('"'+$orcaEnvironment+'"'))
 $orcaProcess=Start-Process -FilePath $orcaPython -ArgumentList $orcaArguments -WorkingDirectory $orcaRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $orcaState ($orcaRole+'-stdout.log')) -RedirectStandardError (Join-Path $orcaState ($orcaRole+'-stderr.log')) -PassThru
 $orcaProcesses+=@{name=$orcaRole;process_id=$orcaProcess.Id;started_at=(Get-Date).ToString('o')}
}
$orcaProcesses | ConvertTo-Json | Set-Content -LiteralPath $orcaPidFile -Encoding utf8
$orcaProcesses | ForEach-Object {Write-Output ($_.name+' started; launcher PID '+$_.process_id)}
