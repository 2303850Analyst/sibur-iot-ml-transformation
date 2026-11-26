param([Parameter(Mandatory)][ValidateSet('S1','S2','S3','S4','S5','S6')][string]$Scenario,[ValidateSet('L1','L2','L3')][string]$Load='L1',[ValidateRange(3000,3019)][int]$Seed=3000)
$ErrorActionPreference='Stop'
$dest=New-Item -Force -ItemType Directory (Join-Path $PSScriptRoot "../../report/v3_2/blocks/$Scenario/$Load/$Seed")
docker cp "wms-v32-night:/out/A/$Scenario/$Load/$Seed/comparisons/." $dest.FullName
if($LASTEXITCODE -ne 0){exit 1}
$files=Get-ChildItem -LiteralPath $dest.FullName -Filter 'operation_C*.csv'
if($files.Count -ne 128){throw "Block has $($files.Count) available comparisons"}
foreach($file in $files){
    if((Import-Csv -LiteralPath $file.FullName).Count -ne 25){throw "Invalid operation count: $($file.Name)"}
}
$dest.FullName

