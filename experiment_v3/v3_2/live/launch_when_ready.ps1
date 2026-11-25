$ErrorActionPreference='Stop'
Set-Location $PSScriptRoot
$dir=New-Item -ItemType Directory -Force '../../report/v3_2'
$pidFile=Join-Path $dir.FullName 'live_supervisor.json'
if(Test-Path $pidFile){
    $saved=Get-Content $pidFile -Raw | ConvertFrom-Json
    $process=Get-CimInstance Win32_Process -Filter "ProcessId=$($saved.process_id)"
    if($process -and $process.CommandLine.Contains((Join-Path $PSScriptRoot 'continue.ps1'))){return}
}
$target=Join-Path $PSScriptRoot 'continue.ps1'
$p=Start-Process pwsh -WindowStyle Hidden -ArgumentList @('-NoProfile','-File',"`"$target`"") -PassThru -RedirectStandardOutput (Join-Path $dir.FullName 'live_stdout.log') -RedirectStandardError (Join-Path $dir.FullName 'live_stderr.log')
@{process_id=$p.Id;phase='WAITING_FOR_A';command='live/continue.ps1'} | ConvertTo-Json | Set-Content $pidFile -Encoding utf8

