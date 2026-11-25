$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
Set-Location (Split-Path -Parent $PSScriptRoot)
$saved = if (Test-Path artifacts/integration_results.json) { Get-Content -Raw artifacts/integration_results.json | ConvertFrom-Json }
if ($saved.status -ne 'PASS_DEMO_AND_RESTART') {
    docker compose run --rm --no-deps runner python scripts/demo_tsd_iot.py demo
    docker compose stop gateway
    try {
        docker compose run --rm --no-deps runner python scripts/demo_tsd_iot.py queue
        docker compose restart broker
    } finally {
        docker compose start gateway
    }
    docker compose run --rm --no-deps runner python scripts/demo_tsd_iot.py verify
    docker compose logs --no-color gateway broker > artifacts/mqtt_restart.log
}
