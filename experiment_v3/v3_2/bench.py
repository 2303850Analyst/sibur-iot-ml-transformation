from common import *
from job import execute,now
from concurrent.futures import ProcessPoolExecutor,as_completed
import time,threading,traceback,shutil
def version():
    return fingerprint()
def runpool(tasks,n,label):
    out=[];errors=[];start=time.monotonic()
    with ProcessPoolExecutor(max_workers=n) as pool:
        fs={pool.submit(execute,t):t for t in tasks}
        for f in as_completed(fs):
            try:
                r=f.result()
                if r['audit_violations']:raise RuntimeError('independent audit flagged '+str(r['audit_violations']))
                out.append(r)
            except Exception:errors.append(dict(task=fs[f],error=traceback.format_exc()))
    row=dict(test=label,workers=n,jobs=len(tasks),success=len(out),elapsed_s=time.monotonic()-start,failures=len(errors),
             max_worker_rss_mb=max((r['peak_rss_mb'] for r in out),default=0))
    save(DEST/'preflight'/(label+'.json'),dict(summary=row,records=out,errors=errors));print(row,flush=True)
    return row,out,errors
def csvfile(path,rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
def main():
    ver=version();prefix='pilot-'+ver[:10]
    combos=[(s,l,seed,m) for seed in (2900,2901) for s,l in [('S1','L1'),('S4','L2'),('S6','L3')] for m in (0,63,127)]
    combos+= [('S5','L2',2902,m) for m in (0,127)]+[('S2','L1',2902,127),('S3','L2',2902,127)]
    row,records,errors=runpool([(*c,prefix,ver) for c in combos],4,'contrast')
    if records:csvfile(DEST/'preflight/benchmark.csv',[{**r,'events_per_second':r['event_count']/r['wall_s']} for r in records])
    if errors:raise RuntimeError('Contrast errors, see contrast.json')
    selected=[combos[i] for i in (0,8,4,1,6,3,5,2)]
    ref={(r['scenario_id'],r['load_id'],r['seed'],r['config_id']):r for r in records}
    trials=[]
    for n in (4,6,8):
        row,records,errors=runpool([(*c,prefix+'-scale'+str(n),ver) for c in selected],n,'scale'+str(n))
        trials.append(row)
        if errors:continue
        for r in records:
            b=ref[r['scenario_id'],r['load_id'],r['seed'],r['config_id']]
            assert all(b[k]==r[k] for k in ('ledger_sha256','input_manifest_sha256','event_count','mean_T72_ms','standard_72')),r
    csvfile(DEST/'preflight/benchmark_4_6_8.csv',trials)
    valid=[r for r in trials if r['failures']==0];best=min(valid,key=lambda r:r['elapsed_s'])
    preferred=next((r for r in valid if r['workers']==6 and r['elapsed_s']<=1.05*best['elapsed_s']),best)
    save(DEST/'preflight/selection.json',dict(version=ver,workers=preferred['workers'],selection=preferred,series=prefix,coverage=len(combos)))
if __name__=='__main__':main()

