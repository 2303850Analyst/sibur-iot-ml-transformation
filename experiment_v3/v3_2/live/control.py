import csv,hashlib,json,os,random,shutil,sqlite3,subprocess,sys,time,traceback
from pathlib import Path
from manifest import BASE,manifest,DESIGN
from workload import Work
V3=BASE.parents[1];REPORT=V3/'report/v3_2';DATA=BASE/'results'
def call(cmd,**kw):
    p=subprocess.run(cmd,cwd=BASE,check=True,capture_output=True,**kw)
    return p.stdout.decode('utf8',errors='replace').strip()
def dc(*args):
    return call(['docker','compose','--env-file','../../../.env',*args])
def sql(db,query):
    return dc('exec','-T','db','psql','-X','-v','ON_ERROR_STOP=1','-U','odoo','-d',db,'-Atc',query)
def odoo_shell(db,script,variables=()):
    program='odoo shell -d '+db+' --db_host=db --db_user=odoo --db_password="$PASSWORD" --addons-path=/mnt/legacy,/mnt/extra-addons,/v32/extra --no-http --log-level=error < /v32/'+script
    return dc('run','--rm','--no-deps',*variables,'odoo','sh','-c',program)
def initialize():
    dc('up','-d','db')
    for _ in range(60):
        try:sql('postgres','select 1');break
        except subprocess.SubprocessError:time.sleep(1)
    if not sql('postgres',"select datname from pg_database where datname='wms32_template'"):
        # Stream the pre-existing empty V3 reference template locally between PostgreSQL containers.
        dc('exec','-T','db','createdb','-U','odoo','wms32_template')
        with subprocess.Popen(['docker','exec','wms-inbound-v3-db-1','pg_dump','-Fc','-U','odoo','wms_v3_template'],stdout=subprocess.PIPE) as src:
            subprocess.run(['docker','exec','-i','wms32-live-db-1','pg_restore','--exit-on-error','-U','odoo','-d','wms32_template'],stdin=src.stdout,check=True)
            if src.wait():raise RuntimeError('template transfer failed')
        dc('run','--rm','--no-deps','odoo','odoo','-d','wms32_template','-i','wms_live32','--stop-after-init','--addons-path=/mnt/legacy,/mnt/extra-addons,/v32/extra','--log-level=error')
    assert sql('wms32_template',"select state from ir_module_module where name='wms_live32'")=='installed'
    for db in ('wms32_normal','wms32_constrained'):
        if not sql('postgres',f"select datname from pg_database where datname='{db}'"):
            sql('postgres','create database '+db+' template wms32_template')
            odoo_shell(db,'base_stock.py')
def reset(s):
    dc('stop','odoo')
    label="WMS32 disposable clone, owned by dedicated runner"
    if sql('postgres',"select datname from pg_database where datname='wms32_trial'"):
        assert sql('postgres',"select shobj_description(oid,'pg_database') from pg_database where datname='wms32_trial'")==label
        sql('postgres','drop database wms32_trial')
    source='wms32_constrained' if s=='S5' else 'wms32_normal'
    sql('postgres','create database wms32_trial template '+source)
    sql('postgres',"comment on database wms32_trial is '"+label+"'")
    query="""select md5(string_agg(id::text||'/'||location_id::text||'/'||quantity::text,';' order by id)) from stock_quant;
    select md5(string_agg(id::text||login,';' order by id)) from res_users where left(login,3)='v3-';
    select count(*) from wms_v3_case; select count(*) from wms_v3_event; select count(*) from wms_research_cell"""
    state=sql('wms32_trial',query);assert state==sql(source,query) and state.splitlines()[-3:]==['0','0','5329']
    dc('up','-d','--force-recreate','broker')
    return hashlib.sha256(state.encode()).hexdigest()
