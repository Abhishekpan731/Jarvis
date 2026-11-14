# JARVIS — System Architecture

**Status:** Phase 1 (foundation). This document is the source of truth for
structure, boundaries, and constraints. It is updated at the end of every phase.

**Product principle:** One AI brain. One persistent memory. Multiple specialist
agents. One continuous task engine. Accessible everywhere through a browser.
Zero required device installations.

---

## 1. What JARVIS is — and is not

JARVIS is a cloud-hosted personal AI operating assistant. The browser is the
only user interface. All reasoning, memory, orchestration, and execution run on
infrastructure the user does not install.

JARVIS is **not**:

- A native macOS, iOS, or iPadOS application.
- A browser extension or local background agent.
- A system that controls the user's Mac, iPhone, or local filesystem.
- An always-on wake-word listener. Browsers cannot do this without an open tab
  and an explicit user gesture, and we do not pretend otherwise.

Anything JARVIS cannot actually do is surfaced in the UI as unavailable rather
than mocked. See §8.

---

## 2. System context

```mermaid
flowchart TB
    subgraph devices ["User devices — browser only, nothing installed"]
        mac["macOS Safari / Chrome"]
        iphone["iPhone Safari"]
        ipad["iPad Safari"]
    end

    subgraph edge ["Edge"]
        proxy["Reverse proxy + TLS<br/>Caddy"]
    end

    subgraph app ["JARVIS cloud"]
        web["apps/web<br/>Next.js UI"]
        api["apps/api<br/>FastAPI"]
        worker["apps/worker<br/>task executor"]
        subgraph data ["Data plane"]
            pg[("PostgreSQL 16<br/>+ pgvector")]
            redis[("Redis 7<br/>queue + cache")]
            s3[("S3-compatible<br/>object storage")]
        end
    end

    subgraph external ["External — only when the user connects them"]
        llm["LLM providers<br/>OpenAI-compatible"]
        speech["Speech providers<br/>STT / TTS"]
        oauth["OAuth providers<br/>Google, Microsoft, GitHub…"]
    end

    mac & iphone & ipad -->|HTTPS / WSS| proxy
    proxy --> web
    proxy --> api
    web -->|REST + SSE| api
    api --> pg & redis
    worker --> pg & redis & s3
    api -.->|provider interface| llm & speech
    api -.->|OAuth 2.0| oauth
    worker -.-> worker
```

The web app never talks to a database, a model provider, or an integration
directly. Every privileged operation goes through the API, which enforces
authentication, per-user isolation, and permission checks.

---

## 3. Repository layout

```
jarvis/
  apps/
    api/            FastAPI service: auth, conversations, memory, tasks, agents
    worker/         Durable task executor (separate process, same codebase)
    web/            Next.js UI
  packages/
    shared/         Pydantic models shared by api and worker (no I/O)
    agent-contracts/ Agent identity, capability, and permission definitions
  infrastructure/
    docker/         Dockerfiles
    caddy/          Reverse proxy config
  database/
    migrations/     Alembic migrations (the only way schema changes)
  docs/
    architecture/   This document and the diagrams
    api/            API conventions
    security/       Threat model and security boundaries
  tests/            Cross-service integration tests
  scripts/          Dev tooling
  docker-compose.yml
  .env.example
```

`packages/shared` contains types only. It imports nothing from `apps/`, so the
API and worker cannot drift apart on the shape of a task, a memory, or an agent
result.

---

## 4. Backend

**FastAPI + Pydantic v2 + SQLAlchemy 2.0 (async) + Alembic.**

| Concern | Choice | Why |
|---|---|---|
| API | FastAPI | Async, typed, generates OpenAPI |
| Validation | Pydantic v2 | One schema language for API, DB, and agents |
| ORM | SQLAlchemy 2.0 async | Explicit SQL, asyncpg driver |
| Migrations | Alembic | Schema changes are reviewable and reversible |
| Auth | JWT access token + rotating refresh token | Short-lived credentials, revocable sessions |
| Streaming | Server-Sent Events | Works through proxies; no socket state to recover |
| Tasks | Redis list queue + PostgreSQL state | Redis is fast; Postgres is the source of truth |
| Vectors | pgvector | One database, transactional with the memories it indexes |

### Provider interfaces

Model and speech providers sit behind interfaces in `apps/api/app/providers/`.
Business logic names a capability (`complete`, `transcribe`, `synthesize`),
never a vendor or a model. The concrete provider and model come from
configuration. A missing credential degrades to an explicit "not configured"
response — it never falls back to fabricated output.

```python
class LanguageModel(Protocol):
    name: str
    async def complete(self, messages: list[Message], **opts) -> Completion: ...
    def stream(self, messages: list[Message], **opts) -> AsyncIterator[str]: ...
```

---

## 5. The task engine

This is the part that makes JARVIS an operating assistant rather than a
chatbot. A task is a durable record, not an in-memory coroutine.

### State machine

```mermaid
stateDiagram-v2
    [*] --> CREATED
    CREATED --> UNDERSTANDING
    UNDERSTANDING --> PLANNING
    PLANNING --> WAITING_FOR_APPROVAL: high-risk step
    PLANNING --> EXECUTING: all steps low/medium risk
    WAITING_FOR_APPROVAL --> EXECUTING: approved
    WAITING_FOR_APPROVAL --> CANCELLED: rejected
    EXECUTING --> VERIFYING
    VERIFYING --> COMPLETED
    EXECUTING --> WAITING_FOR_USER: input needed
    WAITING_FOR_USER --> EXECUTING
    EXECUTING --> PAUSED
    PAUSED --> EXECUTING
    UNDERSTANDING --> FAILED
    PLANNING --> FAILED
    EXECUTING --> FAILED
    FAILED --> REQUIRES_RETRY
    REQUIRES_RETRY --> EXECUTING
    CREATED --> CANCELLED
    EXECUTING --> CANCELLED
```

