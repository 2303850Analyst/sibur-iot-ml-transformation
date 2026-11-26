param([ValidateSet('All','Simulation','Integration')][string]$Scope = 'All', [int]$Workers = 10, [switch]$RebuildImages)
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
Set-Location $PSScriptRoot
if (-not (Test-Path -LiteralPath .env)) {
    'DB_PASSWORD','TSD_PASSWORD','GATEWAY_PASSWORD','QUALITY_PASSWORD','ADMIN_PASSWORD' | ForEach-Object {
        "$_=$([Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(24)).ToLowerInvariant())"
    } | Set-Content -LiteralPath .env -Encoding utf8
}
New-Item -ItemType Directory -Force -Path artifacts,series_py312/artifacts | Out-Null
if (-not (Test-Path artifacts/environment_check.json)) {
    $docker = docker version --format '{{.Server.Version}}'
    $machine = Get-CimInstance Win32_ComputerSystem
    @{checked_at=(Get-Date -Format o); os=[System.Runtime.InteropServices.RuntimeInformation]::OSDescription;
      cpu_logical=$machine.NumberOfLogicalProcessors; ram_bytes=$machine.TotalPhysicalMemory;
      free_bytes=(Get-PSDrive C).Free; docker=$docker; compose=(docker compose version); docker_server_authorized=$true} |
        ConvertTo-Json | Set-Content -LiteralPath artifacts/environment_check.json -Encoding utf8
}
$missingImages = @('runner','gateway' | Where-Object {
    $RebuildImages -or -not (docker image ls --quiet --filter "reference=wms-inbound-research-${_}:py312-v1")
})
if ($missingImages.Count) { docker compose build @missingImages }
$integrationSucceeded = $false
try {
    if ($Scope -ne 'Simulation') {
        try {
            & "$PSScriptRoot/scripts/start_odoo.ps1"
            & "$PSScriptRoot/scripts/integration.ps1"
            $integrationSucceeded = $true
        } catch { Write-Warning "Odoo integration incomplete: $_" }
    }
    if ($Scope -ne 'Integration') {
        if (-not (Test-Path series_py312/protocol.yaml)) {
            Copy-Item -LiteralPath src,configs,tests,odoo_addons -Destination series_py312 -Recurse
            Copy-Item -LiteralPath protocol.yaml -Destination series_py312
            if (Test-Path data/raw) {
                New-Item -ItemType Directory -Force -Path series_py312/data | Out-Null
                Copy-Item -LiteralPath data/raw -Destination series_py312/data -Recurse
            }
        } else {
            $frozenInputs = @('protocol.yaml') + @(Get-ChildItem src/*.py,configs/*.yaml -File | ForEach-Object {
                [IO.Path]::GetRelativePath($PSScriptRoot, $_.FullName)
            })
            foreach ($inputFile in $frozenInputs) {
                $frozen = Join-Path series_py312 $inputFile
                if (-not (Test-Path -LiteralPath $frozen) -or
                    (Get-FileHash -LiteralPath $inputFile).Hash -ne (Get-FileHash -LiteralPath $frozen).Hash) {
                    throw "Simulation input differs from the frozen series: $inputFile. Use a separate directory for a new protocol."
                }
            }
        }
        $stage = (Resolve-Path series_py312).Path
        docker compose run --rm --no-deps -v "${stage}:/series" -w /series runner python -m pytest -q
        docker compose run --rm --no-deps -v "${stage}:/series" -w /series runner python -m src.run_matrix --workers $Workers
        docker compose run --rm --no-deps -e PYTHONPATH=/series -v "${stage}:/series" -w /series runner python /lab/scripts/verify_reproduction.py
        docker compose run --rm --no-deps runner python -m scripts.postprocess
    }
    if ($integrationSucceeded -and $Scope -eq 'All') {
        try {
            & "$PSScriptRoot/scripts/replay.ps1"
            docker compose run --rm --no-deps runner python -m scripts.verify_replay
        } catch { Write-Warning "Odoo replay incomplete: $_" }
        try { & "$PSScriptRoot/scripts/benchmark.ps1" } catch { Write-Warning "Locust benchmark incomplete: $_" }
    }
} finally {
    & "$PSScriptRoot/scripts/make_environment.ps1"
    docker compose run --rm --no-deps runner python -m scripts.build_report
}
