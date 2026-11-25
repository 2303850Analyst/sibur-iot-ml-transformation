$ErrorActionPreference='Stop'
$PSNativeCommandUseErrorActionPreference=$true
Set-Location (Resolve-Path (Join-Path $PSScriptRoot '../..'))
$output='experiment_v2/results/api_benchmark'
New-Item -Force -ItemType Directory $output | Out-Null
$namespace='BENCH-V2-'+(Get-Date -Format yyyyMMddHHmmssffff)
"exec(compile(open('/lab/scripts/seed_benchmark.py').read(),'/lab/scripts/seed_benchmark.py','exec'))" |
    docker compose run --rm -T --no-deps -e "BENCHMARK_NAMESPACE=$namespace" odoo odoo shell -d wms_reference --no-http --max-cron-threads=0 --log-level=warn *> "$output/seed.log"
docker stats --no-stream --format '{{json .}}' wms-inbound-research-odoo-1 wms-inbound-research-db-1 > "$output/resources.jsonl"
docker compose run --rm --no-deps -e "BENCHMARK_NAMESPACE=$namespace" -e PYTHONPATH=/lab/scripts runner python -c "from pathlib import Path; import api_benchmark as bench; bench.ROOT=Path('/lab/experiment_v2/results/api_benchmark'); bench.main()" *> "$output/locust.log"
