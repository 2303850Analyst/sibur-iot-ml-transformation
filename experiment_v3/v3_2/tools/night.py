"""Single-owner night runner. Durable per-partition commit + SQLite job queue."""
from evidence import *
from job import execute,now
import os,sqlite3,fcntl,traceback,time,shutil
from concurrent.futures import ProcessPoolExecutor,as_completed
from datetime import datetime,timezone
MIRROR=Path(os.environ.get('WMS32_REPORT','/report'))
def pathsig():
    files=sorted((HOME/'tools').glob('*.py'))
    return hashlib.sha256(js([signatures(),[(p.name,sha(p)) for p in files],sha(HOME/'design/protocol/RESULTS_DATA_CONTRACT_V3_2_CANONICAL.json'),fingerprint()]).encode()).hexdigest()
def group_run(tasks):
    result=[]
    for t in tasks:
        try:
            r=execute(t)
            result.append((t,'FAILED_AUDIT' if r['audit_violations'] else 'COMPLETE',r))
        except Exception:result.append((t,'FAILED',{'error':traceback.format_exc()}))
    return result
def schema(db):
    db.execute('pragma journal_mode=WAL')
    db.execute('create table if not exists job(engine text, scenario text, load text, seed integer, mask integer, status text, detail text, primary key(engine,scenario,load,seed,mask))')
    for p in (HOME/'design/execution/plans').glob('*.csv'):
        eng=p.name[0]
        with p.open(encoding='utf-8-sig') as f:
            db.executemany('insert or ignore into job values(?,?,?,?,?,?,?)',[(eng,r['scenario_id'],r['load_id'],int(r['seed']),int(r['config_id'][1:]),'PENDING',None) for r in csv.DictReader(f)])
    db.commit()
def status(db,phase,**fields):
    rows=db.execute('select engine,status,count(*) from job group by engine,status').fetchall()
    state=dict(utc=now(),phase=phase,coverage={k:{s:n for e,s,n in rows if e==k} for k in ('A','B','C')},**fields)
    save(DEST/'checkpoint.json',state);save(MIRROR/'checkpoint.json',state)
    return state
def sync_preflight():
    for p in (DEST/'preflight').glob('*'):
        if p.is_file():shutil.copyfile(p,MIRROR/p.name)
def admission():
    path=DEST/'freeze.json'
    if path.exists():
        record=json.loads(path.read_bytes())
        if record['frozen_version']!=pathsig():raise RuntimeError('Frozen version mismatch')
        return record
    sel=ready()
    rec=dict(frozen_version=pathsig(),workers=sel['workers'],cpu_image_id=os.environ['WMS32_IMAGE_ID'],utc=now(),preflight=sel['version'],
             inputs=signatures(),plans=json.loads((HOME/'preflight/design.json').read_bytes()),benchmark=sha(DEST/'preflight/benchmark_4_6_8.csv'),
             replay=sha(DEST/'preflight/replay_verification.csv'),io_profile=json.loads((HOME/'io_profile.json').read_bytes()))
    save(path,rec);save(MIRROR/'freeze.json',rec);sync_preflight();return rec
def disk_guard():
    if shutil.disk_usage(DEST).free<100_000_000_000:raise RuntimeError('DISK_RESERVE: less than 100 GB free')
def compare_block(s,l,seed):
    d=DEST/'A'/s/l/str(seed);base=d/'C000'
    if not (base/'commit.json').exists():return
    dest=d/'comparisons';dest.mkdir(exist_ok=True)
    for p in sorted(d.glob('C*/commit.json')):
        r=json.loads(p.read_bytes())['record']
        if r['audit_violations']:continue
        out=dest/('operation_'+p.parent.name+'.csv')
        if not out.exists():comparison(p.parent,base,out)
def go():
    DEST.mkdir(exist_ok=True,parents=True);MIRROR.mkdir(exist_ok=True,parents=True)
    lock=(DEST/'session.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    db=sqlite3.connect(DEST/'queue.sqlite');schema(db);status(db,'PREFLIGHT_REPLAY')
    try:fr=admission()
    except Exception:
        sync_preflight();status(db,'BLOCKED_PREFLIGHT',error=traceback.format_exc());raise
    version=fr['frozen_version'];workers=fr['workers'];since=time.monotonic()
    status(db,'A_RUNNING',workers=workers,version=version)
    # Start with all masks at S1/L1; then round-robin scenarios and loads by seed.
    blocks=db.execute("select distinct scenario,load,seed from job where engine='A' and status='PENDING' order by seed,load,scenario").fetchall()
    for s,l,seed in blocks:
        disk_guard()
        ids=[z[0] for z in db.execute("select mask from job where engine='A' and scenario=? and load=? and seed=? and status='PENDING' order by mask",(s,l,seed))]
        masks=sorted(ids,key=lambda m:(m not in (0,127),m))
        chunks=[[(s,l,seed,m,'A',version) for m in masks[i:i+4]] for i in range(0,len(masks),4)]
        # Submit waves so a pressure signal can reduce concurrency before the next wave.
        pending=0
        while pending<len(chunks):
            disk_guard()
            pressure=DEST/'pressure.signal'
            if pressure.exists():
                if workers<=1:
                    status(db,'PAUSED_RESOURCE',reason='host pressure at one worker');return
                workers=max(1,workers-2);pressure.unlink(missing_ok=True)
            wave=chunks[pending:pending+2*workers];pending+=len(wave)
            with ProcessPoolExecutor(max_workers=workers) as pool:
                futures={pool.submit(group_run,c):c for c in wave}
                for f in as_completed(futures):
                    try:result=f.result()
                    except Exception:result=[(t,'FAILED_WORKER',{'error':traceback.format_exc()}) for t in futures[f]]
                    with db:
                        for t,state,info in result:
                            db.execute('update job set status=?,detail=? where engine=? and scenario=? and load=? and seed=? and mask=?',
                                       (state,js(info),'A',*t[:4]))
                    status(db,'A_RUNNING',workers=workers,version=version,block=[s,l,seed],elapsed_s=time.monotonic()-since)
        compare_block(s,l,seed)
        status(db,'A_RUNNING',workers=workers,version=version,last_block=[s,l,seed])
    status(db,'A_MATRIX_PROCESSED',version=version)
if __name__=='__main__':go()

