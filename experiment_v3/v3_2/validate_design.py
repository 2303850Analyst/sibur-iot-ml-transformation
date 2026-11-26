from common import *
import itertools,zipfile
def main():
    names=HOME/'design'
    for ln in (names/'SHA256SUMS.txt').read_text().splitlines():
        h,n=ln.split(None,1);assert sha(names/n.strip().lstrip('*'))==h,n
    table=list(csv.DictReader((names/'protocol/ASIS_25_OPERATIONS.csv').open(encoding='utf-8-sig')))
    assert len(table)==len(ops)==25
    for i,r in enumerate(table,1):
        o=ops[i];assert r['operation_id']==o['operation_id'] and r['resource']==o['primary_resource'] and r['base_rule']==o['baseline_rule']
        if i not in (10,18):
            m=float(r['mode_s']);assert m==o['base_mode_s'] and abs(float(r['min_s'])-.7*m)<1e-8 and abs(float(r['max_s'])-1.3*m)<1e-8
        else:assert r['mode_s']=='dynamic'
    totals={}
    manifest={}
    for p in (names/'execution/plans').glob('*.csv'):
        with p.open(encoding='utf-8-sig') as f: rows=list(csv.DictReader(f))
        k=p.name[0];seeds={'A':range(3000,3020),'B':range(4000,4003),'C':range(5000,5003)}[k]
        masks=sorted({r['config_id'] for r in rows})
        kset={(r['config_id'],r['scenario_id'],r['load_id'],int(r['seed'])) for r in rows}
        assert len(masks)==(25 if k=='C' else 128) and len(kset)==len(rows)
        assert kset==set(itertools.product(masks,['S'+str(i) for i in range(1,7)],['L1','L2','L3'],seeds))
        totals[k]=len(rows);manifest[k]=sha(p)
    ref=json.loads((V3/'results/preflight.json').read_bytes())
    with (V3/'source_registry.csv').open(encoding='utf-8-sig') as f:
        refs=[r for r in csv.DictReader(f) if r['path'].startswith(('series_py312/','experiment_v2/'))]
    failures=[r['path'] for r in refs if sha(V3.parent/r['path'])!=r['sha256']]
    assert not failures,failures[:5]
    pilot=list((V3/'results/pilot').rglob('*'))
    pilot={p.relative_to(V3).as_posix():sha(p) for p in pilot if p.is_file()}
    baseline=HOME/'preflight/legacy_pilot_hashes.json'
    if baseline.exists():assert json.loads(baseline.read_bytes())==pilot
    else:save(baseline,pilot)
    save(HOME/'preflight/design.json',dict(counts=totals,manifest_sha=manifest,registry_sha=sha(PARAM),legacy_files_verified=len(refs),legacy_pilot_files=len(pilot),pilot_prior_independent_baseline=False,operations=25))
    print(totals,len(refs),len(pilot))
if __name__=='__main__':main()

