$ErrorActionPreference='Stop'
$compiler=Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
Add-Type -AssemblyName System.IO.Compression.FileSystem
$tmp=Join-Path ([IO.Path]::GetTempPath()) ('psv-'+[guid]::NewGuid()+'.zip')
try {
    [IO.Compression.ZipFile]::CreateFromDirectory((Join-Path $PSScriptRoot 'payload'),$tmp)
    $exe=Join-Path $PSScriptRoot 'PSV-Prototype.exe'
    & $compiler /nologo /target:exe /platform:anycpu "/out:$exe" "/resource:$tmp,payload.zip" /reference:System.IO.Compression.dll /reference:System.IO.Compression.FileSystem.dll (Join-Path $PSScriptRoot 'launcher/Program.cs')
    if($LASTEXITCODE -ne 0){throw 'Launcher compilation failed'}
} finally { if([IO.File]::Exists($tmp)){[IO.File]::Delete($tmp)} }
