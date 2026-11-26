import argparse
import gzip
import json
from pathlib import Path

import pandas as pd

from src.simulator import DAY, Simulation


def prepare(source_folder, target_folder):
    target_folder.mkdir(parents=True, exist_ok=True)
    for case in sorted((source_folder / 'runs').glob('*-1000-*')):
        if not (case / 'manifest.json').is_file():
            continue
        scenario = case.name.split('-')[0]
        cohort = pd.read_parquet(case / 'pallets.parquet').query('arrival >= @DAY').head(50)
        chosen = set(cohort.id)
        assert len(chosen) == 50
        known = Simulation([], [], scenario, 1000, True, None, 0)
        occupied = set(known.owner)
        physical_buffer = set()
        positions = {}
        in_transit = {}
        initial_move = {}
        events = []
        with gzip.open(case / 'events.jsonl.gz', 'rt', encoding='utf-8') as stream:
            for row in map(json.loads, stream):
                event = row['event']
                pallet = row['pallet']
                t = row['time']
                selected = pallet in chosen
                if event == 'physical_buffer':
                    assert pallet not in physical_buffer
                    physical_buffer.add(pallet)
                    assert len(physical_buffer) <= known.buffer_limit
                elif event == 'reserve_and_start':
                    cell = row['cell']
                    assert cell not in occupied
                    to_final = pallet in positions
                    if not to_final:
                        physical_buffer.remove(pallet)
                    if selected:
                        zone = min(cell // 2600, 2)
                        before = [index for index in range(zone * 2600, min(5329, (zone + 1) * 2600)) if index not in occupied]
                        assert before[0] == cell
                        events.append(dict(operation='final_reserve' if to_final else 'reserve', pallet=pallet, virtual_s=t,
                                           cell=cell, available=before, quality=row['quality']))
                    initial_move[pallet] = not to_final
                    in_transit[pallet] = cell
                    occupied.add(cell)
                elif event == 'physical_move_end':
                    assert in_transit.pop(pallet) == row['cell']
                    origin = positions.get(pallet)
                    if origin is not None:
                        occupied.remove(origin)
                    positions[pallet] = row['cell']
                elif event == 'outbound':
                    occupied.remove(row['cell'])
                    positions.pop(pallet, None)
                    if selected:
                        events.append(dict(operation='outbound', pallet=pallet, cell=row['cell'], virtual_s=t))
                elif selected and event == 'register':
                    assert pallet in physical_buffer
                    events.append(dict(operation='receive', pallet=pallet, quality=row['quality'], virtual_s=t,
                                       physical_buffer=len(physical_buffer)))
                elif selected and event == 'external_release':
                    events.append(dict(operation='release', pallet=pallet, virtual_s=t))
                elif selected and event == 'confirm_place':
                    assert row['primary'] == initial_move[pallet]
                    events.append(dict(operation='place' if row['primary'] else 'final_place', pallet=pallet,
                                       cell=row['cell'], quality=row['quality'], virtual_s=t))
        expected = [dict(id=int(record.id), sku=int(record.sku), quality='released' if record.released_at == record.arrival else 'hold')
                    for record in cohort.itertuples()]
        target = target_folder / f'{case.name}.json.gz'
        with gzip.open(target, 'wt', encoding='utf-8', compresslevel=4) as output:
            json.dump(dict(source=case.name, capacity=known.buffer_limit, expected=expected, events=events), output, separators=(',', ':'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--from', dest='source_folder', type=Path, default=Path('artifacts'))
    args = parser.parse_args()
    prepare(args.source_folder, Path('artifacts/replay/input'))
