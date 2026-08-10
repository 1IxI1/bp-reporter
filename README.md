# BP Reporter

Сервис принимает измерения Withings BPM Connect через Withings Cloud, сохраняет их
в SQLite и публикует итог серии в закрытый Telegram-канал.

## Режимы

### Серия `/bp`

1. Владелец пишет боту `/bp` в личном чате.
2. Первые два новых измерения назначаются левой руке.
3. Бот просит переставить манжету.
4. Следующие два измерения назначаются правой руке.
5. В канал уходит одно тихое сообщение со средними и исходными значениями.

Команды `/status`, `/cancel` и `/retry` доступны только
`TELEGRAM_OWNER_USER_ID` и только в личном чате.

### Без `/bp`

Первое свежее измерение открывает часовое окно. Если второе свежее измерение
приходит не позже чем через час, оба считаются правой рукой и одним сообщением
публикуются в канал. Одиночное измерение после истечения окна не публикуется.

Любой режим дополнительно требует, чтобы измерение было:

- позже `LIVE_AFTER`;
- не старше `MEASUREMENT_MAX_AGE_SECONDS` на момент первого получения;
- ранее не обработано.

Архивные и повторные данные сохраняются, но не публикуются.

## Технологии

- Python 3.12+
- FastAPI и httpx
- SQLite с WAL
- Docker Compose
- pytest

Redis и отдельный планировщик не требуются. Webhook, исходящие сообщения,
OAuth-токены, сессии и курсоры polling сохраняются в одной SQLite-базе.

## Withings API

Используются актуальные публичные endpoints:

- авторизация: `GET https://account.withings.com/oauth2_user/authorize2`;
- токены: `POST https://wbsapi.withings.net/v2/oauth2`, `action=requesttoken`;
- измерения: `POST https://wbsapi.withings.net/measure`, `action=getmeas`;
- подписка: `POST https://wbsapi.withings.net/notify`, `action=subscribe`.

Scope: `user.metrics`. Значения типов 9, 10 и 11 вычисляются как
`value * 10 ** unit`. Все страницы `getmeas` загружаются до `more=0`.

В документации Withings от 2026 года есть противоречие: специализированный
каталог notification categories указывает для давления `appli=4`, а новый
сводный `llms.md` ошибочно называет `appli=16` (в каталоге это activity).
Поэтому дефолт сервиса равен `4`, но может быть изменен через
`WITHINGS_BP_APPLI`.

Источники:

- <https://developer.withings.com/openapi.yaml>
- <https://developer.withings.com/llms.md>
- <https://developer.withings.com/developer-guide/v3/integration-guide/public-health-data-api/data-api/notifications/notification-content>

## Создание Withings Application

После создания организации в Withings Partner Hub:

1. Откройте `Applications` и нажмите `Create application`.
2. Выберите Public API / App-to-app и EU Medical Cloud.
3. Для первой настройки подойдет окружение Development.
4. Название: `BP Reporter`.
5. Integration: `app_to_app`.
6. Callback URL: `https://<домен>/oauth/callback`.
7. Сохраните выданные Client ID и Client Secret только в серверный `.env`.

Webhook URL в dashboard указывать не требуется: после OAuth сервис зарегистрирует
его через Notify API. URL должен быть доменным HTTPS URL на порту 443 и отвечать
на `HEAD`:

```text
https://<домен>/withings/webhook?token=<WITHINGS_WEBHOOK_SECRET>
```

Сервис сам добавляет query-параметр `token` к `WITHINGS_WEBHOOK_URL` при подписке.

После запуска откройте в браузере:

```text
https://<домен>/oauth/start
```

Авторизуйтесь тем Withings-аккаунтом, к которому уже привязан BPM Connect.
Authorization code действует 30 секунд, поэтому callback должен быть уже запущен.

Затем зарегистрируйте webhook:

```bash
curl -X POST \
  -H "X-Admin-Secret: $APP_SECRET" \
  https://<домен>/admin/subscribe-webhook
```

## Telegram

1. Перевыпустите опубликованный ранее токен через BotFather командой `/revoke`,
   затем получите новый токен.
2. Откройте личный чат с ботом и нажмите Start.
3. Добавьте бота администратором закрытого канала с правом публикации.
4. Укажите числовой user ID владельца и ID личного чата.
5. Для внутреннего ID канала `3738843374` Bot API-значение имеет вид
   `-1003738843374`.

`TELEGRAM_SILENT=true` добавляет `disable_notification=true` ко всем сообщениям,
то есть подписчики получают тихую публикацию.

Тесты:

```bash
curl -X POST \
  -H "X-Admin-Secret: $APP_SECRET" \
  'https://<домен>/admin/test-telegram?target=private'

curl -X POST \
  -H "X-Admin-Secret: $APP_SECRET" \
  'https://<домен>/admin/test-telegram?target=channel'
```

## Конфигурация

Создайте `.env` из `.env.example`. Файл `.env` не попадает в образ и Git.

Обязательные значения:

