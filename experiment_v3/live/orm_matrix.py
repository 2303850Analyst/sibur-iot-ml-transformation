"""Odoo-shell functional matrix on one restored starting stock per mask.
All trial writes rollback at the outer savepoint. Evidence is emitted to stdout.
This is not the 20-user HTTP experiment.
"""
import json,os,sys,time
from pathlib import Path
sys.path.insert(0,'/lab')
from fixtures_db import make_case
assert env.cr.dbname=='wms_v3'
class Rewind(Exception): pass
for mask in range(int(os.getenv('V3_FIRST_MASK','0')),int(os.getenv('V3_LAST_MASK','127'))+1):
    name='FUNCTIONAL-ALL-'+str(mask)
    start=time.monotonic()
    proof={'mask':mask,'scope':'ORM_FUNCTIONAL_ONLY','complete':False}
    try:
        with env.cr.savepoint():
            c=make_case(env,name,mask)
            pids=c.members.mapped('pallet_id')
            sequence=[]
            def step(n,role,data=None):
                u=env['res.users'].search([('login','=',f'v3-{role}-0')],limit=1)
                assert u and u.has_group('wms_research_v3.role_'+role)
                scope=(data or {}).get('pallet','')
                eid=f'{name}/{n}/{scope}'
                res=c._apply(n,role,u,eid,1,data or {})
                sequence.append((n,role,res))
                return res
            step(1,'production_adapter' if mask&1 else 'production',{'members':pids})
            step(6,'quality',{'quality_case':'Q-'+name});step(7,'quality')
            p=pids[0]
            step(2,'sensor' if mask&16 else 'operator',{'pallet':p,'barcode':p})
            step(3,'operator',{'document':name})
            for n in [4,5]:step(n,'operator')
            if not mask&4:
                for n in [12,13,14,15]:step(n,'operator')
            step(16,'operator',{'pallet':p});step(10,'driver',{'pallet':p})
            p=pids[1]
            step(2,'sensor' if mask&16 else 'operator',{'pallet':p,'barcode':p})
            step(16,'operator',{'pallet':p})
            step(8,'quality',{'decision':'released'})
            step(9,'quality_adapter' if mask&2 else 'quality',{'quality_case':'Q-'+name,'quality_version':1})
            step(10,'driver',{'pallet':p});step(11,'quality')
            if mask&32:
                from odoo import fields
                c._reading(fields.Datetime.now(),1,2)
            choices=[]
            for _ in pids:
                rec=c._next()
                p=rec['pallet']
                assert p in pids and p not in choices
                assert rec['source']==('sensor' if mask&32 else 'delivered_movements')
                choices.append(p)
                a=step(17,'dispatch',{'pallet':p})['address']
                for n in (18,19):step(n,'driver',{'pallet':p,'address':a})
                for n in (20,21):step(n,'operator',{'pallet':p})
            step(22,'closing')
            if not mask&4:step(23,'closing')
            step(24,'exchange',{'ack':name+'/1'})
            if not mask&4:step(25,'closing')
            events=env['wms.v3.event'].search([('case_id','=',c.id)],order='id')
            assert c.closed and set(events.mapped('operation'))==set(range(1,26))
            stock=[]
            for p in c.members:
                quant=env['stock.quant'].search([('location_id','=',p.cell_id.location_id.id),('lot_id','=',p.lot_id.id),('package_id','=',p.package_id.id)])
                assert sum(quant.mapped('quantity'))==p.quantity and p.quality=='released'
                assert p.receipt_id.state==p.transfer_id.state=='done'
                stock.append({'pallet':p.pallet_id,'address':p.cell_id.name,'receipt':p.receipt_id.id,'transfer':p.transfer_id.id,'quantity':p.quantity})
            proof.update(complete=True,operations=events.mapped('operation'),events=len(events),calls=len(sequence),stock=stock,
                         dispatch_candidates=len(choices),seconds=time.monotonic()-start)
            raise Rewind
    except Rewind:
        pass
    except Exception as error:
        proof.update(error=type(error).__name__+': '+str(error)[:200])
    print('V3_PROOF '+json.dumps(proof),flush=True)

