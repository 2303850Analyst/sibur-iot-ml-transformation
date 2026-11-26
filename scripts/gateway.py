import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import paho.mqtt.client as mqtt

from odoo_client import Odoo


DATABASE = Path('/lab/outbox/gateway.sqlite')
TOPIC = 'research/wms/events'


def connection():
    return sqlite3.connect(DATABASE, timeout=10)


def event_payload(message):
    if len(message.payload) > 8192 or message.retain:
        raise ValueError('retained or oversized message')
    body = json.loads(message.payload)
    if set(body) != {'run_id', 'event_id', 'pallet_id', 'operation', 'device_id', 'occurred_at', 'schema_version', 'payload'}:
        raise ValueError('invalid fields')
    datetime.strptime(body['occurred_at'], '%Y-%m-%d %H:%M:%S')
    if body['operation'] not in ('observe', 'presence') or type(body['schema_version']) is not int or body['schema_version'] != 1:
        raise ValueError('invalid operation or version')
    if not isinstance(body['payload'], dict):
        raise ValueError('invalid payload')
    if body['operation'] == 'presence' and (type(body['payload'].get('occupied')) is not int or not 0 <= body['payload']['occupied'] <= 40):
        raise ValueError('invalid presence count')
    for name in ('run_id', 'event_id', 'pallet_id', 'device_id'):
        import re
        if not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,100}', body[name]):
            raise ValueError('invalid identifier')
    return body


def receive(client, userdata, message):
    try:
        body = event_payload(message)
        with connection() as database:
            database.execute('INSERT INTO pending (body) VALUES (?)', (json.dumps(body),))
    except (ValueError, TypeError, KeyError) as error:
        with connection() as database:
            database.execute('INSERT INTO rejected (reason) VALUES (?)', (str(error),))
    client.ack(message.mid, message.qos)


def main():
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    with connection() as database:
        database.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS pending(id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS delivered(id INTEGER PRIMARY KEY, event_id TEXT, response TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS rejected(id INTEGER PRIMARY KEY, reason TEXT NOT NULL);
        ''')
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='wms-research-outbox', clean_session=False,
                         protocol=mqtt.MQTTv311, manual_ack=True)
    client.on_connect = lambda connection, userdata, flags, result, properties: connection.subscribe(TOPIC, qos=1)
    client.on_message = receive
    client.connect_async(os.getenv('MQTT_HOST', '127.0.0.1'), int(os.getenv('MQTT_PORT', '1883')))
    client.loop_start()
    api = None
    while True:
        with connection() as database:
            item = database.execute('SELECT id,body FROM pending ORDER BY id LIMIT 1').fetchone()
        if not item:
            time.sleep(.2)
            continue
        row, raw = item
        message = json.loads(raw)
        try:
            api = api or Odoo('gateway')
            result = api.event(message)
            if result.get('status') not in ('applied', 'rejected', 'conflict'):
                raise RuntimeError('invalid application acknowledgment')
            with connection() as database:
                database.execute('INSERT INTO delivered VALUES (?,?,?)', (row, message['event_id'], json.dumps(result)))
                database.execute('DELETE FROM pending WHERE id=?', (row,))
            print(json.dumps(dict(event_id=message['event_id'], odoo_audit_id=result.get('audit_id'), status=result['status'])))
        except Exception as error:
            print(f'Application acknowledgment pending: {type(error).__name__}')
            api = None
            time.sleep(2)


if __name__ == '__main__':
    main()
