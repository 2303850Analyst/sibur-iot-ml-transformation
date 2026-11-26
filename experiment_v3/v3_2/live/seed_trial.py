"""Provision only a fresh wms32 trial clone before HTTP traffic."""
import json,os
assert env.cr.dbname=='wms32_trial'
cfg=json.loads(os.environ['WMS32_FIXTURE'])
model=env['wms.v3.case'];units=env['wms.research.pallet']
assert not model.search_count([]) and not units.search_count([])
assert env['wms.research.cell'].search_count([('run_id','=','V3')])==5329
warehouse=env['stock.warehouse'].search([],limit=1)
buffer=env['stock.location'].search([('name','=','V3-Buffer'),('location_id','=',warehouse.lot_stock_id.id)],limit=1)
for receipt in cfg['cases']:
    name=receipt['case']
    quality=env['wms.v3.quality'].create({'name':receipt['quality_ref']})
    sku='V32-G'+str(receipt['group'])
    product=env['product.product'].search([('default_code','=',sku)],limit=1) or env['product.product'].create(dict(name=sku,default_code=sku,type='consu',is_storable=True,tracking='lot'))
    lot=env['stock.lot'].create(dict(name=receipt['production_lot'],product_id=product.id,company_id=env.company.id))
    ids=[]
    for pid in receipt['pallets']:
        pkg=env['stock.package'].create({'name':pid})
        pick=units._new_picking(env,product,1,env.ref('stock.stock_location_suppliers'),buffer,warehouse.in_type_id,pid)
        ids.append(units.create(dict(run_id='V3',pallet_id=pid,product_id=product.id,lot_id=lot.id,package_id=pkg.id,
            quantity=1,storage_group=receipt['group'],quality='hold',receipt_id=pick.id)).id)
    model.create(dict(name=name,scope='TRIAL',mask=cfg['mask'],load=cfg['load'],capacity=10 if cfg['scenario']=='S4' else 40,
                      members=[(6,0,ids)],quality_id=quality.id))
env.cr.commit()

