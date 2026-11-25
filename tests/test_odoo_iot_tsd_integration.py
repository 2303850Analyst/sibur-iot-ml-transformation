import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from scripts.odoo_client import Odoo, envelope


pytestmark = pytest.mark.skipif(os.getenv('RUN_LIVE_ODOO') != '1', reason='requires the dedicated live Odoo test database')


def test_concurrent_receipt_and_restricted_roles():
    first, second = Odoo('tsd'), Odoo('tsd')
    pallet = 'FULL-01'
    status = first.status(pallet)
    record = status['snapshot']
    payload = {field: record[field] for field in ('lot', 'sku', 'quantity')}
    barrier = Barrier(2)

    def receive(client):
        barrier.wait(timeout=5)
        return client.event(envelope(pallet, 'receive', payload))

    with ThreadPoolExecutor(2) as executor:
        output = list(executor.map(receive, (first, second)))
    snapshot = first.status(pallet)
    assert output[0] == output[1] and output[0]['status'] == 'applied'
    assert len(snapshot['snapshot']['quants']) == 1 and snapshot['snapshot']['quants'][0][2] == 1
    assert sum(entry[2:4] == ['receive', 'applied'] for entry in snapshot['audit']) == 1
    request = dict(csrf_token=first.csrf, message=json.dumps(envelope(pallet, 'release')))
    denied = first.http.post(first.url + '/wms/event', data=request, timeout=10)
    assert denied.status_code == 403
    report = Path('artifacts/api_concurrency.json')
    report.write_text(json.dumps(dict(evidence='ODOO_INTEGRATION', status='PASS', before=status, after=snapshot,
                         response=output, unauthorized_release_status=denied.status_code), indent=2))
