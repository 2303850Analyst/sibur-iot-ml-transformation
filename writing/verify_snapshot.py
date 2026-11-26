import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

writing = Path(__file__).resolve().parent
project = writing.parent
series = project / 'series_py312'
art = series / 'artifacts'
read = lambda path: json.loads(path.read_text(encoding='utf-8-sig'))
digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
freeze = read(art / 'simulation_freeze.json')
assert all(digest(series / name) == expected for name, expected in freeze['files'].items())
assert digest(project / 'protocol.yaml') == freeze['files']['protocol.yaml']
matrix = pd.read_csv(art / 'runs.csv').set_index('run_id')
manifest = pd.read_csv(art / 'run_manifest.csv').set_index('run_id')
assert matrix.index.is_unique and set(matrix.index) == set(manifest.index)
assert len(matrix) == 1080 and matrix.architecture.eq('C').sum() == 360
groups = matrix.groupby(['scenario', 'level', 'architecture'])
assert len(groups) == 54 and groups.size().eq(20).all()
assert manifest.fingerprint.eq(freeze['fingerprint']).all()
assert matrix.groupby(['scenario', 'level', 'seed']).input_hash.nunique().eq(1).all()
for name, row in matrix.iterrows():
    folder = art / 'runs' / name
    evidence = read(folder / 'manifest.json')
    assert evidence['fingerprint'] == freeze['fingerprint'] and evidence['metrics']['input_hash'] == row.input_hash
    source = folder / 'pallets.parquet'
    assert digest(source) == evidence['files'][source.name]
    pallets = pd.read_parquet(source, columns=['arrival','placed_at','registered_at'])
    cohort = pallets[pallets.arrival.between(86400, 31*86400, inclusive='left')]
    assert len(cohort) == row.inbound
    for event, limit, metric in [('placed_at',86400,'placement_bounded_s'),('registered_at',600,'registration_bounded_s')]:
        assert np.isclose(np.fmin(cohort[event]-cohort.arrival, limit).mean(), row[metric], rtol=0, atol=1e-8)
assert read(art / 'experiment_status.json')['technical_status'] == 'FULL_PASS'
assert read(project / 'artifacts/replay/verification.json')['verified_cases'] == 54
assert read(project / 'artifacts/api_benchmark/status.json')['status'] == 'PASS'
print('Verified input hashes, 1080 manifests and pallet-level primary means, 54 groups and 360 C runs.')
