# JARVIS — Implementation Roadmap

Each phase ends with working, tested software and updated documentation. A
phase is not done because code was written; it is done because its tests pass.

## Phase 1 — Foundation (current)

Monorepo, configuration, structured logging, PostgreSQL with migrations, the
identity tables, authentication (register, login, refresh, logout), the task
state machine with recorded transitions, agent registry, health endpoints, and
the API skeleton the UI talks to.

**Done when:** a user can register and log in, a task can be created and moved
through every legal state transition with each one recorded, illegal
transitions are rejected, and one user cannot read another's data.

## Phase 2 — User management

Preferences, session listing and revocation, account deletion that actually
deletes, password change.

## Phase 3 — Conversation and model integration

Conversations and messages persisted per user, the language-model provider
interface, streaming responses over SSE, a working chat view. Requires an
OpenAI-compatible credential; without one the UI shows "not configured" rather
than a canned reply.

## Phase 4 — Persistent memory

Memory CRUD, keyword and semantic retrieval, the proposal-then-commit rule so
nothing is remembered without confirmation, privacy classes, export and delete.

## Phase 5 — Agent orchestration

The supervisor plans a task into steps, routes each step to an agent, checks
permissions, and combines results. First real agent behaviours: research
(web) and personal assistant (briefing from data JARVIS actually has).

## Phase 6 — Durable execution

The worker process, Redis queue, heartbeat recovery, idempotent steps,
cancellation, and the test that kills the worker mid-task and asserts
completion after restart.

## Phase 7 — Voice

Browser speech capture, provider-abstracted transcription and synthesis,
push-to-talk, English and Hindi, text fallback. No wake word.

## Phase 8 — Integrations

OAuth 2.0 connection flow, encrypted token storage, Gmail and Google Calendar
first, the standardized integration interface so further providers are
additive.

## Phase 9 — Notifications and automations

In-app notifications, browser notifications where permission is granted, email
as the guaranteed channel, scheduled briefing, user-defined automations.

## Phase 10 — Security hardening

Prompt-injection tests against retrieved content, permission-escalation tests,
rate limiting, CSRF and XSS verification, dependency scanning in CI.

## Phase 11 — Observability

OpenTelemetry traces, per-task token and cost accounting, queue depth, worker
health, an admin view that shows none of the user's content.

## Phase 12 — Production deployment

Managed Postgres and Redis, object storage, TLS, CI/CD, backups, and a
recovery procedure that has been executed rather than only described.

## Explicitly deferred

- Native applications of any kind. Out of scope permanently.
- Wake-word activation. Impossible under the zero-install constraint.
- Local Mac or iOS control. Impossible under the zero-install constraint.
- Autonomous financial transactions. HIGH risk, and not in scope even then
  without a specific, confirmed instruction per transaction.
- Multi-user collaboration on a single JARVIS account. `project_members`
  exists so this is possible later; it is not built now.
