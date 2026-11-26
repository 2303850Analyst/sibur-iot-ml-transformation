"""Dispatch reads delivered evidence and local model coefficients only."""
import json,math
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from odoo import fields,models
from .policy import enabled,observed_count,pick_job
from .models import require

@lru_cache(maxsize=1)
def coefficients():
    return json.loads(Path('/lab/forecast_coefficients.json').read_bytes())

class Zone(models.Model):
    _name='wms.v3.observation'
    _description='Delivered physical zone count'
    scope=fields.Char(required=True,index=True)
    stamp=fields.Datetime(required=True)
    serial=fields.Integer(required=True)
    count=fields.Integer(required=True)
    _unique=models.Constraint('unique(scope,serial)','Repeated reading')

class Dispatch(models.Model):
    _inherit='wms.v3.case'
    scope=fields.Char(default='TECH',required=True,index=True)
    load=fields.Selection([(i,i) for i in ('L1','L2','L3')],default='L1',required=True)
    capacity=fields.Integer(default=40,required=True)
    def _reading(self,stamp,serial,count):
        self.ensure_one()
        require(enabled(self.mask,6),'sensor_disabled')
        now=fields.Datetime.now()
        stamp=fields.Datetime.to_datetime(stamp)
        require(stamp<=now and type(serial) is int and serial>=0 and type(count) is int,'invalid_reading')
        obs=self.env['wms.v3.observation']
        old=obs.search([('scope','=',self.scope),('serial','=',serial)],limit=1)
        if old:require(old.stamp==stamp and old.count==count,'conflicting_reading')
        else:obs.create(dict(scope=self.scope,stamp=stamp,serial=serial,count=count))
        return {'status':'stored','serial':serial}
    def _next(self):
        self.ensure_one()
        cases=self.search([('scope','=',self.scope),('closed','=',False),('mask','=',self.mask)])
        log=self.env['wms.v3.event']
        now=fields.Datetime.now()
        now_s=(now-fields.Datetime.to_datetime('1970-01-01')).total_seconds()
        recent=log.search([('case_id.scope','=',self.scope),('operation','=',2),('create_date','>=',now-timedelta(hours=25))])
        bins=[{},{}]
        index=int(now_s//1800)
        for event in recent:
            at=(event.create_date-fields.Datetime.to_datetime('1970-01-01')).total_seconds()
            g=event.result.get('group')
            if g in (0,1):
                i=int(at//1800);bins[g][i]=bins[g].get(i,0)+1
        f0=[];f1=[]
        for g in (0,1):
            w,b,mean=coefficients()[self.load+'/'+str(g)]
            lags=[bins[g].get(index-n,0) for n in (1,2,3,4,48)]
            angle=2*math.pi*(index%48)/48
            vec=lags+[math.sin(angle),math.cos(angle)]
            f0.append(sum(lags[:4])/4)
            f1.append(max(0,b+sum(x*y for x,y in zip(w,vec))))
        jobs=[];lookup={}
        balance=0
        for c in cases:
            state=c.checks or {}
            for p in c.members:
                pid=p.pallet_id
                if '2/'+pid in state and '18/'+pid not in state and not state.get('10/'+pid,{}).get('picking'):
                    balance+=1
                if '10/'+pid in state and '9' in state and p.quality=='released' and '17/'+pid not in state:
                    reg=log.search([('case_id','=',c.id),('operation','=',16),('member','=',pid)],limit=1)
                    when=(reg.create_date-fields.Datetime.to_datetime('1970-01-01')).total_seconds()
                    jobs.append((when,pid,p.storage_group,180+(40,80)[p.storage_group]))
                    lookup[pid]=c.name
        snap=self.env['wms.v3.observation'].search([('scope','=',self.scope)],order='stamp desc,serial desc',limit=1)
        reading=((snap.stamp-fields.Datetime.to_datetime('1970-01-01')).total_seconds(),snap.count) if snap and enabled(self.mask,6) else None
        n,source=observed_count(now_s,reading,balance,self.capacity)
        chosen=pick_job(jobs,now_s,n,self.capacity,f1 if enabled(self.mask,7) else f0,self.mask)
        return dict(case=lookup[chosen[1]] if chosen else None,pallet=chosen[1] if chosen else None,
                    observed=n,source=source,forecast=f1 if enabled(self.mask,7) else f0,candidates=len(jobs))

