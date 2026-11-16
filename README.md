# JARVIS

A personal AI assistant that runs in the cloud and is used through a browser.
Nothing is installed on the user's devices.

## Requirements

- Python 3.12
- Node.js 20 or newer
- Docker, for PostgreSQL and Redis

## Run

Copy the environment template to the repository-root `.env`, then set its
values. Generate a strong random `JWT_SECRET` with
`python -c "import secrets; print(secrets.token_urlsafe(48))"` and set it
explicitly before production. The production guard checks only that the secret
has at least 32 characters; it does not check strength or uniqueness, and an
unset variable does not fail closed. Never commit `.env` or populated secret
values.

```bash
cp config/env/.env.example .env
docker compose --project-directory . -f infra/compose/docker-compose.yml up -d postgres redis
python3.12 -m venv .venv
.venv/bin/pip install -e . --group dev
set -a
. ./.env
set +a
.venv/bin/alembic -c apps/api/alembic.ini upgrade head
.venv/bin/uvicorn app.main:app --app-dir apps/api --port 8000
```

The Compose command starts only PostgreSQL and Redis. Run the API, worker, and
web app as host processes as shown here; starting all Compose services as well
would bind the same API and web ports.

In a second terminal, start the worker. Tasks stay queued until it is running:

```bash
.venv/bin/python -m jarvis_worker
```

In a third terminal:

```bash
cd apps/web
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000).

PostgreSQL is published on port 5433 and Redis on 6380, because 5432 and 6379
are often already taken.

## Run on local Kubernetes

Requires `kind` and `kubectl`. The local cluster uses HTTP and does not
configure TLS. Review [Kubernetes credential handling](docs/developer/INSTALLATION.md#kubernetes-credentials)
before applying Secrets; never commit populated secret values. Build the images
first (`docker compose --project-directory . -f infra/compose/docker-compose.yml build`), then:

```bash
kind create cluster --name jarvis --config infra/k8s/base/kind-config.yaml
kind load docker-image jarvis-api:local jarvis-worker:local jarvis-web:local --name jarvis

kubectl apply -f infra/k8s/base/namespace.yaml -f infra/k8s/base/rbac.yaml
kubectl apply -f infra/k8s/secrets/app/ -f infra/k8s/secrets/data/
kubectl apply -f infra/k8s/data/
kubectl apply -f infra/k8s/base/migrate-job.yaml
kubectl -n jarvis wait --for=condition=Complete job/migrate --timeout=180s
kubectl apply -f infra/k8s/app/
kubectl -n jarvis wait --for=condition=Available deployment/api deployment/worker deployment/web --timeout=240s
```

The kind config maps NodePorts to host ports 8001 (API) and 3001 (web), kept
distinct from the Compose ports above so both stacks can run at the same
time. LocalStack has no NodePort; reach it with
`kubectl -n jarvis port-forward svc/localstack 4566:4566`. Manual
verification URLs, curl commands, and a disposable test account are in
[evidence/runs/local-deploy-20261116/local-verification-urls.txt](evidence/runs/local-deploy-20261116/local-verification-urls.txt).

## Configure a model

Set `OPENAI_API_KEY` in the root `.env` created above. Without it the API
starts normally, `/api/v1/health` reports `"configured": false`, and sending a
message returns 503 `ai_not_configured`. JARVIS does not invent replies.

## Test

```bash
.venv/bin/python -m pytest
```

The auth and conversation tests need the database running.

## Layout

```text
apps/api          FastAPI service
apps/worker       Durable executor. Claims jobs from PostgreSQL.
apps/web          Next.js 15 interface. Talks only to the API.
packages/shared   Agent registry and task state machine
database          Alembic migrations
infra/compose     Docker Compose stack
infra/k8s         Kubernetes manifests for local kind deployment
docs              Architecture, API, security, user and developer guides
```

## Guides

- [User guide](docs/apps/USER_GUIDE.md)
- [Web app](docs/apps/web.md)
- [Worker](docs/apps/WORKER.md)
- [Developer guide](docs/developer/GUIDE.md)
- [Installation](docs/developer/INSTALLATION.md)
- [Architecture](docs/architecture/ARCHITECTURE.md)

## Status

See [docs/architecture/roadmap.md](docs/architecture/roadmap.md#current-implementation-status).
Task execution is durable. The worker claims jobs from PostgreSQL with
`FOR UPDATE SKIP LOCKED`. Without the worker, tasks are queued and never
claimed. Row-level security isolates each account. Nothing is installed on
the user's devices.
