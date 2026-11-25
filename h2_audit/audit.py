import csv
import gzip
import hashlib
import json
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from ledger import Finding, Ledger, KINDS, require

ROOT = Path(__file__).resolve().parent
SERIES = Path('/series/artifacts')
read = lambda path: json.loads(path.read_text(encoding='utf-8-sig'))


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def audit(folder):
    receipt = read(folder / 'reproduction.json')
    expected = read(folder / 'manifest.json')
    assert receipt['fingerprint'] == expected['fingerprint'] == read(ROOT / 'environment.json')['fingerprint']
    assert sha(folder / 'manifest.json') == receipt['manifest_sha256'] == sha(SERIES/'runs'/folder.name/'manifest.json')
    assert sha(folder / 'events.jsonl.gz') == receipt['events_sha256']
    assert sha(folder / 'initial.json.gz') == receipt['initial_sha256']
    with gzip.open(folder / 'initial.json.gz') as stream:
        ledger = Ledger(json.load(stream))
    assert ledger.input_hash == receipt['input_hash'] == expected['metrics']['input_hash']
    timings = {}
    failures = []
    with gzip.open(folder / 'events.jsonl.gz') as stream:
        for line, raw in enumerate(stream, 1):
            row = json.loads(raw)
            before = ledger.snapshot(row)
            try:
                ledger.apply(row)
                if row['event'] in ('register','confirm_place','reserve_and_start','external_release'):
                    require('quality' in row or row['event'] == 'external_release','trace_integrity','missing quality field')
                if row['event'] == 'register' or (row['event'] == 'confirm_place' and row['primary']):
                    timings[row['pallet'], row['event']] = row['time']
            except Finding as problem:
                failures.append(dict(kind=problem.kind,reason=str(problem),line=line,event=row,before=before,
                                     after=ledger.snapshot(row),run_id=folder.name))
                break
    try:
        if not failures:
            ledger.finish()
            table_path = SERIES/'runs'/folder.name/'pallets.parquet'
            assert sha(table_path) == expected['files']['pallets.parquet']
            for pallet in pd.read_parquet(table_path).itertuples():
                key = pallet.id
                require((key in ledger.held) == (pallet.quality == 'hold'), 'illegal_release', 'terminal quality mismatch')
                require(ledger.inbound[key][1] == pallet.sku, 'trace_integrity', 'terminal SKU mismatch')
                for operation, measured in [('register',pallet.registered_at),('confirm_place',pallet.placed_at)]:
                    actual = timings.get((key, operation))
                    require((actual is None and np.isnan(measured)) or
                            (actual is not None and abs(actual-measured) <= 0.000001),
                            'trace_integrity', f'terminal pallet {key} time mismatch for {operation}')
    except Finding as problem:
        failures.append(dict(kind=problem.kind,reason=str(problem),line=line,time=ledger.time,run_id=folder.name))
    summary = dict(run_id=folder.name, status='FAILED' if failures else 'PASS', lines_processed=line,
                   operations=ledger.count, first_findings=failures, complete=not failures,
                   counts={kind: (None if failures else 0) for kind in KINDS}, input_hash=ledger.input_hash,
                   log_sha256=receipt['events_sha256'])
    (folder/'audit.json').write_text(json.dumps(summary,indent=2))
    return summary


def finish():
    folders = [ROOT/'runs'/f'S1-L2-{seed}-{arch}' for seed in range(1000,1020) for arch in 'BC']
    with ProcessPoolExecutor(5) as workers:
        summaries = list(workers.map(audit,folders))
    with (ROOT/'coverage.csv').open('w',newline='') as stream:
        writer = csv.DictWriter(stream,fieldnames=['run_id','status','lines_processed','input_hash','log_sha256',*KINDS])
        writer.writeheader()
        for result in summaries:
            writer.writerow({**{key:result[key] for key in writer.fieldnames if key not in KINDS}, **result['counts']})
    metrics = [read(folder/'manifest.json')['metrics'] for folder in folders]
    base, candidate = metrics[::2], metrics[1::2]
    assert all(old['input_hash'] == new['input_hash'] for old,new in zip(base,candidate))
    indices = np.random.default_rng(20261118).integers(0,20,size=(5000,20))
    def contrast(metric, scale):
        values = np.array([(new[metric]-old[metric])*scale for old,new in zip(base,candidate)])
        return float(values.mean()), np.quantile(values[indices].mean(axis=1),[.0125,.9875]).tolist()
    effect, interval = contrast('placement_bounded_s',-1)
    late, late_ci = contrast('prep_late_share',100)
    orig = read(SERIES/'hypotheses.json')['hypotheses']['H2']
    assert np.allclose(interval,orig['ci975_s']) and np.allclose(late_ci,orig['prep_late_ci975_pp'])
    assert np.isclose(effect,orig['mean_s']) and np.isclose(late,orig['prep_late_delta_pp'])
    passed = sum(row['complete'] for row in summaries)
    ok = passed == 40 and late_ci[1] <= 1 and interval[0] > 0
    result = dict(status='VERIFIED_FOR_H2_SIMULATION_SCOPE' if ok else 'GUARD_NOT_VERIFIED',
        scope='SimPy S1/L2 B,C seeds 1000..1019',fingerprint=read(ROOT/'environment.json')['fingerprint'],
        verified_runs=passed,total_runs=40, newly_reconstructed=38, seed1000_controls=2,
        lines_read=sum(row['lines_processed'] for row in summaries),
        violations={key:0 for key in KINDS} if passed==40 else None, effect_seconds=effect,
        interval975_seconds=interval, late_delta_pp=late,late_ci975_pp=late_ci,late_upper_limit_pp=1,
        quality_authority='Model external release schedule only; no Odoo user/role data in SimPy logs.')
    (ROOT/'h2_sidecar.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2), flush=True)
    if not ok:
        raise RuntimeError('See run-level audit findings')


if __name__ == '__main__':
    finish()
