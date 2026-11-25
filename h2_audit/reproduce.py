import gzip
import hashlib
import json
import os
import platform
import shutil
from concurrent.futures import ProcessPoolExecutor
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

import joblib
import pandas as pd

from src import run_matrix as matrix
from src.data import campaign, sample, sha
from src.simulator import Simulation

OUT = Path('/audit')
ROOT = Path('artifacts')
IMAGE = 'sha256:ee86866e0f5b7d2460e7fff28a480c30ef19ce167f8bfed8bfd39101724ce28e'


def save(path, obj):
    path.write_text(json.dumps(obj, indent=2, sort_keys=True, allow_nan=False), encoding='utf-8')


def packed(path, obj):
    with path.open('wb') as raw, gzip.GzipFile(filename='', fileobj=raw, mode='wb', mtime=0) as stream:
        stream.write(json.dumps(obj, separators=(',', ':')).encode())


def preflight():
    freeze = matrix.read_json(ROOT / 'simulation_freeze.json')
    files = {name: sha(Path(name)) for name in freeze['files']}
    assert files == freeze['files']
    runtime = dict(python=platform.python_version(), packages={name: version(name) for name in
        ('numpy', 'pandas', 'scipy', 'joblib', 'scikit-learn', 'simpy', 'pyarrow', 'PyYAML')})
    fp = hashlib.sha256(json.dumps(dict(files=files, runtime=runtime), sort_keys=True).encode()).hexdigest()
    assert fp == freeze['fingerprint']
    data = matrix.read_json(Path('data/manifests/wepastacks.json'))
    raw = Path('data/raw/wepastacks_orders.json.gz')
    assert sha(raw) == data['gzip_sha256']
    with gzip.open(raw) as stream:
        assert hashlib.file_digest(stream, 'sha256').hexdigest() == data['raw_sha256']
    save(OUT / 'environment.json', dict(fingerprint=fp, image=IMAGE, runtime=runtime, verified_files=files,
                                        raw_sha256=data['raw_sha256'], gzip_sha256=data['gzip_sha256']))
    return fp


def pair(args):
    seed, fp = args
    inbound, outbound, mapping = campaign(sample(matrix.SOURCE, matrix.MANIFEST, 450, seed), matrix.MANIFEST, seed, 'S1')
    model, mean = joblib.load(ROOT / 'models/L2.joblib')
    for arch in 'BC':
        name = f'S1-L2-{seed}-{arch}'
        source = ROOT / 'runs' / name
        expected = matrix.read_json(source / 'manifest.json')
        assert expected['fingerprint'] == fp
        folder = OUT / 'runs' / name
        folder.mkdir(exist_ok=True, parents=True)
        meta = dict(run_id=name, level='L2', seed=seed, scenario='S1', architecture=arch)
        simulation = Simulation(inbound, outbound, 'S1', seed, True, model if arch == 'C' else None, mean, log=True)
        packed(folder / 'initial.json.gz', dict(inbound=inbound, outbound=outbound,
            held=[int(idx) for idx, value in enumerate(simulation.random['quality']) if value < .05],
            cells=sorted([cell, sku] for sku, queue in enumerate(simulation.stored_skus) for cell, _ in queue)))
        metrics = dict(evidence='SIMULATION', **meta, **simulation.run())
        assert metrics == expected['metrics'], (name, {k for k in metrics if metrics[k] != expected['metrics'][k]})
        with TemporaryDirectory() as temp:
            scratch = Path(temp)
            simulation.save(scratch, meta)
            pd.DataFrame(dict(model_day=range(len(mapping)), source_day=mapping, evidence='SIMULATION')).to_csv(
                scratch / 'day_map.csv', index=False)
            checks = {file: dict(original=sha(source / file), replay=sha(scratch / file), manifest=digest)
                      for file, digest in expected['files'].items()}
            assert all(len(set(row.values())) == 1 for row in checks.values()), (name, checks)
            shutil.copyfile(scratch / 'events.jsonl.gz', folder / 'events.jsonl.gz')
        shutil.copyfile(source / 'manifest.json', folder / 'manifest.json')
        save(folder / 'reproduction.json', dict(run_id=name, fingerprint=fp, all_metrics_equal=True,
             metrics_count=len(metrics), input_hash=simulation.input_hash, original_artifacts=checks,
             initial_sha256=sha(folder/'initial.json.gz'), manifest_sha256=sha(folder/'manifest.json'),
             events_sha256=sha(folder/'events.jsonl.gz')))
    return seed


if __name__ == '__main__':
    fingerprint = preflight()
    with ProcessPoolExecutor(5, initializer=matrix.initialize) as workers:
        for finished in workers.map(pair, [(seed, fingerprint) for seed in range(1000, 1020)]):
            print('verified pair', finished, flush=True)
