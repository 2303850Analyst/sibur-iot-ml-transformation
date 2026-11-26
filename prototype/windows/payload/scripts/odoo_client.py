import json
import os
import uuid
from datetime import UTC, datetime

import requests


class Odoo:
    def __init__(self, role):
        self.url = os.getenv('ODOO_URL', 'http://127.0.0.1:18069')
        self.http = requests.Session()
        response = self.http.post(self.url + '/web/session/authenticate', json={
            'jsonrpc': '2.0', 'method': 'call', 'params': {'db': 'wms_reference', 'login': f'research-{role}',
                                                         'password': os.environ[f'{role.upper()}_PASSWORD']},
        }, timeout=15)
        if not response.json().get('result', {}).get('uid'):
            raise RuntimeError(f'Odoo authentication failed for {role}')
        self.csrf = self.http.get(self.url + '/wms/session', timeout=15).json()['csrf']

    def event(self, message):
        response = self.http.post(self.url + '/wms/event', data={
            'csrf_token': self.csrf, 'message': json.dumps(message)}, timeout=20)
        if response.status_code not in (200, 409, 422):
            response.raise_for_status()
        return response.json()

    def status(self, pallet_id, run_id='DEMO'):
        response = self.http.get(self.url + '/wms/status', params=dict(pallet_id=pallet_id, run_id=run_id), timeout=20)
        response.raise_for_status()
        return response.json()


def envelope(pallet, operation, payload=None, device='tsd-test', run='DEMO', event_id=None):
    return dict(run_id=run, pallet_id=pallet, event_id=event_id or uuid.uuid4().hex,
                device_id=device, schema_version=1, operation=operation, payload=payload or {},
                occurred_at=datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S'))
