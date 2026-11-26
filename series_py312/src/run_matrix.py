import argparse
import hashlib
import json
import os
import platform
import shutil
import sys
from importlib.metadata import version
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import yaml

from .data import NORMAL, build_data, campaign, sample, sha
from .forecast import fit
from .simulator import Simulation


ROOT = Path('artifacts')
RUNS = ROOT / 'runs'
SOURCE = None
MANIFEST = None
CONFIG = None


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def initialize():
    global SOURCE, MANIFEST, CONFIG
    SOURCE = pd.read_parquet(NORMAL / 'source.parquet')
    MANIFEST = read_json(Path('data/manifests/wepastacks.json'))
    CONFIG = yaml.safe_load(Path('protocol.yaml').read_text(encoding='utf-8'))


def atomic_json(path, value):
    temp = path.with_suffix('.pending')
    temp.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False,
                               default=lambda item: item.item() if isinstance(item, np.generic) else str(item)), encoding='utf-8')
    temp.replace(path)


def verify_cache(folder, fingerprint):
    marker = folder / 'manifest.json'
    if not marker.is_file():
        return None
    saved = read_json(marker)
    if saved['fingerprint'] != fingerprint:
        raise RuntimeError(f'Different frozen inputs at {folder}; use a separate output series')
    if any(not (folder / name).exists() or sha(folder / name) != digest for name, digest in saved['files'].items()):
        raise RuntimeError(f'Invalid cached artifacts at {folder}')
    return saved['metrics']


def pair(level, seed, scenarios, fingerprint):
    data = sample(SOURCE, MANIFEST, CONFIG['loads'][level], seed)
    model, mean = joblib.load(ROOT / 'models' / f'{level}.joblib')
    rows = []
    for scenario in scenarios:
        inbound, outbound, mapping = campaign(data, MANIFEST, seed, scenario)
        for arch in CONFIG['architectures']:
            run_id = f'{scenario}-{level}-{seed}-{arch}'
            folder = RUNS / run_id
            cached = verify_cache(folder, fingerprint)
            if cached is not None:
                rows.append(cached)
                continue
            folder.mkdir(parents=True, exist_ok=True)
            meta = dict(run_id=run_id, level=level, seed=seed, scenario=scenario, architecture=arch)
            simulation = Simulation(inbound, outbound, scenario, seed, arch != 'A', model if arch == 'C' else None,
                                    mean, log=seed == 1000)
            metrics = dict(evidence='SIMULATION', **meta, **simulation.run())
            simulation.save(folder, meta)
            pd.DataFrame(dict(model_day=range(len(mapping)), source_day=mapping, evidence='SIMULATION')).to_csv(
                folder / 'day_map.csv', index=False)
            file_hashes = {path.name: sha(path) for path in folder.iterdir() if path.is_file() and path.suffix != '.pending'}
            atomic_json(folder / 'manifest.json', dict(fingerprint=fingerprint, files=file_hashes, metrics=metrics))
            rows.append(metrics)
    return rows


def diagnostics(fp):
    chosen = sample(SOURCE, MANIFEST, CONFIG['loads']['L1'], 1000)
    inbound, outbound, _ = campaign(chosen, MANIFEST, 1000, 'S1')
    model, mean = joblib.load(ROOT / 'models/L1.joblib')
    short_in = [row for row in inbound if row[0] < 105000]
    short_out = [row for row in outbound if row[0] < 105000]
    base = Simulation(short_in, short_out, 'S1', 1000, True, None, mean, log=True)
    fallback = Simulation(short_in, short_out, 'S1', 1000, True, None, mean, log=True)
    assert base.run() == fallback.run()
    assert base.events == fallback.events
    no_prep_base = Simulation(short_in, short_out, 'S1', 1000, True, None, mean, prep=False, log=True)
    no_prep_ml = Simulation(short_in, short_out, 'S1', 1000, True, model, mean, prep=False, log=True)
    no_prep_base.run()
    no_prep_ml.run()
    assert no_prep_base.events == no_prep_ml.events
    atomic_json(ROOT / 'diagnostics.json', dict(evidence='SIMULATION', scope='DIAGNOSTIC_ONLY', fingerprint=fp,
                equal_forecast_equal_actions=True, missing_model_fallback=True, no_prep_same_physical_transitions=True))


def freeze():
    protocol_hash = sha(Path('protocol.yaml'))
    Path('protocol.sha256').write_text(f'{protocol_hash}  protocol.yaml\n', encoding='utf-8')
    tracked = [*Path('src').glob('*.py'), *Path('configs').glob('*.yaml'), Path('protocol.yaml'),
               *ROOT.glob('models/*.joblib'), NORMAL / 'source.parquet', Path('data/manifests/wepastacks.json')]
    hashes = {path.as_posix(): sha(path) for path in sorted(tracked)}
    runtime = dict(python=platform.python_version(), packages={name: version(name) for name in (
        'numpy', 'pandas', 'scipy', 'joblib', 'scikit-learn', 'simpy', 'pyarrow', 'PyYAML')})
    fp = hashlib.sha256(json.dumps({'files': hashes, 'runtime': runtime}, sort_keys=True).encode()).hexdigest()
    return fp, hashes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--smoke', action='store_true')
    args = parser.parse_args()
    ROOT.mkdir(exist_ok=True)
    if not (ROOT / 'simulation_freeze.json').exists():
        protocol = yaml.safe_load(Path('protocol.yaml').read_text(encoding='utf-8'))
        data, manifest = build_data()
        fit(data, manifest, protocol)
    initialize()
    fingerprint, hashes = freeze()
    freeze_path = ROOT / 'simulation_freeze.json'
    if freeze_path.exists() and read_json(freeze_path)['fingerprint'] != fingerprint:
        raise RuntimeError('Current code, protocol, data or model differs from frozen inputs.')
    diagnostics(fingerprint)
    if args.smoke:
        print('Simulation smoke: PASS', flush=True)
        return
    atomic_json(freeze_path, dict(fingerprint=fingerprint, files=hashes, python=sys.version, platform=platform.platform()))
    (ROOT / 'python_environment.txt').write_text(shutil.which(sys.executable) + '\n' + sys.version, encoding='utf-8')
    rows = []
    scenarios = list(CONFIG['scenarios'])
    first, last = CONFIG['seeds'].values()
    with ProcessPoolExecutor(max_workers=args.workers, initializer=initialize) as workers:
        jobs = [workers.submit(pair, level, seed, scenarios, fingerprint) for seed in range(first, last + 1)
                for level in CONFIG['loads']]
        for job in as_completed(jobs):
            rows.extend(job.result())
            print(f'{len(rows)}/1080 complete', flush=True)
    frame = pd.DataFrame(rows).sort_values(['scenario', 'level', 'architecture', 'seed'])
    assert frame.run_id.nunique() == len(frame) == 1080
    assert frame.architecture.eq('C').sum() == 360
    cells = frame.groupby(['scenario', 'level', 'architecture']).size()
    assert len(cells) == 54 and cells.eq(20).all()
    assert frame.groupby(['scenario', 'level', 'seed']).input_hash.nunique().eq(1).all()
    frame.to_csv(ROOT / 'runs.csv', index=False)
    frame[['evidence', 'run_id', 'scenario', 'level', 'seed', 'architecture', 'input_hash']].assign(
        fingerprint=fingerprint, status='VALID').to_csv(ROOT / 'run_manifest.csv', index=False)


if __name__ == '__main__':
    main()
