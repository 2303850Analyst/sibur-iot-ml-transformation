"""Initial scenario stock is posted through Odoo inventory adjustment."""
import os,random
assert env.cr.dbname in ('wms32_normal','wms32_constrained')
assert not env['wms.v3.case'].search_count([])
cells=env['wms.research.cell'].search([('run_id','=','V3'),('zone','=','storage')],order='name')
assert len(cells)==5200
count=4940 if env.cr.dbname=='wms32_constrained' else 3640
selected=random.Random(20261120).sample(list(range(5200)),count)
product=env['product.product'].create(dict(name='V32 initial stock',default_code='V32-INITIAL',type='consu',is_storable=True))
quants=env['stock.quant'].with_context(inventory_mode=True)
for i in range(0,count,200):
    qs=quants.create([dict(product_id=product.id,location_id=cells[k].location_id.id,inventory_quantity=1) for k in selected[i:i+200]])
    qs.action_apply_inventory()
assert sum(quants.search([('product_id','=',product.id)]).mapped('quantity'))==0 or quants.search_count([('product_id','=',product.id),('quantity','=',1),('location_id','in',cells.location_id.ids)])==count
assert quants.search_count([('product_id','=',product.id),('quantity','=',1),('location_id','in',cells.location_id.ids)])==count
env.cr.commit()