def trial(mode,s,l,k,m,path):
    state=reset(s);data=manifest(s,l,k)
    spec=json.dumps(dict(cases=data,mask=m,scenario=s,load=l))
    odoo_shell('wms32_trial','seed_trial.py',('-e','WMS32_FIXTURE='+spec))
    dc('up','-d','odoo')
    import requests
    for _ in range(50):
        try:
            if requests.get('http://127.0.0.1:18132/web/login',timeout=2).status_code==200:break
        except requests.RequestException:pass
        time.sleep(1)
    from monitor import monitor,metrics
    with monitor() as samples:result=Work(mode,s,l,k,m,path).run()
    atomic(path/'host_metrics.json',samples)
    calls=json.loads((path/'requests.json').read_bytes())
    atomic(path/'latencies.json',metrics(calls))
    result['distinct_active_users']=len({(r['role'],r['user']) for r in calls if r.get('user') is not None})

    q="""select count(*) filter(where c.closed),count(*) from wms_v3_case c;
    select count(distinct receipt_id),count(*) from wms_research_pallet;
    select count(*) from wms_research_pallet p join wms_research_cell c on c.id=p.cell_id where
    (c.zone='storage' and p.quality!='released') or not exists(select 1 from stock_quant q where q.location_id=c.location_id and q.package_id=p.package_id and q.lot_id=p.lot_id and q.quantity=p.quantity);
    select count(*) from (select reserved_id from wms_research_pallet where reserved_id is not null group by reserved_id having count(*)>1)t"""
    lines=sql('wms32_trial',q).splitlines()
    closed,offered=map(int,lines[0].split('|'));n,total=map(int,lines[1].split('|'))
    assert offered==len(data) and n==total==sum(len(r['pallets']) for r in data) and lines[-2:]==['0','0'],lines
    if mode=='B':assert closed==result['complete']
    result.update(initial_sha=state,input_hash=hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest(),db_check=lines)
    result['status']='FAILED' if result['failed'] else 'COMPLETE'
    return result
def atomic(path,obj):
    path.parent.mkdir(exist_ok=True,parents=True);tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(obj,indent=2),encoding='utf8');tmp.replace(path)
def main():
    a=json.loads((REPORT/'checkpoint.json').read_bytes())
    if a['phase']!='A_MATRIX_PROCESSED':raise SystemExit('Wait for A workloads to finish before live tests')
    import msvcrt
    DATA.mkdir(parents=True,exist_ok=True)
    lock=(DATA/'live.lock').open('a+b')
    if lock.tell()==0:lock.write(b'1');lock.flush()
    lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    seal=json.loads((REPORT/'live_input_seal.json').read_bytes())
    for name,digest in seal['files'].items():
        assert hashlib.sha256((BASE/name).read_bytes()).hexdigest()==digest,'live input change: '+name
    initialize()
    # Pin functional harness and all inputs. Refuse changed code on resume.
    files=[p for p in BASE.rglob('*') if p.is_file() and 'results' not in p.parts and '__pycache__' not in p.parts and p.suffix in ('.py','.json','.csv','.yaml','.conf')]
    files+=list((V3/'odoo_addons').rglob('*.py'))+list((V3.parent/'odoo_addons').rglob('*.py'))+[V3/'live/check_http.py',V3/'live/mqtt_bridge.py']
    key=hashlib.sha256(b''.join(p.read_bytes() for p in sorted(files))).hexdigest()
    freeze=DATA/'live_freeze.json'
    if freeze.exists():assert json.loads(freeze.read_bytes())['hash']==key
    else:atomic(freeze,{'hash':key})
    for mode in ('B','C'):
        # Qualify each scenario independently on both endpoints before admitting its main tasks.
        eligible=set();qual={}
        for s in ['S'+str(i) for i in range(1,7)]:
            try:
                for m in (0,127):
                    part=DATA/'qualification'/mode/s/str(m);mark=part/'commit.json'
                    if not mark.exists():
                        result=trial(mode,s,'L1',2900,m,part)
                        assert result['status']=='COMPLETE',result
                        atomic(mark,result)
                eligible.add(s)
            except Exception:qual[s]=traceback.format_exc()
            atomic(REPORT/f'{mode}_qualification.json',{'passed':sorted(eligible),'issues':qual})
        plan=next((BASE.parent/'design/execution/plans').glob(mode+'_*.csv'))
        with plan.open(encoding='utf-8-sig') as f:rows=list(csv.DictReader(f))
        random.Random(20261120).shuffle(rows)
        counts={'COMPLETE':0,'FAILED':0,'BLOCKED_QUALIFICATION':0}
        for r in rows:
            s,l,k,m=r['scenario_id'],r['load_id'],int(r['seed']),int(r['config_id'][1:])
            if s not in eligible:counts['BLOCKED_QUALIFICATION']+=1;continue
            part=DATA/mode/s/l/str(k)/r['config_id'];marker=part/'commit.json'
            if not marker.exists():
                try:result=trial(mode,s,l,k,m,part)
                except Exception:result={'status':'FAILED','error':traceback.format_exc()}
                result['files']={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in part.glob('*.json') if p!=marker}
                atomic(marker,result)
            saved=json.loads(marker.read_bytes())
            if not all(hashlib.sha256((part/name).read_bytes()).hexdigest()==h for name,h in saved.get('files',{}).items()):
                counts['FAILED']+=1
            else:counts[saved['status']]+=1
            atomic(REPORT/f'{mode}_coverage.json',counts)
        atomic(REPORT/f'{mode}_coverage.json',counts)
if __name__=='__main__':main()