Every transition is written to `task_events` in the same transaction as the
state change. There is no state change without a recorded event, and no event
without a state change.

### Durability rules

1. **PostgreSQL holds the truth.** Task state, step state, and checkpoints live
   in Postgres. Redis only holds the queue of task IDs waiting for a worker.
2. **A worker crash loses nothing.** On startup a worker reclaims tasks stuck
   in `EXECUTING` past their heartbeat timeout and requeues them. Steps are
   idempotent, keyed by `(task_id, step_index, attempt)`.
3. **Closing the browser changes nothing.** The browser is a subscriber to task
   events, not a participant in execution.
4. **Restart survival is tested**, not assumed. The integration test kills the
   worker mid-task and asserts the task still completes.

### Approval gating

Each agent action carries a risk class (see
[security model](../security/SECURITY.md)). A plan containing any `HIGH` risk
step transitions to `WAITING_FOR_APPROVAL` and does not proceed until the user
approves that specific step. Approval is bound to the exact action payload, so
a step cannot be swapped after approval.

---

## 6. Agent architecture

A supervisor is the only component that plans. Specialist agents execute one
assigned step and return structured output. They cannot plan, delegate, or call
tools outside their declared permissions.

```mermaid
flowchart LR
    user["User instruction"] --> orch["Supervisor / orchestrator"]
    orch --> plan["Plan: ordered steps"]
    plan --> gate{"Any HIGH risk step?"}
    gate -->|yes| approval["WAITING_FOR_APPROVAL"]
    gate -->|no| exec["Execute steps"]
    approval -->|approved| exec
    exec --> a1["Personal"]
    exec --> a2["Email"]
    exec --> a3["Calendar"]
    exec --> a4["Research"]
    exec --> a5["Developer"]
    exec --> a6["Document"]
    exec --> a7["Finance"]
    exec --> a8["Automation"]
    a1 & a2 & a3 & a4 & a5 & a6 & a7 & a8 --> verify["Verify + combine"]
    verify --> result["Result + memory update"]
```

Every agent is a data record, not a class hierarchy: an ID, a description, a
capability list, a permission list, input/output schemas, a timeout, and a
retry policy. Adding an agent means adding a record and a handler, not editing
the orchestrator.

Phase 1 registers the eight agents and their contracts. It does not yet give
them real tools — an agent with no connected integration reports that fact
instead of inventing a result.

---

## 7. Memory

Ten categories: personal profile, communication preferences, professional
context, current projects, long-term goals, technical knowledge, decisions,
recurring tasks, conversation summaries, user-defined instructions.

Two retrieval paths, merged by a weighted score:

- **Keyword** — PostgreSQL full-text search (`tsvector`), exact and fast.
- **Semantic** — pgvector cosine distance over embeddings.

Ranking combines semantic similarity, keyword rank, recency decay, and an
explicit importance weight the user can set. Every memory stores its source
(which conversation, which message) and a privacy class. A memory tagged
`sensitive` is excluded from any agent whose permissions do not include it.

**Nothing becomes a memory automatically.** The orchestrator may *propose* a
memory; only the user, or an explicit user instruction to remember something,
commits it. Unverified inferences are never persisted as fact.

---

## 8. What zero-install makes impossible

These are constraints of the platform, not missing features. They are
documented here so they are never quietly "implemented" by faking them.

| Requested capability | Reality | What JARVIS does instead |
|---|---|---|
| Always-on wake word | A browser tab cannot listen permanently, and iOS suspends background tabs | Push-to-talk microphone button; optional Siri Shortcut that opens JARVIS with a prompt |
| Control of native Mac apps | No local process exists to drive them | Cloud browser automation and official APIs only |
| Local filesystem access | The browser sandbox forbids it | Cloud document storage and connected drives (Drive, OneDrive) |
| Background work on the device | Impossible without an install | All background work runs in the cloud worker |
| Reliable push on iOS Safari | Web Push on iOS requires the site to be added to the Home Screen, which is a user action we cannot require | In-app notifications always; web push where the browser grants it; email as the guaranteed channel |
| Unrestricted computer use | Would require a local agent | Isolated cloud browser with per-session limits, timeouts, and an audit log |

---

## 9. Cross-device continuity

All state that matters lives in PostgreSQL, keyed by `user_id`: conversations,
messages, memories, tasks, approvals, notifications, preferences. The browser
holds only the access token (in memory) and the refresh token (HttpOnly,
Secure, SameSite cookie).

Opening JARVIS on a second device is a login plus a fetch. There is no sync
protocol because there is no local replica.

---

## 10. Deployment

`docker-compose.yml` runs the full stack locally: Postgres, Redis, API, worker,
web, and Caddy. The same images deploy to a single host or a managed platform
without code changes; only environment variables differ.

Production expects managed PostgreSQL, managed Redis, and S3-compatible object
storage. Terraform under `infrastructure/terraform/` is intentionally empty in
Phase 1 — infrastructure is not provisioned until the application justifies it.

---

## 11. Phase status

| Phase | Scope | Status |
|---|---|---|
| 1 | Architecture, monorepo, database, auth, health, task state machine | **In progress** |
| 2 | User management, sessions, preferences | Not started |
| 3 | Conversations and model provider integration | Not started |
| 4 | Persistent memory and retrieval | Not started |
| 5 | Agent orchestration | Not started |
| 6 | Durable task execution and worker | Not started |
| 7 | Voice interface | Not started |
| 8 | External integrations | Not started |
| 9 | Notifications and automations | Not started |
| 10 | Security hardening | Not started |
| 11 | Observability | Not started |
| 12 | Production deployment | Not started |
