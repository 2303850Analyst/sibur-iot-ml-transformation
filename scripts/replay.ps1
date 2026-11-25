param([string]$Series = 'series_py312/artifacts')
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
Set-Location (Split-Path -Parent $PSScriptRoot)
if ((Test-Path artifacts/replay/status.json) -and ((Get-Content -Raw artifacts/replay/status.json | ConvertFrom-Json).status -eq 'PASS_PROJECTED_REPLAY')) { return }
docker compose run --rm --no-deps runner python -m scripts.prepare_replay --from $Series
$inputs = @(Get-ChildItem -LiteralPath artifacts/replay/input -Filter '*.json.gz' -File)
if ($inputs.Count -ne 54) { throw "Expected 54 seed1000 traces; found $($inputs.Count)." }
$namespace = 'R' + (Get-Date).ToUniversalTime().ToString('yyyyMMddHHmmssffff')
"exec(compile(open('/lab/scripts/replay_odoo.py').read().split('address_records = seed_cells()')[0] + 'seed_cells()', '/lab/scripts/replay_odoo.py', 'exec'))" |
    docker compose run --rm -T --no-deps -e "REPLAY_NAMESPACE=$namespace" odoo odoo shell -d wms_reference --no-http --max-cron-threads=0 --log-level=warn *> artifacts/replay/pool.log
$workers = 8
$work = (Get-Location).Path
$jobs = for ($index = 0; $index -lt $workers; $index++) {
    Start-Job -ArgumentList $work,$namespace,$index,$workers -ScriptBlock {
        param($folder,$prefix,$shard,$total)
        Set-Location $folder
        "exec(compile(open('/lab/scripts/replay_odoo.py').read(), '/lab/scripts/replay_odoo.py', 'exec'))" |
            docker compose run --rm -T --no-deps -e "REPLAY_NAMESPACE=$prefix" -e "REPLAY_SHARD=$shard" -e "REPLAY_SHARDS=$total" odoo odoo shell -d wms_reference --no-http --max-cron-threads=0 --log-level=warn *> "artifacts/replay/shard-$shard.log"
        if ($LASTEXITCODE -ne 0) { throw "Replay shard $shard failed." }
    }
}
Wait-Job -Job $jobs | Out-Null
$cases = @(Get-ChildItem -LiteralPath artifacts/replay -Filter 'shard-*.json' | ForEach-Object {
    $receipt = Get-Content -LiteralPath $_.FullName -Raw | ConvertFrom-Json
    if ($receipt.namespace -eq $namespace) { $receipt.cases }
})
$good = ($cases.Count -eq 54 -and @($cases.case | Sort-Object -Unique).Count -eq 54 -and @($jobs | Where-Object State -ne 'Completed').Count -eq 0)
@{evidence='ODOO_INTEGRATION'; status=if ($good) {'PASS_PROJECTED_REPLAY'} else {'PARTIAL_REPLAY'}; namespace=$namespace; cases=$cases;
  limitations='Background occupancy projected from canonical logs; native stock transfers tested on selected pallets. Replay tracks physical dispatch through an internal transit location; live demo API counts registered pallets until confirmation.'} |
    ConvertTo-Json -Depth 7 | Set-Content -LiteralPath artifacts/replay/status.json -Encoding utf8
if (-not $good) { $jobs | Receive-Job; throw "Odoo replay completed only $($cases.Count) of 54 cells." }
