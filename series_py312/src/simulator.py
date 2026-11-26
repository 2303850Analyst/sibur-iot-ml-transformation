import gzip
import hashlib
import heapq
import json
import math
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import simpy

from .data import DAY
from .forecast import HALF, features


WARM = DAY
STOP = 31 * DAY
END = 32 * DAY


@dataclass(slots=True)
class Pallet:
    id: int
    arrival: float
    sku: int
    source_row: int
    buffer_at: float = math.nan
    capture_at: float = math.nan
    delivered_at: float = math.nan
    registered_at: float = math.nan
    released_at: float = math.nan
    started_at: float = math.nan
    physical_end_at: float = math.nan
    placed_at: float = math.nan
    final_at: float = math.nan
    departed_at: float = math.nan
    active_operator: float = 0
    active_forklift: float = 0
    quality: str = 'released'
    state: str = 'future'
    cell: int = -1
    first_cell: int = -1
    defects: str = ''


def preview_policy(now, pressure, preparation, incoming):
    if preparation and preparation[0][0] - now <= 900:
        return 'prep'
    if pressure >= 8:
        return 'move' if incoming else None
    if preparation:
        return 'prep'
    return 'move' if incoming else None


def draws(seed, count):
    def stream(code):
        return np.random.default_rng(np.random.SeedSequence([seed, code]))

    variance = np.log1p(.15 ** 2)
    return dict(quality=stream(1).random(count), labels=stream(2).random(count), record=stream(3).random(count),
                miss=stream(4).random((count, 2)), redelivery=stream(5).random(count), reorder=stream(6).uniform(0, 30, count),
                work=stream(7).lognormal(-variance / 2, np.sqrt(variance), count),
                manual=stream(8).triangular(8, 12, 20, count), scan=stream(9).triangular(.2, .5, 1, count),
                link=stream(10).triangular(.1, .3, 1, (count, 2)), reoffer=stream(11).random(count))


