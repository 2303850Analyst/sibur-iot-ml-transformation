$ErrorActionPreference='Stop'
Set-Location $PSScriptRoot/..
function DC { docker compose --env-file ../.env @args; if ($LASTEXITCODE -ne 0){throw 'V3 Docker service failed'} }
DC up -d db broker
foreach($name in @('wms_v3','wms_v3_template')){
    $service=if($name -eq 'wms_v3'){'odoo'}else{'trial'}
    $exists=DC exec -T db psql -U odoo -d postgres -Atc "SELECT datname FROM pg_database WHERE datname='$name'"
    if($exists -ne $name){
        DC run --rm --no-deps $service odoo -d $name -i wms_research_v3 --without-demo --stop-after-init --addons-path=/mnt/legacy,/mnt/extra-addons --log-level=error
    }
    if($name -eq 'wms_v3_template'){
        DC run --rm --no-deps trial sh -c 'odoo shell -d wms_v3_template --db_host=db --db_user=odoo --db_password="$PASSWORD" --addons-path=/mnt/legacy,/mnt/extra-addons --no-http --log-level=error < /lab/seed.py'
    }
}
DC up -d odoo

