import itertools
import json
import os
import time
from pathlib import Path

import gevent
from locust import HttpUser, constant_pacing, task
from locust.env import Environment

from odoo_client import Odoo, envelope


ROOT = Path('artifacts/api_benchmark')
RUN = ''
TICKS = itertools.count()
PALLETS = itertools.count(1)


class Operator(HttpUser):
    host = os.environ.get('ODOO_URL', 'http://127.0.0.1:18069')
    wait_time = constant_pacing(1.5)

    def on_start(self):
        service = Odoo('tsd')
        self.client.cookies.update(service.http.cookies)
        self.csrf = service.csrf

    def submit(self, pallet, operation, payload=None):
        message = envelope(pallet, operation, payload, run=RUN, device='locust')
        with self.client.post('/wms/event', data={'csrf_token': self.csrf, 'message': json.dumps(message)},
                              name=operation, catch_response=True, timeout=12) as response:
            if response.status_code == 200:
                data = response.json()
                if data.get('status') == 'applied':
                    return data
            elif response.status_code in (409, 422):
                COUNTS['business_rejections'] += 1
                response.failure('business rejection')
                return None
            COUNTS['write_technical_errors'] += 1
            response.failure('unexpected transport or protocol response')
            return None

    @task
    def scan(self):
        if next(TICKS) % 20:
            self.client.get(f'/wms/status?run_id={RUN}&pallet_id=P-000', name='status', timeout=12)
            return
        index = next(PALLETS)
        if index > 130:
            COUNTS['fixture_exhausted'] += 1
            return
        pallet = f'P-{index:03d}'
        payload = dict(sku='REF-01', lot=f'{RUN}-{pallet}', quantity=1)
        if not self.submit(pallet, 'receive', payload):
            return
        reserved = self.submit(pallet, 'reserve')
        if reserved and self.submit(pallet, 'place', {'address': reserved['snapshot']['reserved']}):
            COUNTS['completed_pallets'] += 1


def reset_counters():
    return dict(completed_pallets=0, business_rejections=0, write_technical_errors=0, fixture_exhausted=0)


def main():
    global RUN, TICKS, PALLETS, COUNTS
    namespace = os.environ['BENCHMARK_NAMESPACE']
    ROOT.mkdir(parents=True, exist_ok=True)
    results = []
    for count in (1, 5, 20):
        RUN = f'{namespace}-{count}'
        TICKS, PALLETS, COUNTS = itertools.count(), itertools.count(1), reset_counters()
        environment = Environment(user_classes=[Operator], stop_timeout=3)
        runner = environment.create_local_runner()
        runner.start(count, spawn_rate=count)
        gevent.sleep(30)
        environment.stats.reset_all()
        COUNTS = reset_counters()
        start = time.monotonic()
        gevent.sleep(120)
        elapsed = time.monotonic() - start
        entries = [dict(operation=entry.name, requests=entry.num_requests, failures=entry.num_failures,
                        p95_ms=entry.get_response_time_percentile(.95))
                   for entry in environment.stats.entries.values() if entry.num_requests]
        totals = environment.stats.total
        results.append(dict(users=count, elapsed_s=elapsed, http_requests=totals.num_requests, http_errors=totals.num_failures,
                            p95_ms=totals.get_response_time_percentile(.95), by_operation=entries, **COUNTS))
        runner.quit()
        (ROOT / 'status.json').write_text(json.dumps(dict(evidence='API_BENCHMARK', status='RUNNING', levels=results), indent=2))
    valid = all(row['completed_pallets'] > 0 and not row['fixture_exhausted'] and not row['write_technical_errors']
                for row in results)
    (ROOT / 'status.json').write_text(json.dumps(dict(evidence='API_BENCHMARK', status='PASS' if valid else 'FAIL',
        levels=results, warmup_s=30, measurement_s=120, requests='19 status tasks / 1 three-call stock workflow',
        warning='Host API only; not physical warehouse capacity. A request in flight across the warmup boundary may be counted.'), indent=2))


if __name__ == '__main__':
    main()
