$ErrorActionPreference='Stop'
Set-Location $PSScriptRoot/..
function DC { docker compose --env-file ../.env @args; if ($LASTEXITCODE -ne 0) {throw 'Docker Compose failed'} }
# Preserve prior trial evidence. Only an isolated V3 database is renamed.
$prior = DC exec -T db psql -U odoo -d postgres -Atc "select datname from pg_database where datname='wms_v3_trial'"
if ($prior -eq 'wms_v3_trial') {
    DC stop trial
    $archiveName='wms_v3_saved_'+(Get-Date -Format yyyyMMddHHmmss)
    DC exec -T db psql -U odoo -d postgres -v ON_ERROR_STOP=1 -c "ALTER DATABASE wms_v3_trial RENAME TO $archiveName"
}
DC exec -T db psql -U odoo -d postgres -v ON_ERROR_STOP=1 -c 'CREATE DATABASE wms_v3_trial TEMPLATE wms_v3_template'
DC run --rm --no-deps trial sh -c 'src=/var/lib/odoo/filestore/wms_v3_template; dest=/var/lib/odoo/filestore/wms_v3_trial; mkdir -p "$dest"; if test -d "$src"; then cp -an "$src/." "$dest/"; fi'
DC up -d trial
DC exec -T -e V3_MASK=0 -e V3_LOAD=L1 -e V3_CASE_COUNT=14 trial sh -c 'odoo shell -d wms_v3_trial --db_host=db --db_user=odoo --db_password="$PASSWORD" --addons-path=/mnt/legacy,/mnt/extra-addons --no-http --log-level=error < /lab/seed.py'
& ../.venv/Scripts/python.exe live/pilot_load.py "results/pilot/live-$(Get-Date -Format yyyyMMddHHmmss)"
exit $LASTEXITCODE

