$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
$commits = @{}
foreach ($image in 'odoo:19.0','postgres:16-alpine','eclipse-mosquitto:2','python:3.12-slim','wms-inbound-research-gateway:py312-v1','wms-inbound-research-runner:py312-v1') {
    $entry = docker image inspect $image 2>$null | ConvertFrom-Json
    if ($entry) {
        $commits[$image] = @{id=$entry.Id; digests=@($entry.RepoDigests)}
    } else { $commits[$image] = @{status='not_local'} }
}
$packages = if (Test-Path artifacts/host-pip-freeze.txt) { @(Get-Content artifacts/host-pip-freeze.txt) } else { @() }
$dockerPackages = if ($commits['wms-inbound-research-runner:py312-v1'].id) {
    @(docker compose run --rm --no-deps runner pip freeze --all 2>$null)
} else { @() }
@{frozen_at=(Get-Date -Format o); local_git_commit=$null; source_commit='da6280a8f8be1010eae64264c69a90258a288173';
  local_code_identity='series_py312/artifacts/simulation_freeze.json'; docker_images=$commits;
  host_python=if (Get-Command python -ErrorAction SilentlyContinue) { (python --version) } else { $null };
  host_packages=$packages; runner_packages=$dockerPackages; initial_amd64_odoo_manifest='sha256:512d3a362b896f5e16d9f541011288389101965e435d66d8affbb11fcc314fe9'} |
    ConvertTo-Json -Depth 8 | Set-Content -LiteralPath environment.lock.json -Encoding utf8
