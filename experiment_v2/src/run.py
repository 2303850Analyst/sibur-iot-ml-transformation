import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import csv
import gzip
import hashlib
import json
import os
from pathlib import Path
import sys
import time

import joblib
import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge

HERE = Path(__file__).resolve().parents[1]
V1 = HERE.parent/'series_py312'
sys.path.insert(0,str(V1))
sys.path.insert(0,str(HERE.parent/'h2_audit'))
from ledger import Ledger,require
from src.data import sample,campaign,sha
from src.forecast import features
from engine import Warehouse, WARM,STOP,END,cell_class
from measure import measure

RESULT = HERE/'results'
DATA = HERE/'data'
LEVELS = {'L1':300,'L2':450,'L3':900}


def source():
    return pd.read_parquet(V1/'data/normalized/source.parquet'),json.loads((V1/'data/manifests/wepastacks.json').read_text())


def train():
    DATA.mkdir(exist_ok=True)
    RESULT.mkdir(exist_ok=True)
    data,manifest = source()
    models,rows = {},[]
    for level,nominal in LEVELS.items():
        events = sample(data,manifest,nominal,20261118)
        params = []
        for group in range(2):
            deliveries = events[events.direction.eq('delivery') & (events.sku//10==group)]
            counts = np.bincount(deliveries.seconds.to_numpy(dtype=int)//1800,minlength=(max(manifest['partitions']['test'])+1)*48)
            days = manifest['partitions']['train']
            first,last = days[0]*48,(days[-1]+1)*48
            mean = float(np.mean(counts[first:last]))
            reg = Ridge(alpha=10).fit([features(counts,bin_id,mean,first) for bin_id in range(first+48,last)],counts[first+48:last])
            params.append((reg.coef_,reg.intercept_,mean))
            for split in ('validation','test'):
                days = manifest['partitions'][split]
                bins = np.arange(days[0]*48,(days[-1]+1)*48)
                design = np.array([features(counts,bin_id,mean,first) for bin_id in bins])
                for method,pred in [('F0',design[:,5]),('F1',reg.predict(design).clip(0))]:
                    rows.append(dict(level=level,group=group,partition=split,method=method,
                                     bins=len(bins),mae=np.mean(abs(counts[bins]-pred))))
        models[level] = params
    joblib.dump(models,DATA/'models.joblib')
    pd.DataFrame(rows).to_csv(DATA/'holdout.csv',index=False)


class Check(Ledger):
    def __init__(self,sim):
        initial = dict(inbound=sim.inbound,outbound=sim.outbound,held=np.flatnonzero(sim.random['quality'] <
            (.20 if sim.scenario=='S5' else .05)).tolist(),cells=[(cell,sku) for sku,q in enumerate(sim.stored_skus) for cell,_ in q])
        super().__init__(initial)
        self.long_hold = sim.scenario=='S5'
    def apply(self,row):
        pallet = row['pallet']
        if row['event']=='reserve_and_start' and pallet not in self.held and self.inbound[pallet][2]%5==0:
            require(cell_class(row['cell']) in (1,3),'illegal_moves','special lot outside special cell')
        if self.long_hold and row['event']=='external_release':
            require(pallet in self.held and pallet in self.location and abs(self.inbound[pallet][0]+86400-row['time'])<.000002,
                    'illegal_release','release inconsistent with S5 schedule')
            self.held.remove(pallet)
            self.count['external_release']+=1
            return
        super().apply(row)


def fingerprint():
    freeze=json.loads((V1/'artifacts/simulation_freeze.json').read_text())
    for name,digest in freeze['files'].items():
        assert sha(V1/name)==digest,name
    files=[HERE/'protocol.md',DATA/'models.joblib',*sorted((HERE/'src').glob('*.py'))]
    # Analysis and publishing scripts can evolve separately from the generating process.
    files=[item for item in files if item.name in ('protocol.md','models.joblib','engine.py','measure.py','run.py')]
    hashes={item.name:sha(item) for item in files}
    hashes['v1']=freeze['fingerprint']
    hashes['ledger']=sha(HERE.parent/'h2_audit/ledger.py')
    return hashlib.sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest(),hashes


def initialize():
    global source_data,manifest,models
    source_data,manifest=source()
    models=joblib.load(DATA/'models.joblib')


def work(job):
    level,seed,pilot,fp=job
    selected=sample(source_data,manifest,LEVELS[level],seed)
    rows=[]
    scenarios=['S4'] if pilot and seed==1901 else ['S6'] if pilot and seed==1902 else ['S1'] if pilot else ['S1','S2','S3','S4','S5','S6']
    for scenario in scenarios:
        incoming,outgoing,_=campaign(selected,manifest,seed,scenario)
        variants=['BASE','A2','B2','C2','IGN','FAIL'] if pilot else ['A2','B2','C2']
        if not pilot and level=='L2' and scenario in ('S1','S4','S5','S6'):
            variants+=['BASE','NEAR','FIFO','IGN','IDEAL']
        for variant in variants:
            name=f'{scenario}-{level}-{seed}-{variant}'
            folder=RESULT/('pilot' if pilot else 'jobs')
            folder.mkdir(exist_ok=True,parents=True)
            target=folder/(name+'.json')
            if target.exists():
                row=json.loads(target.read_text())
                assert row['fingerprint']==fp
                rows.append(row)
                continue
            detail=level=='L2' and seed in (1900,2000) and scenario in ('S1','S4','S5','S6') and variant in ('A2','B2','C2','BASE','IDEAL')
            sim=Warehouse(incoming,outgoing,scenario,seed,variant,models[level],detail)
            sim.audit=Check(sim)
            assert sim.audit.input_hash==sim.input_hash
            sim.execute()
            sim.audit.finish()
            row,frame=measure(sim,detail)
            row.update(run_id=name,level=level,scenario=scenario,seed=seed,variant=variant,fingerprint=fp,
                       audited_events=sum(sim.audit.count.values()),audit_pass=True,
                       safety_source='independent streamed event ledger')
            if detail:
                dest=RESULT/'traces'/name
                dest.mkdir(exist_ok=True,parents=True)
                frame.to_parquet(dest/'pallets.parquet',index=False)
                pd.DataFrame(sim.actions).to_parquet(dest/'actions.parquet',index=False)
                pd.DataFrame([(at,*state) for at,state in sim.states],columns=['time','busy','free0','free1','free2','free3','free4']).to_parquet(dest/'capacity.parquet',index=False)
                with gzip.open(dest/'events.jsonl.gz','wt',encoding='utf8',compresslevel=1) as handle:
                    for event in sim.eventlog:
                        handle.write(json.dumps(event,default=lambda obj:obj.item(),separators=(',',':'))+'\n')
            temp=target.with_suffix('.tmp')
            temp.write_text(json.dumps(row,allow_nan=False,default=lambda obj:obj.item()))
            temp.replace(target)
            rows.append(row)
    return len(rows)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('stage',choices=('train','pilot','freeze','main'))
    parser.add_argument('--workers',type=int,default=10)
    args=parser.parse_args()
    if args.stage=='train':
        if (RESULT/'freeze.json').exists(): raise RuntimeError('Already frozen')
        train()
        sys.exit()
    fp,hashes=fingerprint()
    if args.stage=='freeze':
        files=list((RESULT/'pilot').glob('*.json'))
        assert len(files)==54 and all(json.loads(p.read_text())['fingerprint']==fp for p in files)
        (RESULT/'freeze.json').write_text(json.dumps(dict(fingerprint=fp,files=hashes,pilot_runs=54),indent=2))
        sys.exit()
    pilot=args.stage=='pilot'
    if not pilot: assert json.loads((RESULT/'freeze.json').read_text())['fingerprint']==fp
    jobs=[(level,seed,pilot,fp) for seed in (range(1900,1903) if pilot else range(2000,2020)) for level in LEVELS]
    with ProcessPoolExecutor(args.workers,initializer=initialize) as pool:
        futures=[pool.submit(work,job) for job in jobs]
        for future in as_completed(futures):
            print('completed block',future.result(),flush=True)
