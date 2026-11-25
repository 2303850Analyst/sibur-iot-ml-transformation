import hashlib
import gzip
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import run
from measure import measure
from engine import Warehouse

def verify_v1():
    manifests=list((run.V1/'artifacts/runs').glob('*/manifest.json'))
    assert len(manifests)==1080
    files=0
    for marker in manifests:
        manifest=json.loads(marker.read_text())
        for name,digest in manifest['files'].items():
            assert run.sha(marker.parent/name)==digest,marker.parent/name
            files+=1
    return files


def main():
    run.initialize()
    fp,_=run.fingerprint()
    assert fp==json.loads((run.RESULT/'freeze.json').read_text())['fingerprint']
    checks=[]
    for scenario,level,seed,variant in [('S4','L2',2000,'B2'),('S6','L2',2001,'C2'),('S5','L3',2002,'A2')]:
        name=f'{scenario}-{level}-{seed}-{variant}'
        saved=json.loads((run.RESULT/'jobs'/(name+'.json')).read_text())
        incoming,outgoing,_=run.campaign(run.sample(run.source_data,run.manifest,run.LEVELS[level],seed),run.manifest,seed,scenario)
        model=Warehouse(incoming,outgoing,scenario,seed,variant,run.models[level],True)
        model.audit=run.Check(model)
        model.execute()
        model.audit.finish()
        row,frame=measure(model,True)
        for key,value in row.items(): assert saved[key]==value,(name,key,saved[key],value)
        original=run.RESULT/'traces'/name/'events.jsonl.gz'
        if original.exists():
            replay=run.Check(Warehouse(incoming,outgoing,scenario,seed,variant,run.models[level],False))
            with gzip.open(original,'rt') as stream:
                for index,line in enumerate(stream):
                    event=json.loads(line)
                    assert event==model.eventlog[index]
                    replay.apply(event)
            assert len(model.eventlog)==index+1
            replay.finish()
        checks.append(dict(run=name,metrics=len(row),identical_events=len(model.eventlog) if original.exists() else None))
    prior=json.loads((run.RESULT/'v1_integrity.json').read_text())
    assert prior['status']=='PASS' and prior['manifests']==1080
    files=prior['hashes']
    (run.RESULT/'verification.json').write_text(json.dumps(dict(status='PASS',fingerprint=fp,
        repeated_runs=checks,v1_manifests_verified=1080,v1_manifest_file_hashes_verified=files),indent=2))
    print('Verified',checks,files)


if __name__=='__main__':
    main()
