"""Functional check on actual HTTP sessions and stock postings. Not a live matrix trial."""
import argparse,hashlib,hmac,json,re,time,uuid
from pathlib import Path
import requests
ROOT=Path(__file__).resolve().parents[1]
import os
URL=os.environ.get('V3_ODOO_URL','http://127.0.0.1:18079')
DATABASE=os.environ.get('V3_ODOO_DB','wms_v3')
def secret():
    values=dict(line.strip().split('=',1) for line in (ROOT.parent/'.env').read_text().splitlines() if line.strip() and not line.lstrip().startswith('#') and '=' in line)
    return values['TSD_PASSWORD'].strip(chr(34)+chr(39)).encode()
class Terminal:
    def __init__(self,role,index=0):
        self.http=requests.Session()
        self.role=role
        login=f'v3-{role}-{index}'
        password=hmac.new(secret(),login.encode(),hashlib.sha256).hexdigest()
        response=self.http.post(URL+'/web/session/authenticate',json={'jsonrpc':'2.0','method':'call','params':{'db':DATABASE,'login':login,'password':password}},timeout=30)
        assert response.json()['result']['uid']
        self.token=re.search(r"csrf_token:'([^']+)'",self.http.get(URL+'/v3/tsd',timeout=20).text).group(1)
    def step(self,msg):
        response=self.http.post(URL+'/v3/step',data={'csrf_token':self.token,'message':json.dumps(msg)},timeout=60)
        return response.status_code,response.json()
def execute(prefix,bridge):
    from concurrent.futures import ThreadPoolExecutor
    roles={'production':2,'operator':6,'driver':4,'quality':2,'dispatch':2,'closing':2,'supervisor':2}
    identities=[(r,i) for r,n in roles.items() for i in range(n)]+[(r,0) for r in ('exchange','sensor','quality_adapter','production_adapter')]
    with ThreadPoolExecutor(4) as pool:
        sessions=dict(zip(identities,pool.map(lambda t:Terminal(*t),identities)))
    rows=[]
    for mask in (0,127):
        name=f'TECH-{prefix}-{mask}'
        members=[name+'-0',name+'-1']
        def step(n,role,data=None,expect=200,as_role=None):
            msg={'role':as_role or role,'case':name,'operation':n,'version':1,'event_id':uuid.uuid4().hex,'data':data or {}}
            channel={1:'production',2:'arrival',9:'quality',24:'exchange'}.get(n)
            if role in ('production_adapter','sensor','quality_adapter','exchange') and channel:
                status,result=bridge.submit(channel,msg)
            else:
                status,result=sessions[(role,0)].step(msg)
            rows.append(dict(mask=mask,operation=n,role=role,case=name,status=status,event_id=msg['event_id'],data=msg['data'],result=result))
            assert status==expect,(n,role,status,result)
            if n==16 and status==200:
                repeated=sessions[(role,0)].step(msg)
                assert repeated==(status,result)
            return result
        step(25,'closing',expect=422)
        step(8,'operator',expect=403,as_role='quality')
        step(1,'production_adapter' if mask else 'production',{'members':members})
        for p in members:
            step(2,'sensor' if mask else 'operator',{'pallet':p,'barcode':p})
        step(3,'operator',{'document':name})
        for n in (4,5):step(n,'operator')
        step(6,'quality',{'quality_case':'Q-'+name})
        step(7,'quality')
        if not mask:
            for n in (12,13,14,15):step(n,'operator')
        for p in members:step(16,'operator',{'pallet':p})
        step(10,'driver',{'pallet':members[0]})
        step(8,'quality',{'decision':'released'})
        step(9,'quality_adapter' if mask else 'quality',{'quality_case':'Q-'+name,'quality_version':1})
        step(10,'driver',{'pallet':members[1]})
        step(11,'quality')
        for p in members:
            if mask:
                from datetime import datetime,timezone
                step('reading','sensor',{'stamp':datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),'count':2,'serial':members.index(p)+1})
            proposal=step('next','dispatch')
            assert proposal['pallet']==p and proposal['candidates']>=1
            addr=step(17,'dispatch',{'pallet':p})['address']
            for n in (18,19):step(n,'driver',{'pallet':p,'address':addr})
            for n in (20,21):step(n,'operator',{'pallet':p})
            if p==members[0]:step(22,'closing',expect=422)
        step(22,'closing')
        if not mask:step(23,'closing')
        step(24,'exchange',{'ack':'wrong'},expect=422)
        step(24,'exchange',{'ack':name+'/1'})
        if not mask:step(25,'closing')
    path=ROOT/'results/functional'/f'{prefix}.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps({'scope':'FUNCTIONAL_ONLY','sessions':len(sessions),'rows':rows},indent=2))
    print(path,len(rows))
if __name__=='__main__':
    import sys
    from mqtt_bridge import Bridge
    with Bridge(ROOT/'results/functional/bridge.sqlite') as bridge:
        execute(sys.argv[1],bridge)


