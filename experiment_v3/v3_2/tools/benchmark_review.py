from core_revision import *
from bench import csvfile
import statistics,collections
def summarize(report):
    report=Path(report)
    data=json.loads((report/'contrast.json').read_bytes())['records']
    grouped=collections.defaultdict(list)
    for r in data:grouped[r['scenario_id'],r['load_id']].append(r['wall_s'])
    stats=[dict(scenario=s,load=l,n=len(v),median_wall_s=statistics.median(v),max_wall_s=max(v)) for (s,l),v in sorted(grouped.items())]
    csvfile(report/'benchmark_mixture.csv',stats)
    host=HOME/'preflight/wms-v32-bench-r2-host.csv'
    samples=list(csv.DictReader(host.open(encoding='utf-8-sig'))) if host.exists() else []
    if samples:save(report/'host_monitor_summary.json',dict(min_available_mb=min(int(r['available_mb']) for r in samples),
        max_cpu_pct=max(int(r['cpu_pct']) for r in samples),samples=len(samples),thermal='UNKNOWN: no calibrated core sensor',
        peak_disk_bytes_s=max(int(r['disk_bytes_s']) for r in samples)))
if __name__=='__main__':summarize(sys.argv[1])

