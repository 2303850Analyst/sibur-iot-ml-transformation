import hashlib
import importlib.util
from pathlib import Path

import numpy as np

from src.forecast import features
from src.simulator import DAY, Simulation, draws, preview_policy


def test_feature_cannot_use_target_or_future():
    history = np.arange(100)
    original = features(history, 48, 6)
    history[48:] = -1000
    assert features(history, 48, 6) == original
    assert original[:5] == [47, 46, 45, 44, 0]


def test_policy_deadline_and_no_preemption():
    preparation = [[2000, 200, 1]]
    assert preview_policy(1099, 8, preparation, None) is None
    assert preview_policy(1100, 8, preparation, (1, 2, 0)) == 'prep'
    assert preview_policy(1099, 8, preparation, (1, 2, 0)) == 'move'
    assert preview_policy(1099, 7.9, preparation, (1, 2, 0)) == 'prep'


def test_random_stream_prefix_is_stable():
    short, long = draws(1000, 40), draws(1000, 50)
    assert all(np.array_equal(values, long[name][:40]) for name, values in short.items())


def test_same_forecast_same_transitions_and_seed_log(tmp_path):
    entries = [(float(DAY + 120 + index * 2), index % 20, index) for index in range(90)]
    results, hashes = [], []
    for repeat in range(2):
        simulation = Simulation(entries, [], 'S4', 1000, True, None, 5, log=True)
        results.append(simulation.run())
        folder = tmp_path / str(repeat)
        folder.mkdir()
        simulation.save(folder, {})
        hashes.append(hashlib.sha256((folder / 'events.jsonl.gz').read_bytes()).hexdigest())
    assert results[0] == results[1]
    assert hashes[0] == hashes[1]
    assert results[0]['waited_entry_count'] > 0
    assert results[0]['placed_by_drain'] == len(entries)


def test_storage_contract():
    path = Path('odoo_addons/wms_research/rules.py')
    spec = importlib.util.spec_from_file_location('rules', path)
    rules = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rules)
    assert rules.eligible('hold', 0, 'quarantine', 0)
    assert not rules.eligible('hold', 0, 'storage', 0)
    assert not rules.eligible('released', 0, 'storage', 1)
    assert rules.eligible('released', 1, 'storage', 1)
    identity = {'sku': 'one', 'lot': 'expected', 'quantity': 1}
    assert rules.identity_matches(identity, identity)
    assert not rules.identity_matches({**identity, 'lot': 'other'}, identity)


def test_network_outage_uses_serializable_times_and_log(tmp_path):
    entries = [(float(DAY + 43200 + index), index % 20, index) for index in range(50)]
    simulation = Simulation(entries, [], 'S3', 1000, True, None, 5, log=True)
    simulation.run()
    assert any(row['event'] == 'observation_received' and row['time'] > DAY + 43800 for row in simulation.events)
    simulation.log('numpy_scalar', late=np.bool_(True), identifier=np.int64(42))
    simulation.save(tmp_path, {})
