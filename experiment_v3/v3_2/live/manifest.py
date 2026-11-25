import hashlib,json,random
from pathlib import Path
from itertools import combinations
BASE=Path(__file__).resolve().parent
DESIGN=json.loads((BASE/'design.json').read_bytes())
def manifest(s,l,seed):
    count=DESIGN['offered'][l]
    records=[]
    for i in range(count):
        name=f'R-{s}-{l}-{seed}-{i:03d}'
        rand=random.Random(hashlib.sha256(name.encode()).digest())
        n=rand.choice(DESIGN['pallets_per_case'])
        records.append(dict(case=name,production_lot='LOT-'+name,quality_ref='Q-'+name,planned_s=i*60/count/(2 if s=='S6' else 1),
            pallets=[name+f'-{j}' for j in range(n)],group=rand.randrange(2),label_error=s=='S2' and i==0,
            stale_version=s=='S2' and i==0,quarantine=s=='S5',decision='failed' if s=='S5' and i==count-1 else 'released'))
    return records
if __name__=='__main__':
    from itertools import product
    import csv
    dest=BASE/'input_hashes.csv'
    with dest.open('w',newline='') as f:
        w=csv.writer(f);w.writerow(['scenario','load','seed','offered','pallets','sha256'])
        for s,l,k in product([f'S{i}' for i in range(1,7)],['L1','L2','L3'],[4000,4001,4002,5000,5001,5002]):
            data=manifest(s,l,k)
            w.writerow([s,l,k,len(data),sum(len(r['pallets']) for r in data),hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()])

