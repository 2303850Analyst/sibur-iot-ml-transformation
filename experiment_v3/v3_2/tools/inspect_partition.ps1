param([Parameter(Mandatory)][ValidateSet('S1','S2','S3','S4','S5','S6')][string]$Scenario,[ValidateSet('L1','L2','L3')][string]$Load='L1',[ValidateRange(3000,3019)][int]$Seed=3000,[ValidateRange(0,127)][int]$Mask=0)
$ErrorActionPreference='Stop'
$cfg='C{0:D3}' -f $Mask
$folder=New-Item -Force -ItemType Directory (Join-Path $PSScriptRoot "../../report/v3_2/export/$Scenario/$Load/$Seed/$cfg")
docker cp "wms-v32-night:/out/A/$Scenario/$Load/$Seed/$cfg/." $folder.FullName
if($LASTEXITCODE -ne 0){exit 1}
$meta=Get-Content (Join-Path $folder.FullName 'commit.json') -Raw | ConvertFrom-Json -AsHashtable
foreach($f in $meta.files.Keys){
    if((Get-FileHash (Join-Path $folder.FullName $f)).Hash.ToLowerInvariant() -ne $meta.files[$f]){throw "Checksum mismatch: $f"}
}
$folder.FullName
