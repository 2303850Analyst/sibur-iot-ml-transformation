param([ValidateSet('preflight','functional','pilot','live-pilot','audit','report','freeze','run','resume')][string]$Stage='resume')
$ErrorActionPreference='Stop'
Set-Location $PSScriptRoot
$python=(Resolve-Path ../.venv/Scripts/python.exe).Path
function RunPy { & $python @args; if ($LASTEXITCODE -ne 0){exit $LASTEXITCODE} }
switch($Stage){
    'preflight' { RunPy src/check_design.py; RunPy src/preflight.py }
    'functional' { & pwsh -NoProfile -File live/functional.ps1; exit $LASTEXITCODE }
    'pilot' { RunPy src/technical_series.py }
    'live-pilot' { & pwsh -NoProfile -File live/pilot.ps1; exit $LASTEXITCODE }
    'audit' {
        RunPy src/audit.py
        RunPy src/audit_database.py
        RunPy src/audit_database.py wms_v3_trial
    }
    'report' {
        RunPy src/live_metrics.py
        RunPy src/report.py
        $bundle=Join-Path $HOME '.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
        $work=New-Item -ItemType Directory -Path (Join-Path $env:TEMP ('wms-v3-workbook-'+[guid]::NewGuid().ToString('N')))
        New-Item -ItemType Junction -Path (Join-Path $work.FullName 'node_modules') -Target (Join-Path $bundle 'node_modules') | Out-Null
        Copy-Item report/build.mjs $work.FullName
        & "$bundle/bin/node.exe" (Join-Path $work.FullName 'build.mjs') (Resolve-Path report).Path
        exit $LASTEXITCODE
    }
    'freeze' { RunPy src/primary.py freeze }
    'run' { RunPy src/primary.py run }
    'resume' {
        RunPy -m unittest discover -s tests
        RunPy live/functional_masks.py
        RunPy src/technical_series.py
        RunPy src/audit.py
        RunPy src/audit_database.py
        RunPy src/coverage.py
        if(Test-Path protocol.sha256){RunPy src/primary.py run}
        else {
            RunPy src/continuation.py
            exit $LASTEXITCODE
        }
    }
}

