import os


namespace = os.environ['BENCHMARK_NAMESPACE']
warehouse = env['stock.warehouse'].search([], limit=1)
locations = env['stock.location']
pallets = env['wms.research.pallet']
cells = env['wms.research.cell']
product = env['product.product'].search([('default_code', '=', 'REF-01')], limit=1)
assert product and namespace.startswith('BENCH-')
supplier = env.ref('stock.stock_location_suppliers')
for users in (1, 5, 20):
    run = f'{namespace}-{users}'
    buffer = locations.create(dict(name=f'{run}-BUFFER', usage='internal', location_id=warehouse.lot_stock_id.id))
    for index in range(131):
        address = f'{run}-LOC-{index:03d}'
        destination = locations.create(dict(name=address, usage='internal', location_id=warehouse.lot_stock_id.id))
        cells.create(dict(run_id=run, name=address, zone='storage', storage_group=0,
                          distance=40 + index / 10, location_id=destination.id))
        pallet = f'P-{index:03d}'
        lot = env['stock.lot'].create(dict(name=f'{run}-{pallet}', product_id=product.id, company_id=env.company.id))
        package = env['stock.package'].create(dict(name=f'{run}-{pallet}'))
        receipt = pallets._new_picking(env, product, 1, supplier, buffer, warehouse.in_type_id, f'{run}-{pallet}')
        pallets.create(dict(run_id=run, pallet_id=pallet, product_id=product.id, lot_id=lot.id, package_id=package.id,
                            quantity=1, storage_group=0, quality='released', receipt_id=receipt.id))
env.cr.commit()
print(f'Benchmark fixture committed: 3 snapshots of 131 expected pallets ({namespace})')