```dotenv
WITHINGS_CLIENT_ID=
WITHINGS_CLIENT_SECRET=
WITHINGS_REDIRECT_URI=https://<домен>/oauth/callback
WITHINGS_WEBHOOK_URL=https://<домен>/withings/webhook
WITHINGS_WEBHOOK_SECRET=<отдельная случайная строка>
WITHINGS_USER_ID=

TELEGRAM_BOT_TOKEN=<новый токен>
TELEGRAM_OWNER_USER_ID=
TELEGRAM_PRIVATE_CHAT_ID=
TELEGRAM_CHANNEL_ID=-1003738843374
TELEGRAM_SILENT=true

DATABASE_URL=sqlite:////data/bp-reporter.db
APP_SECRET=<другая случайная строка>
TIMEZONE=Europe/Minsk
LIVE_AFTER=2026-08-11T00:00:00Z
SESSION_TIMEOUT_MINUTES=20
POLLING_ENABLED=true
POLLING_INTERVAL_SECONDS=10
```

`WITHINGS_USER_ID` можно оставить пустым до первого OAuth. После callback его
можно взять из SQLite или debug endpoint и зафиксировать в `.env`.

`LIVE_AFTER` следует выставить непосредственно перед включением live-обработки,
особенно если сначала импортируется архив. Если параметр пуст, сервис безопасно
сохраняет данные, но ничего не назначает сессиям и не публикует.

Polling раз в 10 секунд намеренно агрессивнее рекомендации Withings. Последний
статус API виден в `/health`; статус `601` означает фактическое ограничение частоты.
Webhook остается основным каналом доставки.

## Запуск

Локально:

```bash
uv sync --extra dev
uv run uvicorn app.main:app --reload
```

В Docker:

```bash
mkdir -p data
docker compose up -d --build
docker compose logs -f bp-reporter
```

Контейнер слушает только `127.0.0.1:8787`; перед ним нужен HTTPS reverse proxy.
SQLite находится в `./data` и имеет права `0600` внутри контейнера. Каталог нужно
включить в закрытые резервные копии.

Проверка:

```bash
curl https://<домен>/health
curl -X POST \
  -H "X-Admin-Secret: $APP_SECRET" \
  https://<домен>/admin/poll-now
curl -H "X-Admin-Secret: $APP_SECRET" \
  https://<домен>/debug/recent-measurements
```

Debug/admin endpoints принимают `X-Admin-Secret` либо
`Authorization: Bearer <APP_SECRET>`.

## Надежность

- Webhook сначала фиксируется в SQLite и сразу получает `202`.
- Повторный webhook увеличивает счетчик, но не создает второе событие.
- Measure groups дедуплицируются по `userid + grpid`; без `grpid` используется
  hash от пользователя, времени и SYS/DIA/pulse.
- Незавершенные сессии и Telegram outbox переживают рестарт.
- Новый refresh token атомарно заменяет старый при каждой ротации.
- Withings transport, 5xx, `522` и `601` повторяются с backoff.
- При однозначном отказе Telegram сообщение повторяется с backoff.
- При timeout после отправки результат считается `uncertain` и не повторяется:
  это предотвращает двойную медицинскую публикацию.
- Логи структурированные и по умолчанию не содержат значений давления, токенов
  или client secret.

## Проверка Wi-Fi Sync

Этот критерий проверяется на реальном приборе до включения публикаций:

1. Закройте Withings App принудительно.
2. Выключите Bluetooth на телефоне.
3. Оставьте BPM Connect в зоне домашнего Wi-Fi.
4. Сделайте одно измерение.
5. Через веб-интерфейс Withings убедитесь, что измерение появилось в аккаунте.
6. После OAuth вызовите `/admin/poll-now` и проверьте запись через debug endpoint.

Factory reset и отвязка прибора не нужны. Телефон после этой проверки нужен только
для изменения Wi-Fi устройства.

## Импорт истории

Публичный API не записывает давление. Используйте официальный CSV import в
веб-интерфейсе Withings до включения webhook.

`convert_history.py` принимает нормализованный промежуточный CSV:

```csv
date,systolic,diastolic,pulse
2025-04-10 08:30:00,146,90,71
```

Конвертация:

```bash
uv run python convert_history.py history-normalized.csv history-output \
  --template Import_Blood_Pressure.csv \
  --existing already-in-withings.csv \
  --timezone Europe/Minsk
```

Скрипт:

- сохраняет первую строку скачанного официального шаблона byte-for-byte;
- без шаблона использует `Date,Heart rate,Systole,Diastole`;
- пишет дату как `yyyy-mm-dd hh:mm:ss` и разделитель-запятую;
- создает файлы максимум по 300 измерений;
- исключает дубликаты и строки из `--existing`;
- создает `conversion-report.json` с номерами и причинами пропущенных строк.

Парсер исходного семейного архива намеренно не угадывает пока неизвестный формат.
После получения реального примера его нужно преобразовать в указанный
нормализованный CSV или добавить отдельный адаптер.

## Тесты

```bash
uv sync --extra dev
uv run pytest
```

Тесты покрывают `unit`, ротацию refresh token, pagination, webhook HEAD и
дедупликацию, четыре состояния `/bp`, автоматическую пару правой руки, backfill,
таймаут, тихую Telegram-публикацию и CSV batches.
