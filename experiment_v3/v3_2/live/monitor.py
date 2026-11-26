import subprocess,time,threading,statistics
from contextlib import contextmanager
def metrics(logs):
    result=[]
    for phase,limit in [('active',60),('with_tail',75),('all',float('inf'))]:
        groups={}
        for r in logs:
            if r['end']>limit:continue
            key=(str(r['n']),r['role'])
            groups.setdefault(key,[]).append(r)
        for (operation,role),g in groups.items():
            latency=sorted((r['end']-r['start'])*1000 for r in g)
            queue=sorted((r['start']-r.get('queued',r['start']))*1000 for r in g)
            def pct(a,p):return a[round((len(a)-1)*p)]
            result.append(dict(phase=phase,operation=operation,role=role,calls=len(g),errors=sum(r['http']>=400 for r in g),latency_p50_ms=pct(latency,.5),
                latency_p95_ms=pct(latency,.95),latency_p99_ms=pct(latency,.99),queue_p95_ms=pct(queue,.95)))
    return result
@contextmanager
def monitor():
    stop=threading.Event();samples=[]
    def poll():
        while not stop.is_set():
            ts=time.monotonic()
            cmd=['docker','stats','--no-stream','--format','{{.Name}} {{.CPUPerc}} {{.MemUsage}}','wms32-live-odoo-1','wms32-live-db-1']
            snap=subprocess.run(cmd,capture_output=True,text=True)
            db=subprocess.run(['docker','exec','wms32-live-db-1','psql','-U','odoo','-d','wms32_trial','-Atc',
                "select (select count(*) from pg_locks where not granted), deadlocks from pg_stat_database where datname=current_database()"],capture_output=True,text=True)
            samples.append(dict(monotonic=ts,docker=snap.stdout.splitlines(),waiting_locks_and_deadlocks=db.stdout.strip()))
            stop.wait(1)
    t=threading.Thread(target=poll,daemon=True);t.start()
    try:yield samples
    finally:stop.set();t.join(15)