class Simulation:
    def __init__(self, inbound, outbound, scenario, seed, automatic, model, train_mean, *, prep=True, log=False):
        self.env = simpy.Environment()
        self.operator = simpy.Resource(self.env, capacity=1)
        self.forklifts = simpy.Resource(self.env, capacity=2)
        self.change = self.env.event()
        self.inbound, self.outbound = inbound, outbound
        self.pallets = [Pallet(index, *row) for index, row in enumerate(inbound)]
        self.scenario, self.automatic, self.model, self.train_mean = scenario, automatic, model, train_mean
        self.prep_enabled, self.record = prep, log
        self.random = draws(seed, len(inbound))
        self.buffer_limit = 10 if scenario == 'S4' else 40
        self.buffer = simpy.Resource(self.env, self.buffer_limit)
        self.buffer_tokens = {}
        self.distances = list(np.linspace(40, 60, 2600)) + list(np.linspace(80, 100, 2600)) + list(np.linspace(30, 40, 129))
        self.owner = {}
        self.free = [[], [], []]
        self.stored_skus = [deque() for _ in range(20)]
        rng = np.random.default_rng(np.random.SeedSequence([seed, 900]))
        fill = int(2600 * (.95 if scenario == 'S5' else .70))
        for zone in range(3):
            cells = range(zone * 2600, min((zone + 1) * 2600, 5329))
            initialized = set(rng.choice(list(cells), fill, replace=False)) if zone != 2 else set()
            for cell in cells:
                if cell in initialized:
                    sku = int(rng.integers(zone * 10, zone * 10 + 10))
                    self.stored_skus[sku].append((cell, None))
                    self.owner[cell] = -1
                else:
                    self.free[zone].append(cell)
        self.initial = 2 * fill
        self.prep_queue = []
        self.move_queues = [[], [], []]
        self.pressure = 0.0
        self.events = []
        self.decisions = []
        self.preparations = []
        self.registration_outbox = 0
        self.observed = np.zeros(END // HALF + 1, dtype=np.int32)
        self.actual = np.bincount([int(row[0] // HALF) for row in inbound], minlength=END // HALF + 1)
        self.forecasts = []
        self.timeline = []
        self.out_requests = self.out_missing = self.initial_departed = 0
        self.wms_buffer = self.waiting_entry = self.first_total = 0
        self.full_seconds = self.last_buffer_time = 0.0
        self.operator_seconds = self.fork_seconds = self.prep_seconds = 0.0
        self.exposure = self.abs_error_wms = self.abs_error_estimate = 0
        self.sensor_value = None
        self.sensor_time = -1
        self.input_hash = hashlib.sha256(json.dumps([inbound, outbound], separators=(',', ':')).encode()).hexdigest()

    def log(self, action, pallet=None, **extra):
        if self.record:
            self.events.append(dict(time=round(self.env.now, 6), event=action,
                                    pallet=pallet.id if pallet is not None else None,
                                    **{name: value.item() if isinstance(value, np.generic) else value
                                       for name, value in extra.items()}))

    def notify(self):
        self.change.succeed()
        self.change = self.env.event()

    def network(self, now, index, leg=0):
        latency = self.random['link'][index, leg]
        seconds = now % DAY
        if self.scenario == 'S3' and 43200 <= seconds < 43800:
            return float(latency + 43800 - seconds + self.random['reorder'][index])
        return float(latency)

    @staticmethod
    def overlap(start, end):
        return max(0, min(end, STOP) - max(start, WARM))

    def buffer_change(self):
        if len(self.buffer_tokens) == self.buffer_limit:
            self.full_seconds += self.overlap(self.last_buffer_time, self.env.now)
        self.last_buffer_time = self.env.now

    def zone(self, pallet):
        return pallet.sku // 10 if pallet.quality == 'released' else 2

    def queue(self, pallet):
        self.ready_index[pallet.id] = pallet
        heapq.heappush(self.move_queues[self.zone(pallet)], (pallet.arrival, pallet.id))
        self.notify()

    def valid_task(self, zone, pallet):
        return pallet.state in ('ready', 'ready_final') and self.zone(pallet) == zone

    def admit(self, pallet):
        self.waiting_entry += 1
        request = self.buffer.request()
        yield request
        self.waiting_entry -= 1
        self.buffer_change()
        self.buffer_tokens[pallet.id] = request
        pallet.buffer_at = self.env.now
        pallet.state = 'buffer'
        self.log('physical_buffer', pallet)

    def release_quality(self, pallet, duration):
        yield self.env.timeout(duration)
        pallet.quality = 'released'
        self.log('external_release', pallet)
        if pallet.state == 'quarantine':
            pallet.state = 'ready_final'
            self.queue(pallet)
        elif pallet.state == 'ready':
            self.queue(pallet)

    def active_registration(self, pallet, duration):
        with self.operator.request() as request:
            yield request
            start = self.env.now
            yield self.env.timeout(duration)
            self.operator_seconds += self.overlap(start, self.env.now)
            pallet.active_operator += duration

    def pathway(self, pallet):
        index = pallet.id
        pallet.state = 'entry'
        self.log('physical_arrival', pallet, sku=pallet.sku, source_row=pallet.source_row)
        admission = self.env.process(self.admit(pallet))
        held = self.random['quality'][index] < (.20 if self.scenario == 'S5' else .05)
        pallet.released_at = pallet.arrival + (DAY if self.scenario == 'S5' else 7200) if held else pallet.arrival
        if held:
            pallet.quality = 'hold'
            self.env.process(self.release_quality(pallet, pallet.released_at - pallet.arrival))
        label = self.random['labels'][index] < (.05 if self.scenario == 'S2' else .005)
        wrong = self.scenario == 'S2' and self.random['record'][index] < .02
        manual = not self.automatic or label or wrong
        if self.automatic and not manual:
            yield self.env.timeout(float(self.random['scan'][index]))
            if self.random['miss'][index, 0] < .01:
                self.log('scanner_miss', pallet)
                yield self.env.timeout(2)
                if self.random['miss'][index, 1] < .01:
                    self.log('tsd_fallback', pallet)
                    yield self.env.timeout(60)
                    manual = True
        if label or wrong:
            pallet.defects = '+'.join(kind for kind, flag in [('label', label), ('record', wrong)] if flag)
            self.log('identity_exception', pallet, reason=pallet.defects)
        if manual:
            yield self.env.process(self.active_registration(pallet, float(self.random['manual'][index]) + (120 if label or wrong else 0)))
        pallet.capture_at = self.env.now
        self.registration_outbox += 1
        delay = self.network(self.env.now, index)
        yield self.env.timeout(delay)
        pallet.delivered_at = self.env.now
        self.registration_outbox -= 1
        self.observed[int(pallet.capture_at // HALF)] += 1
        self.log('observation_received', pallet, occurred_at=round(pallet.capture_at, 6), source='TSD' if manual else 'IOT')
        if ((self.scenario == 'S3' and delay > 1 and self.random['redelivery'][index] < .10) or
                (self.scenario == 'S2' and self.random['reoffer'][index] < .10)):
            self.log('idempotent_duplicate', pallet)
        yield admission
        yield self.env.process(self.active_registration(pallet, 20))
        assert pallet.state == 'buffer' and math.isnan(pallet.registered_at)
        pallet.registered_at = self.env.now
        self.wms_buffer += 1
        pallet.state = 'ready'
        self.log('register', pallet, quality=pallet.quality, lot=f'LOT-{index}', quantity=1)
        self.queue(pallet)

    def arrivals(self):
        for pallet in self.pallets:
            yield self.env.timeout(pallet.arrival - self.env.now)
            self.env.process(self.pathway(pallet))

    def departures(self):
        for at, sku in self.outbound:
            yield self.env.timeout(at - self.env.now)
            if self.env.now >= WARM:
                self.out_requests += 1
            slots = self.stored_skus[sku]
            if not slots:
                if self.env.now >= WARM:
                    self.out_missing += 1
                self.log('unfilled_outbound', sku=sku)
                continue
            cell, pallet = slots.popleft()
            if pallet is None:
                self.initial_departed += 1
            else:
                assert pallet.state == 'stored' and pallet.quality == 'released'
                pallet.state, pallet.departed_at = 'departed', self.env.now
            del self.owner[cell]
            heapq.heappush(self.free[sku // 10], cell)
            self.log('outbound', pallet, cell=cell, sku=sku)
            self.notify()

    def prepare(self):
        if not self.prep_enabled:
            return
        for created in range(0, STOP, 3600):
            yield self.env.timeout(created - self.env.now)
            item = [created + 7200, created, len(self.preparations), None, None]
            self.preparations.append(item)
            heapq.heappush(self.prep_queue, item)
            self.log('preparation_released', task=item[2], due_s=item[0])
            self.notify()

    def predictor(self):
        prediction = self.train_mean
        for at in range(0, END, 300):
            yield self.env.timeout(at - self.env.now)
            target = at // HALF
            if at % HALF == 0:
                history = features(self.observed, target, self.train_mean)
                baseline = self.train_mean if target < 4 else history[5]
                prediction = baseline if self.model is None else float(max(0, self.model.predict([history])[0]))
                if WARM <= at < STOP:
                    self.forecasts.append((at, at + HALF, int(self.actual[target]), baseline, prediction))
            ready = sum(self.valid_task(self.zone(pallet), pallet) for pallet in self.ready_index.values())
            nominal_capacity = 2 * HALF / (2 * 70 + 180)
            self.pressure = ready + max(0, prediction - nominal_capacity)
            if self.record:
                self.decisions.append((at, ready, prediction, self.pressure))
            if at % 3600 == 0:
                self.timeline.append((at, self.waiting_entry, len(self.buffer_tokens), self.wms_buffer,
                                      ready, self.first_total, len(self.owner), len(self.prep_queue)))
            self.notify()

    def observe_buffer(self):
        for at in range(0, STOP, 30):
            yield self.env.timeout(at - self.env.now)
            physical = len(self.buffer_tokens)
            if self.automatic and not (self.scenario == 'S3' and 43200 <= at % DAY < 43800):
                self.sensor_value, self.sensor_time = physical, at
            if at >= WARM:
                estimate = self.sensor_value if self.sensor_value is not None else self.wms_buffer
                self.exposure += 1
                self.abs_error_wms += abs(physical - self.wms_buffer)
                self.abs_error_estimate += abs(physical - estimate)

    def peek(self):
        options = []
        for zone, jobs in enumerate(self.move_queues):
            while jobs and not self.valid_task(zone, self.pallets[jobs[0][1]]):
                heapq.heappop(jobs)
            if jobs and self.free[zone]:
                options.append((*jobs[0], zone))
        return min(options) if options else None

    def driver(self):
        while True:
            incoming = self.peek()
            job = preview_policy(self.env.now, self.pressure, self.prep_queue, incoming)
            if not job:
                yield self.change
                continue
            with self.forklifts.request() as forklift:
                yield forklift
                incoming = self.peek()
                job = preview_policy(self.env.now, self.pressure, self.prep_queue, incoming)
                start = self.env.now
                if job == 'prep':
                    task = heapq.heappop(self.prep_queue)
                    task[3] = start
                    self.log('preparation_started', task=task[2])
                    yield self.env.timeout(900)
                    task[4] = self.env.now
                    self.prep_seconds += self.overlap(start, self.env.now)
                    self.fork_seconds += self.overlap(start, self.env.now)
                    self.log('preparation_completed', task=task[2], late=self.env.now > task[0])
                    continue
                if job != 'move':
                    continue
                _, index, zone = incoming
                heapq.heappop(self.move_queues[zone])
                pallet = self.pallets[index]
                initial_move = pallet.state == 'ready'
                destination = heapq.heappop(self.free[zone])
                assert destination not in self.owner and self.zone(pallet) == zone
                self.owner[destination] = index
                self.ready_index.pop(index)
                origin = pallet.cell
                if initial_move:
                    pallet.started_at = start
                    self.buffer_change()
                    self.buffer.release(self.buffer_tokens.pop(index))
                pallet.state = 'moving'
                self.log('reserve_and_start', pallet, cell=destination, quality=pallet.quality)
                duration = 2 * self.distances[destination] + 180 * self.random['work'][index]
                yield self.env.timeout(float(duration))
                self.fork_seconds += self.overlap(start, self.env.now)
                pallet.active_forklift += duration
                if initial_move:
                    pallet.physical_end_at = self.env.now
                if origin >= 0:
                    del self.owner[origin]
                    heapq.heappush(self.free[2], origin)
                pallet.cell, pallet.state = destination, 'confirming'
                self.log('physical_move_end', pallet, cell=destination)
                self.env.process(self.confirm(pallet, zone, initial_move))
                self.notify()

    def confirm(self, pallet, zone, initial_move):
        yield self.env.timeout(self.network(self.env.now, pallet.id, 1))
        if initial_move:
            assert math.isnan(pallet.placed_at)
            pallet.placed_at, pallet.first_cell = self.env.now, pallet.cell
            self.first_total += 1
            self.wms_buffer -= 1
        if zone == 2:
            pallet.state = 'quarantine'
            if pallet.quality == 'released':
                pallet.state = 'ready_final'
                self.queue(pallet)
        else:
            assert pallet.quality == 'released' and self.zone(pallet) == zone
            pallet.state, pallet.final_at = 'stored', self.env.now
            self.stored_skus[pallet.sku].append((pallet.cell, pallet))
        self.log('confirm_place', pallet, cell=pallet.cell, quality=pallet.quality, primary=initial_move)
        self.notify()

    def run(self):
        self.ready_index = {}
        for process in (self.arrivals, self.departures, self.prepare, self.predictor, self.observe_buffer, self.driver, self.driver):
            self.env.process(process())
        self.env.run(until=END)
        self.buffer_change()
        return self.metrics()

    def metrics(self):
        assert len(self.owner) + sum(map(len, self.free)) == 5329
        assert self.initial - self.initial_departed == sum(owner == -1 for owner in self.owner.values())
        assert len(self.buffer_tokens) <= self.buffer_limit and self.wms_buffer >= 0
        assert all(pallet.state != 'future' for pallet in self.pallets)
        assert sum(pallet.state == 'entry' for pallet in self.pallets) == self.waiting_entry
        assert self.first_total == sum(math.isfinite(pallet.placed_at) for pallet in self.pallets)
        assert all(self.owner.get(pallet.cell) == pallet.id for pallet in self.pallets
                   if pallet.state in ('stored', 'quarantine', 'ready_final', 'confirming'))
        assert self.wms_buffer == sum(math.isfinite(pallet.registered_at) and not math.isfinite(pallet.placed_at)
                                      for pallet in self.pallets)
        cohort = [pallet for pallet in self.pallets if WARM <= pallet.arrival < STOP]
        timing = lambda attr: np.array([getattr(pallet, attr) for pallet in cohort])
        arrivals, registrations, placed, final = map(timing, ('arrival', 'registered_at', 'placed_at', 'final_at'))
        buffer_at, started = map(timing, ('buffer_at', 'started_at'))
        prep = [task for task in self.preparations if task[1] >= WARM and task[0] <= STOP]
        late = sum(task[4] is None or task[4] > task[0] for task in prep)
        reg_delay, place_delay = registrations - arrivals, placed - arrivals
        physical_wait = np.fmin(buffer_at, STOP) - arrivals + np.maximum(0, np.fmin(started, STOP) - np.fmin(buffer_at, STOP))
        restricted = lambda delay, cap: float(np.fmin(delay, cap).mean())
        pctl = lambda delay, quantile: float(np.nanpercentile(delay, quantile)) if np.isfinite(delay).any() else None
        forecast = np.array(self.forecasts)
        actual, baseline, predicted = forecast[:, 2:].T
        return dict(inbound=len(cohort), input_hash=self.input_hash, registration_bounded_s=restricted(reg_delay, 600),
                    register_p95_s=pctl(reg_delay, 95), register_60_share=float(np.mean(reg_delay <= 60)),
                    unregistered=int(np.isnan(registrations).sum()), placement_bounded_s=restricted(place_delay, DAY),
                    place_p95_s=pctl(place_delay, 95), placed_by_stop=int(np.sum(placed < STOP)),
                    placed_by_drain=int(np.isfinite(placed).sum()), unplaced_stop=int(np.sum(~(placed < STOP))),
                    unplaced_drain=int(np.isnan(placed).sum()), final_by_drain=int(np.isfinite(final).sum()),
                    waited_entry_count=int(np.sum(np.fmin(buffer_at, STOP) > arrivals)),
                    entry_wait_minutes=float((np.fmin(buffer_at, STOP) - arrivals).sum() / 60),
                    wait_minutes=float(physical_wait.sum() / 60), full_buffer_share=self.full_seconds / (STOP - WARM),
                    outside_trend_per_day=float(np.polyfit(np.arange(30), np.array(self.timeline)[24:744:24, 1], 1)[0]),
                    wms_buffer_mae=self.abs_error_wms / self.exposure, observed_buffer_mae=self.abs_error_estimate / self.exposure,
                    operator_hours=self.operator_seconds / 3600, forklift_hours=self.fork_seconds / 3600,
                    preparation_hours=self.prep_seconds / 3600, forklift_utilization=self.fork_seconds / (2 * (STOP - WARM)),
                    prep_due=len(prep), prep_late=late, prep_late_share=late / len(prep) if prep else 0,
                    out_requests=self.out_requests, out_missing=self.out_missing, initial_departed=self.initial_departed,
                    baseline_mae=float(np.mean(abs(actual - baseline))), forecast_mae=float(np.mean(abs(actual - predicted))),
                    forecast_wape=float(np.sum(abs(actual - predicted)) / np.sum(actual)) if actual.sum() else None,
                    duplicate_receipts=0, illegal_moves=0, illegal_release=0, negative_stock=0)

    def save(self, root, meta):
        frame = pd.DataFrame({field: [getattr(row, field) for row in self.pallets] for field in Pallet.__slots__})
        frame['evidence'] = 'SIMULATION'
        frame.to_parquet(root / 'pallets.parquet', index=False)
        pd.DataFrame(self.forecasts, columns=['issue_s', 'target_end_s', 'actual', 'baseline', 'prediction']).assign(
            evidence='SIMULATION', **meta).to_parquet(root / 'forecast.parquet', index=False)
        pd.DataFrame(self.timeline, columns=['time_s', 'outside', 'physical_buffer', 'wms_buffer', 'ready',
                                            'first_placements_total', 'reserved_or_occupied', 'prep_waiting']).assign(
            evidence='SIMULATION').to_parquet(root / 'queue.parquet', index=False)
        pd.DataFrame(self.preparations, columns=['due_s', 'created_s', 'task', 'started_s', 'completed_s']).assign(
            evidence='SIMULATION').to_parquet(root / 'preparation.parquet', index=False)
        if self.record:
            with (root / 'events.jsonl.gz').open('wb') as stream, gzip.GzipFile(fileobj=stream, mode='wb', mtime=0, filename='') as target:
                for record in self.events:
                    target.write((json.dumps(dict(evidence='SIMULATION', **record), separators=(',', ':'), sort_keys=True) + '\n').encode())
            pd.DataFrame(self.decisions, columns=['time_s', 'ready', 'forecast', 'pressure']).assign(
                evidence='SIMULATION').to_parquet(root / 'decisions.parquet', index=False)
