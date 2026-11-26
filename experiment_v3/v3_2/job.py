from common import *
from engine import Engine
from inputs import fixtures,digest
from ledger import output,write_table
from datetime import datetime,timezone
import resource,time,tempfile,shutil,fcntl,gzip
from functools import lru_cache
@lru_cache(maxsize=1)
def input_block(s,l,seed):
    d=DEST/'inputs'/s/l/str(seed);d.mkdir(parents=True,exist_ok=True);p=d/'manifest.json.gz'
    with (d/'prepare.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if not p.exists():
            x=fixtures(s,l,seed);raw=js(x).encode();h=hashlib.sha256(raw).hexdigest()
            tmp=p.with_suffix('.tmp')
            with gzip.open(tmp,'wb',compresslevel=2) as f:f.write(raw)
            save(d/'identity.json',dict(sha256=h,source=sha(V3.parent/'series_py312/data/normalized/source.parquet'),scenario=s,load=l,seed=seed))
            tmp.replace(p)
        else:
            with gzip.open(p,'rb') as f:raw=f.read()
            h=hashlib.sha256(raw).hexdigest()
            assert h==json.loads((d/'identity.json').read_bytes())['sha256']
            x=json.loads(raw)
        return x,h
def now():return datetime.now(timezone.utc).isoformat()
def execute(task):
    s,l,seed,mask,series,version=task
    rid=f'{series}-{s}-{l}-{seed}-C{mask:03d}';folder=DEST/series/s/l/str(seed)/f'C{mask:03d}';folder.parent.mkdir(parents=True,exist_ok=True)
    marker=folder/'commit.json'
    if marker.exists():
        m=json.loads(marker.read_bytes())
        if m['version']!=version:raise RuntimeError('existing partition version mismatch')
        for name,h in m['files'].items():
            if sha(folder/name)!=h:raise RuntimeError(f'checksum mismatch {rid}/{name}')
        return m['record']
    start=time.monotonic();cpu=time.process_time();start_utc=now()
    x,ih=input_block(s,l,seed);tmp=Path(tempfile.mkdtemp(prefix='.pending-',dir=folder.parent))
    e=Engine(x,mask).execute()
    r=output(e,tmp,rid)
    r.update(start_utc=start_utc,finish_utc=now(),run_id=rid,config_id=f'C{mask:03d}',scenario_id=s,load_id=l,seed=seed,input_manifest_sha256=ih,
        parameter_registry_sha256=sha(PARAM),git_commit=None,engine_version=version,model_version=sha(V3/'data/ridge.json'),
        resource_roster_hash=hashlib.sha256(js(params['staffing_reference_assumption']).encode()).hexdigest(),scenario_hash=hashlib.sha256(js([s,ih]).encode()).hexdigest(),
        sim_observation_start_s=0,sim_observation_end_s=e.end,completeness_status='INVALID' if r['audit_violations'] else 'PILOT_ONLY' if series!='A' else 'COMPLETE',
        wall_s=time.monotonic()-start,cpu_s=time.process_time()-cpu,peak_rss_mb=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024)
    write_table(tmp/'runs.parquet',[r])
    files={p.name:sha(p) for p in tmp.iterdir() if p.is_file()}
    save(tmp/'commit.json',dict(files=files,record=r,version=version));tmp.rename(folder)
    return r
if __name__=='__main__':
    s,l,seed,mask,series=sys.argv[1:]
    print(execute((s,l,int(seed),int(mask),series,fingerprint())))

