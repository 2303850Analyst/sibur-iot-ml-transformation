param([string]$Container='wms-v32-bench')
$dir=Join-Path $PSScriptRoot '../preflight'
$csv=Join-Path $dir "$Container-host.csv"
while ((docker inspect --format '{{.State.Running}}' $Container 2>$null) -eq 'true'){
    $os=Get-CimInstance Win32_OperatingSystem
    $cpu=Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor -Filter "Name='_Total'"
    $disk=Get-CimInstance Win32_PerfFormattedData_PerfDisk_PhysicalDisk -Filter "Name='_Total'"
    $mem=Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory
    $free=(Get-PSDrive C).Free
    $s=[pscustomobject]@{utc=[datetime]::UtcNow.ToString('o');available_mb=$mem.AvailableMBytes;cpu_pct=$cpu.PercentProcessorTime;disk_bytes_s=$disk.DiskBytesPersec;page_reads_s=$mem.PageReadsPersec;free_bytes=$free;docker=(docker stats --no-stream --format '{{.CPUPerc}} {{.MemUsage}}' $Container);wsl_swap=(wsl -d Ubuntu -- free -b | Select-String 'Swap:' | Out-String).Trim()}
    $s | Export-Csv -NoTypeInformation -Append -Path $csv
    if($free -lt 100000000000 -or $mem.AvailableMBytes -lt 2800){
        docker exec $Container touch /out/pressure.signal | Out-Null
    }
    Start-Sleep -Seconds 5
}
