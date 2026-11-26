from common import *
import time,subprocess,statistics
sys.path.insert(0,str(V3/'live'))
import check_http
from mqtt_bridge import Bridge
cmd=['docker','compose','--env-file','../.env','exec','-T','odoo','sh','-c','odoo shell -d wms_v3 --db_host=db --db_user=odoo --db_password="$PASSWORD" --addons-path=/mnt/legacy,/mnt/extra-addons --no-http --log-level=error < /lab/seed.py']
result=subprocess.run(cmd,cwd=V3,check=True,capture_output=True,text=True)
fixture=next(line.split()[1] for line in result.stdout.splitlines() if line.startswith('FIXTURE '))
# Retain session secret lookup in its original scope, redirect only new evidence.
key=check_http.secret()
check_http.secret=lambda:key
import mqtt_bridge
mqtt_bridge.secret=lambda:key
check_http.ROOT=HOME/'preflight'
samples=[]
def observer(cls,name,channel):
    orig=getattr(cls,name)
    def timed(self,*args):
        t=time.perf_counter();res=orig(self,*args)
        if res[0]==200:samples.append(dict(op=args[-1]['operation'],channel=channel,seconds=time.perf_counter()-t))
        return res
    setattr(cls,name,timed)
observer(check_http.Terminal,'step','http')
observer(Bridge,'submit','mqtt_e2e')
with Bridge(HOME/'preflight/measure_inbox.sqlite') as bridge:check_http.execute(fixture,bridge)
save(HOME/'preflight/io_samples.json',samples)
def measured(op,ch):
    return statistics.median([r['seconds'] for r in samples if r['op']==op and r['channel']==ch])
save(HOME/'io_profile.json',dict(sensor_seconds=measured(2,'mqtt_e2e'),quality_seconds=measured(9,'mqtt_e2e'),production_seconds=measured(1,'mqtt_e2e'),http_seconds=statistics.median([r['seconds'] for r in samples if r['channel']=='http' and isinstance(r['op'],int)]),origin='SOFTWARE_MEASUREMENT',use='Scenario transport/service inputs based on median local technical calls; elapsed application time distinct from operator work.'))


