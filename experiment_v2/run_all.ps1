param([int]$Workers=12,[switch]$RepeatIntegration,[switch]$RepeatBenchmark)
$ErrorActionPreference='Stop'
$PSNativeCommandUseErrorActionPreference=$true
Set-Location $PSScriptRoot
$lab=(Resolve-Path ..).Path
$study=(Get-Location).Path
$runner='sha256:ee86866e0f5b7d2460e7fff28a480c30ef19ce167f8bfed8bfd39101724ce28e'
$options=@('run','--rm','--network','none','-e','PYTHONDONTWRITEBYTECODE=1','-e','OPENBLAS_NUM_THREADS=1','-e','OMP_NUM_THREADS=1','-v',"${lab}:/lab:ro",'-v',"${study}:/lab/experiment_v2",'-w','/lab/experiment_v2',$runner)
if (-not (Test-Path results/freeze.json)) {
    docker @options python src/run.py train
    docker @options python src/run.py pilot --workers $Workers
    docker @options python -m pytest -q -p no:cacheprovider tests/test_policy.py
    docker @options python src/run.py freeze
}
docker @options python src/run.py main --workers $Workers
docker @options python src/analyze.py
docker @options python src/counterfactual.py
python tests/verify_v1.py
docker @options python tests/verify_series.py
docker @options python src/inspect_wait.py
if ($RepeatIntegration -or -not (Test-Path results/odoo_recovery.json)) { & "$PSScriptRoot/tests/integration.ps1" }
if ($RepeatBenchmark -or -not (Test-Path results/api_benchmark/status.json) -or (Get-Content results/api_benchmark/status.json -Raw|ConvertFrom-Json).status -ne 'PASS') { & "$PSScriptRoot/tests/benchmark.ps1" }
docker @options python src/report.py
