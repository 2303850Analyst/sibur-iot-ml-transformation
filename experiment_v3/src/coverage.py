import csv,hashlib,json,sqlite3
from collections import Counter
from pathlib import Path
from inputs import ROOT
from preflight import write

def main():
    folder=ROOT/'results';coverage=folder/'coverage';coverage.mkdir(exist_ok=True)
    stats={}
    for engine,file in [('PROCESS_SIMULATION','run_manifest.csv'),('LIVE_ODOO','live_manifest.csv')]:
        missing=coverage/('missing_'+file)
        cnt=Counter()
        with (ROOT/'plans'/file).open(encoding='utf-8-sig',newline='') as f,missing.open('w',newline='',encoding='utf-8-sig') as out:
            r=csv.DictReader(f);w=csv.DictWriter(out,fieldnames=r.fieldnames);w.writeheader()
            for row in r:
                cnt['planned']+=1
                commit=folder/'primary'/row['run_id']/'commit.json'
                if commit.exists():
                    item=json.loads(commit.read_bytes())
                    if item.get('run_id')!=row['run_id']:raise ValueError('unexpected commit')
                    cnt[item['status']]+=1
                else:w.writerow(row);cnt['missing']+=1
        stats[engine]=dict(cnt)
    pilots=[]
    for p in sorted((folder/'pilot').glob('*/summary.json')):
        v=json.loads(p.read_bytes())
        pilots.append(dict(id=p.parent.name,mask=v.get('mask'),cases=v.get('cases'),status='INVALID_FOR_COMPARISON' if (p.parent/'reliability.json').exists() else v['status'],audit=v.get('audit_findings'),input_hash=v.get('input_sha256')))
    orm_groups=sorted((folder/'functional_masks').glob('*'),key=lambda p:p.stat().st_mtime,reverse=True)
    orm_rows=[json.loads(f.read_bytes()) for f in orm_groups[0].glob('C*.json')] if orm_groups else []
    orm={'successful':sum(bool(r['complete']) for r in orm_rows),'total':128,'failed':[r['mask'] for r in orm_rows if not r['complete']],
         'scope':'one two-pallet functional receipt per mask, real Odoo, transactional rollback; not a 20-user trial'}
    cp={'functional_odoo_masks':orm,'status':'PARTIAL','design_id':json.loads((ROOT/'protocol.json').read_bytes())['protocol_id'],'counts':stats,'pilots':pilots,
        'functional_artifacts':[p.name for p in sorted((folder/'functional').glob('*.json'))],
        'next_command':'pwsh -File ./run_all.ps1 resume','primary_protocol_frozen':(ROOT/'protocol.sha256').exists()}
    write(coverage/'coverage.json',cp);write(ROOT/'plans/coverage.json',cp);write(folder/'checkpoints/latest.json',cp)
    print(json.dumps(stats))
    return cp
if __name__=='__main__':main()

