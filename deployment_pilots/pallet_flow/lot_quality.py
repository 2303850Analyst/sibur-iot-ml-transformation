"""Compare the existing receiving fleets with a reusable signed lot disposition."""
import json
import multiprocessing as mp
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import simpy

import per_pallet_quality as base


class LotRelease:
    def setup_quality(self):
        self.lab = simpy.Resource(self.e, capacity=3)
        self.doc_owner = {}
        self.inbound_reservations = Counter()
        self.started_tests = set()
        self.completed_tests = set()
        self.held = set()
        flags = {}
        first = {}
        for pallet in self.x['pallets'].values():
            q = pallet['q']
            state = (pallet['held'], pallet['failed'])
            assert flags.setdefault(q, state) == state
            first[q] = min(first.get(q, pallet['ready']), pallet['ready'])
        for q, (held, failed) in flags.items():
            if held:
                self.held.add(q)
                self.quality_known[q] = self.e.event()
            else:
                status = 'failed' if failed else 'released'
                self.quality_known[q] = self.e.process(self.authorized(q, first[q], status))

    def inspect(self, case, pallet):
        raw = self.x['pallets'][pallet]
        q = raw['q']
        if q not in self.held or q in self.started_tests:
            return
        self.started_tests.add(q)
        with self.lab.request() as ticket:
            yield ticket
            yield self.e.timeout(600.)
        status = 'failed' if raw['failed'] else 'released'
        yield from self.authorized(q, self.e.now, status)
        self.completed_tests.add(q)
        self.quality_known[q].succeed(status)


class Digital(LotRelease, base.Robotic):
    pass


class Handheld(LotRelease, base.Manual):
    pass


def simulate(task):
    robots, isolated, seed = task
    base.cs.args = SimpleNamespace(reserve_rack=False)
    trial, sim = base.trial, base.sim
    x = trial.manifest(seed)
    draws = sim.old.Inputs(x, sim.P.SEED, json.loads(sim.old.IO_FILE.read_bytes()))
    forecasters, _ = sim.old.compare.train_forecasters(x)
    if isolated:
        group = next(g for g in x['receipts'] if g['case'] == 'D01-024')
        x['receipts'] = [group]
        x['pallets'] = {p:x['pallets'][p] for p in group['members']}
        x['departures'] = []
        for raw in x['pallets'].values():
            raw.update(held=False, failed=False, label_bad=False, bypass=1., read1=0., decode_type=.9)
        draws.linked['case'][draws.c_index[group['case']]] = 0.
    for raw in x['pallets'].values():
        raw.update(scan_factor=1.3, check_factor=1.3)
    cls = Digital if robots else Handheld
    m = cls.__new__(cls)
    m.robot_count = robots
    m.pallet_case = {p:g['case'] for g in x['receipts'] for p in g['members']}
    m.__init__(x, 'RETURN' if robots else 'DATA_RETURN', draws, forecasters, True,
               .9, 100., False, robots or 2, .8 if robots else 1., charge_hours=1., chargers=1)
    if robots:
        m.setup((robots, 0, 0, 1, 'Ex'), 120, 0)
    else:
        m.configure(False)
        m.feed_limit = 3
        m.setup_feed(120, 0, 'cross', 0)
    m.setup_quality()
    m.execute()
    r = trial.report(m, 'lot', seed)
    visits = defaultdict(dict)
    for v in m.visits:
        if v['op'] in (10, 18, 20) and v['branch'] == 'APPLIED' and v['finish'] is not None and v['finish'] <= m.end:
            visits[v['pallet']][v['op']] = v
    release = {a['quality']:t for k,t,_,_,a in m.events if k == 'quality_delivered' and a['decision'] == 'released'}
    for p, trace in visits.items():
        if 18 in trace:
            q = x['pallets'][p]['q']
            assert not x['pallets'][p]['failed'] and release[q] <= trace[18]['request']
    cohort_pallets = {p for p,c in m.pallet_case.items() if c in m.cohort}
    quality_objects = {x['pallets'][p]['q'] for p in cohort_pallets}
    durations, stage_list = [], []
    for group in x['receipts']:
        c = group['case']
        if c not in m.cohort or c not in m.closed:
            continue
        p = max(group['members'], key=lambda p:visits[p][20]['finish'])
        tr = visits[p]
        feed, _, handoff = m.feed_times[p]
        registered = m.tags[p]['registered']
        stamps = [group['ready'], x['pallets'][p]['ready'], feed, handoff, registered,
                  tr[18]['request'], tr[18]['grant'], tr[18]['finish'], m.closed[c]]
        segments = np.diff(stamps)
        elapsed = m.closed[c] - group['ready']
        assert min(segments) >= -1e-7 and abs(sum(segments) - elapsed) < 1e-5
        durations.append(elapsed)
        stage_list.append(segments)
    assert durations and len(durations) == r['closed_all']
    ntrip = sum(10 in visits.get(p, {}) for p in cohort_pallets)
    if isolated:
        assert ntrip == 0 and not m.started_tests
    backlog = r['backlog']
    stable = (r['closed_all'] == r['eligible'] and r['p20_in_30'] >= .995*r['eligible_pallets']
              and max(backlog[-1]-backlog[0], backlog[-1]-backlog[2]) <= np.ceil(320/24))
    return dict(robots=robots, isolated=isolated, seed=seed, offered=len(cohort_pallets),
                eligible=r['eligible'], closed=r['closed_all'], closed30=r['closed_in_30'],
                backlog=backlog, stable=bool(stable), mean=np.mean(durations), p95=np.percentile(durations,95),
                mean_stages=np.mean(stage_list, axis=0).tolist(), quality_objects=len(quality_objects),
                tests=len(quality_objects & m.completed_tests), trips_to_quarantine=ntrip,
                checks=r['checks'])


if __name__ == '__main__':
    tasks = [(n, isolated, 241126) for isolated in (True, False) for n in (0, 1, 2)]
    with mp.Pool(3) as pool:
        rows = pool.map(simulate, tasks, chunksize=1)
    path = Path(__file__).parent/'results'/'lot_quality_320.json'
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(rows, indent=2, allow_nan=False, default=lambda value:value.item()))
    tmp.replace(path)
