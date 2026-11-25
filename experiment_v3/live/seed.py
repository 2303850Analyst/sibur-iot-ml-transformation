"""Execute in an isolated V3 Odoo shell only."""
import hashlib, hmac, os, uuid, json
assert env.cr.dbname in ('wms_v3','wms_v3_template','wms_v3_trial')
prefix='V3'
secret=os.environ['TSD_PASSWORD'].encode()
def password(name):
    return hmac.new(secret,name.encode(),hashlib.sha256).hexdigest()
env.ref('base.user_admin').password=password('admin')
profiles={'production':2,'operator':6,'driver':4,'quality':2,'dispatch':2,'closing':2,'supervisor':2,
          'sensor':1,'exchange':1,'production_adapter':1,'quality_adapter':1}
users=env['res.users'].with_context(no_reset_password=True)
for role,count in profiles.items():
    for i in range(count):
        login=f'v3-{role}-{i}'
        if not users.search_count([('login','=',login)]):
            users.create({'login':login,'name':login,'group_ids':[(6,0,[env.ref('base.group_user').id,env.ref('wms_research_v3.role_'+role).id])],'password':password(login)})
mutex=env['wms.research.mutex']
if not mutex.browse(1).exists():
    assert mutex.create({}).id==1
wh=env['stock.warehouse'].search([],limit=1)
loc=env['stock.location']
def location(name):
    return loc.search([('location_id','=',wh.lot_stock_id.id),('name','=',name)],limit=1) or loc.create({'name':name,'location_id':wh.lot_stock_id.id,'usage':'internal'})
buffer=location('V3-Buffer')
cells=env['wms.research.cell']
count=cells.search_count([('run_id','=','V3')])
if not count:
    for start in range(0,5329,300):
        idx=list(range(start,min(start+300,5329)))
        places=loc.create([{'name':f'V3-{i:04d}','location_id':wh.lot_stock_id.id,'usage':'internal'} for i in idx])
        cells.create([{'name':p.name,'location_id':p.id,'run_id':'V3','storage_group':int(i>=2600 and i<5200),
            'zone':'quarantine' if i>=5200 else 'storage','distance':(30 if i>=5200 else 40+40*int(i>=2600))+
            (10 if i>=5200 else 20)*(1-(i%129)/128)} for i,p in zip(idx,places)])
assert cells.search_count([('run_id','=','V3')])==5329
product=env['product.product'].search([('default_code','=','V3-SKU')],limit=1) or env['product.product'].create({'name':'V3 SKU','default_code':'V3-SKU','type':'consu','is_storable':True,'tracking':'lot'})
cases=env['wms.v3.case']
pallets=env['wms.research.pallet']
import sys
sys.path.insert(0,'/lab')
from fixtures_db import make_case
if env.cr.dbname=='wms_v3':
    token=uuid.uuid4().hex[:12]
    for mask in (0,127):
        make_case(env,f'TECH-{token}-{mask}',mask)
    print('FIXTURE '+token)
elif env.cr.dbname=='wms_v3_trial':
    mask=int(os.environ['V3_MASK'])
    size=int(os.environ['V3_CASE_COUNT'])
    for index in range(size):
        c=make_case(env,f'TRIAL-{index:04d}',mask)
        c.write({'scope':'TRIAL','load':os.environ['V3_LOAD']})
env.cr.commit()
