# AGENTS.md

These instructions apply to the entire repository.

## Project Goal

BP Reporter is a single-user medical-data service that moves Withings BPM Connect
measurements to a private Telegram channel. Favor small, reliable changes over additional
infrastructure.

## Required Invariants

- A `/bp` session assigns exactly two measurements to the left arm and two to the right.
- Without `/bp`, only two eligible measurements within one hour form a right-arm report.
- Never publish measurements at or before `LIVE_AFTER` or older than the configured age.
- Deduplicate by Withings `userid + grpid`, with the documented fallback key when needed.
- A webhook must be persisted before returning and processed outside the request path.
- Repeated webhooks and overlapping polling ranges must not duplicate Telegram messages.
- OAuth refresh tokens rotate; always persist the replacement token atomically.
- Store timestamps in UTC and convert only for display.
- Telegram commands are private and restricted to `TELEGRAM_OWNER_USER_ID`.
- Telegram reports remain silent unless configuration explicitly changes that behavior.

## Repository Map

- `app/main.py`: FastAPI application, routes, authentication, and lifecycle.
- `app/service.py`: session state machine, ingestion, outbox, formatting, and deduplication.
- `app/clients.py`: Withings and Telegram HTTP clients.
- `app/db.py`: SQLite schema and transaction wrapper.
- `app/config.py`: environment configuration and safety defaults.
- `convert_history.py`: normalized CSV and Telegram JSONL history converter.
- `tests/`: behavior and API contract tests.

## External API Contracts

- Withings API base URL is `https://wbsapi.withings.net`.
- OAuth token requests use `/v2/oauth2` with `action=requesttoken`.
- Measurements use `/measure`, `action=getmeas`, and types `9,10,11`.
- Decode every measurement as `value * 10 ** unit`.
- Continue fetching while `more` is set, using the returned `offset`.
- Blood-pressure notifications use `/notify`, `action=subscribe`, and `appli=4`.
- OAuth and webhook `HEAD` checks return HTTP 200 for dashboard compatibility.
- Telegram requests use HTML parse mode; escape any future user-controlled text.
- Do not enable verbose `httpx` logging because Telegram embeds its token in request URLs.

## Development

Use Python 3.12-compatible syntax and keep network work asynchronous. Keep SQLite
transactions short and never perform HTTP calls while holding a database transaction.
Do not add Redis, a queue service, or a scheduler unless a concrete requirement needs it.

Run before committing:

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
git diff --check
```

Add tests for state transitions, deduplication, backfill protection, token rotation, or
message formatting whenever those behaviors change.

## Security And Data

- Never commit `.env`, OAuth tokens, Telegram tokens, SQLite files, JSONL exports, or
  generated history files.
- Never log blood-pressure values or secrets.
- Keep admin/debug endpoints protected by `APP_SECRET`.
- Do not weaken OAuth state validation or webhook query-secret validation.
- Treat ambiguous Telegram send outcomes as non-retryable to avoid duplicate reports.
- Do not reset, unlink, or factory-reset the Withings device.

## Git

Commit messages use a short capitalized subject with no body, for example:
`Add webhook retry`.

Commit and push only when requested. Do not include unrelated local changes.
