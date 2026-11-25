import argparse
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as parquet


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--series', default='series_py312/artifacts', type=Path)
    args = parser.parse_args()
    runs = args.series / 'runs'
    table_writer = None
    accuracy = []
    slices = []
    try:
        for run in sorted(runs.iterdir()):
            manifest = run / 'manifest.json'
            if not manifest.exists():
                continue
            source = parquet.read_table(run / 'forecast.parquet')
            if table_writer is None:
                table_writer = parquet.ParquetWriter(args.series / 'forecast_predictions.parquet', source.schema, compression='zstd')
            table_writer.write_table(source)
            values = source.to_pandas()
            actual = values.actual.to_numpy()
            row = {field: values[field].iloc[0] for field in ('run_id', 'scenario', 'level', 'architecture', 'seed', 'evidence')}
            for method in ('baseline', 'prediction'):
                error = abs(values[method].to_numpy() - actual)
                row[method + '_MAE'] = np.mean(error)
                row[method + '_WAPE'] = np.sum(error) / np.sum(actual) if np.sum(actual) else None
            accuracy.append(row)
            queue = pd.read_parquet(run / 'queue.parquet')
            pallet = pd.read_parquet(run / 'pallets.parquet', columns=['arrival', 'placed_at'])
            for hours in (8, 24, 168, 720):
                cutoff = 86400 + hours * 3600
                cohort = pallet[pallet.arrival.between(86400, cutoff, inclusive='left')]
                at = queue.loc[queue.time_s.eq(cutoff)].iloc[0]
                completed = (cohort.placed_at < cutoff).sum()
                slices.append(dict(evidence='SIMULATION', run_id=run.name, hours=hours, arrivals=len(cohort),
                                   completed=completed, incomplete=len(cohort) - completed,
                                   outside=int(at.outside), physical_buffer=int(at.physical_buffer), wms_buffer=int(at.wms_buffer)))
    finally:
        if table_writer:
            table_writer.close()
    pd.DataFrame(accuracy).to_csv(args.series / 'scenario_forecast_accuracy.csv', index=False)
    pd.DataFrame(slices).to_csv(args.series / 'horizon_slices.csv', index=False)


if __name__ == '__main__':
    main()
