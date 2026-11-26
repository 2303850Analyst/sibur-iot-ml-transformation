import gzip
import hashlib
import json
import shutil
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd


DAY = 86400
REVISION = 'da6280a8f8be1010eae64264c69a90258a288173'
DATA_PATH = '1_environment/slapstack/slapstack/use_cases/wepastacks/2_orders.json'
BASE_URL = f'https://raw.githubusercontent.com/malerinc/slapstack/{REVISION}'
RAW = Path('data/raw')
NORMAL = Path('data/normalized')


def sha(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def obtain(relative, destination, *, compress=False):
    if destination.exists():
        return
    source_path = Path('data/candidate') / relative
    source = source_path.open('rb') if source_path.exists() else urllib.request.urlopen(f'{BASE_URL}/{relative}', timeout=120)
    temp = destination.with_suffix('.download')
    with source, temp.open('wb') as target:
        if compress:
            with gzip.GzipFile(fileobj=target, mode='wb', mtime=0, filename='') as compressed:
                shutil.copyfileobj(source, compressed)
        else:
            shutil.copyfileobj(source, target)
    temp.replace(destination)


def build_data():
    RAW.mkdir(parents=True, exist_ok=True)
    NORMAL.mkdir(parents=True, exist_ok=True)
    manifest_dir = Path('data/manifests')
    manifest_dir.mkdir(parents=True, exist_ok=True)
    orders_path = RAW / 'wepastacks_orders.json.gz'
    obtain(DATA_PATH, orders_path, compress=True)
    obtain('LICENSE', RAW / 'LICENSE-repository.txt')
    obtain('1_environment/slapstack/LICENSE', RAW / 'LICENSE-environment.txt')
    with gzip.open(orders_path, 'rb') as stream:
        raw = stream.read()
    rows = json.loads(raw)
    assert all(len(row) == 6 and row[0] in ('delivery', 'retrieval') and
               all(type(field) is int for field in row[1:]) for row in rows)
    data = pd.DataFrame(rows, columns=['direction', 'source_sku', 'seconds', 'dock', 'batch', 'week'])
    data['source_row'] = np.arange(len(data))
    data['day'] = data.seconds // DAY
    data['tod'] = data.seconds % DAY
    first_day, last_day = map(int, (data.day.min(), data.day.max()))
    actual = data[data.day.between(first_day + 1, last_day - 1)].copy()
    day_count = last_day - first_day - 1
    train_days, validation_days = round(.65 * day_count), round(.15 * day_count)
    train_end = first_day + 1 + train_days
    test_start = train_end + validation_days
    actual['partition'] = np.select((actual.day < train_end, actual.day < test_start), ('train', 'validation'), default='test')
    actual['sku'] = (actual.source_sku - 1) % 20
    actual['evidence'] = 'SIMULATION'
    partitions = dict(train=list(range(first_day + 1, train_end)), validation=list(range(train_end, test_start)),
                      test=list(range(test_start, last_day)))
    assert all(len(partitions[part]) >= minimum for part, minimum in zip(partitions, (28, 7, 14)))
    assert len(set().union(*map(set, partitions.values()))) == day_count
    actual.to_parquet(NORMAL / 'source.parquet', index=False)
    counts = actual[actual.partition.eq('train')].direction.value_counts()
    scales = {direction: float(counts[direction] / train_days) for direction in ('delivery', 'retrieval')}
    manifest = dict(evidence='SIMULATION', status='WEPASTACKS_ADAPTED', commit=REVISION, upstream_path=DATA_PATH,
                    raw_sha256=hashlib.sha256(raw).hexdigest(), gzip_sha256=sha(orders_path),
                    observed_records=len(data), observed_delivery=int(data.direction.eq('delivery').sum()),
                    observed_retrieval=int(data.direction.eq('retrieval').sum()), observed_sku=int(data.source_sku.nunique()),
                    seconds=[int(data.seconds.min()), int(data.seconds.max())], partitions=partitions,
                    excluded_boundary_days=[first_day, last_day], train_pallets_per_day=scales,
                    license_files={path.name: sha(path) for path in RAW.glob('LICENSE*')})
    (manifest_dir / 'wepastacks.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    Path('artifacts/data_audit.md').write_text(f'''# Аудит данных · SIMULATION

Источник: `malerinc/slapstack`, commit `{REVISION}`, `{DATA_PATH}`.
GitHub API ответил 403 из-за лимита; файлы проверены по Git-клону. Репозиторий и пакет среды распространяются по MIT; в каталоге WEPAStacks отдельный файл лицензии не обнаружен. Два текста лицензии сохранены рядом с данными. Сырые пользовательские материалы не передавались вовне.

Проверено {len(data)} шестипольных записей: {manifest['observed_delivery']} delivery, {manifest['observed_retrieval']} retrieval, {manifest['observed_sku']} различных исходных SKU. По документации поставщика каждая запись соответствует одной паллете. Время - секунды от относительного нуля, диапазон {manifest['seconds']}; календарного дня недели нет. Граничные сутки {first_day} и {last_day} исключены как потенциально неполные. Хронологически: {train_days}/{validation_days}/{len(partitions['test'])} полных дней, без перекрытия.

Парсер сохраняет тип, SKU, секунды, док, партию, неделю и индекс записи. В симуляции SKU свёрнуты детерминированно до 20; исходные и адаптированные ряды раздельны. Прореживание по каждому направлению рассчитывается от среднего train; время суток не меняется, кроме явно заданного S6. Прогрев берётся из последних validation-суток. Тестовые недели выбираются блоками с возвращением; последние неполные недели не используются в блочном сценарии, но входят в исходную оценку точности.

30 дней - сценарное воспроизведение короткого внешнего профиля; это не дополнительные 30 дней фактических наблюдений. Длительности, качество, задержки, дефекты, начальный запас, ресурсы и затраты - допущения стенда, не свойства WEPA или СИБУРа. Повторы отличаются seed, но не представляют независимые склады. На стыке блоков возможны календарные артефакты.
''', encoding='utf-8')
    return actual, manifest


def sample(data, manifest, nominal, seed):
    sequence = np.random.default_rng(np.random.SeedSequence([seed, nominal, 812]))
    threshold = np.where(data.direction.eq('delivery'), nominal / manifest['train_pallets_per_day']['delivery'],
                         nominal / manifest['train_pallets_per_day']['retrieval'])
    assert threshold.max() <= 1
    return data[sequence.random(len(data)) < threshold].copy()


def campaign(sampled, manifest, seed, scenario):
    rng = np.random.default_rng(np.random.SeedSequence([seed, 101]))
    test = manifest['partitions']['test']
    blocks = [test[index:index + 7] for index in range(0, len(test) - 6, 7)]
    order = [manifest['partitions']['validation'][-1]] + [day for index in rng.integers(0, len(blocks), 5) for day in blocks[index]][:30]
    groups = {int(day): frame for day, frame in sampled.groupby('day')}
    inbound, outbound = [], []
    for model_day, source_day in enumerate(order):
        group = groups[source_day]
        deliveries = group[group.direction.eq('delivery')]
        releases = group[group.direction.eq('retrieval')]
        hours = deliveries.tod.to_numpy(dtype=float, copy=True)
        if scenario == 'S6' and model_day > 0:
            burst = np.random.default_rng(np.random.SeedSequence([seed, model_day, 201]))
            slots = burst.choice(len(hours), round(.6 * len(hours)), replace=False)
            hours[slots] = burst.uniform(0, 7200, len(slots)) + burst.uniform(28800, 64800)
        inbound.extend(zip((hours + model_day * DAY).tolist(), deliveries.sku, deliveries.source_row))
        outbound.extend(zip(releases.tod + model_day * DAY, releases.sku))
    return sorted(inbound), sorted(outbound), order
