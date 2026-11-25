from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

from .data import DAY, sample


HALF = 1800
LAGS = (1, 2, 3, 4, 48)


def features(history, target_bin, train_mean, first=0):
    def lag(offset):
        key = target_bin - offset
        return history[key] if key >= first else train_mean

    angle = 2 * np.pi * (target_bin % 48) / 48
    return [*(lag(offset) for offset in LAGS),
            sum(lag(offset) for offset in range(1, 5)) / 4,
            sum(lag(offset) for offset in range(1, 49)) / 48, np.sin(angle), np.cos(angle)]


def fit(data, manifest, protocol):
    folder = Path('artifacts/models')
    folder.mkdir(parents=True, exist_ok=True)
    forecast_rows = []
    for level, nominal in protocol['loads'].items():
        thinned = sample(data, manifest, nominal, 20261118)
        times = thinned.loc[thinned.direction.eq('delivery'), 'seconds'] // HALF
        counts = np.bincount(times, minlength=(max(manifest['partitions']['test']) + 1) * 48)
        training = manifest['partitions']['train']
        first, limit = training[0] * 48, (training[-1] + 1) * 48
        baseline = counts[first:limit].mean()
        train_bins = range(first + 48, limit)
        model = HistGradientBoostingRegressor(**protocol['forecast'])
        model.fit([features(counts, key, baseline, first) for key in train_bins], counts[first + 48:limit])
        joblib.dump((model, float(baseline)), folder / f'{level}.joblib')
        for partition in ('validation', 'test'):
            days = manifest['partitions'][partition]
            bins = range(days[0] * 48, (days[-1] + 1) * 48)
            table = [features(counts, key, baseline, first) for key in bins]
            prediction = model.predict(table).clip(0)
            forecast_rows.extend(dict(evidence='SIMULATION', cohort='source_reconciled_history', load=level,
                                      partition=partition, issue_s=key * HALF, target_end_s=(key + 1) * HALF,
                                      actual=int(counts[key]), baseline=row[5], ml=float(value))
                                 for key, row, value in zip(bins, table, prediction))
    pd.DataFrame(forecast_rows).to_parquet('artifacts/source_forecast.parquet', index=False)
    pd.DataFrame([dict(feature=name, availability='closed_interval_delivered_by_issue') for name in (
        'lag_1', 'lag_2', 'lag_3', 'lag_4', 'lag_48', 'mean_4', 'mean_48')] + [
            dict(feature=name, availability='clock_at_issue') for name in ('sin_time', 'cos_time')]).to_csv(
                'artifacts/feature_availability.csv', index=False)
