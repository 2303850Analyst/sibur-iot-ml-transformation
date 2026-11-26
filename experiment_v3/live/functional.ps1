$ErrorActionPreference='Stop'
Set-Location $PSScriptRoot/..
# Restart only the technical application to load its current V3 Python sources.
docker compose --env-file ../.env restart odoo
if ($LASTEXITCODE -ne 0){exit 1}
$trace=docker compose --env-file ../.env exec -T odoo sh -c 'odoo shell -d wms_v3 --db_host=db --db_user=odoo --db_password="$PASSWORD" --addons-path=/mnt/legacy,/mnt/extra-addons --no-http --log-level=error < /lab/seed.py'
if ($LASTEXITCODE -ne 0){exit 1}
$fixture=[regex]::Match(($trace -join "`n"),'FIXTURE ([0-9a-f]{12})').Groups[1].Value
if (-not $fixture){throw 'Fixture ID missing'}
& ../.venv/Scripts/python.exe live/check_http.py $fixture
exit $LASTEXITCODE

