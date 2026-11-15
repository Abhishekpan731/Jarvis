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

## Phase 6 — Durable execution (done)

The worker process claims from a PostgreSQL job table with `FOR UPDATE SKIP
LOCKED`, holds a lease it heartbeats, and reaps expired leases on restart.
Cancellation and idempotency keys are in place. Covered by
`tests/test_durable_jobs.py` and `tests/test_worker_recovery.py`. The queue is
PostgreSQL, not Redis; Redis remains available for a future rate limiter.

## Phase 7 — Voice (done, browser path)

Push-to-talk in the web client, English and Hindi, text fallback when the
browser cannot capture, and a provider boundary that reports unconfigured
instead of inventing a transcript. No wake word. Server-side transcription and
synthesis are not wired: no speech credential is configured. See
`docs/architecture/VOICE.md`. Covered by `tests/test_voice.py`.

## Phase 8 — Integrations

Done for Gmail and Google Calendar, without a live Google call. The connection
flow uses PKCE and a single-use state. Tokens are Fernet ciphertext. The web
app posts the callback, so the authorization code is not on the API query
string. A command that needs a missing connection is not queued, and a worker
does not invent a mailbox or calendar read. No client id is configured, so this
is not device-tested. Covered by `tests/test_integrations.py`.

## Phase 9 — Notifications and automations (done, in-app only)

A signed-in user pulls notification rows. The worker writes a daily briefing
from that user's open tasks, and a daily reminder the user wrote, once per
local day. The web client can show an unread row with the browser Notification
API after the user grants permission. It does not push to a device, and it does
not send email: no mail credential is configured, and the API reports
`email_configured: false`. Covered by `tests/test_notifications.py`. Not
device-tested.

## Phase 10 — Security hardening

Implemented and tested in `tests/test_security.py`.

- Retrieved memory and conversation summaries are quoted as data before they
  reach a model. A forged `system:` line is relabelled and cannot become a role.
- A step whose agent is not in the registry, or whose action names another
  agent's permission, fails before the runner. An ordinary sentence for a
  registered agent still runs.
- Task creation and assistant commands share a per-account write limit. A
  restart clears it. Login throttling is separate.
- The API and the web client send nosniff, DENY, and a CSP that sets
  frame-ancestors to none. Message text is rendered as text. The refresh cookie is HttpOnly,
  SameSite=Lax, and limited to `/api/v1/auth`, so it is not a CSRF token for
  task writes. State-changing routes still require a Bearer token.
- `.github/workflows/ci.yml` runs `pip-audit` and `npm audit --audit-level=high`.
  That scan has not run in GitHub Actions yet.

## Phase 11 — Observability

Token counts and an estimated cost are stored in `model_usage`. The row has no
prompt and no reply. `/api/v1/admin/overview` returns queue depth and those
sums. It is closed unless `ADMIN_TOKEN` is set, and a user JWT does not open
it. The worker health check on `127.0.0.1` adds queue depth and in-flight
jobs. Neither response includes message text.

Not done: an OpenTelemetry exporter. That needs a new package, which has not
been added. A trace today is one log line with ids and counts.

## Phase 12 — Production deployment

Managed Postgres and Redis, object storage, TLS, CI/CD, backups, and a
recovery procedure that has been executed rather than only described.

## Explicitly deferred

- Native applications. Not built, and not installed on any device. The backend
  command contract they would call already exists; see
  `docs/architecture/ASSISTANTS.md`. Building the iOS or Android app needs
  explicit approval because it means installing software on a personal device.
- Wake-word activation. Impossible under the zero-install constraint.
- Local Mac or iOS control. Impossible under the zero-install constraint.
- Autonomous financial transactions. HIGH risk, and not in scope even then
  without a specific, confirmed instruction per transaction.
- Multi-user collaboration on a single JARVIS account. `project_members`
  exists so this is possible later; it is not built now.
