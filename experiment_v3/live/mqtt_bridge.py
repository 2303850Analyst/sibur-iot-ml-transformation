"""Signed device -> real broker -> persistent inbox -> idempotent Odoo command.
QoS 1 is acknowledged only after SQLite commit. Re-delivery reuses the event ID.
"""
import hashlib,hmac,json,sqlite3,threading,time,uuid
from pathlib import Path
import paho.mqtt.client as mqtt
from check_http import ROOT,Terminal,secret

TOPIC='wms/v3/input/'
ROUTE={'arrival':('sensor',2),'production':('production_adapter',1),'quality':('quality_adapter',9),'exchange':('exchange',24)}
def signed(channel,msg):
    raw=json.dumps(msg,sort_keys=True,separators=(',',':'))
    key=hmac.digest(secret(),channel.encode(),'sha256')
    return json.dumps({'message':msg,'signature':hmac.new(key,raw.encode(),hashlib.sha256).hexdigest()})
class Bridge:
    def __init__(self,path):
        self.path=path
        path.parent.mkdir(parents=True,exist_ok=True)
        self.ready=threading.Event()
        self.sessions={}
        self.stop=threading.Event()
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS inbox(id TEXT PRIMARY KEY,channel TEXT NOT NULL,body TEXT NOT NULL,status INTEGER,reply TEXT,attempts INTEGER DEFAULT 0)")
        self.client=mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,client_id='wms-v3-durable-bridge',clean_session=False,manual_ack=True)
        self.client.on_connect=self.connected
        self.client.on_subscribe=lambda *args:self.ready.set()
        self.client.on_message=self.received
    def db(self):return sqlite3.connect(self.path,timeout=20)
    def connected(self,client,userdata,flags,reason,properties):
        if not reason.is_failure:client.subscribe(TOPIC+'+',qos=1)
    def received(self,client,userdata,packet):
        channel=packet.topic.removeprefix(TOPIC)
        try:
            if channel not in ROUTE or len(packet.payload)>12000:raise ValueError()
            wire=json.loads(packet.payload)
            msg=wire['message']
            if not hmac.compare_digest(wire['signature'],json.loads(signed(channel,msg))['signature']):raise ValueError()
            if (msg['role'],msg['operation'])!=ROUTE[channel]:raise ValueError()
            body=json.dumps(msg,sort_keys=True)
            with self.db() as db:
                old=db.execute('SELECT body FROM inbox WHERE id=?',(msg['event_id'],)).fetchone()
                if old and old[0]!=body:raise ValueError()
                db.execute('INSERT OR IGNORE INTO inbox(id,channel,body) VALUES(?,?,?)',(msg['event_id'],channel,body))
        except (ValueError,TypeError,KeyError):
            pass
        client.ack(packet.mid,packet.qos)
    def worker(self):
        while not self.stop.is_set():
            with self.db() as db:
                row=db.execute('SELECT id,channel,body FROM inbox WHERE status IS NULL ORDER BY attempts,rowid LIMIT 1').fetchone()
            if not row:
                self.stop.wait(.05);continue
            key,channel,body=row
            role=ROUTE[channel][0]
            try:
                if role not in self.sessions:self.sessions[role]=Terminal(role)
                code,reply=self.sessions[role].step(json.loads(body))
                if code>=500 or code in (401,403):raise RuntimeError('retry')
                with self.db() as db:
                    db.execute('UPDATE inbox SET status=?,reply=?,attempts=attempts+1 WHERE id=?',(code,json.dumps(reply),key))
            except Exception:
                self.sessions.pop(role,None)
                with self.db() as db:db.execute('UPDATE inbox SET attempts=attempts+1 WHERE id=?',(key,))
                self.stop.wait(.5)
    def __enter__(self):
        self.client.connect('127.0.0.1',18893)
        self.client.loop_start()
        if not self.ready.wait(15):raise RuntimeError('broker subscribe timeout')
        self.thread=threading.Thread(target=self.worker,daemon=True)
        self.thread.start()
        return self
    def __exit__(self,*args):
        self.stop.set();self.thread.join(65);self.client.disconnect();self.client.loop_stop()
    def submit(self,channel,msg):
        payload=signed(channel,msg)
        self.client.publish(TOPIC+channel,payload,qos=1).wait_for_publish(timeout=10)
        limit=time.monotonic()+40
        while time.monotonic()<limit:
            with self.db() as db:
                row=db.execute('SELECT status,reply FROM inbox WHERE id=?',(msg['event_id'],)).fetchone()
            if row and row[0] is not None:return row[0],json.loads(row[1])
            time.sleep(.05)
        raise TimeoutError('Odoo acknowledgement pending')

