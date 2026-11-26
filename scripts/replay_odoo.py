import gzip
import json
import os
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path('/lab/replay')
NAMESPACE = os.environ['REPLAY_NAMESPACE']
SHARD = int(os.getenv('REPLAY_SHARD', '0'))
SHARDS = int(os.getenv('REPLAY_SHARDS', '1'))
warehouse = env['stock.warehouse'].search([], limit=1)
locations = env['stock.location']
pallets = env['wms.research.pallet']
cell_model = env['wms.research.cell']
supplier = env.ref('stock.stock_location_suppliers')
customer = env.ref('stock.stock_location_customers')
products = env['product.product']


def seed_cells():
    existing = cell_model.search([('run_id', '=', 'REPLAY-POOL')])
    if existing:
        assert len(existing) == 5329
        return {int(row.name.split('-')[-1]): row for row in existing}
    names = [f'Replay-{index:04d}' for index in range(5329)]
    places = locations.create([dict(name=name, usage='internal', location_id=warehouse.lot_stock_id.id) for name in names])
    distances = [40 + 20 * index / 2599 for index in range(2600)] + [80 + 20 * index / 2599 for index in range(2600)] + [
        30 + 10 * index / 128 for index in range(129)]
    made = cell_model.create([dict(run_id='REPLAY-POOL', name=name, location_id=place.id, distance=distance,
                                    zone='quarantine' if index >= 5200 else 'storage', storage_group=min(index // 2600, 1))
                              for index, (name, place, distance) in enumerate(zip(names, places, distances))])
    env.cr.commit()
    return dict(enumerate(made))


def run_case(case, addresses):
    run = f'{NAMESPACE}-{case["source"]}'
    buffer = locations.create(dict(name=f'{run}-Buffer', usage='internal', location_id=warehouse.lot_stock_id.id))
    transit = locations.create(dict(name=f'{run}-Transit', usage='internal', location_id=warehouse.lot_stock_id.id))
    records = {}
    for row in case['expected']:
        index, sku = row['id'], row['sku']
        code = f'SKU-{sku:02d}'
        product_name = f'{run}-{code}'
        product = products.search([('name', '=', product_name)], limit=1) or products.create(dict(
            name=product_name, default_code=code, type='consu', is_storable=True, tracking='lot'))
        lot = env['stock.lot'].search([('product_id', '=', product.id), ('name', '=', f'LOT-{index}')], limit=1)
        if not lot:
            lot = env['stock.lot'].create(dict(name=f'LOT-{index}', product_id=product.id, company_id=env.company.id))
        ledger = f'{run}-P{index}'
        package = env['stock.package'].create(dict(name=ledger))
        receipt = pallets._new_picking(env, product, 1, supplier, buffer, warehouse.in_type_id, ledger)
        records[index] = pallets.create(dict(run_id=run, pallet_id=f'P{index}', product_id=product.id, lot_id=lot.id,
            package_id=package.id, quantity=1, storage_group=sku // 10, quality=row['quality'], receipt_id=receipt.id))
    audit = []
    for offset, step in enumerate(case['events']):
        record = records[step['pallet']]
        operation = step['operation']
        address = addresses[step['cell']] if 'cell' in step else None
        if operation == 'outbound':
            assert record.cell_id.id == address.id and record.quality == 'released'
            transfer = pallets._new_picking(env, record.product_id, 1, address.location_id, customer,
                                            warehouse.out_type_id, record.pallet_id)
            record._finish(transfer, True)
            record.cell_id = False
            audit.append(dict(operation=operation, picking_id=transfer.id, pallet=record.pallet_id,
                              end=record._snapshot()))
            continue
        payload = record._expected() if operation == 'receive' else {'address': address.name} if operation.endswith('place') else {}
        box = dict(reference_cell_pool='REPLAY-POOL', reference_serial_replay=True)
        if operation == 'receive':
            box.update(reference_buffer_capacity=case['capacity'], reference_physical_buffer=step['physical_buffer'])
        elif operation == 'place':
            box['reference_transit_id'] = transit.id
        if 'available' in step:
            box['reference_available'] = [addresses[index].id for index in step['available']]
        message = dict(run_id=run, pallet_id=record.pallet_id, event_id=f'{run}-{offset}', device_id='replay', schema_version=1,
                       occurred_at=(datetime(2000, 1, 1) + timedelta(seconds=step['virtual_s'])).strftime('%Y-%m-%d %H:%M:%S'),
                       operation=operation, payload=payload)
        result = pallets.with_context(**box)._apply(message, 'QUALITY' if operation == 'release' else 'TSD')
        snapshot = result['snapshot']
        assert result['status'] == 'applied', (step, result)
        if operation.endswith('reserve'):
            assert snapshot['reserved'] == address.name, (step, result)
        if operation.endswith('place'):
            assert snapshot['cell'] == address.name and snapshot['quants'] == [[address.name, record.lot_id.name, 1]], (step, result)
        if operation == 'receive':
            assert snapshot['receipt_state'] == 'done' and snapshot['quants'] == [[buffer.name, record.lot_id.name, 1]], (step, result)
        if 'quality' in step:
            assert step['quality'] == snapshot['quality'], (step, result)
        audit.append(dict(operation=operation, pallet=record.pallet_id, canonical=step, result=result))
        if operation == 'reserve':
            transfer = pallets._new_picking(env, record.product_id, 1, buffer, transit,
                                            warehouse.int_type_id, record.pallet_id)
            record._finish(transfer, True)
            transit_snapshot = record._snapshot()
            assert transit_snapshot['quants'] == [[transit.name, record.lot_id.name, 1]]
            audit.append(dict(operation='physical_dispatch', pallet=record.pallet_id, picking_id=transfer.id,
                              end=transit_snapshot))
    env.cr.commit()
    return audit


address_records = seed_cells()
completed = []
for path in sorted((ROOT / 'input').glob('*.json.gz'))[SHARD::SHARDS]:
    with gzip.open(path, 'rt', encoding='utf-8') as source:
        fixture = json.load(source)
    audit = run_case(fixture, address_records)
    key = fixture['source']
    with gzip.open(ROOT / f'{key}.audit.json.gz', 'wt', encoding='utf-8') as trace:
        json.dump(dict(evidence='ODOO_INTEGRATION', namespace=NAMESPACE, case=key, audit=audit), trace)
    completed.append(dict(case=key, pallets=len(fixture['expected']), transitions=len(audit)))
    (ROOT / f'shard-{SHARD}.json').write_text(json.dumps(dict(evidence='ODOO_INTEGRATION', namespace=NAMESPACE, cases=completed,
        status='PASS_SHARD',
        limitations='Full unselected background occupancy is a trace-derived availability projection; sampled stock transitions use native Odoo stock ORM.'), indent=2))
    print(f'Replay {len(completed)}/54: {key}', flush=True)
