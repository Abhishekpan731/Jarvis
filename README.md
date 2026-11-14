# JARVIS

A personal AI assistant that runs in the cloud and is used through a browser.
Nothing is installed on the user's devices.

## Requirements

- Python 3.12
- Node.js 20 or newer
- Docker, for PostgreSQL and Redis

## Run

```bash
docker compose up -d
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
DATABASE_URL_SYNC=postgresql+psycopg://jarvis:jarvis@localhost:5433/jarvis \
  .venv/bin/alembic -c apps/api/alembic.ini upgrade head
.venv/bin/uvicorn app.main:app --app-dir apps/api --port 8000
```

In another terminal:

```bash
cd apps/web
npm install
npm run dev
```

Open http://localhost:3000.

PostgreSQL is published on port 5433 and Redis on 6380, because 5432 and 6379
are often already taken.

## Configure a model

Copy `.env.example` to `.env` and set `OPENAI_API_KEY`. Without it the API
starts normally, `/api/v1/health` reports `"configured": false`, and sending a
message returns 503 `ai_not_configured`. JARVIS does not invent replies.

## Test

```bash
.venv/bin/python -m pytest
```

The auth and conversation tests need the database running.

## Layout

```
apps/api        FastAPI service
apps/web        Next.js interface
packages/shared Agent registry and task state machine
database        Alembic migrations
docs            Architecture, API, security
```

## Status

See [docs/architecture/IMPLEMENTATION_STATUS.md](docs/architecture/IMPLEMENTATION_STATUS.md).
Task execution is in-process and not durable. The worker is a later phase.
