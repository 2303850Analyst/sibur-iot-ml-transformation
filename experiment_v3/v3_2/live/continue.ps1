$ErrorActionPreference='Stop'
Set-Location $PSScriptRoot
$checkpoint='../../report/v3_2/checkpoint.json'
if (!(Test-Path $checkpoint)){throw 'A checkpoint not yet available'}
if ((Get-Content $checkpoint -Raw | ConvertFrom-Json).phase -ne 'A_MATRIX_PROCESSED'){
    $code=docker wait wms-v32-night
    if ($LASTEXITCODE -ne 0 -or [int]$code -ne 0){throw 'A exited before live admission'}
}
if ((Get-Content $checkpoint -Raw | ConvertFrom-Json).phase -ne 'A_MATRIX_PROCESSED'){throw 'A is paused; resume A before live tests'}
& ../../../.venv/Scripts/python.exe control.py
exit $LASTEXITCODE

