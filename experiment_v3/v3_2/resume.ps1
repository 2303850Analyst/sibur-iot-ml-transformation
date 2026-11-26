$ErrorActionPreference='Stop'
Set-Location $PSScriptRoot
$release='wms_v32_code_20261120_r2'
$name='wms-v32-night'
$reports=New-Item -Force -ItemType Directory ../report/v3_2
$existing=docker ps -a --filter "name=^/$name$" --format '{{.Names}}'
if($existing -eq $name){
    $state=docker inspect -f '{{.State.Status}}' $name
    if($state -ne 'running'){docker start $name}
} else {
    $image=docker image inspect wms-inbound-research-runner:py312-v1 --format '{{.Id}}'
    if($LASTEXITCODE -ne 0){throw 'Missing runner image'}
    docker volume inspect $release *> $null
    if($LASTEXITCODE -ne 0){throw 'Frozen candidate code volume missing'}
    docker run -d --name $name --network none --restart on-failure:2 -e WMS32_IMAGE_ID=$image -e WMS32_OUTPUT=/out -e WMS32_REPORT=/report -v "${release}:/lab:ro" -v wms_v32_results:/out -v "$($reports.FullName):/report" -w /lab/experiment_v3/v3_2 $image python -u tools/night.py
    if($LASTEXITCODE -ne 0){throw 'Could not start runner'}
}
$monitor=(Resolve-Path tools/host_monitor.ps1).Path
Start-Process pwsh -WindowStyle Hidden -ArgumentList @('-NoProfile','-File',"`"$monitor`"",'-Container',$name)
docker inspect --format '{{.Name}} {{.State.Status}}' $name

& (Join-Path $PSScriptRoot 'live/launch_when_ready.ps1')
