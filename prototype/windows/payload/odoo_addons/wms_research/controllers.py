import json
import math
import re
from datetime import datetime
from pathlib import Path

from odoo import http
from odoo.http import request


OPERATIONS = {
    'TSD': {'receive', 'reserve', 'place', 'final_reserve', 'final_place'},
    'IOT': {'observe', 'presence'},
    'QUALITY': {'release'},
}


def valid(message):
    try:
        if type(message['schema_version']) is not int or message['schema_version'] != 1:
            return False
        for name in ('pallet_id', 'run_id', 'event_id', 'device_id'):
            if not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,100}', message[name]):
                return False
        datetime.strptime(message['occurred_at'], '%Y-%m-%d %H:%M:%S')
        payload = message['payload']
        operation = message['operation']
        if not isinstance(payload, dict):
            return False
        if operation == 'receive':
            quantity = payload['quantity']
            return (type(quantity) in (int, float) and math.isfinite(quantity) and quantity > 0
                    and all(isinstance(payload[name], str) for name in ('sku', 'lot')))
        if operation == 'presence':
            return type(payload['occupied']) is int and 0 <= payload['occupied'] <= 40
        if operation.endswith('place'):
            return isinstance(payload['address'], str)
        return True
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


class ResearchController(http.Controller):
    def _roles(self):
        suffix = {'TSD': 'tsd', 'IOT': 'gateway', 'QUALITY': 'quality'}
        return {role for role, name in suffix.items() if request.env.user.has_group(f'wms_research.role_{name}')}

    @http.route('/wms/tsd', type='http', auth='user', methods=['GET'])
    def screen(self):
        if 'TSD' not in self._roles():
            return request.make_json_response({'status': 'forbidden'}, status=403)
        markup = (Path(__file__).parent / 'static' / 'tsd.html').read_text(encoding='utf-8')
        return request.make_response(markup, headers=[('Content-Type', 'text/html; charset=utf-8')])

    @http.route('/wms/session', type='http', auth='user', methods=['GET'])
    def session(self):
        roles = sorted(self._roles())
        if not roles:
            return request.make_json_response({'status': 'forbidden'}, status=403)
        return request.make_json_response({'roles': roles, 'csrf': request.csrf_token()})

    @http.route('/wms/status', type='http', auth='user', methods=['GET'])
    def status(self, run_id='', pallet_id=''):
        if not self._roles() & {'TSD', 'QUALITY'}:
            return request.make_json_response({'status': 'forbidden'}, status=403)
        env = request.env['wms.research.pallet'].sudo()
        pallet = env.search([('run_id', '=', run_id), ('pallet_id', '=', pallet_id)], limit=1)
        if not pallet:
            return request.make_json_response({'status': 'unknown_pallet'}, status=404)
        events = request.env['wms.research.event'].sudo().search([
            ('run_id', '=', run_id), ('pallet_id', '=', pallet_id)], order='id')
        audit = [[entry.id, entry.source, entry.operation, entry.status, entry.reason or None,
                  entry.picking_id.id or None, entry.event_id, str(entry.received_at), str(entry.processed_at)]
                 for entry in events]
        return request.make_json_response({'snapshot': pallet._snapshot(), 'audit': audit})

    @http.route('/wms/event', type='http', auth='user', methods=['POST'], csrf=True)
    def event(self, message=''):
        try:
            parsed = json.loads(message) if len(message) <= 8192 else None
            if not isinstance(parsed, dict) or not valid(parsed):
                raise ValueError('invalid envelope')
            operation = parsed['operation']
        except (ValueError, TypeError, KeyError):
            return request.make_json_response({'status': 'invalid_message'}, status=400)
        role = next((item for item in self._roles() if operation in OPERATIONS[item]), None)
        if not role:
            return request.make_json_response({'status': 'forbidden'}, status=403)
        result = request.env['wms.research.pallet'].sudo()._apply(parsed, role)
        return request.make_json_response(result, status={'applied': 200, 'rejected': 422, 'conflict': 409}[result['status']])
