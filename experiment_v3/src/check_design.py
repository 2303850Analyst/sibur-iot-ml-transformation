import csv,itertools,json,hashlib
from collections import Counter
from inputs import ROOT
from preflight import write
def check():
    known=json.loads((ROOT/'design/V3_CONFIGURATIONS_128.json').read_bytes())
    headers=['M'+str(i) for i in range(1,8)]
    def read(file):
        with file.open(encoding='utf-8-sig',newline='') as stream:return list(csv.DictReader(stream))
    table=read(ROOT/'plans/configurations.csv')
    assert len(known)==len(table)==128
    lookup={v['mask']:v for v in known}
    for i,row in enumerate(table):
        m=int(row['mask']);k=lookup[m]
        assert row['config_id']==k['config_id'] and row['name_ru']==k['name_ru']
        assert [int(row[b]) for b in headers]==[(m>>(j-1))&1 for j in range(1,8)]==[k['flags'][b] for b in headers]
    assert [int(r['mask']) for r in table]==sorted(range(128),key=lambda m:(m>=64,m.bit_count(),m))
    details={}
    for n,engine,seeds in [('run','PROCESS_SIMULATION',range(3000,3020)),('live','LIVE_ODOO',range(4000,4003))]:
        rows=read(ROOT/f'plans/{n}_manifest.csv')
        expected=set(itertools.product(range(128),range(1,7),range(1,4),seeds))
        actual={(int(r['config_id'][1:]),int(r['scenario'][1:]),int(r['load'][1:]),int(r['seed'])) for r in rows}
        assert actual==expected and len(rows)==len(actual)==len({r['run_id'] for r in rows})
        assert all(r['engine']==engine and r['status']=='PLANNED_NOT_EXECUTED' for r in rows)
        assert all((r['concurrent_users']=='20')==(engine=='LIVE_ODOO') for r in rows)
        details[engine]=len(rows)
    write(ROOT/'results/design_crosscheck.json',{'archive_checksums':11,'config_json_csv_equal':True,'unique_cartesian_products':details})
    print(details)
if __name__=='__main__':check()

