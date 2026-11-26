$ErrorActionPreference='Stop'
$PSNativeCommandUseErrorActionPreference=$true
Set-Location (Resolve-Path (Join-Path $PSScriptRoot '../..'))
$directory=(Resolve-Path experiment_v2).Path
docker compose run --rm --no-deps runner python experiment_v2/tests/select_paths.py
"exec(compile(open('/v2/tests/seed_paths.py').read(),'/v2/tests/seed_paths.py','exec'))" |
    docker compose run --rm -T --no-deps -v "${directory}:/v2:ro" odoo odoo shell -d wms_reference --no-http --max-cron-threads=0 --log-level=warn
docker compose run --rm --no-deps runner python experiment_v2/tests/live_paths.py paths
$network='wms-inbound-research_default'
$gateway='wms-inbound-research-gateway-1'
docker network disconnect $network $gateway
try {
    docker compose run --rm --no-deps runner python experiment_v2/tests/live_paths.py queue
} finally {
    docker network connect --alias gateway $network $gateway
}
docker compose run --rm --no-deps runner python experiment_v2/tests/live_paths.py verify
