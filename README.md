# BP Reporter

BP Reporter collects blood-pressure measurements from a Withings BPM Connect through
the official Withings Cloud API, stores them in SQLite, and publishes session summaries
to a private Telegram channel.

## Behavior

- `/bp` starts a four-measurement session: two on the left arm, then two on the right.
- The owner receives a private confirmation after every measurement.
- After the fourth measurement, one silent summary is posted to the family channel.
- Without `/bp`, two fresh measurements taken within one hour are treated as right-arm
  measurements and published together.
- After the first measurement without `/bp`, an owner-only inline button can start the
  full cycle and count that measurement as the first left-arm reading.
- Old, stale, incomplete, and duplicate measurements are stored but never published.
- Sessions, OAuth tokens, webhook events, deduplication, and Telegram outbox state survive
  restarts.

## Stack

Python 3.12+, FastAPI, httpx, SQLite/WAL, Docker Compose, and pytest.

## Configuration

Copy `.env.example` to `.env` and set at least:

```dotenv
WITHINGS_CLIENT_ID=
WITHINGS_CLIENT_SECRET=
WITHINGS_REDIRECT_URI=https://bp.example.com/oauth/callback
WITHINGS_WEBHOOK_URL=https://bp.example.com/withings/webhook
WITHINGS_WEBHOOK_SECRET=

TELEGRAM_BOT_TOKEN=
TELEGRAM_OWNER_USER_ID=
TELEGRAM_PRIVATE_CHAT_ID=
TELEGRAM_CHANNEL_ID=-1001234567890

DATABASE_URL=sqlite:////data/bp-reporter.db
APP_SECRET=
TIMEZONE=Europe/Minsk
LIVE_AFTER=2026-08-11T00:00:00Z
```

`LIVE_AFTER` is a mandatory publication safety boundary. Leave it unset to store data
without assigning or publishing measurements.

## Run

```bash
uv sync --extra dev
uv run uvicorn app.main:app --reload
```

Or with Docker:

```bash
mkdir -p data
docker compose up -d --build
```

The container binds to `127.0.0.1:8787` and must be placed behind a public HTTPS reverse
proxy. Keep the SQLite directory private and backed up.

## Withings Setup

Create a Public API / App-to-app application in the EU cloud and register:

```text
https://bp.example.com/oauth/callback
```

Then open `https://bp.example.com/oauth/start`, authorize `user.metrics`, and subscribe:

```bash
curl -X POST \
  -H "X-Admin-Secret: $APP_SECRET" \
  https://bp.example.com/admin/subscribe-webhook
```

The service uses `getmeas` types `9`, `10`, and `11`, applies
`value * 10 ** unit`, follows pagination, and rotates refresh tokens. Blood-pressure
notifications use `appli=4`; this value was verified against the live API.

## Telegram Setup

Start a private chat with the bot, add it as a channel administrator, and configure the
owner, private-chat, and channel IDs. `TELEGRAM_SILENT=true` sends all messages with
`disable_notification=true`.

Available owner-only private commands: `/bp`, `/status`, `/cancel`, and `/retry`.

## History Import

Withings does not provide a public write API for blood pressure. Convert historical data
for the official web CSV importer:

```bash
uv run python convert_history.py bp.jsonl history-output --timezone Europe/Minsk
```

The converter supports normalized CSV and Telegram JSONL, preserves an optional official
template header, deduplicates rows, reports invalid records, and splits output into files
of at most 300 measurements.

## Tests

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Health and protected diagnostics are available at `/health`, `/debug/session`, and
`/debug/recent-measurements`.
