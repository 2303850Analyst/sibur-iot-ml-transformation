import json
from pathlib import Path

cases=json.loads(Path('/v2/results/odoo_fixtures.json').read_text())
warehouse=env['stock.warehouse'].search([],limit=1)
locations=env['stock.location']
pallets=env['wms.research.pallet']
for case in cases:
    run=case['run']
    product=env['product.product'].create(dict(name=run,default_code=f"V2-{case['sku']}",type='consu',is_storable=True,tracking='lot'))
    lot=env['stock.lot'].create(dict(name=run+'-LOT',product_id=product.id,company_id=env.company.id))
    package=env['stock.package'].create(dict(name=run+'-PACKAGE'))
    buffer=locations.create(dict(name=run+'-BUFFER',usage='internal',location_id=warehouse.lot_stock_id.id))
    receipt=pallets._new_picking(env,product,1,env.ref('stock.stock_location_suppliers'),buffer,warehouse.in_type_id,run)
    pallets.create(dict(run_id=run,pallet_id=case['pallet'],product_id=product.id,lot_id=lot.id,package_id=package.id,
        quantity=1,storage_group=case['sku']//10,quality='hold' if case['cells'][0]>=5200 else 'released',receipt_id=receipt.id))
    for cell in case['cells']:
        name=f'V2-{cell}'
        loc=locations.create(dict(name=name,usage='internal',location_id=warehouse.lot_stock_id.id))
        env['wms.research.cell'].create(dict(run_id=run,name=name,location_id=loc.id,distance=30 if cell>=5200 else 40,
              zone='quarantine' if cell>=5200 else 'storage',storage_group=min(1,cell//2600)))
env.cr.commit()
print('Committed V2 expected receipts:',len(cases))
