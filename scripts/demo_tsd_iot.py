import argparse
import json
import os
import time
from pathlib import Path

from paho.mqtt.publish import single

from odoo_client import Odoo, envelope


OUTPUT = Path('artifacts/integration_results.json')


def publish(message):
    single('research/wms/events', json.dumps(message), qos=1, retain=False,
           hostname=os.getenv('MQTT_HOST', '127.0.0.1'), port=int(os.getenv('MQTT_PORT', '1883')))


def wait_event(api, pallet, event_id):
    for attempt in range(50):
        data = api.status(pallet)
        if any(event[6] == event_id for event in data['audit']):
            return data
        time.sleep(.4)
    raise AssertionError(f'MQTT event not delivered for {pallet}')


def exercise():
    api = Odoo('tsd')
    quality = Odoo('quality')
    results = []

    def step(pallet, name, operation, payload=None, *, account=api, expected='applied'):
        before = api.status(pallet)
        result = account.event(envelope(pallet, operation, payload))
        after = api.status(pallet)
        record = dict(case=name, before=before['snapshot'], after=after['snapshot'], result=result, audit=after['audit'])
        record['pass'] = result.get('status') == expected and bool(result.get('audit_id')) and all(
            quant[2] > 0 for quant in after['snapshot']['quants'])
        results.append(record)
        save(results, 'RUNNING')
        assert record['pass'], record
        return result

    def identity(pallet):
        snapshot = api.status(pallet)['snapshot']
        return {key: snapshot[key] for key in ('sku', 'lot', 'quantity')}

    page = api.http.get(api.url + '/wms/tsd', timeout=10)
    assert page.ok and 'crypto.randomUUID' in page.text
    pallet = 'TSD-01'
    original = identity(pallet)
    step(pallet, 'wrong_lot', 'receive', {**original, 'lot': 'WRONG'}, expected='rejected')
    first = step(pallet, 'tsd_receive', 'receive', original)
    duplicate = step(pallet, 'new_event_same_business_key', 'receive', original)
    assert first == duplicate
    step(pallet, 'conflicting_quantity', 'receive', {**original, 'quantity': 2}, expected='conflict')
    reserve = step(pallet, 'tsd_reserve', 'reserve')['snapshot']['reserved']
    step(pallet, 'wrong_address', 'place', {'address': 'B-01'}, expected='rejected')
    assert step(pallet, 'tsd_place', 'place', {'address': reserve})['snapshot']['quants'] == [[reserve, original['lot'], 1]]

    pallet = 'IOT-01'
    observed = envelope(pallet, 'observe', {'barcode': pallet}, 'scanner', event_id='DEMO-SCAN-IOT-01')
    before = api.status(pallet)['snapshot']
    publish(observed)
    observation = wait_event(api, pallet, observed['event_id'])
    publish(observed)
    presence = envelope(pallet, 'presence', {'occupied': 1}, 'presence', event_id='DEMO-PRESENCE-01')
    publish(presence)
    checked = wait_event(api, pallet, presence['event_id'])
    assert len([event for event in checked['audit'] if event[6] == observed['event_id']]) == 1
    assert not observation['snapshot']['quants'] or before['receipt_state'] == 'done'
    results.append(dict(case='mqtt_duplicate_and_presence', before=before, after=checked['snapshot'], audit=checked['audit'], **{'pass': True}))
    step(pallet, 'receive_after_observation', 'receive', identity(pallet))
    reserve = step(pallet, 'iot_reserve_via_tsd', 'reserve')['snapshot']['reserved']
    step(pallet, 'iot_confirm_via_tsd', 'place', {'address': reserve})

    pallet = 'HOLD-01'
    step(pallet, 'receive_hold', 'receive', identity(pallet))
    step(pallet, 'reserve_hold', 'reserve')
    step(pallet, 'hold_cannot_place_in_storage', 'place', {'address': 'B-01'}, expected='rejected')
    step(pallet, 'quarantine', 'place', {'address': 'Q-01'})
    step(pallet, 'authorized_release', 'release', account=quality)
    step(pallet, 'reserve_after_release', 'final_reserve')
    assert step(pallet, 'store_after_release', 'final_place', {'address': 'B-01'})['snapshot']['cell'] == 'B-01'
    pallet = 'LATE-01'
    step(pallet, 'out_of_order', 'place', {'address': 'A-01'}, expected='rejected')
    step(pallet, 'missing_scan_tsd_fallback', 'receive', identity(pallet))
    delayed = envelope(pallet, 'observe', {'barcode': pallet}, 'scanner', event_id='DEMO-DELAYED-01')
    publish(delayed)
    checked = wait_event(api, pallet, delayed['event_id'])
    assert sum(quant[2] for quant in checked['snapshot']['quants']) == 1
    results.append(dict(case='delayed_scan_after_tsd', audit=checked['audit'], after=checked['snapshot'], **{'pass': True}))
    step(pallet, 'full_compatible_storage', 'reserve', expected='rejected')
    save(results, 'PASS_CORE_ONLY')


def save(results, status):
    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_text(json.dumps(dict(evidence='ODOO_INTEGRATION', status=status, cases=results), indent=2), encoding='utf-8')


def restart_probe(action):
    api = Odoo('tsd')
    pallet = 'IOT-01'
    event = envelope(pallet, 'observe', {'barcode': pallet}, 'scanner', event_id='DEMO-SCAN-IOT-01')
    probe = envelope(pallet, 'presence', {'occupied': 0}, 'presence', event_id='DEMO-RESTART-PROBE')
    if action == 'queue':
        publish(event)
        publish(probe)
        return
    checked = wait_event(api, pallet, probe['event_id'])
    assert sum(quant[2] for quant in checked['snapshot']['quants']) == 1
    assert sum(row[2:4] == ['receive', 'applied'] for row in checked['audit']) == 1
    results = json.loads(OUTPUT.read_text())['cases']
    results.append(dict(case='gateway_down_queue_then_broker_and_gateway_restart', audit=checked['audit'],
                        after=checked['snapshot'], **{'pass': True}))
    save(results, 'PASS_DEMO_AND_RESTART')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=('demo', 'queue', 'verify'), default='demo', nargs='?')
    phase = parser.parse_args().phase
    exercise() if phase == 'demo' else restart_probe(phase)
