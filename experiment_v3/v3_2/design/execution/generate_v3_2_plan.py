#!/usr/bin/env python3
"""Сгенерировать неизменяемый план V3.2 для 3 контуров, без запуска стенда.

python execution/generate_v3_2_plan.py
Параметры, сроки и входные события замораживаются отдельно ДО основного исполнения.
"""
from pathlib import Path
import csv, json, itertools, hashlib
ROOT=Path(__file__).resolve().parents[1]
DEST=ROOT/'execution'/'plans'
DEST.mkdir(parents=True,exist_ok=True)
S=[f'S{i}' for i in range(1,7)]
L=['L1','L2','L3']
D=json.loads((ROOT/'protocol'/'02_STRESS_MASKS_25.json').read_text(encoding='utf-8'))
masks=[d['mask_id'] for d in D['masks']]
assert len(masks)==len(set(masks))==25
assert all(f'C{i:03d}' in masks for i in [0,1,2,4,8,16,32,64,127])
for comb in itertools.combinations(range(7),3):
    seen={tuple((int(mask[1:])>>i)&1 for i in comb) for mask in masks}
    assert len(seen)==8, (comb,seen)
plans=[
  ('A_PROCESS_SIMULATION.csv',[f'C{i:03d}' for i in range(128)],range(3000,3020),46080),
  ('B_LIVE_ODOO_CONTRACT.csv',[f'C{i:03d}' for i in range(128)],range(4000,4003),6912),
  ('C_LIVE_ODOO_STRESS.csv',masks,range(5000,5003),1350),
]
for fn,ms,seeds,expected in plans:
    path=DEST/fn
    with path.open('w',encoding='utf-8',newline='') as f:
        writer=csv.writer(f)
        writer.writerow(['config_id','scenario_id','load_id','seed','status'])
        n=0
        for scenario,load,seed,config in itertools.product(S,L,seeds,ms):
            writer.writerow([config,scenario,load,seed,'PLANNED'])
            n+=1
    assert n==expected,(fn,n,expected)
    print(fn,n,hashlib.sha256(path.read_bytes()).hexdigest())
print('OK: 46 080 A; 6 912 B; 1 350 C; C strength3 verified')
