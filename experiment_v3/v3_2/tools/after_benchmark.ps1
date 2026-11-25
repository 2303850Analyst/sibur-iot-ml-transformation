param([string]$Bench)
$ErrorActionPreference='Stop'
$rc=docker wait $Bench
if ($LASTEXITCODE -ne 0 -or [int]$rc -ne 0){throw "Benchmark failed; examine docker logs $Bench"}
& pwsh -NoProfile -File (Join-Path $PSScriptRoot '../resume.ps1')
exit $LASTEXITCODE
