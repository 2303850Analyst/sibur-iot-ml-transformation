import json
from pathlib import Path
import sys
import time

sys.path.insert(0,'/lab/scripts')
from odoo_client import Odoo,envelope
from demo_tsd_iot import publish

root=Path('/lab/experiment_v2/results')
cases=json.loads((root/'odoo_fixtures.json').read_text())
tsd=Odoo('tsd')
quality=Odoo('quality')


def execute(case):
    key,run=case['pallet'],case['run']
    records=[]
    def send(op,payload=None,role=tsd,expected='applied'):
        message=envelope(key,op,payload,run=run)
        result=role.event(message)
        assert result['status']==expected,(op,result)
        records.append(dict(operation=op,status=expected,audit_id=result['audit_id'],snapshot=result['snapshot']))
        return result
    expected=tsd.status(key,run)['snapshot']
    assert expected['receipt_state']!='done'
    send('place',{'address':'bad'},expected='rejected')
    identity={name:expected[name] for name in ('sku','lot','quantity')}
    for event in case['events']:
        op=event['event']
        if op=='register':
            applied=send('receive',identity)
            duplicate=send('receive',identity)
            assert applied==duplicate and applied['snapshot']['receipt_state']=='done'
        elif op=='external_release':
            send('release',role=quality)
        elif op=='reserve_and_start':
            final=bool(records[-1]['snapshot']['cell'])
            response=send('final_reserve' if final else 'reserve')
            assert response['snapshot']['reserved']==f"V2-{event['cell']}"
        elif op=='confirm_place':
            confirm='place' if event['primary'] else 'final_place'
            send(confirm,{'address':'INVALID'},expected='rejected')
            address=f"V2-{event['cell']}"
            result=send(confirm,{'address':address})
            assert result['snapshot']['quants']==[[address,identity['lot'],1]]
    return dict(source_run=case['source_run'],run=run,pallet=key,records=records)

if sys.argv[1]=='paths':
    out=[execute(case) for case in cases]
    (root/'odoo_replay.json').write_text(json.dumps(dict(status='PASS',paths=out),indent=2))
else:
    case=cases[0]
    key,run=case['pallet'],case['run']
    event=envelope(key,'presence',{'occupied':1},device='v2-device',run=run,event_id=run+'-network')
    if sys.argv[1]=='queue':
        publish(event)
        publish(event)
        time.sleep(.5)
        assert not any(row[6]==event['event_id'] for row in tsd.status(key,run)['audit'])
    else:
        for attempt in range(60):
            rows=[row for row in tsd.status(key,run)['audit'] if row[6]==event['event_id']]
            if rows: break
            time.sleep(.5)
        assert len(rows)==1 and rows[0][3]=='applied'
        (root/'odoo_recovery.json').write_text(json.dumps(dict(status='PASS',event=event['event_id'],audit=rows,
            method='network disconnected from gateway, two QoS1 messages during outage, reconnect and deduplication')))
