"""Local prototype entry point. Installed only in the independently packaged demo."""
import hashlib
import os
import re
import time
from html import escape
from pathlib import Path

import requests
from odoo import http
from odoo.http import request


class LocalPrototype(http.Controller):
    @http.route("/wms/demo", type="http", auth="public", methods=["GET"])
    def launch(self):
        csrf = escape(request.csrf_token(), quote=True)
        page = f"""<!doctype html><html lang="ru"><meta charset="utf-8">
        <title>Открыть ТСД</title><p id="message">Открываем рабочее место приёмщика…</p>
        <form method="post" action="/wms/demo/login"><input type="hidden" name="csrf_token" value="{csrf}">
        <input type="hidden" name="ticket"></form>
        <script>const token=location.hash.slice(1); history.replaceState(null,'','/wms/demo');
        if(token){{document.forms[0].elements.ticket.value=token;document.forms[0].submit();}}
        else document.getElementById('message').textContent='Для входа запустите PSV-Prototype.exe.';</script></html>"""
        return request.make_response(page, headers=[("Content-Type", "text/html; charset=utf-8"),
            ("Cache-Control", "no-store"), ("Referrer-Policy", "no-referrer")])

    @http.route("/wms/demo/login", type="http", auth="public", methods=["POST"], csrf=True)
    def login(self, ticket=""):
        if re.fullmatch(r"[A-Za-z0-9_-]{43}", ticket):
            key = hashlib.sha256(ticket.encode()).hexdigest()
            marker = Path("/tmp/psv-demo-tickets") / key
            try:
                age = time.time() - marker.stat().st_mtime
                marker.unlink()
            except FileNotFoundError:
                age = float("inf")
            if 0 <= age <= 120:
                request.session.authenticate(request.env, {
                    "type": "password", "login": "research-tsd", "password": os.environ["TSD_PASSWORD"]})
                return request.redirect("/wms/tsd")
        return request.make_response("Ссылка истекла. Запустите PSV-Prototype.exe ещё раз.", status=403)

    @http.route("/wms/demo/scan", type="http", auth="user", methods=["POST"], csrf=True)
    def scan(self):
        if not request.env.user.has_group("wms_research.role_tsd"):
            return request.make_json_response({"status": "forbidden"}, status=403)
        try:
            response = requests.post("http://sensor:8080/scan", timeout=8)
            response.raise_for_status()
            return request.make_json_response(response.json())
        except requests.RequestException:
            return request.make_json_response({"status": "sensor_unavailable"}, status=503)
