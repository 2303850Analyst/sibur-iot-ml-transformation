"""Replay a saved case (or verify ten cases including longest T72). No main writes."""
from core_revision import *
from engine import Engine
from job import input_block
from ledger import output
import tempfile,csv
import pyarrow.parquet as pq
def verify(part,case=None):
    m=json.loads((part/'commit.json').read_bytes());r=m['record']
    if sha(part/'cases.parquet')!=m['files']['cases.parquet']:raise ValueError('corrupt source partition')
    x,h=input_block(r['scenario_id'],r['load_id'],r['seed']);assert h==r['input_manifest_sha256']
    e=Engine(x,int(r['config_id'][1:])).execute()
    with tempfile.TemporaryDirectory(dir=str(DEST)) as tmp:
        summary=output(e,Path(tmp),r['run_id'])
        assert summary['ledger_sha256']==r['ledger_sha256'] and summary['audit_violations']==0
        a=pq.read_table(part/'cases.parquet');b=pq.read_table(Path(tmp)/'cases.parquet')
        assert a.equals(b),'case Parquet differs'
        assert pq.read_table(part/'visits.parquet').equals(pq.read_table(Path(tmp)/'visits.parquet')),'visit Parquet differs'
        allcases=[(i,x['case_id'],x['T72_ms'],x['trace_sha256']) for i,x in enumerate(a.select(['case_id','T72_ms','trace_sha256','cohort']).to_pylist()) if x['cohort']]
        if case:selected=[item for item in allcases if item[1]==case]
        else:selected=sorted(allcases,key=lambda v:hashlib.sha256(('V32-prespecified-'+v[1]).encode()).digest())[:9]+[max(allcases,key=lambda v:(v[2],v[1]))]
        assert selected
        if case:
            rows=[v for v in e.visits if v['case']==case]
            target=DEST/'replay'/r['run_id'];target.mkdir(parents=True,exist_ok=True)
            save(target/(case+'.json'),dict(case=a.slice(selected[0][0],1).to_pylist()[0],visits=rows))
        return [dict(run_id=r['run_id'],case_id=c,case_visits_sha256=hs,parquet_equal=True,all_cases=len(a),all_case_vectors_equal=True,input_hash=h) for _,c,_,hs in selected]
if __name__=='__main__':
    for row in verify(Path(sys.argv[1]),sys.argv[2] if len(sys.argv)>2 else None):print(row)

