import gzip
import hashlib
import json
from collections import Counter
from itertools import product
from pathlib import Path


ROOT = Path('artifacts/replay')


def read_trace(path):
    with gzip.open(path, 'rt', encoding='utf-8') as stream:
        return json.load(stream)


def verify():
    receipt = json.loads((ROOT / 'status.json').read_text(encoding='utf-8-sig'))
    assert receipt['status'] == 'PASS_PROJECTED_REPLAY'
    names = {f'S{scenario}-L{load}-1000-{architecture}'
             for scenario, load, architecture in product(range(1, 7), range(1, 4), 'ABC')}
    cases = {row['case']: row for row in receipt['cases']}
    assert len(receipt['cases']) == 54 and cases.keys() == names
    counts = Counter()
    hashes = {}
    for name in sorted(names):
        fixture = read_trace(ROOT / 'input' / f'{name}.json.gz')
        path = ROOT / f'{name}.audit.json.gz'
        result = read_trace(path)
        assert result['namespace'] == receipt['namespace'] and result['case'] == fixture['source'] == name
        expected = {f'P{pallet["id"]}': f'SKU-{pallet["sku"]:02d}' for pallet in fixture['expected']}
        assert len(expected) == cases[name]['pallets'] == 50
        audit = result['audit']
        actual = [row for row in audit if row['operation'] != 'physical_dispatch']
        assert len(audit) == cases[name]['transitions'] and len(actual) == len(fixture['events'])
        received = set()
        for step, row in zip(fixture['events'], actual):
            pallet = row['pallet']
            operation = row['operation']
            assert pallet == f'P{step["pallet"]}' and operation == step['operation']
            counts[operation] += 1
            if operation == 'outbound':
                assert row['end']['cell'] is None and row['picking_id']
                continue
            assert row['canonical'] == step and row['result']['status'] == 'applied'
            snapshot = row['result']['snapshot']
            assert snapshot['sku'] == expected[pallet] and snapshot['lot'] == f'LOT-{step["pallet"]}'
            assert all(quant[1] == snapshot['lot'] and quant[2] > 0 for quant in snapshot['quants'])
            if operation == 'receive':
                assert pallet not in received and snapshot['receipt_state'] == 'done'
                received.add(pallet)
        assert sum(row['operation'] == 'reserve' for row in actual) == len(audit) - len(actual)
        counts['physical_dispatch'] += len(audit) - len(actual)
        counts['selected_pallets'] += len(expected)
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (ROOT / 'verification.json').write_text(json.dumps(dict(evidence='ODOO_INTEGRATION', status='PASS',
        namespace=receipt['namespace'], verified_cases=len(cases), counts=counts, audit_sha256=hashes), indent=2))


if __name__ == '__main__':
    verify()
