$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Test-Path -LiteralPath .env)) {
    'DB_PASSWORD','TSD_PASSWORD','GATEWAY_PASSWORD','QUALITY_PASSWORD','ADMIN_PASSWORD' | ForEach-Object {
        "$_=$([Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(24)).ToLowerInvariant())"
    } | Set-Content -LiteralPath .env -Encoding utf8
}
New-Item -ItemType Directory -Force -Path artifacts | Out-Null
docker compose up -d --wait db broker
docker compose stop odoo
docker compose run --rm -T --no-deps odoo odoo -d wms_reference -i wms_research --stop-after-init --no-http --without-demo --max-cron-threads=0 *> artifacts/odoo_install.log
"exec(compile(open('/lab/scripts/seed_odoo.py').read(), '/lab/scripts/seed_odoo.py', 'exec'))" |
    docker compose run --rm -T --no-deps odoo odoo shell -d wms_reference --no-http --max-cron-threads=0 *> artifacts/odoo_seed.log
docker compose up -d --no-build odoo gateway
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    try {
        if ((Invoke-WebRequest 'http://127.0.0.1:18069/web/login' -TimeoutSec 3).StatusCode -eq 200) {
            docker compose exec -T odoo odoo --version
            docker compose exec -T db postgres --version
            docker compose exec -T broker mosquitto -h | Select-Object -First 1
            return
        }
    } catch { Start-Sleep -Seconds 2 }
}
throw 'Odoo did not return its login page; see docker compose logs odoo.'
