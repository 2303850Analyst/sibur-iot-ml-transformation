param([switch]$NoBrowser,[switch]$Stop)
$ErrorActionPreference='Stop'
Set-Location -LiteralPath $PSScriptRoot
[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)
$state=Join-Path $env:LOCALAPPDATA 'PSV-Prototype'
$envFile=Join-Path $state 'settings.env'
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Для запуска нужен Docker Desktop с Linux containers. Установите его с https://www.docker.com/products/docker-desktop/ и повторите запуск.'
}
function EngineReady {
    $ErrorActionPreference='Continue'
    $os = & docker info --format '{{.OSType}}' 2>$null
    return ($LASTEXITCODE -eq 0 -and $os -eq 'linux')
}
if (-not (EngineReady)) {
    $desktop=@((Join-Path $env:LOCALAPPDATA 'Programs/DockerDesktop/Docker Desktop.exe'),
               (Join-Path $env:ProgramFiles 'Docker/Docker/Docker Desktop.exe')) |
        Where-Object {Test-Path -LiteralPath $_} | Select-Object -First 1
    if($desktop){Start-Process -FilePath $desktop -WindowStyle Hidden}
    Write-Host 'Ждём запуска Docker Desktop в режиме Linux containers…'
    for($i=0;$i -lt 60 -and -not (EngineReady);$i++){Start-Sleep 2}
    if(-not (EngineReady)){throw 'Docker Engine недоступен. Откройте Docker Desktop, включите Linux containers и запустите прототип повторно.'}
}
if(!(Test-Path -LiteralPath $envFile)){
    if($Stop){return}
    $rng=[Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $lines=foreach($name in 'DB_PASSWORD','TSD_PASSWORD','GATEWAY_PASSWORD','QUALITY_PASSWORD','ADMIN_PASSWORD'){
            $bytes=New-Object byte[] 32
            $rng.GetBytes($bytes)
            $name+'='+[BitConverter]::ToString($bytes).Replace('-','')
        }
        $temp=$envFile+'.tmp'
        [IO.File]::WriteAllLines($temp,[string[]]$lines,(New-Object Text.UTF8Encoding($false)))
        [IO.File]::Move($temp,$envFile)
    } finally {$rng.Dispose()}
}
$composeArgs=@('compose','-p','psv-user-prototype','--env-file',$envFile,'-f',(Join-Path $PSScriptRoot 'compose.yaml'))
function Compose {
    & docker @composeArgs @args
    if($LASTEXITCODE -ne 0){throw ('Docker Compose завершился с кодом '+$LASTEXITCODE+'. См. сообщение выше.')}
}
if($Stop){Compose stop;return}
Write-Host 'Запускаем PostgreSQL и MQTT…'
Compose up -d --wait db broker
$exists=Compose exec -T db psql -U odoo -d postgres -Atc "SELECT count(*) FROM pg_database WHERE datname='wms_reference'"
$seeded=$false
if("$exists".Trim() -eq '1'){
    $table=Compose exec -T db psql -U odoo -d wms_reference -Atc "SELECT to_regclass('wms_research_pallet')"
    if("$table".Trim() -eq 'wms_research_pallet'){
        $count=Compose exec -T db psql -U odoo -d wms_reference -Atc "SELECT count(*) FROM wms_research_pallet WHERE run_id='DEMO' AND pallet_id IN ('TSD-01','IOT-01','HOLD-01','LATE-01','FULL-01','MQTT-02')"
        $seeded=("$count".Trim() -eq '6')
    }
}
if(-not $seeded){
    Write-Host 'Первая установка: готовим Odoo и шесть учебных паллет. Это может занять несколько минут.'
    Compose stop odoo
    Compose run --rm -T --no-deps odoo odoo -d wms_reference -i wms_research --stop-after-init --no-http --without-demo --max-cron-threads=0
    "exec(compile(open('/lab/scripts/seed_odoo.py').read(),'/lab/scripts/seed_odoo.py','exec'))" | & docker @composeArgs run --rm -T --no-deps odoo odoo shell -d wms_reference --no-http --max-cron-threads=0
    if($LASTEXITCODE -ne 0){throw 'Не удалось подготовить учебные документы Odoo.'}
}
Write-Host 'Запускаем Odoo, шлюз IoT и имитатор считывателя…'
$gatewayImage=& docker image ls --quiet --filter 'reference=psv-prototype-gateway:v1'
if($LASTEXITCODE -ne 0){throw 'Не удалось проверить образ IoT-шлюза.'}
if(-not $gatewayImage){Compose build gateway}
Compose up -d --no-build odoo gateway sensor
$binding=Compose port odoo 8069
if("$binding".Trim() -notmatch '^127\.0\.0\.1:(\d+)$'){throw 'Не удалось определить локальный адрес Odoo.'}
$url='http://127.0.0.1:'+$Matches[1]
$ready=$false
for($i=0;$i -lt 90;$i++){
    try {$page=Invoke-WebRequest -UseBasicParsing -Uri "$url/wms/demo" -TimeoutSec 3
        if($page.StatusCode -eq 200 -and $page.Content.Contains('/wms/demo/login')){$ready=$true;break}
    } catch {Start-Sleep 2}
}
if(-not $ready){throw 'Odoo не открыл экран входа. Повторите запуск после проверки Docker Desktop.'}
Write-Host "Рабочее место готово: $url/wms/tsd"
if(-not $NoBrowser){
    $ticket=Compose exec -T odoo python3 /lab/scripts/ticket.py
    if("$ticket".Trim() -notmatch '^[A-Za-z0-9_-]{43}$'){throw 'Не удалось создать одноразовый вход.'}
    Start-Process ($url+'/wms/demo#'+"$ticket".Trim())
}
