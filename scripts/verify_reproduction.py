import json
import tempfile
from pathlib import Path

import joblib

from src.data import campaign, sample, sha
from src import run_matrix
from src.simulator import Simulation


def main():
    run_matrix.initialize()
    fp, _ = run_matrix.freeze()
    cases = []
    model, mean = joblib.load('artifacts/models/L1.joblib')
    for seed in (1000, 1001):
        name = f'S1-L1-{seed}-C'
        original = run_matrix.read_json(Path('artifacts/runs') / name / 'manifest.json')
        assert original['fingerprint'] == fp
        thinned = sample(run_matrix.SOURCE, run_matrix.MANIFEST, 300, seed)
        inbound, outbound, _ = campaign(thinned, run_matrix.MANIFEST, seed, 'S1')
        digests = [original['files']['events.jsonl.gz']] if 'events.jsonl.gz' in original['files'] else []
        for trial in range(2 if not digests else 1):
            simulation = Simulation(inbound, outbound, 'S1', seed, True, model, mean, log=True)
            metrics = simulation.run()
            assert all(original['metrics'][key] == value for key, value in metrics.items())
            with tempfile.TemporaryDirectory() as temporary:
                simulation.save(Path(temporary), {})
                digests.append(sha(Path(temporary) / 'events.jsonl.gz'))
        assert len(digests) == 2 and digests[0] == digests[1]
        cases.append(dict(run_id=name, sha256=digests[0], matches=True))
    Path('artifacts/reproducibility.json').write_text(json.dumps(dict(evidence='SIMULATION', status='PASS', cases=cases), indent=2))


if __name__ == '__main__':
    main()
