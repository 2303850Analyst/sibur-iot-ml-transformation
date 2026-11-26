import os


env.ref('base.user_admin').password = os.environ['ADMIN_PASSWORD']
for login in ('tsd', 'gateway', 'quality'):
    users = env['res.users'].with_context(no_reset_password=True)
    groups = [env.ref('base.group_user').id, env.ref(f'wms_research.role_{login}').id]
    if login == 'tsd':
        groups.append(env.ref('stock.group_stock_user').id)
    current = users.search([('login', '=', f'research-{login}')], limit=1)
    if not current:
        users.create(dict(login=f'research-{login}', name=f'Research {login}', group_ids=[(6, 0, groups)],
                          password=os.environ[f'{login.upper()}_PASSWORD']))

if not env['wms.research.mutex'].browse(1).exists():
    assert env['wms.research.mutex'].create({}).id == 1

warehouse = env['stock.warehouse'].search([], limit=1)
locations = env['stock.location']


def location(name):
    return locations.search([('name', '=', name), ('location_id', '=', warehouse.lot_stock_id.id)], limit=1) or locations.create(
        dict(name=name, usage='internal', location_id=warehouse.lot_stock_id.id))


buffer = location('WMS-R Buffer')
cells = env['wms.research.cell']
for address, zone, group, distance in [('Q-01', 'quarantine', 0, 30), ('A-01', 'storage', 0, 40),
                                      ('A-02', 'storage', 0, 45), ('B-01', 'storage', 1, 80)]:
    if not cells.search_count([('name', '=', address)]):
        cells.create(dict(name=address, zone=zone, storage_group=group, distance=distance,
                          location_id=location(address).id))

pallets = env['wms.research.pallet']
products = env['product.product']
fixtures = [('TSD-01', 0, 'released'), ('IOT-01', 0, 'released'), ('HOLD-01', 1, 'hold'),
            ('LATE-01', 0, 'released'), ('FULL-01', 0, 'released'), ('MQTT-02', 0, 'released')]
for pallet, group, quality in fixtures:
    if pallets.search_count([('run_id', '=', 'DEMO'), ('pallet_id', '=', pallet)]):
        continue
    sku = f'REF-{group + 1:02d}'
    product = products.search([('default_code', '=', sku)], limit=1) or products.create(dict(
        name=sku, default_code=sku, type='consu', is_storable=True, tracking='lot'))
    lot = env['stock.lot'].create(dict(name=f'LOT-{pallet}', product_id=product.id, company_id=env.company.id))
    package = env['stock.package'].create(dict(name=f'DEMO-{pallet}'))
    receipt = pallets._new_picking(env, product, 1, env.ref('stock.stock_location_suppliers'),
                                   buffer, warehouse.in_type_id, f'DEMO-{pallet}')
    pallets.create(dict(run_id='DEMO', pallet_id=pallet, product_id=product.id, lot_id=lot.id,
                        package_id=package.id, quantity=1, storage_group=group,
                        quality=quality, receipt_id=receipt.id))
env.cr.commit()
print(f'Seed verified: {pallets.search_count([("run_id", "=", "DEMO")])} expected pallets')
