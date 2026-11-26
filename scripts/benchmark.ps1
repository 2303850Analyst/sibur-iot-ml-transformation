$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
Set-Location (Split-Path -Parent $PSScriptRoot)
if ((Test-Path artifacts/api_benchmark/status.json) -and ((Get-Content -Raw artifacts/api_benchmark/status.json | ConvertFrom-Json).status -eq 'PASS')) { return }
New-Item -ItemType Directory -Force -Path artifacts/api_benchmark | Out-Null
$namespace = 'BENCH-' + (Get-Date).ToUniversalTime().ToString('yyyyMMddHHmmssffff')
"exec(compile(open('/lab/scripts/seed_benchmark.py').read(), '/lab/scripts/seed_benchmark.py', 'exec'))" |
    docker compose run --rm -T --no-deps -e "BENCHMARK_NAMESPACE=$namespace" odoo odoo shell -d wms_reference --no-http --max-cron-threads=0 --log-level=warn *> artifacts/api_benchmark/seed.log
docker stats --no-stream --format '{{json .}}' wms-inbound-research-odoo-1 wms-inbound-research-db-1 > artifacts/api_benchmark/resources.jsonl
docker inspect --format '{"name":{{json .Name}},"memory_bytes":{{.HostConfig.Memory}},"nano_cpus":{{.HostConfig.NanoCpus}},"cpu_set":{{json .HostConfig.CpusetCpus}}}' wms-inbound-research-odoo-1 wms-inbound-research-db-1 > artifacts/api_benchmark/container_limits.jsonl
docker info --format '{"docker_vm_cpu":{{.NCPU}},"docker_vm_memory_bytes":{{.MemTotal}}}' > artifacts/api_benchmark/docker_vm.json
docker compose exec -T db psql -U odoo -d postgres -Atc "SELECT pg_database_size('wms_reference')" > artifacts/api_benchmark/database_bytes.txt
docker compose run --rm --no-deps -e "BENCHMARK_NAMESPACE=$namespace" runner python scripts/api_benchmark.py *> artifacts/api_benchmark/locust.log
