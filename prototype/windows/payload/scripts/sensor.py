"""Publish an emulated reader observation on the demo's private Docker network."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from paho.mqtt.publish import single
from odoo_client import envelope

class Reader(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/scan":
            self.send_error(404)
            return
        event = envelope("IOT-01", "observe", {"barcode": "IOT-01"}, device="demo-sscc")
        try:
            single("research/wms/events", json.dumps(event), hostname="broker", qos=1)
        except OSError:
            self.send_error(503)
            return
        body = json.dumps({"event_id": event["event_id"]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

ThreadingHTTPServer(("0.0.0.0", 8080), Reader).serve_forever()
