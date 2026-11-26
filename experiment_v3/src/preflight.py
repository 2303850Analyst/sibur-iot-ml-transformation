import csv,hashlib,json,subprocess,zipfile
from pathlib import Path
import pandas as pd
import yaml
from inputs import ROOT,source
from process import ACTIONS, FLOW

def shafile(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def write(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf8')
def rows(path,alist):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(alist[0]));w.writeheader();w.writerows(alist)
def docker(*args):
    p=subprocess.run(['docker',*args],capture_output=True,text=True,encoding='utf8',errors='replace')
    if p.returncode:raise RuntimeError('Docker probe failed: '+str(args[:2]))
    return p.stdout.strip()
def main():
    legacy=ROOT.parent;design=json.loads((ROOT/'protocol.json').read_bytes())
    registry=[]
    for root,purpose in [(ROOT/'design','authoritative design'),(legacy/'series_py312','frozen V1'),(legacy/'experiment_v2','V2 original'),(legacy/'h2_audit','earlier independent audit'),(legacy/'phase_diagnostic','earlier stage diagnostics'),(legacy/'writing','research context'),(legacy/'odoo_addons','legacy Odoo'),(legacy/'scripts','legacy integration')]:
        for p in sorted(root.rglob('*')):
            if not p.is_file() or '__pycache__' in p.parts or '.pytest_cache' in p.parts:continue
            registry.append(dict(path=p.relative_to(legacy).as_posix(),sha256=shafile(p),purpose=purpose))
    for p in [legacy/'compose.yaml',legacy/'protocol.yaml',*sorted((Path.home()/'Downloads').glob('*КНИР*.pdf'))]:
        registry.append(dict(path=str(p),sha256=shafile(p),purpose='original reference'))
    rows(ROOT/'source_registry.csv',registry)
    source_df,meta=source()
    assert all(shafile(legacy/'series_py312/data/raw'/f)==h for f,h in meta['license_files'].items())
    assert shafile(legacy/'series_py312/data/raw/wepastacks_orders.json.gz')==meta['gzip_sha256']
    v2=Path.home()/'Downloads/WMS_V2_20261119_142512.zip'
    verified=0
    if v2.exists():
        with zipfile.ZipFile(v2) as z:
            for i in z.infolist():
                if i.is_dir():continue
                assert shafile(legacy/i.filename)==hashlib.sha256(z.read(i)).hexdigest(),i.filename
                verified+=1
    assert shafile(legacy/'series_py312/protocol.yaml')==(legacy/'series_py312/protocol.sha256').read_text().split()[0]
    versions={s:docker('inspect','--format','{{.Image}}','wms-inbound-v3-'+s+'-1') for s in ['odoo','db','broker']}
    data=dict(operating_profile='local isolated Docker; reference experiment',images=versions,
              odoo=docker('exec','wms-inbound-v3-odoo-1','odoo','--version'),postgres=docker('exec','wms-inbound-v3-db-1','postgres','--version'),
              mosquitto=docker('exec','wms-inbound-v3-broker-1','mosquitto','-h').splitlines()[0],v2_archive_files_verified=verified,
              source_rows=len(source_df),partitions={k:len(v) for k,v in meta['partitions'].items()},
              unresolved_references=['WMS_V2_REVIEW_20261119/REVIEW_RU.md','WMS_ASIS_STRATEGIC/ASIS_REPORT_RU.md'],
              design_sha256=shafile(ROOT/'protocol.json'),runtime_design_freeze=False)
    write(ROOT/'results/preflight.json',data)
    config=ROOT/'configs';config.mkdir(exist_ok=True)
    for name,val in [('modules',design['modules']),('operations',design['operations']),
            ('resources',{'human_servers':{r:1 for r in ['production','operator','driver','quality','dispatch','closing']},'calendar':'continuous scenario; independent finite queues','forklifts':2,'storage_addresses':5329}),
            ('scenarios',yaml.safe_load((legacy/'series_py312/protocol.yaml').read_text())['scenarios'])]:
        (config/(name+'.yaml')).write_text(yaml.safe_dump(val,allow_unicode=True,sort_keys=False),encoding='utf8')
    params=[dict(operation='P%02d'%n,action=design['operations'][n-1]['name_ru'],resource=r,unit='s/visit',value=t,uncertainty='uncalibrated; sensitivity 0.5x to 2x',origin='NEW_SCENARIO_ASSUMPTION',scope=design['operations'][n-1]['object']) for n,(r,t) in ACTIONS.items() if t]
    rows(ROOT/'parameter_registry.csv',params)
    assumptions=[dict(parameter='initial_fill',value='0.70; S5 0.95',unit='fraction',origin='LEGACY_SCENARIO'),
      dict(parameter='shared_quality',value='day/original SKU/source batch',unit='object key',origin='NEW_SCENARIO_ASSUMPTION'),
      dict(parameter='nonconformity',value='0.005; S5 0.02',unit='probability per quality object',origin='NEW_SCENARIO_ASSUMPTION'),
      dict(parameter='physical_arrival',value='ready+30',unit='s',origin='NEW_SCENARIO_ASSUMPTION'),
      dict(parameter='source_directions',value='WEPAStacks delivery/retrieval',unit='one pallet',origin='SOURCE_DATA'),
      dict(parameter='baseline',value='WMS and handheld scanners; no fixed sensors, end-to-end automation or ML',unit='configuration',origin='USER_ASSUMPTION'),
      dict(parameter='actual_SIBUR_resource_rates',value='unknown',unit='RUB/h',origin='UNKNOWN')]
    rows(ROOT/'assumptions.csv',assumptions)
    prim=[(0,1,'S1','L2','T72_min'),(0,2,'S5','L2','T72_min'),(0,4,'S1','L2','labor_min'),(0,8,'S4','L2','T72_min'),
          (0,16,'S1','L1','labor_min'),(8,40,'S4','L2','T72_min'),(63,127,'S6','L2','T72_min')]
    write(ROOT/'results/hypotheses.json',[dict(id='H'+str(i),control=c,variant=v,scenario=s,load=l,metric=m,unit='minutes/receipt',
          paired_unit='external seed',H0='mean(control-variant)<=0',H1='mean(control-variant)>0',family='Holm 0.05',practical_lower_bound=0.05)
          for i,(c,v,s,l,m) in enumerate(prim,1)])
    print(json.dumps(data,ensure_ascii=True))
if __name__=='__main__':main()

