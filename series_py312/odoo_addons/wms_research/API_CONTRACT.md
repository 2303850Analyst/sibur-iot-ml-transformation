# API референсного стенда · ODOO_INTEGRATION

Сервис доступен на `127.0.0.1:18069`; база `wms_reference`. Роли `research-tsd`, `research-gateway`, `research-quality`. Имена открыты, пароли сгенерированы локально в `.env`. Операции записи используют аутентифицированную сессию Odoo и CSRF, не административные пароли и не анонимный вызов.

| Метод | Путь | Назначение |
|---|---|---|
| POST | `/web/session/authenticate` | Открыть сессию Odoo (`jsonrpc`, `params.db/login/password`) |
| GET | `/wms/session` | Доступные роли и CSRF-токен |
| GET | `/wms/tsd` | Браузерный экран, сессия ТСД |
| GET | `/wms/status?run_id=DEMO&pallet_id=TSD-01` | Ожидаемый SKU/lot/количество, документы, ячейка, остаток, аудит |
| POST | `/wms/event` | Два form-поля: `csrf_token` и `message` (JSON) |

`message`: `run_id`, `pallet_id`, `operation`, `event_id`, `device_id`, `occurred_at` (UTC `YYYY-MM-DD HH:MM:SS`), `schema_version`=1, `payload`. Для `receive` передаются `sku`,`lot`,`quantity`; для `place` и `final_place` - `address`; для `presence` - целое `occupied` от 0 до 40. Ответ содержит `status`, `audit_id`, `picking_id`, `snapshot`, `reason`. Коды: 200 applied, 409 conflict, 422 rejected, 400 invalid, 403 forbidden. Одинаковый повтор возвращает исходный прикладной результат, а не создаёт новую складскую операцию.

ТСД: `receive / reserve / place / final_reserve / final_place`; IoT: `observe / presence`; качество: `release`. Датчик не подтверждает приёмку и не освобождает партию из карантина. `GET /wms/status` - этап проверки ожидаемого документа перед подтверждением; идентификация повторяется внутри транзакции. Поддерживается собственная резервация мест, а остаток изменяет только штатное проведение Odoo.

Рабочий Python-клиент (запустить из корня проекта, предварительно подставив только в локальное окружение `TSD_PASSWORD` из `.env`):

```python
from scripts.odoo_client import Odoo, envelope

api = Odoo('tsd')
expected = api.status('TSD-01')['snapshot']
print(api.event(envelope('TSD-01', 'receive', {key: expected[key] for key in ('sku', 'lot', 'quantity')})))
```

Брокер слушает только на локальном интерфейсе хоста; MQTT без пароля - техническое допущение однопользовательского стенда, не промышленная конфигурация. MQTT QoS 1 не гарантирует единственность складской транзакции. Шлюз подтверждает MQTT после долговременной записи в outbox и удаляет запись из очереди только после прикладного ответа Odoo.
